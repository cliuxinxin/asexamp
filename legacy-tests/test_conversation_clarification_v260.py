"""Real SQLite assertions usable even when server test extras are unavailable."""
import tempfile
import unittest

from tcg.clarification import get_draft, update_draft, save_draft, share_draft
from tcg.conversation_workflow import execute
from tcg.documents import parse_text
from tcg.schemas import DomainError
from tcg.storage import Store


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.chat = self.store.create_chat(self.store.list('project')[0]['id'], 'Clarification')
        text, chunks = parse_text('Confirm the lock duration.')
        self.store.add_source(self.chat['id'], 'Requirement', 'primary', text, chunks)
        _, run = self.store.create_run(self.chat['id'], {'content': 'Generate tests', 'intent': 'generate_case',
            'mode': 'hitp', 'experience': 'reliable'})
        self.rid = run['id']
        self.store.update_run(self.rid, status='waiting', stage='clarification', _interrupt_id='gate-1',
            interrupt={'type': 'clarification', 'questions': ['Lock duration?'], 'question_suggestions': [
                {'question': 'Lock duration?', 'answer': '5 minutes', 'basis': 'Unconfirmed',
                 'refs': [], 'confidence': 'assumption'}]})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_adopt_card_edit_reload_save_share_and_idempotent_source(self):
        draft = get_draft(self.store, self.rid)
        adopted = update_draft(self.store, self.rid, {'expected_revision': draft['revision'], 'adopt_all': True})
        qid = adopted['questions'][0]['id']
        edited = update_draft(self.store, self.rid, {'expected_revision': adopted['revision'], 'answers': {qid: '10 minutes'}})
        self.assertTrue(edited['questions'][0]['adopted'])
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(get_draft(self.store, self.rid), edited)
        saved = save_draft(self.store, self.rid, {'expected_revision': edited['revision']})
        self.assertTrue(saved['submitted'])
        self.assertFalse(saved['shared'])
        shared = share_draft(self.store, self.rid, {'expected_revision': saved['revision']})
        self.assertTrue(shared['shared'])
        again = save_draft(self.store, self.rid)
        self.assertEqual(again['source_id'], shared['source_id'])
        sources = [s for s in self.store.list('source', chat_id=self.chat['id']) if s['role'] == 'clarification']
        self.assertEqual(len(sources), 1)
        self.assertIn('10 minutes', sources[0]['_text'])
        self.assertEqual(sources[0]['_clarification_draft']['questions'][0]['suggestion']['confidence'], 'assumption')
        self.assertEqual(self.store.run(self.rid)['status'], 'waiting')

    def test_stale_revision_question_set_and_clear(self):
        draft = get_draft(self.store, self.rid)
        adopted = update_draft(self.store, self.rid, {'expected_revision': draft['revision'], 'adopt_all': True})
        with self.assertRaises(DomainError):
            update_draft(self.store, self.rid, {'expected_revision': draft['revision'], 'answer': 'stale'})
        qid = adopted['questions'][0]['id']
        cleared = update_draft(self.store, self.rid, {'expected_revision': adopted['revision'], 'answers': {qid: ''}})
        self.assertFalse(cleared['questions'][0]['adopted'])
        self.store.update_run(self.rid, interrupt={'type': 'clarification', 'questions': ['Who can unlock?']})
        changed = get_draft(self.store, self.rid)
        self.assertNotEqual(changed['question_set_version'], draft['question_set_version'])
        self.assertEqual(changed['questions'][0]['answer'], '')
        with self.assertRaises(DomainError):
            update_draft(self.store, self.rid, {'expected_revision': changed['revision'],
                'question_set_version': draft['question_set_version'], 'adopt_all': True})

    def test_full_textarea_edit_reconciles_adopted_question_without_old_answer(self):
        draft = get_draft(self.store, self.rid)
        draft = update_draft(self.store, self.rid, {'expected_revision': draft['revision'], 'adopt_all': True})
        edited = update_draft(self.store, self.rid, {'expected_revision': draft['revision'],
            'answer': draft['answer'].replace('5 minutes', '10 minutes')})
        self.assertEqual(edited['answer'], 'Lock duration?\n10 minutes')
        self.assertEqual(edited['questions'][0]['answer'], '10 minutes')
        self.assertTrue(edited['questions'][0]['adopted'])
        saved = save_draft(self.store, self.rid)
        self.assertNotIn('5 minutes', self.store.get('source', saved['source_id'])['_text'])
        cleared = update_draft(self.store, self.rid, {'expected_revision': saved['revision'], 'answer': ''})
        self.assertEqual(cleared['answer'], '')
        self.assertFalse(cleared['questions'][0]['adopted'])

    def test_edit_confirmed_fact_preserves_history_and_supersedes_shared_source(self):
        draft = get_draft(self.store, self.rid)
        update_draft(self.store, self.rid, {'expected_revision': draft['revision'], 'adopt_all': True})
        first = save_draft(self.store, self.rid)
        first = share_draft(self.store, self.rid)
        qid = first['questions'][0]['id']
        update_draft(self.store, self.rid, {'expected_revision': first['revision'], 'answers': {qid: '10 minutes'}})
        newer = save_draft(self.store, self.rid)
        self.assertNotEqual(first['source_id'], newer['source_id'])
        old = self.store.get('source', first['source_id'])
        self.assertFalse(old['_active'])
        self.assertFalse(old['_project_shared'])
        self.assertEqual(old['_superseded_by'], newer['source_id'])
        self.assertNotIn(first['source_id'], self.store.run(self.rid)['_source_ids'])
        self.assertEqual(self.store.run(self.rid)['input_version'], 2)


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_hold_version_prevents_pending_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                chat = store.create_chat(store.list('project')[0]['id'], 'Controls')
                _, run = store.create_run(chat['id'], {'content': 'Generate', 'intent': 'generate_case', 'mode': 'hitp'})
                store.update_run(run['id'], status='waiting', stage='scenario_review', control_version=3,
                    interrupt={'type': 'scenario_review'}, _interrupt_id='gate')
                result = await execute(store, object(), chat, 'workflow.continue', {'run_id': run['id'],
                    'expected_control_version': 2, 'approved': True})
                self.assertEqual(result['status'], 'cancelled')
                self.assertEqual(store.run(run['id'])['status'], 'waiting')
            finally:
                store.close()

    async def test_saved_stop_requires_explicit_goal_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                chat = store.create_chat(store.list('project')[0]['id'], 'Controls')
                _, run = store.create_run(chat['id'], {'content': 'Generate', 'intent': 'generate_case', 'mode': 'hitp'})
                store.update_run(run['id'], status='waiting', stage='scenario_review', stop_after='scenarios',
                    interrupt={'type': 'scenario_review'}, _interrupt_id='gate')
                result = await execute(store, object(), chat, 'workflow.continue', {'run_id': run['id'], 'approved': True})
                self.assertEqual(result['status'], 'needs_input')
                self.assertEqual(store.run(run['id'])['status'], 'waiting')
                changed = await execute(store, object(), chat, 'workflow.update_scope', {'run_id': run['id'], 'stop_after': 'review'})
                self.assertEqual(changed['status'], 'succeeded')
                self.assertEqual(store.run(run['id'])['stop_after'], 'review')
                self.assertEqual(store.run(run['id'])['input_version'], 1)
            finally:
                store.close()
