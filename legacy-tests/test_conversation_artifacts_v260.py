"""Real SQLite capability contracts with controlled model responses."""
import copy
import json

import tempfile
import unittest

from tcg.artifact_actions import apply_action, preview_action
from tcg.schemas import DEFAULT_PROFILE, DomainError
from tcg.storage import Store, dump, now


class Engine:
    def __init__(self, respond):
        self.respond = respond

    def fits(self, task, context):
        return len(json.dumps(context)) < 1000000

    async def invoke_model(self, task, context, run_id=None):
        result = self.respond(task, copy.deepcopy(context))
        return await result if hasattr(result, '__await__') else copy.deepcopy(result)


def data(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]['id']
    chat = store.create_chat(project, 'Conversation artifacts')
    source = store.add_source(chat['id'], 'Rules', 'primary', 'Lock for ten minutes.', [{'text': 'Lock for ten minutes.', 'location': 'P1'}])
    ref = source['id'] + '#P1'
    profile = {**DEFAULT_PROFILE, 'excel_columns': [*DEFAULT_PROFILE['excel_columns'], {'field': 'actual_result', 'header': '执行结果', 'value_source': 'manual'}, {'field': 'manual_note', 'header': '人工备注', 'value_source': 'manual'}]}
    def artifact(aid, kind, items, report=None):
        row = {'id': aid, 'project_id': project, 'chat_id': chat['id'], 'type': kind, 'title': aid,
               'revision': 1, 'items': items, 'report': report or {}, 'created_at': now(), '_visible': True,
               '_source_ids': [source['id']], '_source_roles': {source['id']: 'primary'}, '_profile': profile}
        with store.transaction():
            store.put('artifact', row)
            store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (aid, 1, dump(row), now(), 'generated', dump({'added': [r['id'] for r in items], 'updated': [], 'deleted': []})))
        return row
    analysis = artifact('analysis', 'analysis', [{'id': f'R{i}', 'title': f'Rule {i}', 'description': f'Rule {i}', 'refs': [ref]} for i in (1, 2)])
    scenarios = artifact('scenarios', 'scenarios', [{'id': f'S{i}', 'title': f'Scenario {i}', 'description': f'Rule {i}', 'priority': 'P1', 'requirement_ids': [f'R{i}'], 'refs': [ref]} for i in (1, 2)], {'lineage': {'analysis_artifact_id': 'analysis', 'analysis_revision': 1}})
    cases = artifact('cases', 'cases', [{'id': f'C{i}', 'title': f'Case {i}', 'scenario_id': f'S{i}', 'type': 'Business', 'priority': 'P1', 'preconditions': '', 'steps': [{'action': 'Log in', 'expected': f'Rule {i}'}], 'refs': [ref], 'actual_result': 'Human result', 'manual_note': 'Keep human note', 'custom_note': 'Preserve other data'} for i in (1, 2)], {'lineage': {'scenario_artifact_id': 'scenarios', 'scenario_revision': 1}})
    yield store, chat, analysis, scenarios, cases, ref, artifact
    store.close()


def wait_run(store, chat, aid='scenarios'):
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'guided', 'content': 'Generate', 'artifact_id': aid})
    run.update(status='waiting', interrupt={'type': 'scenario_review', 'artifact_id': aid}, _interrupt_id='gate-1')
    store.save_run(run)
    return run


async def test_snapshot_read_finishes_after_main_run_advances(data, action):
    store, chat, _, scenarios, _, ref, _ = data
    run = wait_run(store, chat)
    def answer(task, context):
        active = store.run(run['id'])
        assert not active.get('_edit_token'), 'A read must leave confirmation available'
        assert not getattr(store, '_workspace_action_tokens', {}), 'A read must not claim a write lease'
        active.update(status='running')
        active.pop('interrupt', None)
        store.save_run(active)
        store.revise_artifact('scenarios', 1, [{**r, 'title': 'New title'} for r in scenarios['items']])
        if task == 'artifact_estimate':
            return {'scenarios': [{'scenario_id': 'S1', 'min_count': 2, 'max_count': 3, 'rationale': context['scenarios'][0]['title'], 'assumptions': []}]}
        return {'answer': context['artifact']['items'][0]['title'], 'refs': [ref]}
    result = await preview_action(store, Engine(answer), 'scenarios', {'action': action, 'instruction': 'Read', 'selected_ids': ['S1']})
    assert result['revision'] == 1
    assert result['estimate']['min_count'] == 2 if action == 'estimate' else result['answer'] == 'Scenario 1'
    assert store.run(run['id'])['status'] == 'running'
    assert store.list('action_proposal', chat_id=chat['id']) == []


