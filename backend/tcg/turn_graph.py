"""Persistent reference-only conversation execution, separate from the fixed Run graph."""
import asyncio
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from .conversation_context import NeedsInput
from .schemas import DomainError


class TurnState(TypedDict, total=False):
    turn_id: str
    command_id: str
    route: str


class TurnGraph:
    def __init__(self, controller):
        self.controller = controller
        self.store = controller.store
        self.graph = None
        self._lock = asyncio.Lock()
        self._manager = None

    async def initialize(self):
        async with self._lock:
            if self.graph is not None:
                return
            self._manager = AsyncSqliteSaver.from_conn_string(str(self.store.directory / 'conversation-checkpoints.sqlite3'))
            saver = await self._manager.__aenter__()
            builder = StateGraph(TurnState)
            builder.add_node('plan', self.plan)
            builder.add_node('command', self.command)
            builder.add_node('result', self.result)
            builder.add_node('followup', self.followup)
            builder.add_node('waiting', self.waiting)
            builder.add_edge(START, 'plan')
            builder.add_conditional_edges('plan', self.route, {'command': 'command', 'followup': 'followup', 'end': END})
            builder.add_edge('command', 'result')
            builder.add_conditional_edges('result', self.route, {'command': 'command', 'followup': 'followup', 'wait': 'waiting', 'end': END})
            builder.add_edge('waiting', 'plan')
            builder.add_conditional_edges('followup', self.route, {'plan': 'plan', 'end': END})
            self.graph = builder.compile(checkpointer=saver)

    def load(self, state):
        turn = self.store.get('conversation_turn', state['turn_id'])
        return turn, self.store.get('chat', turn['chat_id'])

    def route(self, state):
        return state['route']

    def next_command(self, turn):
        if turn['status'] == 'cancelled':
            return {'route': 'end'}
        entry = next((a for a in turn['actions'] if not a.get('_presented')), None)
        if entry:
            return {'command_id': entry['id'], 'route': 'command'}
        return {'command_id': '', 'route': 'followup'}

    async def plan(self, state):
        turn, chat = self.load(state)
        if turn['status'] == 'cancelled':
            return {'route': 'end'}
        if not turn.get('_planned'):
            self.controller._record_plan(turn, await self.controller._interpret(turn, chat))
        if not turn['actions']:
            self.controller._save_turn(turn)
            return {'route': 'end'}
        turn['status'] = 'running'
        self.store.put('conversation_turn', turn)
        return self.next_command(turn)

    async def command(self, state):
        turn, chat = self.load(state)
        if turn['status'] == 'cancelled':
            return {}
        command = self.store.get('conversation_command', state['command_id'])
        if command['status'] == 'needs_input':
            return {}
        try:
            result = await self.controller._execute(chat, turn, command)
        except NeedsInput as exc:
            with self.store.transaction():
                self.controller._pending(turn, command, exc)
                self.controller._save_turn(turn)
            return {}
        # This reference record also captures deferred results, which have no effect receipt.
        command = self.store.get('conversation_command', command['id'])
        if command['status'] != 'cancelled':
            command['result'] = result
            self.store.put('conversation_command', command)
        return {}

    async def result(self, state):
        turn, chat = self.load(state)
        if turn['status'] == 'cancelled':
            return {'route': 'end'}
        command = self.store.get('conversation_command', state['command_id'])
        entry = next(a for a in turn['actions'] if a['id'] == command['id'])
        if command['status'] == 'needs_input' and not command.get('result'):
            entry['status'] = 'needs_input'
            turn['status'] = 'needs_input'
            self.controller._save_turn(turn)
            return {'route': 'wait'}
        result = command['result']
        if not entry.get('_presented'):
            with self.store.transaction():
                entry.update(status=result['status'], result=result)
                if result['status'] != 'deferred':
                    turn['parts'].extend(result.get('parts', []))
                    entry['_presented'] = result['status'] != 'needs_input'
                    self.controller._remember_result(chat, turn, command, result)
                turn['message'] = '\n\n'.join(a.get('result', {}).get('message', '') for a in turn['actions'] if a.get('result', {}).get('message'))
                turn['status'] = 'running' if result['status'] == 'succeeded' else result['status']
                self.controller._save_turn(turn)
        if result['status'] != 'succeeded':
            return {'route': 'wait' if result['status'] in ('needs_input','needs_confirmation','deferred') else 'end'}
        return self.next_command(turn)

    async def waiting(self, state):
        interrupt({'turn_id':state['turn_id'], 'command_id':state.get('command_id')})
        return {}

    async def followup(self, state):
        turn, chat = self.load(state)
        if turn['status'] == 'cancelled':
            return {'route': 'end'}
        if turn.get('_continue_planning') and turn.get('_planning_round', 0) < 2:
            turn['_planning_round'] = turn.get('_planning_round', 0) + 1
            turn['_continue_planning'] = False
            turn['_planned'] = False
            self.store.put('conversation_turn', turn)
            return {'route': 'plan'}
        turn['status'] = 'succeeded'
        turn['message'] = turn.get('message') or '本轮处理完成。'
        self.controller._save_turn(turn)
        if not self.store.runs(chat_id=chat['id'], statuses=('queued', 'running')):
            self.controller._schedule_drain(chat['id'])
        return {'route': 'end'}

    async def run(self, turn):
        await self.initialize()
        if not turn.get('_graph_version'):
            turn.update(_graph_version='turn-v270',_thread_id=turn['id'])
            self.store.put('conversation_turn',turn)
        config = {'configurable': {'thread_id': turn['id']}, 'recursion_limit': 80}
        checkpoint = await self.graph.aget_state(config)
        try:
            waiting=any(task.interrupts for task in checkpoint.tasks)
            graph_input=Command(resume={'turn_id':turn['id']}) if waiting else None if checkpoint.next else {'turn_id':turn['id']}
            await self.graph.ainvoke(graph_input, config)
            current=self.store.get('conversation_turn', turn['id'])
            # Some LangGraph versions stop on a node cancellation without raising it.
            # A nonterminal projection with no user interrupt is still recoverable.
            if current['status']=='running':
                remaining=await self.graph.aget_state(config)
                if remaining.next and not any(task.interrupts for task in remaining.tasks):
                    raise asyncio.CancelledError()
        except asyncio.CancelledError:
            current = self.store.get('conversation_turn', turn['id'])
            if current['status'] != 'cancelled':
                current.update(status='recoverable', message='本轮连接中断，已完成操作已保存；重试会接着处理未完成部分。')
                self.controller._save_turn(current)
            raise
        except Exception as exc:
            current = self.store.get('conversation_turn', turn['id'])
            if current['status'] != 'cancelled':
                current.update(status='failed' if isinstance(exc, (DomainError, NeedsInput)) else 'recoverable',
                               message='本轮操作未完成：' + str(exc)[:500])
                self.controller._save_turn(current)
            cause=exc
            seen=set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause,asyncio.CancelledError):
                    raise asyncio.CancelledError() from exc
                cause=cause.__cause__ or cause.__context__
        return self.controller.response(self.store.get('conversation_turn', turn['id']))

    async def close(self):
        if self._manager is not None:
            await self._manager.__aexit__(None, None, None)
            self._manager = None
            self.graph = None
