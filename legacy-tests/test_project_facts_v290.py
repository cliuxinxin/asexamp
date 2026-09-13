"""Project facts retain exact evidence and explicit confirmation/scope boundaries."""
import tempfile
import unittest
import asyncio

from tcg.clarification import get_draft, save_draft, share_draft, update_draft
from tcg.documents import parse_text
from tcg.project_context import share_clarification, shared_context, shared_sources
from tcg.schemas import DomainError
from tcg.storage import Store


class ProjectFactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.project = self.store.list('project')[0]
        self.chat = self.store.create_chat(self.project['id'], 'Access rules')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def source(self, content):
        text, chunks = parse_text(content)
        return self.store.add_source(self.chat['id'], '撤权规则', 'clarification', text, chunks)

    def new_run(self, chat=None):
        return self.store.create_run((chat or self.chat)['id'], {
            'content': '生成访问权限用例', 'intent': 'generate_case', 'mode': 'hitp'})[1]

    def clarification(self):
        run = self.new_run()
        self.store.update_run(run['id'], status='waiting', stage='clarification', _interrupt_id='gate-1',
            interrupt={'type': 'clarification', 'questions': ['移出白名单多久撤权？'],
                       'question_suggestions': [{'question': '移出白名单多久撤权？',
                           'answer': '10 分钟', 'confidence': 'assumption', 'refs': []}]})
        return run, get_draft(self.store, run['id'])

    def test_submitted_answer_is_shared_and_available_now_without_approving_stage(self):
        run, draft = self.clarification()
        saved = save_draft(self.store, run['id'], {'answer': '立即撤权',
            'scope': {'module': '访问权限', 'version': 'v2'}, 'confirmed_by': '当前用户'})
        self.assertTrue(saved['shared'])
        source = self.store.get('source', saved['source_id'])
        self.assertEqual(source['status'], 'confirmed')
        self.assertEqual(source['scope'], {'module': '访问权限', 'version': 'v2'})
        self.assertEqual(source['provenance']['chat_id'], self.chat['id'])
        self.assertEqual(source['provenance']['run_id'], run['id'])
        current = self.store.run(run['id'])
        self.assertIn(source['id'], current['_source_ids'])
        self.assertEqual(current['status'], 'waiting')
        self.assertEqual(current['_interrupt_id'], 'gate-1')
        self.assertEqual(saved['pending_questions'], [])
        self.assertEqual(saved['resolved_questions'][0]['id'], draft['questions'][0]['id'])
        second = self.store.create_chat(self.project['id'], 'Another member')
        reused = self.new_run(second)
        self.assertIn(source['id'], reused['_source_ids'])
        self.assertEqual(self.store.evidence(reused['_source_ids'])[0]['id'], source['id'] + '#P1')
        self.assertEqual(shared_sources(self.store, self.project['id'], {'module': '支付'}), [])

    def test_suggestion_and_explicit_provisional_answer_never_become_shared_facts(self):
        run, draft = self.clarification()
        self.assertEqual(shared_sources(self.store, self.project['id']), [])
        update_draft(self.store, run['id'], {'adopt_all': True})
        self.assertEqual(shared_sources(self.store, self.project['id']), [])
        saved = save_draft(self.store, run['id'], {'status': 'provisional'})
        source = self.store.get('source', saved['source_id'])
        self.assertEqual(source['status'], 'provisional')
        self.assertIn(source['id'], self.store.run(run['id'])['_source_ids'])
        self.assertFalse(saved['shared'])
        with self.assertRaises(DomainError):
            share_draft(self.store, run['id'])
        self.assertEqual(shared_sources(self.store, self.project['id']), [])
        second = self.store.create_chat(self.project['id'], 'Other task')
        self.assertEqual(self.new_run(second)['_source_ids'], [])
        self.store.update_run(run['id'], status='completed')
        self.assertEqual(self.new_run()['_source_ids'], [])

    def test_same_rule_topic_in_another_module_is_a_separate_scope(self):
        access = self.source('立即撤权')
        billing = self.source('10分钟后撤权')
        share_clarification(self.store, access['id'], self.project['id'],
            scope={'module': '访问权限', 'version': 'v2'}, fact_key='撤权时机')
        share_clarification(self.store, billing['id'], self.project['id'],
            scope={'module': '计费', 'version': 'v2'}, fact_key='撤权时机')
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'],
            {'module': '计费', 'version': 'v2'})], [billing['id']])
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'],
            {'module': '访问权限', 'version': 'v2'})], [access['id']])

    def test_explicit_replacement_preserves_old_refs_and_existing_task_inputs(self):
        old = self.source('移出白名单 10 分钟后撤权')
        share_clarification(self.store, old['id'], self.project['id'],
            scope={'module': '访问权限', 'version': 'v1'}, fact_key='撤权时机')
        snapshot = self.store.evidence_version(old['id'], 1)
        run = self.new_run()
        newer = self.source('从 v2 开始立即撤权')
        confirmed = share_clarification(self.store, newer['id'], self.project['id'],
            scope={'module': '访问权限', 'version': 'v2'}, fact_key='撤权时机', supersedes=[old['id']])
        previous = self.store.get('source', old['id'])
        self.assertEqual(previous['status'], 'superseded')
        self.assertEqual(previous['superseded_by'], newer['id'])
        self.assertEqual(confirmed['supersedes'], [old['id']])
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'])], [newer['id']])
        self.assertEqual(self.store.run(run['id'])['_source_ids'], [old['id']])
        self.assertEqual(self.store.evidence_version(old['id'], 1), snapshot)
        self.assertEqual(self.store.evidence([old['id']])[0]['text'], snapshot[0]['text'])
        history = shared_context(self.store, self.project['id'])['fact_history']
        self.assertEqual(history[0]['id'], old['id'])

    def test_conflicting_same_scope_requires_only_that_rule_and_keeps_confirmed_fact(self):
        first = self.source('立即撤权')
        scope = {'module': '访问权限', 'version': 'v2'}
        share_clarification(self.store, first['id'], self.project['id'], scope=scope, fact_key='撤权时机')
        second = self.source('10 分钟后撤权')
        with self.assertRaises(DomainError) as caught:
            share_clarification(self.store, second['id'], self.project['id'], scope=scope, fact_key='撤权时机')
        self.assertIn('撤权时机', str(caught.exception))
        self.assertIn('立即撤权', str(caught.exception))
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'])], [first['id']])
        other = self.store.create_project('Other')
        with self.assertRaises(DomainError):
            share_clarification(self.store, second['id'], other['id'], supersedes=[first['id']])

    def test_chat_capability_records_message_provenance_and_retry_is_idempotent(self):
        from tcg.project_facts import execute
        args = {'content': '移出白名单立即撤权', 'fact_key': '撤权时机',
                'scope': {'module': '访问权限', 'version': 'v2'}}
        self.store.put('conversation_command', {'id': 'command-1', 'turn_id': 'turn-1',
            'chat_id': self.chat['id'], 'project_id': self.project['id'], 'status': 'running'})
        first = asyncio.run(execute(self.store, object(), self.chat, 'project.confirm_fact', args, 'command-1'))
        again = asyncio.run(execute(self.store, object(), self.chat, 'project.confirm_fact', args, 'command-1'))
        self.assertEqual(first, again)
        self.assertEqual(len(shared_sources(self.store, self.project['id'])), 1)
        self.assertEqual(first['fact']['provenance']['message_id'], 'input:turn-1')
        before = self.store.list('source')
        conflict = asyncio.run(execute(self.store, object(), self.chat, 'project.confirm_fact',
            {**args, 'content': '移出白名单10分钟后撤权'}))
        self.assertEqual(conflict['status'], 'needs_input')
        self.assertEqual(conflict['conflicts'][0]['key'], '撤权时机')
        self.assertEqual(self.store.list('source'), before)
        resolved = asyncio.run(execute(self.store, object(), self.chat, 'project.confirm_fact',
            {**args, 'content': '从v3起10分钟后撤权', 'scope': {'module': '访问权限', 'version': 'v3'},
             'supersedes': [first['fact']['id']]}))
        self.assertEqual(resolved['status'], 'succeeded')
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'])], [resolved['fact']['id']])
        listed = asyncio.run(execute(self.store, object(), self.chat, 'project.list_facts', {'include_history': True}))
        self.assertEqual({s['status'] for s in listed['facts']}, {'confirmed', 'superseded'})

    def test_followup_question_submission_keeps_previously_answered_rule(self):
        run, draft = self.clarification()
        first = save_draft(self.store, run['id'], {'answer': '立即撤权'})
        self.store.update_run(run['id'], interrupt={'type': 'clarification', 'questions': ['管理员能恢复访问吗？']})
        followup = get_draft(self.store, run['id'])
        self.assertIsNone(followup['source_id'])
        second = save_draft(self.store, run['id'], {'answer': '需重新加入白名单'})
        self.assertEqual(set(self.store.run(run['id'])['_source_ids']), {first['source_id'], second['source_id']})
        self.assertEqual(len(shared_sources(self.store, self.project['id'])), 2)

    def test_provisional_edit_cannot_retire_a_confirmed_project_rule(self):
        run, draft = self.clarification()
        first = save_draft(self.store, run['id'], {'answer': '立即撤权'})
        assumed = save_draft(self.store, run['id'], {'answer': '先假设 10 分钟后撤权', 'status': 'provisional'})
        self.assertEqual([s['id'] for s in shared_sources(self.store, self.project['id'])], [first['source_id']])
        self.assertEqual(self.store.get('source', first['source_id'])['status'], 'confirmed')
        self.assertEqual(self.store.get('source', assumed['source_id'])['status'], 'provisional')


if __name__ == '__main__':
    unittest.main()
