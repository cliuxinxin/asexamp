"""Targeted source adoption and exact row lineage, using real SQLite revisions."""
import copy

import pytest

from test_conversation_artifacts_v260 import Engine, data, wait_run
from tcg.artifact_actions import apply_action, preview_action
from tcg.conversation_project import execute
from tcg.dependencies import DependencyConflict
from tcg.schemas import DomainError
from tcg.workspace_changes import assert_current_inputs, prepare_reconciliation, workspace_state
from tcg.workspace_coverage import workspace_context


@pytest.fixture
def workspace(tmp_path):
    fixture = data(tmp_path)
    yield next(fixture)
    next(fixture, None)


def supplement(store, chat):
    return store.add_source(chat['id'], 'session supplement', 'change',
        'Existing sessions must reject the next request.',
        [{'text': 'Existing sessions must reject the next request.', 'location': 'P1'}])


def test_unadopted_upload_keeps_waiting_case_gate_available(workspace):
    store, chat, _, _, cases, _, _ = workspace
    run = wait_run(store, chat, 'cases')
    run['interrupt']['type'] = 'case_draft_review'
    store.save_run(run)
    source = supplement(store, chat)
    assert_current_inputs(store, store.run(run['id']))
    state = workspace_state(store, chat, 'cases')
    assert state['next_action']['kind'] == 'confirm'
    assert source['id'] in state['impact']['source_ids']
    assert store.get('artifact', 'cases') == cases


@pytest.mark.asyncio
async def test_direct_case_adoption_records_scope_and_upstream_discrepancy(workspace):
    store, chat, analysis, scenarios, cases, ref, _ = workspace
    run = wait_run(store, chat, 'cases')
    source = supplement(store, chat)
    extra_ref = source['id'] + '#P1'
    unused = supplement(store, chat)
    def answer(task, context):
        assert task == 'artifact_modify'
        assert context['artifact']['id'] == 'cases'
        assert context['selected_ids'] == ['C1']
        assert unused['id'] not in {e['source_id'] for e in context['evidence']}
        return {'operations': [{'op': 'update', 'id': 'C1', 'item': {
            'steps': [{'action': 'Request with existing session', 'expected': 'Access rejected'}],
            'refs': [ref, extra_ref], 'actual_result': 'AI override'}}], 'summary': 'Updated C1'}
    result = await execute(store, Engine(answer), chat, 'project.update_from_sources', {
        'artifact_id': 'cases', 'expected_revision': 1, 'selected_ids': ['C1'],
        'source_ids': [source['id']], 'sync_targets': ['cases'], 'preview': False})
    saved = store.get('artifact', 'cases')
    assert result['status'] == 'succeeded'
    assert saved['revision'] == 2
    assert saved['items'][1] == cases['items'][1]
    assert saved['items'][0]['actual_result'] == 'Human result'
    assert saved['items'][0]['scenario_id'] == 'S1'
    assert store.get('artifact', 'analysis') == analysis
    assert store.get('artifact', 'scenarios') == scenarios
    assert source['id'] in saved['_source_ids'] and unused['id'] not in saved['_source_ids']
    assert workspace_state(store, chat, 'cases')['impact']['source_ids'] == [unused['id']]
    adoption = saved['report']['source_adoptions'][-1]
    assert adoption['selected_ids'] == ['C1'] and adoption['source_ids'] == [source['id']]
    assert adoption['source_versions'][0]['version'] == 1
    detail = workspace_context(store, saved)['upstream_discrepancies'][0]
    assert detail['item_ids'] == ['C1'] and detail['status'] == 'pending'
    assert {p['artifact_id'] for p in detail['parents']} == {'analysis', 'scenarios'}
    assert store.run(run['id'])['status'] == 'waiting'
    assert_current_inputs(store, store.run(run['id']))


@pytest.mark.asyncio
async def test_scoped_reconciliation_preserves_preview_and_real_source_conflict(workspace):
    store, chat, _, scenarios, _, ref, _ = workspace
    source = supplement(store, chat)
    answer = Engine(lambda *_: {'operations': [{'op': 'update', 'id': 'S1', 'item': {
        'description': 'Existing sessions rejected', 'refs': [ref, source['id'] + '#P1']}}], 'summary': 'S1'})
    result = await prepare_reconciliation(store, answer, chat, {
        'artifact_id': 'scenarios', 'expected_revision': 1, 'selected_ids': ['S1'],
        'source_ids': [source['id']], 'sync_targets': ['scenarios'], 'preview': True})
    assert result['status'] == 'needs_confirmation'
    assert store.get('artifact', 'scenarios') == scenarios
    diff = next(p for p in result['parts'] if p['type'] == 'diff')
    assert [c['artifact_id'] for c in diff['changes']] == ['scenarios']
    store.deactivate_source(source['id'])
    with pytest.raises(DomainError) as error:
        apply_action(store, diff['artifact_id'], diff['proposal_id'])
    assert error.value.status == 409
    assert store.get('artifact', 'scenarios') == scenarios


