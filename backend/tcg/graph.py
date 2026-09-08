"""Checkpointed LangGraph workflow and server-owned durable async runner.

Graph state contains IDs and cursors only. Documents, result batches and immutable
artifact revisions stay in SQLite. Successful node writes are idempotent across
the database/checkpointer commit window. Waiting work resumes only through a
specific persisted LangGraph interrupt ID, including across process restarts.
"""
import asyncio
import json
import time
from collections import Counter
from typing import TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

from .diagnostics import Diagnostics, endpoint_origin, error_details
from .documents import parse_text
from .schemas import DomainError, OutputValidationError, INTENTS, apply_operations, profile_config, validate_items
from .storage import now, public, uid


class State(TypedDict, total=False):
    run_id: str
    intent: str
    analysis_batch: int
    analysis_ref: str
    scenario_page: int
    scenario_ref: str
    case_page: int
    cases_ref: str
    output_ref: str
    done: bool


def batches(items, budget=18000):
    output, current, size = [], [], 0
    for item in items:
        length = len(json.dumps(item, ensure_ascii=False))
        if current and size + length > budget:
            output.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        output.append(current)
    return output or [[]]


def identified(items, prefix):
    if not isinstance(items, list):
        raise DomainError('模型 items 必须为数组')
    return [{**item, 'id': item.get('id') or uid(prefix)} if isinstance(item, dict) else item for item in items]


def routing_excerpt(text, budget):
    """Bound a routing-only preview by serialized size, retaining both ends."""
    if len(json.dumps(text, ensure_ascii=False)) <= budget:
        return text
    marker = '\n[Routing preview: middle omitted]\n'
    def preview(keep):
        left, right = (keep + 1) // 2, keep // 2
        return text[:left] + marker + (text[-right:] if right else '')
    low, high = 0, min(len(text), budget)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(preview(middle), ensure_ascii=False)) <= budget:
            low = middle
        else:
            high = middle - 1
    return preview(low)


