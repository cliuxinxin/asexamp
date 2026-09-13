"""Focused project reuse tests against real SQLite Store, without HTTP/model runtime."""
import ast
import asyncio
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tcg.storage import Store, now
from tcg.schemas import DomainError, MessageInput, profile_config
from tcg.documents import parse_text
from tcg.project_context import (pin_samples, share_clarification, shared_context,
    supplement_run, unshare_clarification, validate_sample_cases)


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(self.directory.name)
        self.project = self.store.list('project')[0]
        self.profile = self.store.list('profile')[0]
        self.chat = self.store.create_chat(self.project['id'], 'first')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def source(self, role='primary', text='正确密码允许登录', chat=None):
        content, chunks = parse_text(text)
        return self.store.add_source((chat or self.chat)['id'], '资料', role, content, chunks)

    def new_run(self, chat=None, **extra):
        request = MessageInput(content='生成登录用例', experience='reliable', intent='generate_case', mode='hitp', **extra).model_dump()
        return self.store.create_run((chat or self.chat)['id'], request)[1]

    def case_artifact(self):
        item = {'id': 'C1', 'title': '登录成功', 'scenario_id': 'S1', 'type': 'Business', 'priority': 'P1',
                'preconditions': '已注册', 'steps': [{'action': '输入有效密码', 'expected': '登录成功'}],
                'refs': ['real#P1'], 'description': '验证登录'}
        value = {'id': 'artifact', 'project_id': self.project['id'], 'chat_id': self.chat['id'],
                 'type': 'cases', '_visible': True, 'created_at': now(), 'items': [item]}
        return self.store.put('artifact', value)

    def test_shared_clarification_reused_with_exact_refs_and_unshare_preserves_original(self):
        source = self.source('clarification', '问题：允许哪些角色？\n回答：注册用户')
        share_clarification(self.store, source['id'], self.project['id'])
        share_clarification(self.store, source['id'], self.project['id'])
        second = self.store.create_chat(self.project['id'], 'second')
        run = self.new_run(second)
        self.assertEqual(run['_source_ids'], [source['id']])
        self.assertEqual(self.store.evidence(run['_source_ids'])[0]['id'], source['id'] + '#P1')
        self.assertEqual(len(shared_context(self.store, self.project['id'])['clarifications']), 1)
        unshare_clarification(self.store, self.project['id'], source['id'])
        third = self.store.create_chat(self.project['id'], 'third')
        self.assertEqual(self.new_run(third)['_source_ids'], [])
        self.assertTrue(self.store.get('source', source['id'])['_active'])
        self.assertEqual(len(self.store.evidence(run['_source_ids'])), 1)

    def test_project_isolation_and_explicit_source_selection(self):
        source = self.source('clarification')
        share_clarification(self.store, source['id'], self.project['id'])
        other = self.store.create_project('other')
        other_chat = self.store.create_chat(other['id'], 'elsewhere')
        self.assertEqual(self.new_run(other_chat)['_source_ids'], [])
        second = self.store.create_chat(self.project['id'], 'explicit')
        self.assertEqual(self.new_run(second, source_ids=[])['_source_ids'], [])
        with self.assertRaises(DomainError):
            share_clarification(self.store, source['id'], other['id'])

    def test_pin_samples_snapshot_format_only_and_version_guard(self):
        artifact = self.case_artifact()
        updated = pin_samples(self.store, artifact['id'], self.profile['id'], 1, ['C1'])
        row = updated['config']['sample_cases'][0]
        self.assertEqual(row['steps'][0]['expected'], '登录成功')
        self.assertNotIn('refs', row)
        self.assertNotIn('scenario_id', row)
        self.assertEqual(updated['version'], 2)
        self.assertEqual(profile_config(updated['config'])['sample_cases'], [row])
        self.assertEqual(self.store.get('artifact', artifact['id'])['items'][0]['refs'], ['real#P1'])
        with self.assertRaises(DomainError):
            pin_samples(self.store, artifact['id'], self.profile['id'], 1, ['C1'])
        with self.assertRaises(DomainError):
            validate_sample_cases([{**row, 'refs': ['real#P1']}])
        with self.assertRaises(DomainError):
            validate_sample_cases([{**row, 'description': 'x' * 12001}])
        other = self.store.create_project('other')
        other_profile = self.store.list('profile', project_id=other['id'])[0]
        with self.assertRaises(DomainError):
            pin_samples(self.store, artifact['id'], other_profile['id'], 1, ['C1'])

    def test_supplement_atomically_restarts_and_preserves_profile_roles_artifacts(self):
        source = self.source()
        run = self.new_run()
        self.store.update_run(run['id'], status='waiting', interrupt={'type': 'scenario_review'})
        self.store.update_run(run['id'], _source_roles={source['id']: 'change'})
        artifact = self.case_artifact()
        new_source = self.source('supplement', '连续失败五次锁定账号')
        result = supplement_run(self.store, run['id'], [new_source['id']], '管理员不受此限制')
        successor = self.store.run(result['run']['id'])
        self.assertEqual(self.store.run(run['id'])['status'], 'cancelled')
        self.assertEqual(successor['intent'], run['intent'])
        self.assertEqual(successor['_profile'], run['_profile'])
        self.assertEqual(successor['_source_roles'][source['id']], 'change')
        self.assertIn(new_source['id'], successor['_source_ids'])
        self.assertEqual(len(successor['_source_ids']), 3)
        self.assertIsNone(successor['_artifact_snapshot'])
        self.assertTrue(successor['_request']['_fresh_after_supplement'])
        self.assertEqual(self.store.get('artifact', artifact['id']), artifact)
        with self.assertRaises(DomainError):
            supplement_run(self.store, run['id'], [new_source['id']])

    def test_supplement_rejects_busy_wrong_project_and_rolls_back_when_creation_fails(self):
        self.source()
        run = self.new_run()
        with self.assertRaises(DomainError):
            supplement_run(self.store, run['id'], [], 'new')
        self.store.update_run(run['id'], status='waiting', _edit_token='editing')
        with self.assertRaises(DomainError):
            supplement_run(self.store, run['id'], [], 'new')
        self.store.update_run(run['id'], _edit_token=None)
        other = self.store.create_project('other')
        other_chat = self.store.create_chat(other['id'], 'other')
        outside = self.source(chat=other_chat)
        with self.assertRaises(DomainError):
            supplement_run(self.store, run['id'], [outside['id']])
        before = self.store.list('source')
        self.store._workspace_action_tokens = {self.project['id']: 'held'}
        with self.assertRaises(DomainError):
            supplement_run(self.store, run['id'], [], 'new')
        self.assertEqual(self.store.run(run['id'])['status'], 'waiting')
        self.assertEqual(self.store.list('source'), before)
        self.assertEqual(len(self.store.runs(chat_id=self.chat['id'])), 1)

    def test_actual_clarification_apply_shares_only_after_submit_and_honors_opt_out(self):
        tree = ast.parse((ROOT / 'backend/tcg/workflow.py').read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'WorkflowEngine')
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'node_apply_answer')
        namespace = {'__package__': 'tcg', 'parse_text': parse_text}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'workflow.py', 'exec'), namespace)
        graph_tree = ast.parse((ROOT / 'backend/tcg/graph.py').read_text())
        graph_class = next(node for node in graph_tree.body if isinstance(node, ast.ClassDef) and node.name == 'Engine')
        resume = next(node for node in graph_class.body if isinstance(node, ast.FunctionDef) and node.name == 'resume')
        namespace['DomainError'] = DomainError
        exec(compile(ast.Module(body=[resume], type_ignores=[]), 'graph.py', 'exec'), namespace)
        for share in (False, True):
            chat = self.store.create_chat(self.project['id'], str(share))
            source = self.source(chat=chat)
            run = self.new_run(chat)
            artifact = self.store.artifact(run['id'], 'analysis', 'analysis', '理解',
                [{'id': 'R1', 'title': '登录', 'description': '密码验证', 'refs': [source['id'] + '#P1']}], {})
            engine = type('Engine', (), {'store': self.store, 'stage': lambda *args: None,
                                        'trace': lambda *args, **kwargs: None, 'schedule': lambda *args: None})()
            state = {'run_id': run['id'], 'analysis_ref': artifact['id'],
                     'clarification_questions': ['允许哪些角色？'], 'clarification_answer': '注册用户'}
            self.store.update_run(run['id'], status='waiting', interrupt={'type': 'clarification'}, _interrupt_id='question')
            namespace['resume'](engine, run['id'], {'answer': state['clarification_answer'], 'save_to_project': share})
            asyncio.run(namespace['node_apply_answer'](engine, state))
            saved = self.store.cache_get(run['id'], 'v7:clarification_source')
            self.assertEqual(bool(self.store.get('source', saved['id']).get('_project_shared')), share)
            self.assertIn(saved['id'], self.store.run(run['id'])['_source_ids'])
        self.assertEqual(len(shared_context(self.store, self.project['id'])['clarifications']), 1)


if __name__ == '__main__':
    unittest.main()