def test_stale_consumed_parent_returns_item_level_conflict(workspace):
    store, chat, _, scenarios, _, _, _ = workspace
    run = wait_run(store, chat, 'cases')
    store.revise_artifact('scenarios', 1,
        [{**r, 'description': 'New rule'} if r['id'] == 'S1' else r for r in scenarios['items']])
    with pytest.raises(DependencyConflict) as error:
        assert_current_inputs(store, store.run(run['id']))
    change = error.value.dependency_changes[0]
    assert change['id'] == 'scenarios' and change['affected_item_ids'] == ['C1']
    assert change['upstream_item_ids'] == ['S1']


def test_workspace_lineage_reads_exact_mixed_parent_versions(workspace):
    store, _, analysis, scenarios, cases, ref, _ = workspace
    store.revise_artifact('analysis', 1, [{**r, 'description': 'Later rule'} for r in analysis['items']])
    new_scenes = store.revise_artifact('scenarios', 1,
        [{**r, 'description': 'Later scene'} if r['id'] == 'S1' else r for r in scenarios['items']],
        report={'lineage': {'analysis_artifact_id': 'analysis', 'analysis_revision': 1,
                            'analysis_revisions': {'R1': 2}}})
    current = store.revise_artifact('cases', 1, cases['items'], report={'lineage': {
        'scenario_artifact_id': 'scenarios', 'scenario_revision': 1, 'scenario_revisions': {'S1': 2}}})
    rows = workspace_context(store, current)['lineage_rows']
    assert rows[0]['scenario']['revision'] == 2
    assert rows[0]['requirements'][0]['revision'] == 2
    assert rows[1]['scenario']['revision'] == 1
    assert rows[1]['requirements'][0]['revision'] == 1
    assert rows[0]['requirements'][0]['evidence'][0]['id'] == ref
    historical = workspace_context(store, store.revision('cases', 1))
    assert historical['lineage_rows'][0]['scenario']['revision'] == 1
    assert historical['lineage_rows'][0]['requirements'][0]['item']['description'] == 'Rule 1'


@pytest.mark.asyncio
async def test_new_source_local_update_then_upstream_sync_resolves_before_continue(workspace):
    store, chat, analysis, scenarios, cases, ref, _ = workspace
    run = wait_run(store, chat, 'cases')
    source = supplement(store, chat)
    new_ref = source['id'] + '#P1'
    def answer(task, context):
        if task == 'project_source_impact':
            return {'requirement_ids': ['R1'], 'new_requirements': [],
                'summary': 'Apply existing-session rejection to R1', 'refs': [new_ref],
                'global_impact': False, 'uncertain': False}
        if task == 'artifact_modify':
            aid = context['artifact']['id']
            row_id = 'C1' if aid == 'cases' else 'R1'
            fields = {'steps': [{'action': 'Request with existing session', 'expected': 'Rejected'}]} if aid == 'cases' else {'description': 'Existing-session requests rejected'}
            return {'operations': [{'op': 'update', 'id': row_id, 'item': {**fields, 'refs': [ref, new_ref]}}], 'summary': row_id}
        if task == 'artifact_sync_scenarios':
            return {'operations': [{'op': 'update', 'id': 'S1', 'item': {
                'description': 'Existing-session requests rejected', 'refs': [ref, new_ref]}}], 'summary': 'S1 synced'}
        assert task == 'artifact_sync'
        return {'operations': [], 'summary': 'C1 already implements the synchronized rule'}
    model = Engine(answer)
    await execute(store, model, chat, 'project.update_from_sources', {'artifact_id': 'cases',
        'selected_ids': ['C1'], 'source_ids': [source['id']], 'sync_targets': ['cases']})
    assert store.run(run['id'])['status'] == 'waiting'
    assert store.get('artifact', 'analysis') == analysis
    assert workspace_context(store, store.get('artifact', 'cases'))['upstream_discrepancies'][0]['status'] == 'pending'
    await execute(store, model, chat, 'project.update_from_sources', {'artifact_id': 'analysis',
        'selected_ids': ['R1'], 'source_ids': [source['id']], 'sync_targets': ['analysis', 'scenarios']})
    with pytest.raises(DependencyConflict):
        assert_current_inputs(store, store.run(run['id']))
    await prepare_reconciliation(store, model, chat, {'artifact_id': 'scenarios',
        'selected_ids': ['S1'], 'related_artifact_ids': ['cases'], 'preview': False})
    saved = store.get('artifact', 'cases')
    assert store.run(run['id'])['status'] == 'waiting'
    assert_current_inputs(store, store.run(run['id']))
    assert workspace_context(store, saved)['upstream_discrepancies'][0]['status'] == 'resolved'
    assert saved['items'][1] == cases['items'][1]
    assert store.get('artifact', 'scenarios')['items'][1] == scenarios['items'][1]