class Engine:
    def __init__(self, store, gateway, settings):
        self.store, self.gateway, self.settings = store, gateway, settings
        self.diagnostics = Diagnostics(store)
        self.heartbeat_seconds = 10
        if hasattr(gateway, 'diagnostics'):
            gateway.diagnostics = self.diagnostics
        if hasattr(gateway, 'request_recorder'):
            gateway.request_recorder = self.record_model_request
        self.tasks = {}
        self.edit_tasks = {}
        self.stopping = False
        self.saver_context = None
        self.graph = None

    async def start(self):
        self.saver_context = AsyncSqliteSaver.from_conn_string(str(self.store.directory / 'checkpoints.sqlite3'))
        saver = await self.saver_context.__aenter__()
        await saver.setup()
        builder = StateGraph(State)
        for name in ('route', 'analysis', 'clarify', 'scenarios', 'scenario_gate', 'cases', 'review', 'single', 'finish'):
            builder.add_node(name, self.observed_node(name))
        builder.add_edge(START, 'route')
        builder.add_conditional_edges('route', self.after_route)
        builder.add_conditional_edges('analysis', self.after_analysis)
        builder.add_conditional_edges('clarify', lambda state: 'finish' if state['intent'] == 'review_requirement' else 'scenarios')
        builder.add_conditional_edges('scenarios', lambda state: 'scenario_gate' if state.get('scenario_ref') else 'scenarios')
        builder.add_conditional_edges('scenario_gate', lambda state: 'finish' if state['intent'] == 'generate_scenario' else 'cases')
        builder.add_conditional_edges('cases', lambda state: 'review' if state.get('cases_ref') else 'cases')
        builder.add_edge('review', 'finish')
        builder.add_edge('single', 'finish')
        builder.add_edge('finish', END)
        self.graph = builder.compile(checkpointer=saver)
        from .agent import Agent
        self.agent = Agent(self, saver)
        self.diagnostics.record('service.ready', graph_nodes=10, history_limit=12)
        # Lifespan already holds the exclusive data-directory OS lock. Any
        # persisted edit token therefore belongs to a request in a dead process;
        # invalidate it without advancing the real interrupt or changing items.
        for run in self.store.runs(statuses=('waiting',)):
            if run.get('_edit_token'):
                self.store.update_run(run['id'], _edit_token=None)
                self.trace('edit.recovered', run['id'])
        for run in self.store.runs(statuses=('queued', 'running')):
            self.trace('run.recovered', run['id'], previous_status=run['status'])
            self.schedule(run['id'])

    async def stop(self):
        self.stopping = True
        pending = list(self.tasks.values()) + list(self.edit_tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if self.saver_context:
                await self.saver_context.__aexit__(None, None, None)
        finally:
            self.diagnostics.record('service.stopped')
            self.diagnostics.close()

    def task_active(self, run_id):
        return any(task and not task.done() for task in (self.tasks.get(run_id), self.edit_tasks.get(run_id)))

    def record_model_request(self, request):
        binding = self.diagnostics.context.get()
        run_id, call_id = binding.get('run_id'), binding.get('call_id')
        if not run_id or not call_id:
            return  # Connection tests do not belong to a conversation run.
        self.store.save_model_request(run_id, call_id, {
            **request, 'at': now(), 'run_id': run_id, 'call_id': call_id,
            'node': binding.get('node'), 'call_key': binding.get('call_key'),
            'attempt': binding.get('attempt'),
        })
        self.diagnostics.record('model.request_saved', request_available=True,
                                message_count=len(request['messages']))

    def schedule(self, run_id):
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        self.trace('run.scheduled', run_id)
        task = asyncio.create_task(self.execute(run_id), name='tcg:' + run_id)
        self.tasks[run_id] = task
        def finished_callback(finished):
            if self.tasks.get(run_id) is finished:
                self.tasks.pop(run_id, None)
                # A resume can arrive after waiting was persisted but before
                # the old runner's final diagnostics have drained.
                if not self.stopping and self.store.run(run_id)['status'] == 'queued':
                    self.schedule(run_id)
        task.add_done_callback(finished_callback)

    def config(self, run_id):
        # Coverage pagination terminates through explicit progress validation,
        # not a small graph recursion cap that silently limits case counts.
        return {'configurable': {'thread_id': run_id}, 'recursion_limit': 1_000_000}

    def trace(self, event, run_id, **fields):
        run = self.store.run(run_id)
        return self.diagnostics.record(event, run_id=run_id, chat_id=run['chat_id'], stage=run['stage'], **fields)

    def observed_node(self, name):
        async def observed(state):
            started = time.monotonic()
            with self.diagnostics.bind(node=name):
                self.trace('node.start', state['run_id'])
                try:
                    result = await getattr(self, 'node_' + name)(state)
                except GraphInterrupt:
                    self.trace('node.interrupted', state['run_id'])
                    raise
                except asyncio.CancelledError:
                    self.trace('node.cancelled', state['run_id'])
                    raise
                except Exception as exc:
                    self.trace('node.error', state['run_id'], level='ERROR', **error_details(exc))
                    raise
                self.trace('node.complete', state['run_id'], elapsed_ms=round((time.monotonic() - started) * 1000))
                return result
        return observed

    async def execute(self, run_id):
        run = self.store.run(run_id)
        started = time.monotonic()
        with self.diagnostics.bind(run_id=run_id, chat_id=run['chat_id']):
            self.trace('run.started', run_id, intent=run['intent'], mode=run['mode'],
                       conversation_messages=len(run['_conversation']), history_total=run.get('_history_total', len(run['_conversation'])), source_count=len(run['_source_ids']))
            try:
                await self._execute(run_id)
            except asyncio.CancelledError:
                self.trace('run.suspended' if self.stopping else 'run.cancelled', run_id)
                raise
            finally:
                current = self.store.run(run_id)
                self.trace('run.' + current['status'], run_id, elapsed_ms=round((time.monotonic() - started) * 1000))

    async def _execute(self, run_id):
        try:
            run = self.store.run(run_id)
            if run['status'] not in ('queued', 'running'):
                return
            graph = self.agent.graph if run.get('graph_version') == 2 else self.graph
            self.store.update_run(run_id, status='running', error=None)
            self.trace('checkpoint.loading', run_id)
            snapshot = await graph.aget_state(self.config(run_id))
            self.trace('checkpoint.loaded', run_id, has_state=bool(snapshot.values), next_nodes=list(snapshot.next), interrupt_count=len(snapshot.interrupts))
            pending = run.get('_resume')
            if pending and any(item.id == pending['interrupt_id'] for item in snapshot.interrupts):
                argument = Command(resume={pending['interrupt_id']: pending['value']})
            elif snapshot.values:
                argument = None
            else:
                argument = {'run_id': run_id, 'intent': run['intent'], 'analysis_batch': 0, 'scenario_page': 0, 'case_page': 0}
            if snapshot.values and not snapshot.next and not snapshot.interrupts:
                # Handles process death after final checkpoint but before status update.
                if run.get('graph_version') != 2:
                    await self.node_finish(snapshot.values)
            else:
                self.trace('graph.invoking', run_id, resuming=isinstance(argument, Command))
                # Persist each boundary before the next model call. With async
                # durability a hard process exit can outrun checkpoint writes.
                await graph.ainvoke(argument, self.config(run_id), durability='sync')
                self.trace('graph.returned', run_id)
            snapshot = await graph.aget_state(self.config(run_id))
            if snapshot.interrupts:
                paused = snapshot.interrupts[0]
                with self.store.transaction():
                    current = self.store.run(run_id)
                    if current.get('graph_version') == 2 and current.get('_instruction_version', 0) != snapshot.values.get('epoch', 0):
                        # Accepted during the tiny call→interrupt boundary:
                        # consume this graph's actual persisted interrupt and
                        # let the next safe boundary apply the durable epoch.
                        self.store.update_run(run_id, status='queued', stage='applying_instruction', _interrupt_id=paused.id,
                            _resume={'interrupt_id': paused.id, 'value': {'instruction': True}})
                    else:
                        self.store.update_run(run_id, status='waiting', stage=paused.value['type'], interrupt=paused.value, _interrupt_id=paused.id, _resume=None)
            elif self.store.run(run_id)['status'] != 'completed' and run.get('graph_version') != 2:
                await self.node_finish(snapshot.values)
        except asyncio.CancelledError:
            # Shutdown preserves queued/running records for the next process.
            raise
        except Exception as exc:
            self.trace('run.error', run_id, level='ERROR', **error_details(exc))
            if self.store.run(run_id)['status'] != 'cancelled' and not self.stopping:
                message = str(exc) if isinstance(exc, DomainError) else f'任务执行失败（{type(exc).__name__}）；已有进度保留，请重试当前阶段'
                current = self.store.run(run_id)
                recovery = {'recovery': self.agent.recovery(current, exc)} if current.get('experience') == 'agent' else {}
                self.store.update_run(run_id, status='failed', error=message, stage='failed', _failed_cache_key=current.get('_active_call_key'), **recovery)

    def stage(self, run_id, stage):
        self.store.assert_running(run_id)
        self.store.update_run(run_id, stage=stage, _active_call_key=None)
        self.trace('stage.changed', run_id)

    def context(self, run_id, **extra):
        run = self.store.run(run_id)
        evidence = self.store.evidence(run['_source_ids'])
        context = {'request': {key: run['_request'][key] for key in ('content', 'intent', 'mode')}, 'profile': run['_profile'], 'conversation': run['_conversation'], 'evidence': [{key: e[key] for key in ('id', 'text', 'location', 'source_id', 'role')} for e in evidence], 'artifact': public(run['_artifact_snapshot']) if run.get('_artifact_snapshot') else None, 'selected_ids': run['_request'].get('selected_ids')}
        context.update(extra)
        return context

    def routing_context(self, run_id):
        """Classify the requested action using bounded metadata, not documents."""
        run = self.store.run(run_id)
        request = run['_request']
        content = routing_excerpt(request['content'], 4000)
        sources = [self.store.get('source', source_id) for source_id in run['_source_ids']]
        roles = {}
        for source in sources:
            roles[source['role']] = roles.get(source['role'], 0) + 1
        history = [message for message in run['_conversation'] if message.get('metadata', {}).get('run_id') != run_id]
        conversation = [{'role': m['role'], 'content': routing_excerpt(m['content'], 800),
                         'original_characters': len(m['content'])} for m in history[-4:]]
        artifact = run.get('_artifact_snapshot')
        context = {
            'request': {'content': content, 'intent': request['intent'], 'mode': request['mode'],
                        'as_requirement': request.get('as_requirement', False),
                        'original_characters': len(request['content']), 'preview_truncated': content != request['content']},
            'conversation': conversation, 'history_messages_available': len(history),
            'sources': {'count': len(sources), 'roles': roles,
                        'characters': sum(source['characters'] for source in sources),
                        'sample_files': [{'name': routing_excerpt(source['name'], 240), 'role': source['role']} for source in sources[:3]]},
            'artifact': {'id': artifact['id'], 'type': artifact['type'], 'title': routing_excerpt(artifact['title'], 256),
                         'revision': artifact['revision'], 'item_count': len(artifact['items'])} if artifact else None,
            'selected_item_count': len(request.get('selected_ids') or []),
        }
        if len(json.dumps(context, ensure_ascii=False)) > 12_000:
            raise DomainError('自动识别的上下文超过预算，请明确选择任务目标后重新发送')
        return context

    def grounded_context(self, run_id, **extra):
        context = self.context(run_id, **extra)
        references = set()
        for field in ('analysis', 'scenarios', 'cases'):
            for item in extra.get(field, []):
                references.update(item.get('refs', []))
        if references:
            selected = [e for e in context['evidence'] if e['id'] in references or e['role'] in ('change', 'clarification')]
            context['evidence_catalog'] = [{'id': e['id'], 'location': e['location'], 'role': e['role']} for e in context['evidence']]
            context['evidence'] = selected
        return context

    async def call(self, run_id, key, task, context):
        self.store.update_run(run_id, _active_call_key=key)
        existing = self.store.cache_get(run_id, key)
        if existing is not None:
            self.trace('model.cache_hit', run_id, call_key=key)
            return existing
        self.store.assert_running(run_id)
        for attempt in range(2):
            if self.stopping:
                raise asyncio.CancelledError()
            self.store.assert_running(run_id)
            try:
                with self.diagnostics.bind(call_key=key, attempt=attempt + 1, max_attempts=2):
                    result = await self.invoke_model(task, context, run_id)
                if self.stopping:
                    raise asyncio.CancelledError()
                self.store.assert_running(run_id)
                return self.store.cache_set(run_id, key, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.assert_running(run_id)
                if attempt or getattr(exc, 'retryable', True) is False:
                    raise
                self.trace('model.retry', run_id, call_key=key, task=task, next_attempt=2)
            await asyncio.sleep(.15)
        raise AssertionError('unreachable')

    async def invoke_model(self, task, context, run_id=None):
        fields = {'task': task, 'call_id': uid('call_'), 'timeout_seconds': self.settings.value['timeout_seconds'],
                  'provider': self.settings.value['provider'], 'model': self.settings.value['model'],
                  'endpoint': endpoint_origin(self.settings.value['base_url']),
                  'context_characters': len(json.dumps(context, ensure_ascii=False)),
                  'evidence_chunks': len(context.get('evidence', [])),
                  'conversation_messages': len(context.get('conversation', []))}
        if run_id:
            run = self.store.run(run_id)
            fields.update(run_id=run_id, chat_id=run['chat_id'], stage=run['stage'])
        if 'batch_index' in context:
            fields.update(batch_index=context['batch_index'] + 1, batch_count=context['batch_count'])
        if 'cursor' in context:
            fields['previous_items'] = len(context.get('previous_items', []))
        started = time.monotonic()
        with self.diagnostics.bind(**fields):
            self.diagnostics.record('model.start', streaming=hasattr(self.gateway, 'generate_stream'))
            pending = ''
            last_flush = 0.0
            async def flush():
                nonlocal pending, last_flush
                if not pending or run_id is None:
                    return
                current = self.store.run(run_id)
                if self.stopping or current['status'] not in ('running', 'waiting'):
                    pending = ''
                    return
                with self.store.transaction():
                    for index in range(0, len(pending), 2048):
                        self.store.append_event(run_id, 'model_delta', {
                            'at': now(), 'run_id': run_id, 'call_id': fields['call_id'],
                            'text': pending[index:index + 2048],
                        })
                pending = ''
                last_flush = time.monotonic()
            async def on_text(text):
                nonlocal pending
                pending += text
                if len(pending) >= 1024 or time.monotonic() - last_flush >= .1:
                    await flush()
            async def heartbeat():
                while True:
                    await asyncio.sleep(self.heartbeat_seconds)
                    self.diagnostics.record('model.waiting', elapsed_ms=round((time.monotonic() - started) * 1000))
            pulse = asyncio.create_task(heartbeat())
            try:
                result = await self._invoke_model(task, context, on_text)
                if self.stopping or (run_id and self.store.run(run_id)['status'] == 'cancelled'):
                    raise asyncio.CancelledError
                if not hasattr(self.gateway, 'generate_stream'):
                    await on_text(json.dumps(result, ensure_ascii=False))
                await flush()
                self.diagnostics.record('model.complete', elapsed_ms=round((time.monotonic() - started) * 1000),
                                        items=len(result.get('items', [])) if isinstance(result.get('items'), list) else None)
                return result
            except asyncio.CancelledError:
                await flush()
                self.diagnostics.record('model.cancelled', elapsed_ms=round((time.monotonic() - started) * 1000))
                raise
            except Exception as exc:
                await flush()
                self.diagnostics.record('model.error', level='ERROR', elapsed_ms=round((time.monotonic() - started) * 1000), **error_details(exc))
                raise
            finally:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)

    async def _invoke_model(self, task, context, on_text=None):
        """Shared transport/result boundary for graph nodes and paused edits."""
        if len(json.dumps(context, ensure_ascii=False)) > 500_000:
            raise DomainError('当前节点上下文超过本地安全预算；请拆分需求或限定范围后重试，未截断覆盖')
        try:
            operation = self.gateway.generate_stream(task, context, on_text) if on_text is not None and hasattr(self.gateway, 'generate_stream') else self.gateway.generate(task, context)
            result = await asyncio.wait_for(operation, timeout=self.settings.value['timeout_seconds'])
            self.diagnostics.record('model.response', json_object=isinstance(result, dict))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise DomainError(f"模型响应超时（本次请求上限为 {self.settings.value['timeout_seconds']:g} 秒）；请检查模型服务后重试，已有进度保留", 504) from None
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError(f'模型请求失败（{type(exc).__name__}）；请检查模型服务和配置后重试', 502) from None
        if not isinstance(result, dict):
            raise DomainError('模型必须返回 JSON 对象，请使用支持结构化输出的模型后重试')
        # Reviews validate operations together with the merged cases in node_review,
        # where the rejected JSON is available for a bounded corrective request.
        if task == 'modify':
            operations = result.get('operations')
            if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
                raise DomainError('模型 operations 必须为修改操作对象数组，请重试编辑')
        return result

    def evidence_map(self, run_id):
        return {e['id']: e for e in self.store.evidence(self.store.run(run_id)['_source_ids'])}

    def answer(self, run_id, key, text, refs=None):
        return self.store.artifact(run_id, key, 'answer', '回答', [{'id': uid('ans_'), 'title': '回答', 'description': text, 'refs': refs or []}])

    async def node_route(self, state):
        run_id = state['run_id']
        self.stage(run_id, 'routing')
        run = self.store.run(run_id)
        intent = run['intent']
        if intent == 'auto':
            context = self.routing_context(run_id)
            self.trace('route.context', run_id, context_characters=len(json.dumps(context, ensure_ascii=False)),
                       source_count=len(run['_source_ids']), conversation_messages=len(context.get('conversation', [])))
            result = await self.call(run_id, 'route', 'route', context)
            intent = result.get('intent')
            if intent not in INTENTS:
                raise DomainError('模型返回了不支持的 Intent')
        source_ids = run['_source_ids']
        if intent in ('review_case', 'query', 'modify') and run.get('_artifact_snapshot'):
            source_ids = list(dict.fromkeys(source_ids + run.get('_artifact_source_ids', run['_artifact_snapshot'].get('_source_ids', []))))
        run = self.store.update_run(run_id, intent=intent, _source_ids=source_ids)
        sources = self.store.evidence(run['_source_ids'])
        needs_requirements = intent in ('review_requirement', 'generate_scenario', 'generate_case', 'review_case')
        if needs_requirements and not any(e['role'] != 'example' for e in sources):
            output = self.answer(run_id, 'missing_source', '请上传或粘贴有效需求后重试。Example 仅供格式参考，不能作为业务需求证据。')
            return {'intent': intent, 'output_ref': output['id'], 'done': True}
        if intent in ('modify', 'review_case') and not run.get('_artifact_snapshot') and intent == 'modify':
            output = self.answer(run_id, 'missing_artifact', '请先选择需要修改的 Artifact。')
            return {'intent': intent, 'output_ref': output['id'], 'done': True}
        if intent == 'review_case' and run.get('_artifact_snapshot'):
            if run['_artifact_snapshot']['type'] != 'cases':
                raise DomainError('Review Case 需要 Case Artifact 或上传的用例文件')
            return {'intent': intent, 'cases_ref': run['_artifact_snapshot']['id']}
        return {'intent': intent}

    def after_route(self, state):
        if state.get('done'):
            return 'finish'
        if state['intent'] in ('review_requirement', 'generate_scenario', 'generate_case'):
            return 'analysis'
        if state['intent'] == 'review_case':
            return 'review' if state.get('cases_ref') else 'cases'
        return 'single'

    async def node_analysis(self, state):
        run_id = state['run_id']
        self.stage(run_id, 'requirement_analysis')
        evidence = [e for e in self.context(run_id)['evidence'] if e['role'] != 'example']
        groups = batches(evidence)
        position = state.get('analysis_batch', 0)
        context = self.context(run_id, evidence=groups[position], batch_index=position, batch_count=len(groups))
        result = await self.call(run_id, f'analysis:{position}', 'analyze_requirement', context)
        items = identified(result.get('items'), 'req_')
        validate_items('analysis', items, {e['id']: e for e in groups[position]})
        report = result.get('report', {})
        if not isinstance(report, dict):
            raise DomainError('Analysis report 必须为对象')
        for field in ('questions', 'assumptions'):
            if not isinstance(report.get(field, []), list) or not all(isinstance(value, str) for value in report.get(field, [])):
                raise DomainError('Analysis ' + field + ' 必须为字符串数组')
        self.store.cache_set(run_id, f'analysis_valid:{position}', {'items': items, 'report': report})
        if position + 1 < len(groups):
            return {'analysis_batch': position + 1}
        merged, reports = [], []
        for index in range(len(groups)):
            data = self.store.cache_get(run_id, f'analysis_valid:{index}')
            merged.extend([{**item, 'id': f'B{index + 1}-{item["id"]}'} for item in data['items']])
            reports.append(data['report'])
        final_report = {'questions': list(dict.fromkeys(q for report in reports for q in report.get('questions', []))), 'assumptions': list(dict.fromkeys(a for report in reports for a in report.get('assumptions', []))), 'requirement_map': {'segments': [r.get('requirement_map', {}) for r in reports]}, 'diagrams': [d for r in reports for d in r.get('diagrams', [])]}
        artifact = self.store.artifact(run_id, 'analysis_artifact', 'analysis', '需求分析', merged, final_report)
        return {'analysis_ref': artifact['id'], 'output_ref': artifact['id']}

    def after_analysis(self, state):
        return 'clarify' if state.get('analysis_ref') else 'analysis'

    async def node_clarify(self, state):
        run_id = state['run_id']
        run = self.store.run(run_id)
        analysis = self.store.get('artifact', state['analysis_ref'])
        questions = analysis.get('report', {}).get('questions', [])
        if run['mode'] == 'hitp' and questions:
            answer = interrupt({'type': 'clarification', 'questions': questions})
            if not isinstance(answer, dict) or not isinstance(answer.get('answer'), str) or not answer['answer'].strip():
                raise DomainError('请填写澄清答案后继续')
            self.stage(run_id, 'applying_clarification')
            existing = self.store.cache_get(run_id, 'clarification_source')
            if not existing:
                text, chunks = parse_text(answer['answer'])
                with self.store.transaction():
                    self.store.assert_running(run_id)
                    source = self.store.add_source(run['chat_id'], '用户澄清', 'clarification', text, chunks)
                    current = self.store.run(run_id)
                    current['_source_ids'].append(source['id'])
                    self.store.save_run(current)
                    self.store.cache_set(run_id, 'clarification_source', {'id': source['id']})
            result = await self.call(run_id, 'clarified_analysis', 'analyze_requirement', self.context(run_id, analysis=analysis['items'], clarification=answer['answer']))
            items = identified(result.get('items'), 'req_')
            report = result.get('report', {})
            report['questions'] = []
            revised = self.store.artifact(run_id, 'clarified_artifact', 'analysis', '需求分析（已澄清）', items, report)
            return {'analysis_ref': revised['id'], 'output_ref': revised['id']}
        return {}

    def pagination(self, result, previous, context_evidence, kind, cursor, scenario_ids=None):
        if type(result.get('has_more')) is not bool:
            raise DomainError('模型分页必须显式返回 has_more 布尔值')
        items = identified(result.get('items'), 'sc_' if kind == 'scenarios' else 'tc_')
        validate_items(kind, items, context_evidence, scenario_ids)
        ids = {item['id'] for item in previous}
        if any(item['id'] in ids for item in items):
            raise DomainError('模型分页重复条目 ID，已停止以防无限循环')
        fingerprints = {json.dumps({key: value for key, value in item.items() if key != 'id'}, sort_keys=True, ensure_ascii=False) for item in previous}
        for item in items:
            fingerprint = json.dumps({key: value for key, value in item.items() if key != 'id'}, sort_keys=True, ensure_ascii=False)
            if fingerprint in fingerprints:
                raise DomainError('模型分页重复相同内容，已停止以防无限循环')
            fingerprints.add(fingerprint)
        if result['has_more'] and (not items or not isinstance(result.get('next_cursor'), str) or not result['next_cursor'] or result['next_cursor'] == cursor):
            raise DomainError('模型分页未取得进展或 cursor 无效；请重试失败节点')
        if not previous and not items:
            raise DomainError('模型没有返回任何条目；未生成空的成功产物')
        return previous + items

    def preserve_case_page(self, original, corrected, evidence, scenario_ids, previous, cursor, used):
        """A schema correction cannot silently reduce coverage or edit valid siblings."""
        def changed(path, expected, value):
            raise OutputValidationError('格式修正改变了原页面的有效内容或分页范围', path, expected, value, 'repair_changed_page')
        before, after = original['items'], corrected['items']
        if len(before) != len(after):
            changed('items', 'same_page_item_count', after)
        if original['has_more'] != corrected['has_more']:
            changed('has_more', 'unchanged_continuation', corrected['has_more'])
        next_cursor = original.get('next_cursor')
        if original['has_more'] and isinstance(next_cursor, str) and next_cursor and next_cursor != cursor and next_cursor not in used:
            if corrected.get('next_cursor') != next_cursor:
                changed('next_cursor', 'unchanged_valid_cursor', corrected.get('next_cursor'))
        ids = Counter(item['id'] for item in before if isinstance(item, dict) and isinstance(item.get('id'), str))
        previous_ids = {item['id'] for item in previous}
        for index, (old, new) in enumerate(zip(before, after)):
            if not isinstance(old, dict):
                continue
            item_id = old.get('id')
            stable = isinstance(item_id, str) and 0 < len(item_id) <= 200 and ids[item_id] == 1 and item_id not in previous_ids
            if stable and new.get('id') != item_id:
                changed(f'items[{index}].id', 'unchanged_valid_id', new.get('id'))
            try:
                validate_items('cases', [{**old, 'id': 'validation-placeholder'}], evidence, scenario_ids)
            except OutputValidationError:
                continue
            if {k: v for k, v in old.items() if k != 'id'} != {k: v for k, v in new.items() if k != 'id'}:
                changed(f'items[{index}]', 'unchanged_valid_case', new)

    async def node_scenarios(self, state):
        run_id = state['run_id']
        self.stage(run_id, 'scenario_generation')
        analysis = self.store.get('artifact', state['analysis_ref'])
        page = state.get('scenario_page', 0)
        previous = self.store.cache_get(run_id, f'scenarios_valid:{page - 1}') if page else None
        items, cursor = (previous['items'], previous['cursor']) if previous else ([], None)
        context = self.grounded_context(run_id, analysis=analysis['items'], requirement_map=analysis.get('report', {}).get('requirement_map'), previous_items=items, cursor=cursor)
        result = await self.call(run_id, f'scenarios:{page}', 'generate_scenarios', context)
        merged = self.pagination(result, items, {e['id']: e for e in context['evidence']}, 'scenarios', cursor)
        used = previous.get('used', []) if previous else []
        if result['has_more'] and result['next_cursor'] in used:
            raise DomainError('模型重复分页 cursor；任务已停止')
        self.store.cache_set(run_id, f'scenarios_valid:{page}', {'items': merged, 'cursor': result.get('next_cursor'), 'used': used + [cursor]})
        if result['has_more']:
            return {'scenario_page': page + 1}
        artifact = self.store.artifact(run_id, 'scenarios_artifact', 'scenarios', '测试场景', merged, {'coverage': [{'scenario_id': i['id'], 'refs': i['refs']} for i in merged]})
        return {'scenario_ref': artifact['id'], 'output_ref': artifact['id']}

    async def node_scenario_gate(self, state):
        run_id = state['run_id']
        run = self.store.run(run_id)
        if run['mode'] == 'hitp' and state['intent'] == 'generate_case':
            artifact = self.store.get('artifact', state['scenario_ref'])
            self.store.publish(run_id, [artifact['id']], '请检查并编辑测试场景，确认后继续生成用例。', waiting=True)
            response = interrupt({'type': 'scenario_review', 'artifact_id': artifact['id'], 'items': artifact['items']})
            if not isinstance(response, dict) or response.get('approved') is not True:
                raise DomainError('请确认场景后继续')
        return {}

    async def node_cases(self, state):
        run_id = state['run_id']
        self.stage(run_id, 'case_generation' if state['intent'] != 'review_case' else 'case_import')
        page = state.get('case_page', 0)
        previous = self.store.cache_get(run_id, f'cases_valid:{page - 1}') if page else None
        items, cursor = (previous['items'], previous['cursor']) if previous else ([], None)
        scenarios = self.store.get('artifact', state['scenario_ref'])['items'] if state.get('scenario_ref') else []
        context = self.grounded_context(run_id, scenarios=scenarios, previous_items=items, cursor=cursor)
        evidence = {e['id']: e for e in context['evidence']}
        scenario_ids = {s['id'] for s in scenarios} if scenarios else None
        used = previous.get('used', []) if previous else []
        task = 'import_cases' if state['intent'] == 'review_case' else 'generate_cases'
        result = await self.call(run_id, f'cases:{page}', task, context)
        original = result
        for validation_attempt in range(2):
            try:
                merged = self.pagination(result, items, evidence, 'cases', cursor, scenario_ids)
                if validation_attempt:
                    self.preserve_case_page(original, result, evidence, scenario_ids, items, cursor, used)
            except OutputValidationError as exc:
                self.trace('cases.validation_failed', run_id, level='WARNING', page_index=page, validation_attempt=validation_attempt + 1, **error_details(exc))
                if validation_attempt:
                    raise DomainError('用例生成格式修正后仍未通过校验，未接受无效用例；可重试当前阶段。' + exc.message) from exc
                repair_context = {**context, 'validation_repair': {'validation_error': exc.issue, 'previous_response': result}}
            else:
                if validation_attempt:
                    self.trace('cases.repair_complete', run_id, page_index=page)
                break
            self.trace('cases.repair_started', run_id, page_index=page)
            result = await self.call(run_id, f'cases_repair:{page}', task, repair_context)
        if result['has_more'] and result['next_cursor'] in used:
            raise DomainError('模型重复分页 cursor；任务已停止')
        self.store.cache_set(run_id, f'cases_valid:{page}', {'items': merged, 'cursor': result.get('next_cursor'), 'used': used + [cursor]})
        if result['has_more']:
            return {'case_page': page + 1}
        artifact = self.store.artifact(run_id, 'cases_artifact', 'cases', '测试用例', merged)
        return {'cases_ref': artifact['id']}

    async def node_review(self, state):
        run_id = state['run_id']
        self.stage(run_id, 'case_review')
        run = self.store.run(run_id)
        artifact = self.store.get('artifact', state['cases_ref'])
        snapshot = (run.get('_artifact_snapshot') or artifact) if state['intent'] == 'review_case' else artifact
        if self.store.cache_get(run_id, 'review_applied'):
            revised = artifact
            result = self.store.cache_get(run_id, 'case_review') or {}
        else:
            scenarios = self.store.get('artifact', state['scenario_ref'])['items'] if state.get('scenario_ref') else []
            context = self.grounded_context(run_id, cases=snapshot['items'], scenarios=scenarios)
            result = await self.call(run_id, 'case_review', 'review_cases', context)
            evidence = {e['id']: e for e in self.store.evidence(list(dict.fromkeys(snapshot['_source_ids'] + run['_source_ids'])))}
            for validation_attempt in range(2):
                try:
                    operations = result.get('operations')
                    if isinstance(operations, list):
                        for operation in operations:
                            if isinstance(operation, dict) and operation.get('op') == 'add' and isinstance(operation.get('item'), dict):
                                operation['item'].setdefault('id', uid('tc_'))
                    items = apply_operations(snapshot['items'], operations, run['_request'].get('selected_ids'))
                    validate_items('cases', items, evidence, {s['id'] for s in scenarios} if scenarios else None)
                except OutputValidationError as exc:
                    self.trace('review.validation_failed', run_id, level='WARNING', validation_attempt=validation_attempt + 1, **error_details(exc))
                    if validation_attempt:
                        raise DomainError('AI 评审格式修正后仍未通过校验；原始用例已保留，可重试此阶段。' + exc.message) from exc
                    repair_context = {
                        **context, 'validation_repair': {'validation_error': exc.issue, 'previous_response': result},
                    }
                else:
                    # Keep the accepted response/report and revision atomic, so a
                    # replay cannot publish the rejected review or apply it twice.
                    with self.store.transaction():
                        revised = self.store.revise_artifact(artifact['id'], snapshot['revision'], items, 'ai_review', run_id, 'review_applied')
                        self.store.cache_set(run_id, 'case_review', result)
                    if validation_attempt:
                        self.trace('review.repair_complete', run_id)
                    break
                self.trace('review.repair_started', run_id)
                result = await self.call(run_id, 'case_review_repair', 'review_cases', repair_context)
        if state['intent'] == 'review_case':
            report = self.store.artifact(run_id, 'review_report', 'review', '用例审查报告', [], result.get('report', {}))
            self.store.cache_set(run_id, 'additional_outputs', {'ids': [report['id']]})
        return {'output_ref': revised['id']}

    async def node_single(self, state):
        run_id, intent = state['run_id'], state['intent']
        self.stage(run_id, intent)
        run = self.store.run(run_id)
        context = self.context(run_id)
        if intent == 'query':
            available = [e for e in context['evidence'] if e['role'] != 'example']
            if not available:
                artifact = self.answer(run_id, 'query_no_evidence', '当前没有可引用的需求证据。请上传需求或选择已有 Artifact 后再提问。')
            else:
                result = await self.call(run_id, 'query', 'query', context)
                refs = result.get('refs')
                if not isinstance(result.get('answer'), str) or not isinstance(refs, list) or not refs:
                    raise DomainError('Query 必须返回带有效 Evidence refs 的回答')
                artifact = self.answer(run_id, 'answer_artifact', result['answer'], refs)
        elif intent == 'learn_template':
            if not context['evidence'] and not context['artifact']:
                artifact = self.answer(run_id, 'template_no_sample', '请上传 Example 模板或选择已有用例，再学习格式。')
            else:
                result = await self.call(run_id, 'template', 'learn_template', context)
                config = profile_config(result.get('config'))
                artifact = self.store.artifact(run_id, 'proposal_artifact', 'proposal', 'Profile 配置建议', [{'id': uid('proposal_'), 'title': '模板学习建议', 'description': str(result.get('summary', '')), 'refs': []}], {'config': config})
        elif intent == 'modify':
            snapshot = run['_artifact_snapshot']
            result = await self.call(run_id, 'modify', 'modify', context)
            operations = result.get('operations')
            if isinstance(operations, list):
                for operation in operations:
                    if operation.get('op') == 'add' and isinstance(operation.get('item'), dict):
                        operation['item'].setdefault('id', uid('item_'))
            items = apply_operations(snapshot['items'], operations, run['_request'].get('selected_ids'))
            artifact = self.store.revise_artifact(snapshot['id'], snapshot['revision'], items, 'ai_modify', run_id, 'modify_applied')
        else:
            raise DomainError('不支持的 Intent')
        return {'output_ref': artifact['id']}

    async def node_finish(self, state):
        run_id = state['run_id']
        if self.store.run(run_id)['status'] == 'completed':
            return {}
        self.stage(run_id, 'publishing')
        if not state.get('output_ref'):
            raise DomainError('任务没有生成最终 Artifact')
        artifact = self.store.get('artifact', state['output_ref'])
        extra = self.store.cache_get(run_id, 'additional_outputs')
        ids = [artifact['id']] + (extra['ids'] if extra else [])
        proposal = artifact.get('report', {}).get('config') if artifact['type'] == 'proposal' else None
        self.store.publish(run_id, ids, '已生成配置建议。请选择保存到 Profile 或仅查看。' if proposal else '已完成。请查看下方结果。', proposal=proposal)
        return {}

    def resume(self, run_id, response):
        with self.store.transaction():
            run = self.store.run(run_id)
            if run['status'] != 'waiting':
                raise DomainError('任务当前未等待人工输入', 409)
            kind = run['interrupt']['type']
            if kind == 'clarification' and (not response.get('answer') or not response['answer'].strip()):
                raise DomainError('请输入澄清答案')
            if kind == 'scenario_review' and response.get('approved') is not True:
                raise DomainError('请确认场景后继续生成用例')
            if kind == 'strategy_review' and response.get('approved') is not True:
                raise DomainError('请确认策略后继续，或使用补充指令修订策略')
            run.update(status='queued', stage='resuming', _resume={'interrupt_id': run['_interrupt_id'], 'value': response}, _edit_token=None)
            run.pop('interrupt', None)
            self.store.save_run(run)
        self.trace('run.resumed', run_id)
        self.schedule(run_id)
        return run

    def retry(self, run_id):
        with self.store.transaction():
            run = self.store.run(run_id)
            if run['status'] != 'failed':
                raise DomainError('仅失败任务可重试', 409)
            # Invalid structured output must be regenerated; successful earlier
            # nodes remain checkpointed. Clear only the failed node's raw cache.
            failed_key = run.get('_failed_cache_key')
            # A later report/publish failure must not erase an already accepted
            # review. This also protects runs created by previous releases.
            committed_review = failed_key == 'case_review' and self.store.cache_get(run_id, 'review_applied')
            if failed_key and not committed_review:
                self.store.cache_delete(run_id, failed_key)
            run.update(status='queued', stage='retrying', error=None)
            self.store.save_run(run)
        self.trace('run.retried', run_id)
        self.schedule(run_id)
        return run

    def cancel(self, run_id):
        with self.store.transaction():
            run = self.store.run(run_id)
            if run['status'] in ('completed', 'cancelled'):
                return run
            run.update(status='cancelled', stage='cancelled', _resume=None, _edit_token=None)
            run.pop('interrupt', None)
            self.store.save_run(run)
        self.trace('run.cancelled', run_id)
        task = self.tasks.get(run_id)
        if task:
            task.cancel()
        edit = self.edit_tasks.get(run_id)
        if edit:
            edit.cancel()
        return run

    async def edit_waiting(self, run_id, content, selected_ids=None):
        previous = self.edit_tasks.get(run_id)
        if previous and not previous.done():
            raise DomainError('当前场景编辑正在进行，请稍候', 409)
        task = asyncio.create_task(self._edit_waiting(run_id, content, selected_ids), name='tcg-edit:' + run_id)
        self.edit_tasks[run_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            if self.store.run(run_id)['status'] == 'cancelled':
                raise DomainError('任务已取消，场景编辑已停止', 409) from None
            raise
        finally:
            if self.edit_tasks.get(run_id) is task:
                self.edit_tasks.pop(run_id, None)

    async def _edit_waiting(self, run_id, content, selected_ids=None):
        with self.store.transaction():
            run = self.store.run(run_id)
            if run['status'] != 'waiting' or run.get('interrupt', {}).get('type') != 'scenario_review':
                raise DomainError('仅等待场景确认的任务支持此编辑', 409)
            if run.get('_edit_token'):
                raise DomainError('当前场景编辑正在进行，请稍候', 409)
            artifact = self.store.get('artifact', run['interrupt']['artifact_id'])
            if selected_ids is not None and not set(selected_ids).issubset({item['id'] for item in artifact['items']}):
                raise DomainError('所选条目无效')
            token = uid('edit_')
            run['_edit_token'] = token
            self.store.save_run(run)
        started = time.monotonic()
        with self.diagnostics.bind(node='paused_edit', call_key='paused_edit', attempt=1, max_attempts=1):
            self.trace('node.start', run_id)
            try:
                context = self.context(run_id, artifact=public(artifact), selected_ids=selected_ids)
                context['request'] = {**context['request'], 'content': content, 'intent': 'modify'}
                result = await self.invoke_model('modify', context, run_id)
                operations = result.get('operations')
                if isinstance(operations, list):
                    for operation in operations:
                        if operation.get('op') == 'add' and isinstance(operation.get('item'), dict):
                            operation['item'].setdefault('id', uid('sc_'))
                items = apply_operations(artifact['items'], operations, selected_ids)
                with self.store.transaction():
                    current = self.store.run(run_id)
                    if current['status'] != 'waiting' or current.get('_edit_token') != token:
                        raise DomainError('任务已继续或取消，拒绝过期编辑结果', 409)
                    updated = self.store.revise_artifact(artifact['id'], artifact['revision'], items, 'ai_waiting_edit')
                    current['interrupt']['items'] = updated['items']
                    current['_edit_token'] = None
                    self.store.save_run(current)
                    self.trace('node.complete', run_id, elapsed_ms=round((time.monotonic() - started) * 1000))
                    return current
            except asyncio.CancelledError:
                self.trace('node.cancelled', run_id)
                raise
            except Exception as exc:
                self.trace('node.error', run_id, level='ERROR', **error_details(exc))
                raise
            finally:
                with self.store.transaction():
                    current = self.store.run(run_id)
                    if current.get('_edit_token') == token:
                        current['_edit_token'] = None
                        self.store.save_run(current)
