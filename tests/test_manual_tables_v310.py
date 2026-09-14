"""Manual table edits preserve upstream artifacts and propose export-template changes."""
import copy

from test_native_journey_v300 import native_journey


def scenarios_ready(j):
    j.turn('根据需求生成测试场景', 'start_pipeline_tool', {'intent': 'generate_scenario', 'stop_after': 'scenarios'}, mode='auto')
    j.completed()
    store = j.app.state.store
    artifacts = store.list('artifact', chat_id=j.chat['id'])
    return next(a for a in artifacts if a['type'] == 'analysis'), next(a for a in artifacts if a['type'] == 'scenarios')


def test_manual_new_scenario_has_authored_evidence_and_no_upstream_mutation(native_journey):
    j = native_journey
    analysis, scenarios = scenarios_ready(j)
    rows = copy.deepcopy(scenarios['items'])
    rows.append({'id': 'SC-MANUAL', 'title': '人工补充并发登录', 'description': '两个设备同时登录，核对访问结果。',
                 'priority': 'P2', 'requirement_ids': [], 'refs': []})
    response = j.client.put('/api/artifacts/' + scenarios['id'], json={
        'expected_revision': scenarios['revision'], 'items': rows})
    assert response.status_code == 200, response.text
    result = response.json()
    row = next(r for r in result['items'] if r['id'] == 'SC-MANUAL')
    assert row['requirement_ids'] == [] and row['refs']
    assert row.get('_independent_origin')
    assert j.app.state.store.get('artifact', analysis['id']) == analysis
    assert len(j.app.state.store.list('artifact', chat_id=j.chat['id'])) == 2
    assert '人工补充并发登录' in str(j.app.state.store.evidence(j.app.state.store.get('artifact', result['id'])['_source_ids']))
    sources = j.app.state.store.list('source', chat_id=j.chat['id'])
    stale = j.client.put('/api/artifacts/' + scenarios['id'], json={
        'expected_revision': scenarios['revision'], 'items': rows})
    assert stale.status_code == 409
    assert j.app.state.store.list('source', chat_id=j.chat['id']) == sources


def test_manual_nonempty_invalid_parent_is_not_converted_to_na(native_journey):
    j = native_journey
    analysis, scenarios = scenarios_ready(j)
    rows = copy.deepcopy(scenarios['items'])
    rows[0]['requirement_ids'] = ['REQ-NOT-IN-THIS-PROJECT']
    response = j.client.put('/api/artifacts/' + scenarios['id'], json={
        'expected_revision': scenarios['revision'], 'items': rows})
    assert response.status_code == 400, response.text
    assert j.app.state.store.get('artifact', scenarios['id']) == scenarios
    assert j.app.state.store.get('artifact', analysis['id']) == analysis


def test_manual_case_column_change_prepares_profile_without_applying_it(native_journey):
    j = native_journey
    j.turn('生成测试用例并评审', 'start_pipeline_tool', {'mode': 'auto'}, mode='auto')
    j.completed()
    store = j.app.state.store
    cases = next(a for a in store.list('artifact', chat_id=j.chat['id']) if a['type'] == 'cases')
    profile = store.list('profile', project_id=j.project['id'])[0]
    rows = copy.deepcopy(cases['items'])
    rows[0]['test_data'] = '已注册账号'
    response = j.client.put('/api/artifacts/' + cases['id'], json={
        'expected_revision': cases['revision'], 'items': rows,
        'column_changes': {'added': [{'field': 'test_data', 'header': '测试数据'}], 'removed': []}})
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['test_data'] == '已注册账号'
    columns = response.json()['report']['table_columns']
    assert next(c for c in columns if c['field'] == 'test_data')['header'] == '测试数据'
    assert store.get('profile', profile['id']) == profile
    prompt = j.snapshot()['conversation_prompt']
    assert prompt['kind'] == 'profile'
    preview = j.client.get('/api/chats/' + j.chat['id'] + '/profile-change', params={'prompt_id': prompt['id']})
    assert preview.status_code == 200
    assert '测试数据' in preview.text
