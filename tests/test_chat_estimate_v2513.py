"""Composer routing + actual action/SQLite checks, with semantic model responses stubbed."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tcg.chat_estimate import interpret_chat, register_chat_estimate_routes
from tcg.schemas import DomainError, DEFAULT_PROFILE
from tcg.storage import Store, dump, now


class Engine:
    def __init__(self, decision=None, hook=None):
        self.decision = decision or {'action': 'estimate', 'scope': 'all'}
        self.hook = hook
        self.calls = []

    def fits(self, task, context):
        return True

    async def invoke_model(self, task, context, run_id=None):
        self.calls.append((task, copy.deepcopy(context), run_id))
        if self.hook:
            self.hook(task, context)
        if task == 'chat_interpret':
            return copy.deepcopy(self.decision)
        if task == 'artifact_estimate':
            return {'scenarios': [{'scenario_id': r['id'], 'min_count': 2, 'max_count': 4,
                                  'rationale': '主要正反向和边界路径', 'assumptions': ['估算依据当前场景，细节待设计']}
                                 for r in context['scenarios']]}
        raise AssertionError(f'Unexpected task {task}')


class ChatEstimateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.project = self.store.list('project')[0]['id']
        self.chat = self.store.create_chat(self.project, 'natural language estimates')['id']
        self.scene = self.artifact('scene-one', 'scenarios', [
            {'id': f'SC-{i}', 'title': f'场景 {i}', 'description': 'SCENARIO-BODY-PRIVATE', 'priority': 'P1', 'refs': []}
            for i in range(1, 4)])
        self.cases = self.artifact('cases-one', 'cases', [{'id': 'TC-1', 'title': 'existing case',
            'scenario_id': 'SC-1', 'steps': [{'action': 'CASE-STEPS-PRIVATE', 'expected': 'CASE-EXPECTED-PRIVATE'}]}],
            {'lineage': {'scenario_artifact_id': self.scene['id'], 'scenario_revision': 1}})
        self.store.add_source(self.chat, 'requirement', 'primary', 'DOCUMENT-PRIVATE', [{'text': 'DOCUMENT-PRIVATE', 'location': 'P1'}])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def artifact(self, aid, kind, items, report=None, chat=None, project=None, title=None):
        value = {'id': aid, 'type': kind, 'project_id': project or self.project, 'chat_id': chat or self.chat,
                 'title': title or aid, 'items': items, 'revision': 1, 'report': report or {}, '_visible': True,
                 'created_at': now(), '_profile': {**DEFAULT_PROFILE, 'sample_cases': [{'title': 'SAMPLE-PRIVATE'}]}}
        self.store.put('artifact', value)
        return value

    def paused(self, kind='scenario_review', artifact_id='scene-one', status='waiting'):
        run = {'id': 'paused', 'project_id': self.project, 'chat_id': self.chat, 'status': status,
               '_interrupt_id': 'confirmation-1', 'stage': kind, 'interrupt': {'type': kind, 'artifact_id': artifact_id}}
        self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (run['id'], self.chat, self.project, status, dump(run)))
        return run

    async def ask(self, engine, content='这些场景大概需要多少条 case，先别生成', **body):
        return await interpret_chat(self.store, engine, self.chat, {'content': content, **body})

    async def test_composer_estimate_with_stale_generate_hint_does_not_create_cases_or_run(self):
        engine = Engine()
        before = self.store.list('artifact', chat_id=self.chat)
        result = await self.ask(engine, intent_hint='generate_case')
        self.assertTrue(result['handled'])
        self.assertEqual(result['kind'], 'estimate')
        self.assertEqual([c[0] for c in engine.calls], ['chat_interpret', 'artifact_estimate'])
        self.assertEqual(engine.calls[0][1]['intent_hint'], 'generate_case')
        self.assertEqual(result['estimate']['min_count'], 6)
        self.assertEqual(result['estimate']['max_count'], 12)
        self.assertEqual(result['estimate']['artifact_revision'], 1)
        self.assertEqual([s['title'] for s in result['estimate']['scenarios']], ['场景 1', '场景 2', '场景 3'])
        self.assertEqual(self.store.runs(self.chat), [])
        self.assertEqual(self.store.list('artifact', chat_id=self.chat), before)
        messages = self.store.list('message', chat_id=self.chat)
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant'])
        self.assertNotIn('run_id', messages[-1]['metadata'])
        self.assertNotIn('artifact_ids', messages[-1]['metadata'])

    async def test_routing_uses_metadata_no_documents_scenario_body_steps_or_samples(self):
        engine = Engine()
        await self.ask(engine, artifact_id='cases-one')
        encoded = json.dumps(engine.calls[0][1])
        for secret in ('DOCUMENT-PRIVATE', 'SCENARIO-BODY-PRIVATE', 'CASE-STEPS-PRIVATE', 'CASE-EXPECTED-PRIVATE', 'SAMPLE-PRIVATE'):
            self.assertNotIn(secret, encoded)
        self.assertIn('scenario_candidates', encoded)
        self.assertEqual(engine.calls[0][1]['sources']['roles'], {'primary': 1})
        self.assertEqual(engine.calls[0][1]['sources']['sample_files'][0]['name'], 'requirement')
        self.assertEqual(engine.calls[1][1]['scenarios'], self.scene['items'])
        self.assertNotIn('evidence', engine.calls[1][1])

    async def test_waiting_scene_survives_and_wins_over_displayed_old_cases(self):
        self.artifact('old-scenes', 'scenarios', [{'id': 'OLD', 'title': 'old'}])
        self.cases['report']['lineage']['scenario_artifact_id'] = 'old-scenes'
        self.store.put('artifact', self.cases)
        before = self.paused()
        result = await self.ask(Engine(), artifact_id='cases-one')
        after = self.store.run('paused')
        self.assertEqual(result['estimate']['artifact_id'], 'scene-one')
        for key in ('status', 'stage', 'interrupt', '_interrupt_id'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(after.get('_edit_token'))
        self.assertEqual(len(self.store.runs(self.chat)), 1)

    async def test_ordinal_subset_selected_ids_and_followup_estimate_context(self):
        engine = Engine({'action': 'estimate', 'scope': 'subset', 'scenario_ordinals': [2, 3]})
        first = await self.ask(engine, '只估第二、第三个场景')
        self.assertEqual([r['scenario_id'] for r in first['estimate']['scenarios']], ['SC-2', 'SC-3'])
        followup = Engine({'action': 'estimate', 'scope': 'inherit_previous'})
        second = await self.ask(followup, '那只算异常的呢？')
        self.assertEqual([r['scenario_id'] for r in second['estimate']['scenarios']], ['SC-2', 'SC-3'])
        context = followup.calls[0][1]
        self.assertEqual(context['previous_estimate']['scenario_ids'], ['SC-2', 'SC-3'])
        self.assertEqual(context['previous_estimate']['artifact_id'], 'scene-one')
        self.assertIn('只算异常', followup.calls[1][1]['instruction'])
        self.assertIn('只估第二、第三个场景', followup.calls[1][1]['instruction'])
        selected = await self.ask(Engine({'action': 'estimate', 'scope': 'selected'}),
                                  '只估选中的场景', artifact_id='scene-one', selected_ids=['SC-1'])
        self.assertEqual(len(selected['estimate']['scenarios']), 1)
        self.assertEqual(len(self.store.list('message', chat_id=self.chat)), 6)
        self.assertEqual(self.store.runs(self.chat), [])

    async def test_explicit_all_expands_previous_subset_and_inheritance_requires_matching_source(self):
        await self.ask(Engine({'action': 'estimate', 'scope': 'subset', 'scenario_ordinals': [2, 3]}))
        result = await self.ask(Engine({'action': 'estimate', 'scope': 'all'}), '重新估算全部场景')
        self.assertEqual(len(result['estimate']['scenarios']), 3)
        self.artifact('different', 'scenarios', [{'id': 'DIFFERENT', 'title': 'different'}])
        with self.assertRaises(DomainError):
            await self.ask(Engine({'action': 'estimate', 'scope': 'inherit_previous'}), artifact_id='different')

    async def test_pending_scene_wins_over_stale_displayed_scenes_until_explicit_source_request(self):
        self.artifact('old-scenes', 'scenarios', [{'id': 'OLD', 'title': 'old'}])
        self.paused()
        result = await self.ask(Engine(), artifact_id='old-scenes')
        self.assertEqual(result['estimate']['artifact_id'], 'scene-one')

    async def test_long_chat_metadata_shrinks_and_short_normal_request_still_routes(self):
        for i in range(15):
            self.artifact(f'large-{i}', 'scenarios', [
                {'id': f'X-{i}-{j}', 'title': '很长的场景标题' * 40, 'description': 'BODY-OMITTED' * 500}
                for j in range(100)])
        self.paused()
        engine = Engine({'action': 'normal', 'intent': 'query'})
        engine.fits = lambda task, context: len(json.dumps(context, ensure_ascii=False)) < 3000
        result = await self.ask(engine, '这个流程怎么运行？', artifact_id='cases-one')
        self.assertEqual(result, {'handled': False, 'intent': 'query'})
        context = engine.calls[0][1]
        self.assertLess(len(json.dumps(context, ensure_ascii=False)), 3000)
        self.assertIn('scene-one', [a['id'] for a in context['scenario_candidates']])
        self.assertTrue(context['scenario_candidates_partial'])
        self.assertTrue(any(a['scenario_titles_partial'] for a in context['scenario_candidates']))
        self.assertNotIn('BODY-OMITTED', json.dumps(context))

    async def test_english_paraphrase_is_sent_to_semantic_classifier(self):
        text = 'Roughly how much test-case design work do these scenarios imply? No authoring yet.'
        engine = Engine()
        result = await self.ask(engine, text)
        self.assertEqual(engine.calls[0][1]['content'], text)
        self.assertTrue(result['handled'])

    async def test_negative_estimation_request_routes_normal_once_without_persisting(self):
        engine = Engine({'action': 'normal', 'intent': 'generate_case'})
        result = await self.ask(engine, '不要估算，直接生成用例')
        self.assertEqual(result, {'handled': False, 'intent': 'generate_case'})
        self.assertEqual([c[0] for c in engine.calls], ['chat_interpret'])
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])

    async def test_waiting_question_routes_query_without_consuming_clarification(self):
        before = self.paused('clarification', None)
        result = await self.ask(Engine({'action': 'normal', 'intent': 'query'}), '为什么需要这个信息？', intent_hint='generate_case')
        self.assertEqual(result, {'handled': False, 'intent': 'query'})
        self.assertEqual(self.store.run('paused'), before)
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])

    async def test_missing_scenarios_at_clarification_stays_paused_without_lease(self):
        self.store.db.execute("DELETE FROM objects WHERE kind='artifact'")
        before = self.paused('clarification', None)
        engine = Engine()
        result = await self.ask(engine)
        self.assertEqual(result['kind'], 'clarification')
        self.assertIn('还没有已保存的场景', result['message']['content'])
        self.assertEqual(self.store.run('paused'), before)
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse(getattr(self.store, '_workspace_action_tokens', {}))

    async def test_ambiguous_sets_cannot_be_selected_by_fabricated_model_authorization(self):
        self.artifact('scene-two', 'scenarios', [{'id': 'OTHER', 'title': 'other'}], title=self.scene['title'])
        engine = Engine({'action': 'estimate', 'scope': 'all', 'artifact_id': 'scene-two'})
        result = await self.ask(engine)
        self.assertEqual(result['kind'], 'clarification')
        self.assertIn('scene-one', result['message']['content'])
        self.assertIn('scene-two', result['message']['content'])
        self.assertEqual(len(engine.calls), 1)
        explicit = await self.ask(engine, '请估算 scene-two 这份场景')
        self.assertEqual(explicit['estimate']['artifact_id'], 'scene-two')

    async def test_duplicate_titles_require_source_id_but_unique_title_is_supported(self):
        self.artifact('scene-two', 'scenarios', [{'id': 'OTHER', 'title': 'other'}], title=self.scene['title'])
        result = await self.ask(Engine({'action': 'estimate', 'artifact_title': self.scene['title']}),
                                f'估算 {self.scene["title"]}')
        self.assertEqual(result['kind'], 'clarification')
        unique = self.artifact('unique', 'scenarios', [{'id': 'UNIQUE', 'title': 'unique'}], title='新版支付场景')
        result = await self.ask(Engine({'action': 'estimate', 'artifact_title': unique['title']}), '估算新版支付场景')
        self.assertEqual(result['estimate']['artifact_id'], 'unique')

    async def test_missing_or_out_of_range_scenario_ids_never_fall_back_to_generation(self):
        for selector in ({'scenario_ids': ['SC-NOPE']}, {'scenario_ordinals': [4]}, {'scenario_ids': []}):
            with self.subTest(selector=selector):
                engine = Engine({'action': 'estimate', 'scope': 'subset', **selector})
                with self.assertRaises(DomainError):
                    await self.ask(engine)
                self.assertEqual(len(engine.calls), 1)
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])
        self.assertEqual(self.store.runs(self.chat), [])

    async def test_other_chat_and_project_artifacts_are_never_candidates_or_authorized(self):
        other_chat = self.store.create_chat(self.project, 'other')['id']
        self.artifact('other-chat-scenes', 'scenarios', [{'id': 'SECRET', 'title': 'OTHER-CHAT-PRIVATE'}], chat=other_chat)
        project = self.store.create_project('other project')['id']
        third_chat = self.store.create_chat(project, 'other project')['id']
        self.artifact('other-project-scenes', 'scenarios', [{'id': 'SECRET', 'title': 'OTHER-PROJECT-PRIVATE'}], chat=third_chat, project=project)
        engine = Engine()
        await self.ask(engine)
        for text in ('OTHER-CHAT-PRIVATE', 'OTHER-PROJECT-PRIVATE'):
            self.assertNotIn(text, json.dumps(engine.calls[0][1]))
        for aid in ('other-chat-scenes', 'other-project-scenes'):
            with self.assertRaises(DomainError):
                await self.ask(Engine(), artifact_id=aid)

    async def test_malformed_or_failed_classifier_keeps_no_messages_or_runs(self):
        for decision in ({'intent': 'generate_case'}, {'action': 'normal', 'intent': 'invented'},
                         {'action': 'estimate', 'scenario_ids': 'SC-1'}):
            with self.subTest(decision=decision), self.assertRaises(DomainError):
                await self.ask(Engine(decision))
        def fail(*_):
            raise DomainError('Model failed')
        with self.assertRaises(DomainError):
            await self.ask(Engine(hook=fail))
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])
        self.assertEqual(self.store.runs(self.chat), [])

    async def test_revision_or_pause_change_during_classification_discards_result(self):
        def change_revision(task, context):
            artifact = self.store.get('artifact', 'scene-one')
            self.store.put('artifact', {**artifact, 'revision': artifact['revision'] + 1})
        with self.assertRaises(DomainError):
            await self.ask(Engine(hook=change_revision))
        self.paused()
        def continue_run(task, context):
            self.store.update_run('paused', status='completed')
        with self.assertRaises(DomainError):
            await self.ask(Engine(hook=continue_run))
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])

    async def test_estimation_lease_rejects_cancellation_and_does_not_publish_old_answer(self):
        self.paused()
        def cancel(task, context):
            if task == 'artifact_estimate':
                self.store.update_run('paused', status='cancelled', _edit_token=None)
        with self.assertRaises(DomainError):
            await self.ask(Engine(hook=cancel))
        self.assertEqual(self.store.list('message', chat_id=self.chat), [])
        self.assertFalse(getattr(self.store, '_workspace_action_tokens', {}))

    async def test_running_request_gets_wait_reply_not_an_estimator_or_new_run(self):
        before = self.paused(status='running')
        engine = Engine()
        result = await self.ask(engine)
        self.assertEqual(result['kind'], 'clarification')
        self.assertIn('正在生成', result['message']['content'])
        self.assertEqual(self.store.run('paused'), before)
        self.assertEqual(len(engine.calls), 1)

    async def test_explicit_requirement_bypasses_and_registered_endpoint_calls_real_interpreter(self):
        engine = Engine()
        result = await self.ask(engine, as_requirement=True)
        self.assertEqual(result, {'handled': False})
        self.assertEqual(engine.calls, [])
        from types import SimpleNamespace
        routes = {}
        app = SimpleNamespace(state=SimpleNamespace(store=self.store, engine=engine),
                              post=lambda path: lambda endpoint: routes.setdefault(path, endpoint))
        register_chat_estimate_routes(app)
        endpoint = routes['/api/chats/{chat_id}/interpret']
        body = endpoint.__annotations__['body'](content='只估算，不生成')
        result = await endpoint(self.chat, body)
        self.assertEqual(result['kind'], 'estimate')


if __name__ == '__main__':
    unittest.main()
