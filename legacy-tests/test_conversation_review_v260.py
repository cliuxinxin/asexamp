"""Focused independent regressions for conversation continuations and receipts."""
import asyncio
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tcg.conversation import ConversationController
from tcg.conversation_context import get_state
from tcg.conversation_receipts import assert_command_live, commit_result
from tcg.schemas import DomainError
from tcg.storage import Store, now


class Engine:
    def __init__(self):
        self.actions = []
        self.boundaries = []

    def fits(self, task, context):
        return True

    async def invoke_model(self, task, context, run_id=None):
        return {'actions': copy.deepcopy(self.actions)}

    def request_boundary(self, run_id, **kwargs):
        self.boundaries.append((run_id, kwargs))


class ControllerReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.project = self.store.list('project')[0]['id']
        self.chat = self.store.create_chat(self.project, 'controller review')
        self.engine = Engine()
        self.calls = []
        self.block_name = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.crash_after_commit = False
        self.read_result = None
        self.auxiliary_tasks = []
        for aid in ('A', 'B'):
            self.store.put('artifact', {
                'id': aid, 'type': 'scenarios', 'title': aid, 'revision': 1,
                'items': [{'id': 'S1', 'title': 'First scenario'}],
                'project_id': self.project, 'chat_id': self.chat['id'],
                '_visible': True, 'created_at': now(),
            })
        effects = {
            'artifact.preview': 'write', 'artifact.apply': 'write',
            'artifact.revise': 'write', 'artifact.estimate': 'read',
            'artifact.read': 'read', 'workflow.continue': 'control',
            'workflow.pause': 'control', 'project.update_from_sources': 'write',
        }
        registry = {name: {'effect': effect, 'parameters': {}, 'execute': self.adapter}
                    for name, effect in effects.items()}
        self.controller = ConversationController(self.store, self.engine, registry)

    async def asyncTearDown(self):
        self.release.set()
        for task in self.auxiliary_tasks:
            if not task.done():
                task.cancel()
        if self.auxiliary_tasks:
            await asyncio.gather(*self.auxiliary_tasks, return_exceptions=True)
        await self.controller.close()
        self.store.close()
        self.temp.cleanup()

    async def adapter(self, store, engine, chat, name, args, turn_id=None):
        self.calls.append((name, copy.deepcopy(args)))
        if name == self.block_name:
            self.entered.set()
            await self.release.wait()
        if name == 'artifact.preview':
            return {'status': 'needs_confirmation', 'message': 'Apply this preview?',
                    'parts': [{'type': 'diff', 'proposal_id': 'proposal-one', 'changes': []}]}
        if name == 'project.update_from_sources' and args.get('scope') != 'all':
            return {'status': 'needs_input', 'message': 'Analyze all requirements?', 'parts': [],
                    'pending': [{'type': 'input', 'capability': name,
                                 'question': 'Analyze all requirements?',
                                 'arguments': {**args, 'scope': 'all'}}]}
        if name == 'artifact.read' and self.read_result is not None:
            return copy.deepcopy(self.read_result)
        if name == 'workflow.continue' and args.get('run_id'):
            run = store.run(args['run_id'])
            if args.get('interrupt_id') != run.get('_interrupt_id'):
                raise DomainError('Confirmation gate changed', 409)
            if args.get('expected_control_version') != run.get('control_version', 0):
                raise DomainError('Control version changed', 409)
        result = {'status': 'succeeded', 'message': name, 'parts': [
            {'type': 'answer', 'text': name}]}
        if name == 'artifact.revise':
            with store.transaction():
                assert_command_live(store, turn_id)
                artifact = store.get('artifact', args['artifact_id'])
                if artifact['revision'] != args['expected_revision']:
                    raise DomainError('Source revision changed', 409)
                artifact['revision'] += 1
                store.put('artifact', artifact)
                commit_result(store, turn_id, result)
            if self.crash_after_commit:
                self.crash_after_commit = False
                raise asyncio.CancelledError()
        return result

    def body(self, key, name=None, args=None, content='request', **extra):
        body = {'client_message_id': key, 'content': content, **extra}
        if name:
            body['command'] = {'name': name, 'arguments': args or {}}
        return body

    async def ask(self, key, name=None, args=None, content='request', **extra):
        return await self.controller.submit(self.chat['id'], self.body(key, name, args, content, **extra))

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.auxiliary_tasks.append(task)
        return task

    def running_run(self):
        run = {'id': 'run-review', 'project_id': self.project, 'chat_id': self.chat['id'],
               'status': 'running', 'created_at': now()}
        self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (
            run['id'], run['chat_id'], run['project_id'], run['status'], json.dumps(run)))
        return run

    async def test_ack_after_side_estimate_does_not_apply_older_preview(self):
        await self.ask('preview', 'artifact.preview', {'artifact_id': 'A'})
        await self.ask('estimate', 'artifact.estimate', {'artifact_id': 'A'})
        await self.ask('ack', content='可以')
        self.assertNotIn('artifact.apply', [name for name, _ in self.calls])
        self.assertEqual(self.store.list('conversation_pending', chat_id=self.chat['id'])[0]['status'], 'open')

    async def test_apply_continues_original_compound_tail_once(self):
        self.engine.actions = [
            {'name': 'artifact.preview', 'arguments': {'artifact_id': 'A'}},
            {'name': 'artifact.read', 'arguments': {'artifact_id': 'A'}},
        ]
        original = await self.ask('compound')
        applied = await self.ask('apply', 'artifact.apply', {'proposal_id': 'proposal-one'})
        repeated = await self.ask('apply', 'artifact.apply', {'proposal_id': 'proposal-one'})
        self.assertEqual(applied['status'], 'succeeded')
        self.assertEqual(repeated['id'], applied['id'])
        self.assertEqual(self.store.get('conversation_turn', original['id'])['status'], 'succeeded')
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)

    async def test_newer_hold_blocks_old_continue_after_preview_application(self):
        self.engine.actions = [
            {'name': 'artifact.preview', 'arguments': {'artifact_id': 'A'}},
            {'name': 'workflow.continue', 'arguments': {}},
        ]
        original = await self.ask('compound-hold')
        await self.ask('hold', 'workflow.pause')
        await self.ask('apply-held', 'artifact.apply', {'proposal_id': 'proposal-one'})
        self.assertNotIn('workflow.continue', [name for name, _ in self.calls])
        last = self.store.get('conversation_turn', original['id'])['actions'][-1]
        self.assertEqual(self.store.get('conversation_command', last['id'])['status'], 'cancelled')

    async def test_deferred_write_keeps_target_and_rejects_updated_revision(self):
        run = self.running_run()
        state = get_state(self.store, self.chat)
        state['focus'] = {'artifact_id': 'A'}
        self.store.put('conversation_state', state)
        deferred = await self.ask('deferred', 'artifact.revise')
        self.assertEqual(deferred['status'], 'deferred')
        await self.ask('side-read', 'artifact.read', {'artifact_id': 'B'})
        a = self.store.get('artifact', 'A')
        a['revision'] = 2
        self.store.put('artifact', a)
        run['status'] = 'completed'
        self.store.save_run(run)
        await self.controller.resume_deferred(self.chat['id'])
        writes = [args for name, args in self.calls if name == 'artifact.revise']
        self.assertEqual([(args['artifact_id'], args['expected_revision']) for args in writes], [('A', 1)])
        self.assertEqual(self.store.get('artifact', 'A')['revision'], 2)
        self.assertEqual(self.store.get('artifact', 'B')['revision'], 1)
        self.assertEqual(self.store.get('conversation_turn', deferred['id'])['status'], 'failed')

    async def test_domain_returned_input_is_persistent_and_resumes_tail(self):
        analysis = self.store.get('artifact', 'A')
        analysis['type'] = 'analysis'
        self.store.put('artifact', analysis)
        self.engine.actions = [
            {'name': 'project.update_from_sources', 'arguments': {'artifact_id': 'A'}},
            {'name': 'artifact.read', 'arguments': {'artifact_id': 'A'}},
        ]
        original = await self.ask('global-impact')
        self.assertEqual(original['status'], 'needs_input')
        self.assertEqual(len(original['pending']), 1)
        pending = original['pending'][0]
        self.assertEqual(self.store.get('conversation_pending', pending['id'])['status'], 'open')
        result = await self.ask('allow-all', 'conversation.resolve', {
            'pending_id': pending['id'], 'arguments': {'scope': 'all'}})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)
        self.assertEqual(self.store.get('conversation_turn', original['id'])['status'], 'succeeded')

    async def test_original_retry_cannot_race_resolved_command(self):
        original_body = self.body('ambiguous', 'artifact.read')
        original = await self.controller.submit(self.chat['id'], original_body)
        self.assertEqual(original['status'], 'needs_input')
        self.block_name = 'artifact.read'
        resolve = self.spawn(self.ask('choose', 'conversation.resolve', {
            'pending_id': original['pending'][0]['id'], 'choice_id': 'B'}))
        await asyncio.wait_for(self.entered.wait(), 2)
        retry = self.spawn(self.controller.submit(self.chat['id'], original_body))
        await asyncio.sleep(0)
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)
        self.release.set()
        resolved, retried = await asyncio.gather(resolve, retry)
        self.assertEqual(resolved['status'], 'succeeded')
        self.assertEqual(retried['status'], 'succeeded')
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)

    async def test_receipt_recovers_interruption_after_committed_write(self):
        self.crash_after_commit = True
        body = self.body('commit-crash', 'artifact.revise', {'artifact_id': 'A'})
        with self.assertRaises(asyncio.CancelledError):
            await self.controller.submit(self.chat['id'], body)
        result = await self.controller.submit(self.chat['id'], body)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.store.get('artifact', 'A')['revision'], 2)
        self.assertEqual([name for name, _ in self.calls].count('artifact.revise'), 1)

    async def test_cancel_inflight_modification_rejects_late_write(self):
        self.block_name = 'artifact.revise'
        write = self.spawn(self.ask('inflight', 'artifact.revise', {'artifact_id': 'A'}))
        await asyncio.wait_for(self.entered.wait(), 2)
        command = next(c for c in self.store.list('conversation_command', chat_id=self.chat['id'])
                       if c['name'] == 'artifact.revise')
        cancelled = await self.ask('cancel-write', 'conversation.cancel', {'command_id': command['id']})
        self.assertEqual(cancelled['status'], 'succeeded')
        self.release.set()
        await write
        self.assertEqual(self.store.get('artifact', 'A')['revision'], 1)
        self.assertEqual(self.store.get('conversation_command', command['id'])['status'], 'cancelled')

    async def test_applied_preview_tail_is_recoverable_during_await(self):
        self.engine.actions = [
            {'name': 'artifact.preview', 'arguments': {'artifact_id': 'A'}},
            {'name': 'artifact.read', 'arguments': {'artifact_id': 'A'}},
        ]
        original = await self.ask('tail-recovery')
        self.block_name = 'artifact.read'
        apply = self.spawn(self.ask('apply-tail', 'artifact.apply', {'proposal_id': 'proposal-one'}))
        await asyncio.wait_for(self.entered.wait(), 2)
        persisted = self.store.get('conversation_turn', original['id'])
        self.assertIn(persisted['status'], ('running', 'recoverable'))
        self.release.set()
        await apply

    async def test_cancel_unplanned_turn_blocks_late_interpreted_write(self):
        async def interpret(task, context, run_id=None):
            self.entered.set()
            await self.release.wait()
            return {'actions': [{'name': 'artifact.revise', 'arguments': {'artifact_id': 'A'}}]}
        self.engine.invoke_model = interpret
        original_task = self.spawn(self.ask('before-plan', content='改一下场景'))
        await asyncio.wait_for(self.entered.wait(), 2)
        original = self.store.list('conversation_turn', chat_id=self.chat['id'])[0]
        self.assertEqual(original['actions'], [])
        cancelled = await self.ask('cancel-before-plan', 'conversation.cancel', {'turn_id': original['id']})
        self.assertEqual(cancelled['status'], 'succeeded')
        self.release.set()
        result = await original_task
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.get('artifact', 'A')['revision'], 1)
        self.assertFalse(any(c['name'] == 'artifact.revise' for c in self.store.list('conversation_command')))

    async def test_delayed_continue_keeps_original_gate_and_control_binding(self):
        run = self.running_run()
        run.update(status='waiting', _interrupt_id='gate-old', control_version=2,
                   interrupt={'type': 'strategy_review', 'artifact_id': 'A'})
        self.store.save_run(run)
        contexts = []
        async def interpret(task, context, run_id=None):
            contexts.append(copy.deepcopy(context))
            self.entered.set()
            await self.release.wait()
            return {'actions': [{'name': 'workflow.continue', 'arguments': {}}]}
        self.engine.invoke_model = interpret
        request = self.spawn(self.ask('delayed-continue', content='确认当前节点，继续'))
        await asyncio.wait_for(self.entered.wait(), 2)
        run.update(_interrupt_id='gate-new', control_version=3,
                   interrupt={'type': 'scenario_review', 'artifact_id': 'B'})
        self.store.save_run(run)
        self.release.set()
        result = await request
        controls = [args for name, args in self.calls if name == 'workflow.continue']
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]['run_id'], run['id'])
        self.assertEqual(controls[0]['interrupt_id'], 'gate-old')
        self.assertEqual(controls[0]['expected_control_version'], 2)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.store.run(run['id'])['_interrupt_id'], 'gate-new')

    async def test_dependent_planning_uses_compact_completed_results(self):
        private = 'UNNECESSARY_CASE_STEP_BODY' * 12000
        self.read_result = {'status': 'succeeded', 'message': 'read complete', 'parts': [
            {'type': 'case_details', 'artifact_id': 'A', 'revision': 1,
             'items': [{'id': 'S1', 'steps': [{'action': private, 'expected': private}]}]}]}
        contexts = []
        checks = []
        def fits(task, context):
            checks.append(copy.deepcopy(context))
            return len(json.dumps(context)) < 85000
        async def interpret(task, context, run_id=None):
            contexts.append(copy.deepcopy(context))
            if len(contexts) == 1:
                return {'actions': [{'name': 'artifact.read', 'arguments': {'artifact_id': 'A'}}],
                        'continue_planning': True}
            return {'actions': [], 'message': '已根据读取结果完成处理。'}
        self.engine.invoke_model, self.engine.fits = interpret, fits
        result = await self.ask('compact-next-round', content='先查看，再根据结果分析')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(contexts), 2)
        self.assertIn('completed_results', contexts[1])
        self.assertNotIn('UNNECESSARY_CASE_STEP_BODY', json.dumps(contexts[1]))
        self.assertEqual(contexts[1]['completed_results'][0]['parts'][0]['ids'], ['S1'])
        self.assertTrue(any('completed_results' in context for context in checks))
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)

    async def test_capacity_rechecked_after_completed_results_before_model(self):
        contexts = []
        self.engine.fits = lambda task, context: 'completed_results' not in context
        async def interpret(task, context, run_id=None):
            contexts.append(copy.deepcopy(context))
            return {'actions': [{'name': 'artifact.read', 'arguments': {'artifact_id': 'A'}}],
                    'continue_planning': True}
        self.engine.invoke_model = interpret
        result = await self.ask('capacity-next-round', content='先查看，再根据结果分析')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(contexts), 1)
        self.assertEqual([name for name, _ in self.calls].count('artifact.read'), 1)
        self.assertEqual(self.store.get('conversation_command', result['actions'][0]['id'])['status'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
