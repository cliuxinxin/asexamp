"""The four requested interactions over HTTP, with only model inference controlled."""
import io

from openpyxl import load_workbook

from test_native_journey_v300 import native_journey


def confirm(j, prompt, tool, *, status='succeeded'):
    j.sequence += 1
    j.gateway.next_call = (tool, {})
    response = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json={
        'client_message_id': 'confirm-' + str(j.sequence), 'content': '同意',
        'mode': 'hitp', 'reply_kind': 'confirm', 'reply_to': prompt['id']})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == status, result
    assert j.gateway.next_call is None
    assert tool in j.gateway.bound_tools[-1]
    return result


def test_preview_independent_scenario_review_then_case_columns_and_profile_export(native_journey):
    j = native_journey
    j.turn('根据需求生成用例，逐步确认', 'start_pipeline_tool')
    _, analysis, prompt = j.gate('strategy_review')
    analysis_before = j.app.state.store.get('artifact', analysis['id'])
    confirm(j, prompt, 'resume_pipeline_tool')
    _, scenarios, prompt = j.gate('scenario_review')
    preview = j.turn('将 SC-1 标为独立场景，需求显示 N/A，标题改为独立登录验证',
        'modify_artifact_tool', {'artifact_id': scenarios['id'], 'item_id': 'SC-1',
        'new_values': {'title': '独立登录验证'}, 'independent': True}, status='needs_confirmation')
    assert j.artifact(scenarios['id'])['revision'] == 1
    workspace = j.client.get('/api/chats/' + j.chat['id'] + '/workspace-state').json()
    assert workspace['pending_proposal']['artifact_id'] == scenarios['id']
    confirm(j, preview['pending'][0], 'apply_artifact_preview_tool')
    revised = j.artifact(scenarios['id'])
    assert revised['items'][0]['requirement_ids'] == []
    assert j.app.state.store.get('artifact', analysis['id']) == analysis_before
    _, _, prompt = j.gate('scenario_review')
    confirm(j, prompt, 'resume_pipeline_tool')
    run, draft, prompt = j.gate('case_result_review')
    assert draft['revision'] == 1 and draft['items'][0]['title'] == '独立登录验证'
    proposal = j.client.get('/api/runs/' + run['id'] + '/review-proposals/' + prompt['proposal_id'])
    assert proposal.status_code == 200, proposal.text
    assert '已评审' in proposal.text
    assert j.artifact(draft['id'])['revision'] == 1
    confirm(j, prompt, 'resume_pipeline_tool')
    j.completed()
    reviewed = j.artifact(draft['id'])
    assert reviewed['revision'] == 2 and reviewed['items'][0]['title'].startswith('已评审')
    profile = j.app.state.store.list('profile', project_id=j.project['id'])[0]
    preview = j.turn('给用例加执行人列，然后导出 Excel', 'modify_case_columns_tool', {
        'artifact_id': draft['id'], 'upsert_columns': [{'field': 'tester', 'header': '执行人',
        'value_source': 'manual'}], 'export_after_approval': True}, status='needs_confirmation')
    result = confirm(j, preview['pending'][0], 'apply_artifact_preview_tool', status='needs_confirmation')
    assert j.artifact(draft['id'])['revision'] == 3
    assert j.app.state.store.get('profile', profile['id']) == profile
    prompt = result['pending'][0]
    assert prompt['kind'] == 'profile'
    applied = j.client.post('/api/chats/' + j.chat['id'] + '/profile-change/apply', json={
        'prompt_id': prompt['id'], 'expected_version': profile['version'], 'selected_keys': ['excel_columns']})
    assert applied.status_code == 200, applied.text
    files = applied.json()['parts'][0]['files']
    assert len(files) == 1
    download = j.client.get(files[0]['url'])
    assert download.status_code == 200
    sheet = load_workbook(io.BytesIO(download.content)).active
    headers = next(sheet.values)
    assert '执行人' in headers
    assert j.artifact(draft['id'])['revision'] == 3, 'Background review must not apply before user confirmation.'
    messages = j.snapshot()['messages']
    parts = [p for message in messages for p in message.get('metadata', {}).get('turn_response', {}).get('parts', [])]
    assert any(p.get('type') == 'files' and p['files'] == files for p in parts)