async def test_read_and_review_use_saved_case_details_without_revision(data):
    from tcg.conversation_artifacts import execute
    store, chat, _, _, cases, ref, _ = data
    run = wait_run(store, chat)
    run.update(status='running')
    store.save_run(run)
    engine = Engine(lambda task, context: {'report': {'summary': 'Check lock boundary', 'issues': [{'id': 'C1', 'description': 'Add a boundary check', 'refs': [ref]}], 'coverage': []}})
    details = await execute(store, engine, chat, 'artifact.read', {'artifact_id': 'cases', 'selected_ids': ['C1']})
    assert details['parts'][0]['type'] == 'case_details'
    assert details['parts'][0]['items'][0]['steps'] == [{'action': 'Log in', 'expected': 'Rule 1'}]
    review = await execute(store, engine, chat, 'artifact.review_cases', {'artifact_id': 'cases', 'selected_ids': ['C1'], 'instruction': 'Review only'})
    assert review['status'] == 'succeeded'
    assert review['parts'][0]['type'] == 'answer'
    assert store.get('artifact', 'cases') == cases
    assert len(store.revisions('cases')) == 1


async def test_explicit_edit_saves_once_at_gate_and_protects_manual_fields(data):
    from tcg.conversation_artifacts import execute
    store, chat, _, _, cases, _, _ = data
    run = wait_run(store, chat, 'cases')
    engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Clear title', 'actual_result': 'Invented', 'manual_note': 'Invented'}}], 'summary': 'Clear title'})
    response = await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'cases', 'expected_revision': 1, 'selected_ids': ['C1'], 'instruction': 'Clarify title'})
    after = store.get('artifact', 'cases')
    assert after['revision'] == 2
    assert after['items'][0]['title'] == 'Clear title'
    assert after['items'][0]['actual_result'] == 'Human result'
    assert after['items'][0]['manual_note'] == 'Keep human note'
    assert after['items'][1] == cases['items'][1]
    assert store.run(run['id'])['status'] == 'waiting'
    assert response['parts'][0] == {'type': 'artifact', 'artifact_id': 'cases', 'revision': 2}
    with unittest.TestCase().assertRaises(DomainError):
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'cases', 'expected_revision': 1, 'instruction': 'Stale edit'})


async def test_preview_discard_and_stale_apply_do_not_write(data):
    from tcg.conversation_artifacts import execute
    store, chat, _, _, cases, _, _ = data
    engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Draft'}}], 'summary': 'Draft'})
    response = await execute(store, engine, chat, 'artifact.preview', {'artifact_id': 'cases', 'selected_ids': ['C1'], 'instruction': 'Preview'})
    proposal_id = response['parts'][0]['proposal_id']
    assert store.get('artifact', 'cases') == cases
    await execute(store, engine, chat, 'artifact.discard', {'artifact_id': 'cases', 'proposal_id': proposal_id})
    with unittest.TestCase().assertRaises(DomainError):
        await execute(store, engine, chat, 'artifact.apply', {'artifact_id': 'cases', 'proposal_id': proposal_id})
    assert store.get('artifact', 'cases') == cases
    response = await execute(store, engine, chat, 'artifact.preview', {'artifact_id': 'cases', 'selected_ids': ['C1'], 'instruction': 'Preview'})
    store.revise_artifact('cases', 1, cases['items'])
    with unittest.TestCase().assertRaises(DomainError):
        await execute(store, engine, chat, 'artifact.apply', {'artifact_id': 'cases', 'proposal_id': response['parts'][0]['proposal_id']})
    assert len(store.revisions('cases')) == 2


