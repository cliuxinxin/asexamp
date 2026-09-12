"""Unified branch drift and safe reconciliation against real saved revisions."""
import copy

import pytest

from test_conversation_artifacts_v260 import Engine, data, wait_run
from tcg.artifact_actions import apply_action, preview_action
from tcg.schemas import DomainError
from tcg.workspace_coverage import workspace_context


@pytest.fixture
def workspace(tmp_path):
    fixture = data(tmp_path)
    yield next(fixture)
    next(fixture, None)


def change_row(store, artifact, item_id, **fields):
    return store.revise_artifact(artifact['id'], artifact['revision'],
        [{**row, **fields} if row['id'] == item_id else row for row in artifact['items']])


def test_report_only_revision_is_current_everywhere(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, analysis, _, _, _, _ = workspace
    store.revise_artifact('analysis', 1, analysis['items'], report={'summary': 'Clearer wording'})
    state = workspace_state(store, chat, 'analysis')
    assert state['impact']['status'] == 'current'
    assert all(stage['status'] == 'current' for stage in state['stages'][:3])
    assert workspace_context(store, store.get('artifact', 'analysis'))['stale']['analysis'] is False


@pytest.mark.asyncio
async def test_scenario_edit_previews_saved_change_without_replaying_instruction(workspace):
    from tcg.workspace_changes import workspace_state, prepare_reconciliation
    store, chat, _, scenarios, cases, _, _ = workspace
    run = wait_run(store, chat)
    changed = change_row(store, scenarios, 'S1', description='Lock for twenty minutes')
    state = workspace_state(store, chat, 'scenarios')
    assert state['next_action']['kind'] == 'reconcile'
    assert state['impact']['affected'][0]['item_ids'] == ['C1']
    calls = []
    def answer(task, context):
        calls.append(task)
        assert task == 'artifact_sync'
        assert context['scenarios'][0]['description'] == 'Lock for twenty minutes'
        return {'operations': [{'op': 'update', 'id': 'C1', 'item': {
            'steps': [{'action': 'Log in', 'expected': 'Lock for twenty minutes'}],
            'actual_result': 'AI must not replace this'}}], 'summary': '同步锁定用例'}
    result = await prepare_reconciliation(store, Engine(answer), chat,
        {**state['next_action']['arguments'], 'instruction': '把时间再翻倍'})
    assert result['status'] == 'needs_confirmation'
    assert store.get('artifact', 'scenarios') == changed
    assert store.get('artifact', 'cases') == cases
    proposal = result['parts'][0]
    applied = apply_action(store, proposal['artifact_id'], proposal['proposal_id'])
    assert calls == ['artifact_sync']
    saved = store.get('artifact', 'cases')
    assert saved['items'][0]['actual_result'] == 'Human result'
    assert saved['items'][1] == cases['items'][1]
    assert store.revision('cases', 1)['items'] == cases['items']
    assert store.run(run['id'])['status'] == 'waiting'
    assert workspace_state(store, chat, 'scenarios')['impact']['status'] == 'current'
    assert applied['artifacts'][0]['revision'] == 2


def test_real_upstream_change_blocks_gate_but_report_wording_does_not(workspace):
    from tcg.workspace_changes import assert_current_inputs
    store, chat, analysis, _, _, _, _ = workspace
    run = wait_run(store, chat)
    store.revise_artifact('analysis', 1, analysis['items'], report={'summary': 'Clarified wording'})
    assert_current_inputs(store, store.run(run['id']))
    change_row(store, store.get('artifact', 'analysis'), 'R1', description='Twenty minutes')
    with pytest.raises(DomainError, match='同步') as exc:
        assert_current_inputs(store, store.run(run['id']))
    assert exc.value.status == 409


@pytest.mark.asyncio
async def test_analysis_and_existing_case_drift_are_both_reconciled(workspace):
    from tcg.workspace_changes import workspace_state, prepare_reconciliation
    store, chat, analysis, scenarios, cases, _, _ = workspace
    change_row(store, analysis, 'R1', description='Requirement changed')
    change_row(store, scenarios, 'S2', description='Independent prior scene edit')
    def answer(task, context):
        if task == 'artifact_sync_scenarios':
            return {'operations': [{'op': 'update', 'id': 'S1', 'item': {'description': 'Requirement changed'}}], 'summary': '同步场景'}
        assert task == 'artifact_sync'
        assert {row['id'] for row in context['scenarios']} == {'S1', 'S2'}
        return {'operations': [{'op': 'update', 'id': 'C' + scene['id'][1:], 'item': {
            'steps': [{'action': 'Log in', 'expected': scene['description']}]}}
            for scene in context['scenarios']], 'summary': '同步用例'}
    result = await prepare_reconciliation(store, Engine(answer), chat, {'artifact_id': 'analysis'})
    part = result['parts'][0]
    apply_action(store, part['artifact_id'], part['proposal_id'])
    assert workspace_state(store, chat, 'analysis')['impact']['status'] == 'current'


def test_state_uses_exact_case_branch_and_keeps_other_generations_out(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, _, scenarios, cases, _, artifact = workspace
    artifact('other-scenes', 'scenarios', scenarios['items'])
    artifact('other-cases', 'cases', cases['items'], {'lineage': {'scenario_artifact_id': 'other-scenes', 'scenario_revision': 1}})
    state = workspace_state(store, chat, 'cases')
    assert [s['artifact_id'] for s in state['stages'][:3]] == ['analysis', 'scenarios', 'cases']
    assert state['impact']['affected'] == []


def test_partial_requirement_sync_matches_workspace_staleness(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, analysis, scenarios, _, _, _ = workspace
    changed = change_row(store, analysis, 'R1', description='Rule 1 changed')
    report = copy.deepcopy(scenarios['report'])
    report['lineage']['analysis_revisions'] = {'R1': changed['revision']}
    store.revise_artifact('scenarios', 1, scenarios['items'], report=report)
    state = workspace_state(store, chat, 'analysis')
    assert state['impact']['status'] == 'current'
    assert workspace_context(store, changed)['stale']['analysis'] is False


@pytest.mark.asyncio
async def test_addition_only_analysis_draft_preserves_existing_rows(workspace):
    store, chat, analysis, _, _, _, _ = workspace
    source = store.add_source(chat['id'], 'new rule', 'change', 'Support passkeys', [{'text': 'Support passkeys', 'location': 'P1'}])
    def answer(task, context):
        assert task == 'artifact_modify'
        assert context['selected_ids'] == []
        assert context['existing_requirement_ids'] == ['R1', 'R2']
        return {'operations': [{'op': 'add', 'item': {'id': 'R3', 'title': 'Passkeys',
            'description': 'Support passkeys', 'refs': [source['id'] + '#P1']}}], 'summary': '新增需求'}
    proposal = await preview_action(store, Engine(answer), 'analysis', {
        'action': 'modify', 'instruction': 'Add passkeys', 'selected_ids': [], 'allow_additions': True,
        'source_ids': [source['id']]})
    apply_action(store, 'analysis', proposal['id'])
    assert store.get('artifact', 'analysis')['items'][:2] == analysis['items']
    assert store.get('artifact', 'analysis')['items'][2]['id'] == 'R3'


@pytest.mark.asyncio
async def test_source_adoption_preview_is_durable_and_only_apply_consumes_source(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, analysis, _, _, _, _ = workspace
    source = store.add_source(chat['id'], 'no business change', 'change', 'Rule unchanged', [{'text': 'Rule unchanged', 'location': 'P1'}])
    before = copy.deepcopy(analysis)
    def no_model(*_):
        raise AssertionError('Already inspected sources must not trigger another model edit')
    proposal = await preview_action(store, Engine(no_model), 'analysis', {
        'action': 'modify', 'instruction': '纳入资料', 'source_ids': [source['id']],
        'source_adoption_only': True, 'source_review': {'summary': '无需修改需求'}})
    assert store.get('artifact', 'analysis') == before
    state = workspace_state(store, chat, 'analysis')
    assert state['pending_proposal']['id'] == proposal['id']
    assert state['next_action']['kind'] == 'apply'
    assert state['impact']['source_ids'] == [source['id']]
    apply_action(store, 'analysis', proposal['id'])
    state = workspace_state(store, chat, 'analysis')
    assert state['pending_proposal'] is None
    assert state['impact']['status'] == 'current'
    assert store.get('artifact', 'analysis')['items'] == before['items']


def test_gate_checks_only_its_input_ancestors_not_old_downstream_cases(workspace):
    from tcg.workspace_changes import assert_current_inputs
    store, chat, _, scenarios, _, _, _ = workspace
    run = wait_run(store, chat)
    change_row(store, scenarios, 'S1', description='New scene')
    assert_current_inputs(store, store.run(run['id']))


@pytest.mark.asyncio
async def test_cancelled_reconciliation_cannot_publish_late_proposal(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, _, scenarios, cases, _, _ = workspace
    change_row(store, scenarios, 'S1', description='New scene')
    command = {'id': 'reconcile-command', 'chat_id': chat['id'], 'project_id': chat['project_id'],
               'status': 'running', 'name': 'workspace.reconcile'}
    store.put('conversation_command', command)
    def answer(task, context):
        store.put('conversation_command', {**command, 'status': 'cancelled'})
        return {'operations': [], 'summary': 'Nothing else'}
    with pytest.raises(DomainError, match='取消'):
        await prepare_reconciliation(store, Engine(answer), chat, {'artifact_id': 'scenarios'}, command['id'])
    assert store.list('action_proposal', chat_id=chat['id']) == []
    assert store.get('artifact', 'cases') == cases


@pytest.mark.asyncio
async def test_source_preview_returns_shared_diff_and_adopts_only_on_apply(workspace):
    from tcg.workspace_changes import prepare_reconciliation, workspace_state
    store, chat, analysis, _, _, _, _ = workspace
    source = store.add_source(chat['id'], 'style clarification', 'change', 'Same rules', [{'text': 'Same rules', 'location': 'P1'}])
    def answer(task, context):
        assert task == 'project_source_impact'
        return {'requirement_ids': [], 'summary': 'Business requirements unchanged', 'new_requirements': [],
                'global_impact': False, 'uncertain': False, 'refs': [source['id'] + '#P1']}
    result = await prepare_reconciliation(store, Engine(answer), chat, {'artifact_id': 'analysis'})
    assert result['status'] == 'needs_confirmation'
    part = next(part for part in result['parts'] if part['type'] == 'diff')
    assert store.get('artifact', 'analysis') == analysis
    apply_action(store, part['artifact_id'], part['proposal_id'])
    assert workspace_state(store, chat, 'analysis')['impact']['source_ids'] == []


def test_unadopted_business_source_blocks_result_gate_but_not_clarification(workspace):
    from tcg.workspace_changes import assert_current_inputs
    store, chat, _, _, _, _, _ = workspace
    run = wait_run(store, chat)
    store.add_source(chat['id'], 'new behavior', 'change', 'Passkeys', [{'text': 'Passkeys', 'location': 'P1'}])
    with pytest.raises(DomainError, match='资料'):
        assert_current_inputs(store, store.run(run['id']))
    run = store.run(run['id'])
    run['interrupt'] = {'type': 'clarification', 'artifact_id': 'analysis'}
    store.save_run(run)
    assert_current_inputs(store, run)


@pytest.mark.asyncio
async def test_source_reconciliation_from_case_focus_uses_current_analysis_revision(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, analysis, _, _, _, _ = workspace
    store.revise_artifact('analysis', 1, analysis['items'], report={'summary': 'Current wording'})
    source = store.add_source(chat['id'], 'same behavior', 'change', 'Unchanged', [{'text': 'Unchanged', 'location': 'P1'}])
    def answer(task, context):
        assert task == 'project_source_impact'
        assert context['analysis'] == {'id': 'analysis', 'revision': 2}
        return {'requirement_ids': [], 'summary': 'Unchanged', 'refs': [source['id'] + '#P1'],
                'new_requirements': [], 'global_impact': False, 'uncertain': False}
    result = await prepare_reconciliation(store, Engine(answer), chat,
        {'artifact_id': 'cases', 'expected_revision': 1})
    part = next(part for part in result['parts'] if part['type'] == 'diff')
    assert part['artifact_id'] == 'analysis'


@pytest.mark.asyncio
async def test_requirement_rule_patches_accept_only_supplied_business_evidence(workspace):
    store, chat, analysis, _, _, ref, _ = workspace
    def answer(task, context):
        return {'operations': [], 'report_patch': {'global_rules': [
            {'text': 'Lock for ten minutes', 'requirement_ids': ['R1'], 'refs': [ref]}]}, 'summary': '同步业务规则'}
    proposal = await preview_action(store, Engine(answer), 'analysis', {'action': 'modify', 'instruction': '更新规则说明'})
    assert proposal['changes'][0]['report']['global_rules'][0]['refs'] == [ref]
    def bad_answer(task, context):
        return {'operations': [], 'report_patch': {'global_rules': [
            {'text': 'Invented', 'refs': ['missing-source#P1']}]}, 'summary': 'Invalid'}
    with pytest.raises(DomainError, match='依据'):
        await preview_action(store, Engine(bad_answer), 'analysis', {'action': 'modify', 'instruction': '更新规则说明'})
    assert store.get('artifact', 'analysis') == analysis


def test_unmapped_added_requirement_is_pending_even_with_zero_existing_scenarios(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, analysis, _, _, ref, _ = workspace
    store.revise_artifact('analysis', 1, analysis['items'] + [
        {'id': 'R3', 'title': 'New requirement', 'description': 'New rule', 'refs': [ref]}])
    state = workspace_state(store, chat, 'analysis')
    effect = state['impact']['affected'][0]
    assert effect['item_ids'] == []
    assert effect['count'] == 0 and effect['upstream_count'] == 1
    assert '新增场景' in state['impact']['summary']
    assert '场景0条' not in state['impact']['summary']


def test_review_status_uses_case_design_at_review_revision(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, _, _, cases, _, _ = workspace
    report = {**cases['report'], 'review_reports': [{'summary': 'Reviewed saved case design'}]}
    reviewed = store.revise_artifact('cases', 1, cases['items'], report=report)
    assert workspace_state(store, chat, 'cases')['stages'][3]['status'] == 'current'
    executed = change_row(store, reviewed, 'C1', actual_result='Human passed', manual_note='Human note')
    assert workspace_state(store, chat, 'cases')['stages'][3]['status'] == 'current'
    change_row(store, executed, 'C1', title='Changed business design')
    assert workspace_state(store, chat, 'cases')['stages'][3]['status'] == 'stale'


def test_source_review_keeps_source_role_confirmation_as_next_step(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, _, _, _, _, _ = workspace
    run = wait_run(store, chat)
    run['interrupt'] = {'type': 'source_review', 'sources': []}
    store.save_run(run)
    state = workspace_state(store, chat, 'analysis')
    assert state['next_action']['kind'] == 'none'
    assert '资料' in state['next_action']['label']


@pytest.mark.asyncio
async def test_explicit_case_branch_never_switches_to_newer_sibling(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, _, scenarios, cases, _, artifact = workspace
    sibling = artifact('later-cases', 'cases', cases['items'], cases['report'])
    change_row(store, scenarios, 'S1', description='Scene changed')
    def answer(task, context):
        assert task == 'artifact_sync'
        assert context['artifact']['id'] == 'cases'
        return {'operations': [{'op': 'update', 'id': 'C1', 'item': {'title': 'Synced chosen case'}}], 'summary': '同步指定用例'}
    result = await prepare_reconciliation(store, Engine(answer), chat,
        {'artifact_id': 'scenarios', 'related_artifact_ids': ['cases']})
    part = result['parts'][0]
    assert [change['artifact_id'] for change in part['changes']] == ['cases']
    apply_action(store, part['artifact_id'], part['proposal_id'])
    assert store.get('artifact', 'later-cases') == sibling


@pytest.mark.asyncio
async def test_explicit_scenario_only_scope_does_not_include_cases(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, analysis, _, cases, _, _ = workspace
    change_row(store, analysis, 'R1', description='New requirement')
    def answer(task, context):
        assert task == 'artifact_sync_scenarios'
        return {'operations': [{'op': 'update', 'id': 'S1', 'item': {'description': 'New requirement'}}], 'summary': '同步场景'}
    result = await prepare_reconciliation(store, Engine(answer), chat,
        {'artifact_id': 'analysis', 'related_artifact_ids': ['scenarios']})
    part = result['parts'][0]
    assert [change['artifact_id'] for change in part['changes']] == ['scenarios']
    apply_action(store, part['artifact_id'], part['proposal_id'])
    assert store.get('artifact', 'cases') == cases


@pytest.mark.asyncio
async def test_row_scoped_reconciliation_is_rejected_before_model(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, _, scenarios, _, _, _ = workspace
    change_row(store, scenarios, 'S1', description='Changed')
    def no_model(*_):
        raise AssertionError('A scoped request must not silently become branch-wide')
    with pytest.raises(DomainError, match='所选'):
        await prepare_reconciliation(store, Engine(no_model), chat,
            {'artifact_id': 'scenarios', 'selected_ids': ['S1']})


def test_semantic_global_rule_change_stales_scenarios_without_row_change(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, analysis, _, _, ref, _ = workspace
    store.revise_artifact('analysis', 1, analysis['items'], report={
        'global_rules': [{'text': 'New authentication requirement', 'refs': [ref]}]})
    state = workspace_state(store, chat, 'analysis')
    assert state['stages'][1]['status'] == 'stale'
    assert state['impact']['affected'][0]['upstream_item_ids'] == ['R1', 'R2']
    assert workspace_context(store, store.get('artifact', 'analysis'))['stale']['analysis']


def test_completed_unreviewed_cases_offer_review_action(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, _, _, _, _, _ = workspace
    state = workspace_state(store, chat, 'cases')
    assert state['next_action']['kind'] == 'generate'
    assert state['next_action']['arguments']['intent'] == 'review_case'
    assert state['next_action']['arguments']['artifact_id'] == 'cases'


@pytest.mark.asyncio
async def test_multi_branch_preview_remains_visible_for_explicit_application(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, _, scenarios, cases, _, artifact = workspace
    artifact('later-cases', 'cases', cases['items'], cases['report'])
    change_row(store, scenarios, 'S1', description='New scene')
    proposal = await preview_action(store, Engine(lambda *_: {'operations': [], 'summary': 'Inspected'}),
        'scenarios', {'action': 'sync', 'instruction': 'Sync both saved sets',
                      'related_artifact_ids': ['cases', 'later-cases']})
    state = workspace_state(store, chat, 'cases')
    assert state['pending_proposal']['id'] == proposal['id']
    assert len(state['pending_proposal']['changes']) == 2


@pytest.mark.asyncio
async def test_explicit_new_source_does_not_reuse_unrelated_pending_preview(workspace):
    from tcg.workspace_changes import prepare_reconciliation
    store, chat, _, scenarios, _, _, _ = workspace
    change_row(store, scenarios, 'S1', description='New scene')
    old = await prepare_reconciliation(store, Engine(lambda *_: {'operations': [], 'summary': 'Inspected'}),
        chat, {'artifact_id': 'scenarios'})
    source = store.add_source(chat['id'], 'additional material', 'change', 'New source', [{'text': 'New source', 'location': 'P1'}])
    def answer(task, context):
        if task == 'artifact_sync':
            return {'operations': [], 'summary': 'Existing scene drift checked'}
        assert task == 'project_source_impact'
        return {'requirement_ids': [], 'new_requirements': [], 'summary': 'New source checked',
                'refs': [source['id'] + '#P1'], 'global_impact': False, 'uncertain': False}
    new = await prepare_reconciliation(store, Engine(answer), chat,
        {'artifact_id': 'analysis', 'source_ids': [source['id']]})
    assert new['parts'][0]['proposal_id'] != old['parts'][0]['proposal_id']
    proposal = store.get('action_proposal', new['parts'][0]['proposal_id'])
    assert source['id'] in proposal['_source_ids']


@pytest.mark.parametrize('mode', ['auto', 'hitp'])
def test_explicit_run_source_exclusion_survives_generation_but_later_upload_is_pending(workspace, mode):
    from tcg.workspace_changes import assert_current_inputs, workspace_state
    store, chat, old_analysis, old_scenarios, _, _, _ = workspace
    selected = old_analysis['_source_ids'][0]
    ignored = store.add_source(chat['id'], 'explicitly excluded B', 'primary', 'Unrelated B', [{'text': 'Unrelated B', 'location': 'P1'}])
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': mode,
        'content': 'Only use A; exclude B', 'source_ids': [selected], 'as_requirement': False})
    analysis = store.artifact(run['id'], 'v7:analysis:artifact', 'analysis', 'Selected A understanding', old_analysis['items'])
    store.put('artifact', {**analysis, '_visible': True})
    scenes = store.artifact(run['id'], 'v4:scenarios_artifact', 'scenarios', 'Selected A scenarios', old_scenarios['items'])
    store.put('artifact', {**scenes, '_visible': True})
    interrupt = {'type': 'scenario_review', 'artifact_id': scenes['id']} if mode == 'hitp' else {
        'type': 'workflow_paused', 'node': 'cases', 'reason': 'write'}
    run = store.update_run(run['id'], status='waiting', interrupt=interrupt)
    assert workspace_state(store, chat, scenes['id'])['impact']['source_ids'] == []
    assert_current_inputs(store, run)
    added = store.add_source(chat['id'], 'later C', 'change', 'New C', [{'text': 'New C', 'location': 'P1'}])
    assert workspace_state(store, chat, scenes['id'])['impact']['source_ids'] == [added['id']]
    with pytest.raises(DomainError, match='资料'):
        assert_current_inputs(store, run)
    assert ignored['id'] not in workspace_state(store, chat, scenes['id'])['impact']['source_ids']
    store.update_run(run['id'], _source_ids=[selected, ignored['id']])
    assert set(workspace_state(store, chat, scenes['id'])['impact']['source_ids']) == {ignored['id'], added['id']}


def test_upload_during_understanding_is_pending_against_run_start_not_artifact_creation(workspace):
    from tcg.workspace_changes import workspace_state
    store, chat, old_analysis, _, _, _, _ = workspace
    selected = old_analysis['_source_ids'][0]
    store.add_source(chat['id'], 'excluded existing B', 'primary', 'B', [{'text': 'B', 'location': 'P1'}])
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'auto',
        'content': 'Only A', 'source_ids': [selected], 'as_requirement': False})
    concurrent = store.add_source(chat['id'], 'uploaded while understanding', 'change', 'C', [{'text': 'C', 'location': 'P1'}])
    analysis = store.artifact(run['id'], 'v7:analysis:artifact', 'analysis', 'A understanding', old_analysis['items'])
    store.put('artifact', {**analysis, '_visible': True})
    assert concurrent['created_at'] < analysis['created_at']
    assert workspace_state(store, chat, analysis['id'])['impact']['source_ids'] == [concurrent['id']]
