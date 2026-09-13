"""Selected UI targets and later stage holds survive semantic routing omissions."""
import copy

import pytest

from test_backend_api import setup_chat, until
from test_manual_journey_v271 import live_journey


def submit(client, chat, number, **body):
    response = client.post('/api/chats/' + chat['id'] + '/turns', json={
        'client_message_id': 'selection-contract-' + str(number), 'content': '处理当前任务', **body})
    assert response.status_code == 200, response.text
    return response.json()


def generated(runtime, stop_after='complete'):
    client, app, model = runtime
    _, chat, _ = setup_chat(client)
    result = submit(client, chat, 1, mode='auto', command={'name': 'workflow.start',
        'arguments': {'content': '生成登录测试', 'intent': 'generate_case', 'stop_after': stop_after}})
    run = until(client, result['actions'][0]['result']['run'])
    assert run['status'] == ('completed' if stop_after == 'complete' else 'waiting'), run
    artifacts = {a['type']: a for a in app.state.store.list('artifact', chat_id=chat['id'])
                 if a['type'] in ('analysis', 'scenarios', 'cases')}
    assert set(artifacts) == {'analysis', 'scenarios', 'cases'}
    return chat, run, artifacts


@pytest.mark.parametrize('selected', [True, False])
def test_selected_ui_artifact_outranks_prior_focus_when_planner_omits_target(live_journey, selected):
    client, app, model = live_journey
    chat, _, artifacts = generated(live_journey)
    analysis, scenarios, cases = (artifacts[k] for k in ('analysis', 'scenarios', 'cases'))
    read = submit(client, chat, 2, command={'name': 'artifact.read', 'arguments': {'artifact_id': analysis['id']}})
    assert read['status'] == 'succeeded', read
    assert app.state.store.get('conversation_state', 'conversation:' + chat['id'])['focus']['artifact_id'] == analysis['id']
    before = {a['id']: copy.deepcopy(a) for a in artifacts.values()}
    model.turn_decision = {'actions': [{'name': 'artifact.preview',
        'arguments': {'sync_related': True, 'related_artifact_ids': [cases['id']]}}]}
    result = submit(client, chat, 3, content='先预览选中场景的微调及关联用例变化',
        artifact_id=scenarios['id'], artifact_revision=scenarios['revision'],
        **({'selected_ids': [scenarios['items'][0]['id']]} if selected else {}))
    assert result['status'] == 'needs_confirmation', result
    assert 'artifact_id' not in result['actions'][0]['arguments'], 'Planner must leave target resolution to the server'
    changes = next(part for part in result['parts'] if part['type'] == 'diff')['changes']
    expected = {scenarios['id'], cases['id']} if selected else set(before)
    assert {change['artifact_id'] for change in changes} == expected
    if selected:
        for change in changes:
            original = before[change['artifact_id']]
            assert change['items'][1] == original['items'][1], 'An unrelated row must remain untouched'
    assert {aid: app.state.store.get('artifact', aid) for aid in before} == before


def test_later_case_stop_cancels_the_unapproved_continue_tail_of_a_preview(live_journey):
    client, app, model = live_journey
    chat, run, artifacts = generated(live_journey, stop_after='cases')
    assert run['interrupt']['type'] == 'case_draft_review'
    cases = artifacts['cases']
    model.turn_decision = {'actions': [
        {'name': 'artifact.preview', 'arguments': {'artifact_id': cases['id'], 'selected_ids': [cases['items'][0]['id']]}},
        {'name': 'workflow.continue', 'arguments': {'run_id': run['id'], 'stop_after': 'review', 'approved': True}},
    ]}
    preview = submit(client, chat, 2, content='先预览用例改动；应用后继续评审')
    assert preview['status'] == 'needs_confirmation', preview
    proposal = next(part['proposal_id'] for part in preview['parts'] if part['type'] == 'diff')
    held = submit(client, chat, 3, content='仍然停在用例草稿，不要继续评审', command={
        'name': 'workflow.update_scope', 'arguments': {'run_id': run['id'], 'stop_after': 'cases'}})
    assert held['status'] == 'succeeded', held
    applied = submit(client, chat, 4, content='应用预览，只保存改动', command={
        'name': 'artifact.apply', 'arguments': {'proposal_id': proposal}})
    # The composite turn includes its cancelled historical tail; the atomic
    # apply receipt and saved artifact still prove that the requested edit won.
    assert applied['status'] == 'cancelled', applied
    apply_command = app.state.store.get('conversation_command', applied['actions'][0]['id'])
    assert apply_command['status'] == 'succeeded'
    assert apply_command['_effect_result']['status'] == 'succeeded'
    assert app.state.store.get('artifact', cases['id'])['items'][0] != cases['items'][0]
    parent = app.state.store.get('conversation_turn', preview['id'])
    tail = app.state.store.get('conversation_command', parent['actions'][1]['id'])
    assert tail['status'] == 'cancelled', tail
    current = client.get('/api/runs/' + run['id']).json()
    assert current['status'] == 'waiting' and current['interrupt']['type'] == 'case_draft_review', current
    assert current['stop_after'] == 'cases'
    assert not any(task == 'review_cases' for task, _ in model.calls)