async def test_upstream_preview_propagates_new_drafts_only_to_linked_rows(data):
    store, chat, analysis, scenarios, cases, _, _ = data
    def answer(task, context):
        if task == 'artifact_modify':
            return {'operations': [{'op': 'update', 'id': 'R1', 'item': {'description': 'Lock for ten minutes'}}], 'summary': 'Clarify rule'}
        if task == 'artifact_sync_scenarios':
            assert [r['id'] for r in context['analysis']] == ['R1']
            return {'operations': [{'op': 'update', 'id': 'S1', 'item': {'description': context['analysis'][0]['description']}}], 'summary': 'Sync scenario'}
        assert task == 'artifact_sync'
        return {'operations': [{'op': 'update', 'id': 'C1', 'item': {'steps': [{'action': 'Log in', 'expected': context['scenarios'][0]['description']}], 'actual_result': 'Invented'}}], 'summary': 'Sync case'}
    proposal = await preview_action(store, Engine(answer), 'analysis', {'action': 'modify', 'instruction': 'Clarify then sync', 'selected_ids': ['R1'], 'sync_related': True})
    assert [c['artifact_id'] for c in proposal['changes']] == ['analysis', 'scenarios', 'cases']
    assert store.get('artifact', 'analysis') == analysis
    assert store.get('artifact', 'scenarios') == scenarios
    assert store.get('artifact', 'cases') == cases
    apply_action(store, 'analysis', proposal['id'])
    assert store.get('artifact', 'cases')['items'][0]['steps'][0]['expected'] == 'Lock for ten minutes'
    assert store.get('artifact', 'cases')['items'][0]['actual_result'] == 'Human result'
    assert store.get('artifact', 'cases')['items'][1] == cases['items'][1]
    assert store.get('artifact', 'scenarios')['items'][1] == scenarios['items'][1]
    assert store.get('artifact', 'cases')['report']['lineage']['scenario_revisions']['S1'] == 2
    assert store.get('artifact', 'scenarios')['report']['lineage']['analysis_revisions']['R1'] == 2


async def test_rejected_downstream_operation_never_partially_saves_upstream(data):
    store, _, analysis, scenarios, cases, _, _ = data
    def answer(task, context):
        target = 'R1' if task == 'artifact_modify' else 'S2'
        return {'operations': [{'op': 'update', 'id': target, 'item': {'description': 'Invalid unrelated edit'}}]}
    with unittest.TestCase().assertRaises(DomainError):
        await preview_action(store, Engine(answer), 'analysis', {'action': 'modify', 'instruction': 'Sync only R1', 'selected_ids': ['R1'], 'sync_related': True})
    assert store.get('artifact', 'analysis') == analysis
    assert store.get('artifact', 'scenarios') == scenarios
    assert store.get('artifact', 'cases') == cases


class ConversationArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.fixture = data(self.directory.name)
        self.data = next(self.fixture)

    def tearDown(self):
        next(self.fixture, None)
        self.directory.cleanup()

    async def test_snapshot_reads(self):
        for action in ('estimate', 'explain'):
            with self.subTest(action=action):
                # Each action runs with its own persisted gate and artifact history.
                with tempfile.TemporaryDirectory() as directory:
                    fixture = data(directory)
                    try:
                        await test_snapshot_read_finishes_after_main_run_advances(next(fixture), action)
                    finally:
                        next(fixture, None)

    async def test_read_and_review_use_saved_case_details_without_revision(self):
        await test_read_and_review_use_saved_case_details_without_revision(self.data)

    async def test_explicit_edit_saves_once_at_gate_and_protects_manual_fields(self):
        await test_explicit_edit_saves_once_at_gate_and_protects_manual_fields(self.data)

    async def test_preview_discard_and_stale_apply_do_not_write(self):
        await test_preview_discard_and_stale_apply_do_not_write(self.data)

    async def test_upstream_preview_propagates_new_drafts_only_to_linked_rows(self):
        await test_upstream_preview_propagates_new_drafts_only_to_linked_rows(self.data)

    async def test_rejected_downstream_operation_never_partially_saves_upstream(self):
        await test_rejected_downstream_operation_never_partially_saves_upstream(self.data)

    async def test_new_evidence_joins_paused_run_without_changing_stop_scope(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, _, _, _, _ = self.data
        run = wait_run(store, chat)
        run['_request']['stop_after'] = 'scenarios'
        store.save_run(run)
        source = store.add_source(chat['id'], 'Change', 'change', 'New rule', [{'text': 'New rule', 'location': 'P1'}])
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'S1', 'item': {'description': 'New rule', 'refs': [source['id'] + '#P1']}}]})
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'scenarios', 'instruction': 'Use the change', 'source_ids': [source['id']], 'selected_ids': ['S1']})
        current = store.run(run['id'])
        assert source['id'] in current['_source_ids']
        assert current['_source_roles'][source['id']] == 'change'
        assert current['_request']['stop_after'] == 'scenarios'
        assert current['status'] == 'waiting'

    async def test_review_report_edit_and_unconfigured_execution_fields_are_protected(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, _, cases, _, _ = self.data
        cases['_profile'] = {}
        store.put('artifact', cases)
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'actual_result': 'Invented', 'title': 'Improved'}}], 'report_patch': {'review_reports': [{'summary': 'Reviewed wording', 'issues': []}]}})
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'cases', 'instruction': 'Improve the review wording', 'selected_ids': ['C1']})
        saved = store.get('artifact', 'cases')
        assert saved['items'][0]['actual_result'] == 'Human result'
        assert saved['report']['review_reports'][0]['summary'] == 'Reviewed wording'

    async def test_project_question_retrieves_relevant_shared_facts_without_sending_full_documents(self):
        from tcg.conversation_artifacts import execute
        from tcg.project_context import share_clarification
        store, chat, _, _, _, _, _ = self.data
        source = store.add_source(chat['id'], 'Confirmed', 'clarification', '锁定时间确认是10分钟。', [{'text': '锁定时间确认是10分钟。', 'location': 'P1'}])
        share_clarification(store, source['id'], chat['project_id'])
        other = store.create_chat(chat['project_id'], 'Other')
        store.add_source(other['id'], 'Large unrelated material', 'primary', 'Unrelated' * 100000, [{'text': 'UNRELATED_PRIVATE_' + 'x' * 1000, 'location': f'P{i}'} for i in range(1, 81)])
        captured = []
        def answer(task, context):
            captured.append(context)
            return {'answer': '锁定10分钟。', 'refs': [source['id'] + '#P1']}
        result = await execute(store, Engine(answer), other, 'artifact.analyze', {'question': '本项目确认的锁定时间是多少？'})
        assert result['parts'][0]['refs'] == [source['id'] + '#P1']
        evidence = [row for context in captured for row in context['evidence']]
        assert len(evidence) <= 12
        assert sum(len(row.get('text', '')) for row in evidence) <= 24000
        assert any(context.get('retrieval', {}).get('omitted_chunks', 0) > 0 for context in captured)

    async def test_late_cancelled_command_rolls_back_artifact_revision(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, _, cases, _, _ = self.data
        command = {'id': 'cmd-edit', 'chat_id': chat['id'], 'project_id': chat['project_id'], 'status': 'running'}
        store.put('conversation_command', command)
        def answer(task, context):
            store.put('conversation_command', {**command, 'status': 'cancelled'})
            return {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Late edit'}}]}
        with self.assertRaises(DomainError):
            await execute(store, Engine(answer), chat, 'artifact.revise', {'artifact_id': 'cases', 'instruction': 'Edit title', 'selected_ids': ['C1']}, 'cmd-edit')
        assert store.get('artifact', 'cases') == cases
        assert len(store.revisions('cases')) == 1
        assert store.list('action_proposal', chat_id=chat['id']) == []

    async def test_safe_boundary_and_linked_ancestor_edits_preserve_gate(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, _, _, _, _ = self.data
        run = wait_run(store, chat)
        engine = Engine(lambda task, context: {'operations': [{'op': 'update', 'id': 'R1', 'item': {'description': 'Clearer rule'}}]})
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'analysis', 'selected_ids': ['R1'], 'instruction': 'Clarify this rule'})
        assert store.run(run['id'])['interrupt']['artifact_id'] == 'scenarios'
        current = store.run(run['id'])
        current['interrupt'] = {'type': 'workflow_paused', 'reason': 'write'}
        current['_interrupt_id'] = 'boundary-2'
        store.save_run(current)
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'analysis', 'selected_ids': ['R1'], 'instruction': 'Clarify this rule'})
        assert store.get('artifact', 'analysis')['revision'] == 3
        assert store.run(run['id'])['status'] == 'waiting'
        assert store.run(run['id'])['interrupt']['type'] == 'workflow_paused'

    async def test_explicit_historical_reads_use_saved_revision(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, scenarios, cases, _, _ = self.data
        store.revise_artifact('cases', 1, [{**r, 'title': 'Current title'} for r in cases['items']])
        store.revise_artifact('scenarios', 1, [{**r, 'title': 'Current scenario'} for r in scenarios['items']])
        def answer(task, context):
            return {'scenarios': [{'scenario_id': row['id'], 'min_count': 1, 'max_count': 2, 'rationale': row['title'], 'assumptions': []} for row in context['scenarios']]}
        read = await execute(store, Engine(answer), chat, 'artifact.read', {'artifact_id': 'cases', 'expected_revision': 1, 'selected_ids': ['C1']})
        assert read['parts'][0]['revision'] == 1
        assert read['parts'][0]['items'][0]['title'] == 'Case 1'
        estimate = await execute(store, Engine(answer), chat, 'artifact.estimate', {'artifact_id': 'scenarios', 'expected_revision': 1, 'selected_ids': ['S1']})
        assert estimate['parts'][0]['data']['artifact_revision'] == 1
        assert estimate['parts'][0]['data']['scenarios'][0]['rationale'] == 'Scenario 1'

    async def test_updated_analysis_refreshes_only_bound_run_requirement_context(self):
        from tcg.conversation_artifacts import execute
        store, chat, analysis, _, _, _, _ = self.data
        run = wait_run(store, chat)
        store.update_run(run['id'], status='running')
        store.cache_set(run['id'], 'workspace:analysis_parent', {'id': 'analysis', 'revision': 1})
        store.cache_set(run['id'], 'v6:requirement_map', {'confirmed_requirements': analysis['items'], 'summary': 'Old summary'})
        store.update_run(run['id'], status='waiting')
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'R1', 'item': {'description': 'New exact rule'}}], 'report_patch': {'summary': 'New summary'}})
        await execute(store, engine, chat, 'artifact.revise', {'artifact_id': 'analysis', 'selected_ids': ['R1'], 'instruction': 'Clarify rule'})
        context = store.cache_get(run['id'], 'v6:requirement_map')
        assert context['confirmed_requirements'][0]['description'] == 'New exact rule'
        assert context['summary'] == 'New summary'
        assert store.cache_get(run['id'], 'workspace:analysis_parent')['revision'] == 2
        assert store.run(run['id'])['input_version'] > 0

    async def test_project_read_excludes_learned_template_role_overrides(self):
        from tcg.conversation_artifacts import execute
        store, chat, _, _, _, ref, _ = self.data
        source = store.add_source(chat['id'], 'Template uploaded as primary', 'primary', 'EXAMPLE_PRIVATE_FACT', [{'text': 'EXAMPLE_PRIVATE_FACT', 'location': 'P1'}])
        store.put('chat', {**chat, '_source_roles': {source['id']: 'example'}})
        captured = []
        def answer(task, context):
            captured.extend(context['evidence'])
            return {'answer': 'Only current requirement facts', 'refs': [ref]}
        await execute(store, Engine(answer), chat, 'artifact.analyze', {'question': 'Summarize project facts'})
        assert all(row['source_id'] != source['id'] for row in captured)

    async def test_ordinary_edit_cannot_reparent_selected_case_or_scenario(self):
        store, _, _, _, _, _, _ = self.data
        for aid, item_id, field, replacement in [('cases', 'C1', 'scenario_id', 'invented-parent'), ('scenarios', 'S1', 'requirement_ids', ['invented-parent'])]:
            with self.subTest(artifact=aid):
                original = store.get('artifact', aid)
                engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': item_id, 'item': {'title': 'Clear title', field: replacement}}]})
                with self.assertRaises(DomainError):
                    proposal = await preview_action(store, engine, aid, {'action': 'modify', 'instruction': 'Only clarify title', 'selected_ids': [item_id]})
                    apply_action(store, aid, proposal['id'])
                assert store.get('artifact', aid) == original

    async def test_reapplying_proposal_returns_original_saved_revision(self):
        store, _, _, _, _, _, _ = self.data
        engine = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Applied title'}}]})
        proposal = await preview_action(store, engine, 'cases', {'action': 'modify', 'instruction': 'Clarify title', 'selected_ids': ['C1']})
        first = apply_action(store, 'cases', proposal['id'])
        later = [{**r, 'title': 'Later title'} for r in store.get('artifact', 'cases')['items']]
        store.revise_artifact('cases', 2, later)
        replay = apply_action(store, 'cases', proposal['id'])
        assert first['artifacts'][0]['revision'] == replay['artifacts'][0]['revision'] == 2
        assert replay['artifacts'][0]['items'][0]['title'] == 'Applied title'
        assert store.get('artifact', 'cases')['revision'] == 3

    async def test_new_items_cannot_reference_unknown_parent_ids(self):
        store, _, _, scenarios, cases, _, _ = self.data
        for artifact, field, bad in [(cases, 'scenario_id', 'unknown'), (scenarios, 'requirement_ids', ['unknown'])]:
            with self.subTest(artifact=artifact['id']):
                addition = {**artifact['items'][0], 'id': 'NEW', field: bad}
                engine = Engine(lambda *_: {'operations': [{'op': 'add', 'item': addition}]})
                with self.assertRaises(DomainError):
                    await preview_action(store, engine, artifact['id'], {'action': 'modify', 'instruction': 'Add requested coverage'})
                assert store.get('artifact', artifact['id']) == artifact

if __name__ == '__main__':
    unittest.main()
