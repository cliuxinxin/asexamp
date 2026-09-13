"""Focused real action/SQLite checks; model stub, no HTTP/LangGraph integration."""
import ast
import asyncio
import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
# Pydantic is a required application dependency; test the actual shared schema module.
from tcg.storage import Store, dump, now
from tcg.schemas import DomainError, DEFAULT_PROFILE
from tcg.artifact_actions import preview_action, apply_action, validate_estimate, action_lease


class Engine:
    def __init__(self, responder, limit=1000000):
        self.responder, self.limit, self.calls = responder, limit, []

    def fits(self, task, context):
        return len(json.dumps(context)) < self.limit

    async def invoke_model(self, task, context, run_id=None):
        self.calls.append((task, copy.deepcopy(context), run_id))
        value = self.responder(task, context)
        return await value if hasattr(value, '__await__') else copy.deepcopy(value)


class ActionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.project = self.store.list('project')[0]['id']
        self.chat = self.store.create_chat(self.project, 'actions')['id']
        self.source = self.store.add_source(self.chat, 'requirement', 'primary', '失败显示错误。', [{'text': '失败显示错误。', 'location': 'P1'}])
        self.ref = self.source['id'] + '#P1'
        self.scenarios = self.artifact('scenario', 'scenarios', [
            {'id': 'S1', 'title': '登录', 'description': '登录规则', 'priority': 'P1', 'refs': [self.ref]},
            {'id': 'S2', 'title': '退出', 'description': '退出规则', 'priority': 'P2', 'refs': [self.ref]}])
        self.cases = self.artifact('cases', 'cases', [self.case('C1', 'S1'), self.case('C2', 'S2')],
            {'lineage': {'scenario_artifact_id': 'scenario', 'scenario_revision': 1}})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def case(self, cid, sid):
        return {'id': cid, 'title': cid, 'scenario_id': sid, 'type': 'Business', 'priority': 'P1',
                'preconditions': '可登录', 'steps': [{'action': '输入账号', 'expected': '显示结果'}],
                'refs': [self.ref], 'actual_result': '已人工填写', 'custom_note': '保留自定义内容'}

    def artifact(self, aid, kind, rows, report=None):
        value = {'id': aid, 'project_id': self.project, 'chat_id': self.chat, 'type': kind,
                 'title': aid, 'revision': 1, 'items': rows, 'created_at': now(), '_visible': True,
                 '_source_ids': [self.source['id']], '_source_roles': {self.source['id']: 'primary'},
                 '_profile': {**DEFAULT_PROFILE, 'excel_columns': [*DEFAULT_PROFILE['excel_columns'],
                              {'field': 'actual_result', 'header': '实际结果', 'value_source': 'manual'}]},
                 'report': report or {}}
        self.store.put('artifact', value)
        self.store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (aid, 1, dump(value), now(), 'fixture', '{}'))
        return value

    def paused(self, artifact_id='scenario'):
        run = {'id': 'waiting', 'project_id': self.project, 'chat_id': self.chat, 'status': 'waiting',
               'stage': 'scenario_review', '_interrupt_id': 'interrupt-1', 'interrupt': {'type': 'scenario_review', 'artifact_id': artifact_id}}
        self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?)', (run['id'], self.chat, self.project, 'waiting', dump(run)))
        return run

    async def test_estimate_is_only_counts_and_does_not_write_cases(self):
        def answer(task, context):
            self.assertEqual(task, 'artifact_estimate')
            self.assertNotIn('evidence', context)
            self.assertNotIn('artifact', context)
            return {'scenarios': [{'scenario_id': r['id'], 'min_count': 2, 'max_count': 4,
                                  'rationale': '覆盖主要正反向路径', 'assumptions': ['边界待确认']} for r in context['scenarios']]}
        engine = Engine(answer)
        result = await preview_action(self.store, engine, 'scenario', {'action': 'estimate', 'instruction': '只估算，不生成'})
        self.assertEqual((result['estimate']['min_count'], result['estimate']['max_count']), (4, 8))
        self.assertEqual(result['changes'], [])
        self.assertEqual(self.store.get('artifact', 'cases'), self.cases)
        with self.assertRaises(DomainError):
            apply_action(self.store, 'scenario', result['id'])
        bad = {'scenarios': [{'scenario_id': 'S1', 'min_count': True, 'max_count': 2, 'rationale': 'x', 'assumptions': []}]}
        with self.assertRaises(DomainError):
            validate_estimate(bad, self.scenarios['items'][:1])

    async def test_explain_repeated_explicit_target_includes_steps(self):
        engine = Engine(lambda task, context: {'answer': context['artifact']['items'][0]['steps'][0]['expected'], 'refs': [self.ref]})
        for _ in range(2):
            result = await preview_action(self.store, engine, 'cases', {'action': 'explain', 'instruction': '解释选中的用例', 'selected_ids': ['C1']})
            self.assertEqual(result['answer'], '显示结果')
            self.assertEqual(engine.calls[-1][1]['artifact']['id'], 'cases')
        self.assertEqual(self.store.get('artifact', 'cases'), self.cases)

    async def test_modify_preview_preserves_manual_custom_and_unselected_then_applies_once(self):
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': '修改标题', 'actual_result': 'AI伪造'}}], 'summary': '只改标题'})
        result = await preview_action(self.store, engine, 'cases', {'action': 'modify', 'instruction': '改C1标题', 'selected_ids': ['C1']})
        self.assertEqual(self.store.get('artifact', 'cases'), self.cases)
        changed = result['changes'][0]['items']
        self.assertEqual(changed[0]['actual_result'], '已人工填写')
        self.assertEqual(changed[0]['custom_note'], '保留自定义内容')
        self.assertEqual(changed[1], self.cases['items'][1])
        saved = apply_action(self.store, 'cases', result['id'])
        self.assertEqual(saved['artifacts'][0]['revision'], 2)
        self.assertTrue(apply_action(self.store, 'cases', result['id'])['already_applied'])
        self.assertEqual(self.store.get('artifact', 'cases')['revision'], 2)

    async def test_analysis_report_patch_is_saved_and_lineage_cannot_be_overwritten(self):
        analysis = self.artifact('analysis', 'analysis', [{'id': 'R1', 'title': '规则', 'description': '旧规则', 'refs': [self.ref]}], {'summary': '旧摘要', 'lineage': {'origin': 'keep'}})
        engine = Engine(lambda *_: {'operations': [], 'report_patch': {'summary': '新摘要', 'diagrams': [{'mermaid': 'flowchart TD\n A-->B'}]}})
        result = await preview_action(self.store, engine, 'analysis', {'action': 'modify', 'instruction': '改图与摘要'})
        apply_action(self.store, 'analysis', result['id'])
        self.assertEqual(self.store.get('artifact', 'analysis')['report']['summary'], '新摘要')
        self.assertEqual(self.store.get('artifact', 'analysis')['report']['lineage'], analysis['report']['lineage'])
        engine.responder = lambda *_: {'operations': [], 'report_patch': {'lineage': {'bad': 'overwrite'}}}
        with self.assertRaises(DomainError):
            await preview_action(self.store, engine, 'analysis', {'action': 'modify', 'instruction': '改图'})

    async def test_rejects_unselected_update_and_unprovided_refs(self):
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C2', 'item': {'title': '不许改变'}}]})
        with self.assertRaises(DomainError):
            await preview_action(self.store, engine, 'cases', {'action': 'modify', 'instruction': '只改C1', 'selected_ids': ['C1']})
        engine.responder = lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'refs': ['invented#P1']}}]}
        with self.assertRaises(DomainError):
            await preview_action(self.store, engine, 'cases', {'action': 'modify', 'instruction': '只改C1', 'selected_ids': ['C1']})
        self.assertEqual(self.store.get('artifact', 'cases'), self.cases)

    async def test_new_source_refs_join_only_when_applied_and_source_changes_reject(self):
        extra = self.store.add_source(self.chat, 'new rule', 'change', '账号不区分大小写', [{'text': '账号不区分大小写', 'location': 'P1'}])
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'refs': [extra['id'] + '#P1'], 'title': '大小写'}}]})
        result = await preview_action(self.store, engine, 'cases', {'action': 'modify', 'instruction': '依据新资料调整', 'selected_ids': ['C1'], 'source_ids': [extra['id']]})
        self.assertNotIn(extra['id'], self.store.get('artifact', 'cases')['_source_ids'])
        apply_action(self.store, 'cases', result['id'])
        self.assertIn(extra['id'], self.store.get('artifact', 'cases')['_source_ids'])
        second = await preview_action(self.store, engine, 'cases', {'action': 'modify', 'instruction': '再次调整', 'selected_ids': ['C1'], 'source_ids': [extra['id']]})
        self.store.deactivate_source(extra['id'])
        with self.assertRaises(DomainError):
            apply_action(self.store, 'cases', second['id'])

    async def test_sync_changes_linked_cases_only_and_checks_all_versions_before_commit(self):
        rows = copy.deepcopy(self.scenarios['items']); rows[0]['description'] = '新登录规则'
        self.store.revise_artifact('scenario', 1, rows)
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': '新登录用例'}}], 'summary': '同步登录'})
        result = await preview_action(self.store, engine, 'scenario', {'action': 'sync', 'instruction': '同步变更场景'})
        self.assertEqual(engine.calls[0][1]['selected_ids'], ['C1'])
        self.assertEqual(result['changes'][0]['items'][1], self.cases['items'][1])
        rows[1]['description'] = '另一变更'
        self.store.revise_artifact('scenario', 2, rows)
        with self.assertRaises(DomainError):
            apply_action(self.store, 'scenario', result['id'])
        self.assertEqual(self.store.get('artifact', 'cases')['revision'], 1)
        # Explicit partial sync records only the selected scenario's new baseline.
        result = await preview_action(self.store, engine, 'scenario', {'action': 'sync', 'instruction': '只同步登录', 'selected_ids': ['S1']})
        saved = apply_action(self.store, 'scenario', result['id'])
        self.assertEqual(saved['artifacts'][0]['items'][0]['title'], '新登录用例')
        from tcg.workspace_coverage import changed_scenario_ids
        self.assertEqual(set(changed_scenario_ids(self.store, self.store.get('artifact', 'scenario'), self.store.get('artifact', 'cases'))), {'S2'})

    async def test_apply_all_or_nothing_on_second_case_artifact_stale(self):
        other = self.artifact('other-cases', 'cases', [self.case('D1', 'S1')], {'lineage': {'scenario_artifact_id': 'scenario', 'scenario_revision': 1}})
        rows = copy.deepcopy(self.scenarios['items']); rows[0]['description'] = 'changed'
        self.store.revise_artifact('scenario', 1, rows)
        engine = Engine(lambda task, ctx: {'operations': [{'op': 'update', 'id': ctx['selected_ids'][0], 'item': {'title': '联动'}}]})
        result = await preview_action(self.store, engine, 'scenario', {'action': 'sync', 'instruction': '同步登录', 'selected_ids': ['S1'], 'related_artifact_ids': ['cases', 'other-cases']})
        self.assertEqual(len(result['changes']), 2)
        self.store.revise_artifact('other-cases', 1, other['items'])
        with self.assertRaises(DomainError):
            apply_action(self.store, 'scenario', result['id'])
        self.assertEqual(self.store.get('artifact', 'cases')['revision'], 1)

    async def test_waiting_preview_locks_writers_and_apply_stays_at_confirmation(self):
        self.paused()
        def answer(task, context):
            self.assertTrue(self.store.run('waiting').get('_edit_token'))
            with self.assertRaises(DomainError):
                self.store.revise_artifact('scenario', 1, self.scenarios['items'])
            with self.assertRaises(DomainError):
                self.store.create_run(self.chat, {'content': 'new', 'intent': 'query', 'mode': 'auto'})
            return {'operations': [{'op': 'update', 'id': 'S1', 'item': {'title': '新场景标题'}}]}
        result = await preview_action(self.store, Engine(answer), 'scenario', {'action': 'modify', 'instruction': '改标题', 'selected_ids': ['S1']})
        self.assertFalse(self.store.run('waiting').get('_edit_token'))
        saved = apply_action(self.store, 'scenario', result['id'])
        self.assertEqual(saved['artifacts'][0]['revision'], 2)
        self.assertEqual(self.store.run('waiting')['status'], 'waiting')
        self.assertEqual(self.store.run('waiting')['interrupt']['items'][0]['title'], '新场景标题')

    async def test_cancel_during_preview_discards_it_and_clears_lease(self):
        self.paused()
        def cancel(*_):
            self.store.update_run('waiting', status='cancelled', _edit_token=None)
            return {'operations': []}
        with self.assertRaises(DomainError):
            await preview_action(self.store, Engine(cancel), 'scenario', {'action': 'modify', 'instruction': '修改'})
        self.assertEqual(self.store.list('action_proposal'), [])
        self.assertFalse(self.store._workspace_action_tokens)
        self.assertEqual(self.store.get('artifact', 'scenario')['revision'], 1)

    async def test_resume_after_preview_rejects_apply(self):
        self.paused()
        result = await preview_action(self.store, Engine(lambda *_: {'operations': []}), 'scenario', {'action': 'modify', 'instruction': '修改'})
        self.store.update_run('waiting', status='completed')
        with self.assertRaises(DomainError):
            apply_action(self.store, 'scenario', result['id'])
        self.assertEqual(self.store.get('artifact', 'scenario')['revision'], 1)

    async def test_deleted_scenario_keeps_reason_evidence_and_baseline_for_sync(self):
        exclusion = self.store.add_source(self.chat, 'scope change', 'change', '排除登录范围。', [{'text': '排除登录范围。', 'location': 'P1'}])
        ref = exclusion['id'] + '#P1'
        engine = Engine(lambda *_: {'operations': [{'op': 'delete', 'id': 'S1', 'reason': '变更排除登录', 'refs': [ref]}]})
        preview = await preview_action(self.store, engine, 'scenario', {'action': 'modify', 'instruction': '删除范围外登录场景', 'selected_ids': ['S1'], 'source_ids': [exclusion['id']]})
        apply_action(self.store, 'scenario', preview['id'])
        deletion = self.store.get('artifact', 'scenario')['report']['item_deletions']['S1']
        self.assertEqual(deletion['reason'], '变更排除登录')
        self.assertEqual(deletion['original_item'], self.scenarios['items'][0])
        def sync(task, context):
            self.assertEqual(context['removed_scenario_ids'], ['S1'])
            self.assertEqual(context['baseline_scenarios'][0]['id'], 'S1')
            self.assertEqual(context['deletion_basis'][0]['refs'], [ref])
            return {'operations': [{'op': 'delete', 'id': 'C1', 'reason': '场景已依据变更排除', 'refs': [ref]}]}
        preview = await preview_action(self.store, Engine(sync), 'scenario', {'action': 'sync', 'instruction': '同步范围排除'})
        apply_action(self.store, 'scenario', preview['id'])
        cases = self.store.get('artifact', 'cases')
        self.assertEqual([r['id'] for r in cases['items']], ['C2'])
        self.assertEqual(cases['report']['item_deletions']['C1']['refs'], [ref])

    async def test_preview_lease_does_not_block_other_chat_in_same_project(self):
        other_chat = self.store.create_chat(self.project, 'independent')['id']
        other = copy.deepcopy(self.cases)
        other.update(id='independent', chat_id=other_chat)
        self.store.put('artifact', other)
        with action_lease(self.store, [self.scenarios]):
            result = self.store.revise_artifact('independent', 1, other['items'])
            self.assertEqual(result['revision'], 2)


if __name__ == '__main__':
    unittest.main()