@pytest.mark.asyncio
async def test_selected_case_sync_does_not_mark_sibling_case_current(workspace):
    store, chat, _, scenarios, cases, _, _ = workspace
    # Two cases share a primary scenario; synchronizing C1 cannot rebase C2.
    store.revise_artifact('cases', 1, [{**r, 'scenario_id': 'S1'} for r in cases['items']])
    store.revise_artifact('scenarios', 1, [{**r, 'description': 'New rule'} if r['id'] == 'S1' else r for r in scenarios['items']])
    before = copy.deepcopy(store.get('artifact', 'cases'))
    run = wait_run(store, chat, 'cases')
    def answer(task, context):
        assert task == 'artifact_sync'
        assert context['selected_ids'] == ['C1']
        return {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Synchronized C1'}}], 'summary': 'C1 synced'}
    await prepare_reconciliation(store, Engine(answer), chat, {'artifact_id': 'cases',
        'expected_revision': 2, 'selected_ids': ['C1'], 'preview': False})
    saved = store.get('artifact', 'cases')
    assert saved['items'][1] == before['items'][1]
    rows = workspace_context(store, saved)['lineage_rows']
    assert rows[0]['scenario']['revision'] == 2 and rows[1]['scenario']['revision'] == 1
    assert rows[0]['stale'] is False and rows[1]['stale'] is True
    from tcg.context_service import artifact_context
    from tcg.artifact_actions import evidence_for
    evidence = evidence_for(store, [saved])[2]
    first = artifact_context(store, 'artifact_modify', saved, [saved['items'][0]], evidence, 'Explain C1', admit=False)
    second = artifact_context(store, 'artifact_modify', saved, [saved['items'][1]], evidence, 'Explain C2', admit=False)
    assert first['scenarios'][0]['description'] == 'New rule'
    assert second['scenarios'][0]['description'] == 'Rule 1'
    with pytest.raises(DependencyConflict) as error:
        assert_current_inputs(store, store.run(run['id']))
    assert error.value.dependency_changes[0]['affected_item_ids'] == ['C2']


def test_workspace_review_marks_only_changed_design_rows(workspace):
    store, _, _, _, cases, _, _ = workspace
    reviewed = store.revise_artifact('cases', 1, cases['items'], report={
        **cases['report'], 'review_reports': [{'summary': 'Design reviewed'}]})
    manual = store.revise_artifact('cases', 2, [{**r, 'actual_result': 'Executed'} for r in reviewed['items']])
    assert workspace_context(store, manual)['review'] == {'status': 'current', 'revision': 2, 'changed_item_ids': []}
    changed = store.revise_artifact('cases', 3, [{**r, 'title': 'New condition'} if r['id'] == 'C1' else r for r in manual['items']])
    assert workspace_context(store, changed)['review'] == {'status': 'stale', 'revision': 2, 'changed_item_ids': ['C1']}


@pytest.mark.asyncio
async def test_reported_conflict_with_confirmed_fact_never_commits_downstream(workspace):
    from tcg.project_context import share_clarification
    store, chat, _, _, cases, _, _ = workspace
    fact = store.add_source(chat['id'], 'session policy', 'clarification', 'Existing sessions remain valid.',
        [{'text': 'Existing sessions remain valid.', 'location': 'P1'}])
    share_clarification(store, fact['id'], chat['project_id'], fact_key='session policy')
    source = supplement(store, chat)
    def answer(task, context):
        assert task == 'artifact_modify'
        assert context['confirmed_project_facts'][0]['source_id'] == fact['id']
        assert fact['id'] + '#P1' in {e['id'] for e in context['evidence']}
        return {'operations': [], 'summary': 'A policy conflict needs one answer', 'source_conflicts': [{
            'source_id': fact['id'], 'item_ids': ['C1'], 'proposed': 'Existing sessions rejected.',
            'refs': [source['id'] + '#P1']}]}
    result = await execute(store, Engine(answer), chat, 'project.update_from_sources', {
        'artifact_id': 'cases', 'selected_ids': ['C1'], 'source_ids': [source['id']]})
    assert result['status'] == 'needs_input'
    assert 'session policy' in result['message']
    assert result['conflicts'][0]['source_id'] == fact['id']
    assert store.get('artifact', 'cases') == cases
    assert store.list('action_proposal', chat_id=chat['id']) == []
