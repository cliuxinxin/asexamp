"""Field sync uses saved keys and explicit Profile approval, never modifies cases."""
import copy
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tcg.main import create_app
from tcg.storage import dump, now


@pytest.fixture
def drift(tmp_path):
    app = create_app(tmp_path, model_gateway=SimpleNamespace())
    with TestClient(app, base_url='http://localhost') as client:
        store = app.state.store
        project = store.list('project')[0]
        profile = store.list('profile')[0]
        chat = store.create_chat(project['id'], '字段同步')
        store.put('chat', {**chat, 'profile_id': profile['id']})
        artifact = {'id': 'cases', 'chat_id': chat['id'], 'project_id': project['id'],
            'title': '用例', 'type': 'cases', 'revision': 1, '_visible': True,
            'created_at': now(), 'report': {}, '_profile': {**profile['config'], 'excel_columns': [
                *profile['config']['excel_columns'], {'field': 'test_data', 'header': '测试数据',
                    'definition': '用于测试的数据', 'value_source': 'ai', 'required': True}]},
            'items': [{'id': 'C1', 'title': '登录', 'type': 'Business', 'priority': 'P1',
                'scenario_id': 'S1', 'preconditions': '', 'refs': ['source#P1'],
                'steps': [{'action': '输入', 'expected': '成功'}], 'test_data': 'account=alice',
                'actual_result': '', '_template_field_notes': {}, 'revision': 4},
                {'id': 'C2', 'title': '边界', 'type': 'Boundary', 'priority': 'P2',
                'scenario_id': 'S1', 'preconditions': '', 'refs': ['source#P1'],
                'steps': [{'action': '输入', 'expected': '提示'}], 'attempts': 0, 'enabled': False}]}
        store.put('artifact', artifact)
        store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
            ('cases', 1, dump(artifact), now(), 'fixture', '{}'))
        yield SimpleNamespace(client=client, store=store, chat=chat, profile=profile, artifact=artifact,
                              url='/api/chats/' + chat['id'] + '/field-drift')


def proposal_body(detection, fields):
    return {key: detection[key] for key in ('artifact_id', 'revision', 'head_revision', 'profile_id', 'profile_version')} | {'fields': fields}


def test_detect_actual_custom_keys_scope_labels_and_values(drift):
    d = drift
    response = d.client.get(d.url)
    assert response.status_code == 200, response.text
    detected = response.json()
    candidates = {row['field']: row for row in detected['candidates']}
    assert set(candidates) == {'test_data', 'actual_result', 'attempts', 'enabled'}
    assert candidates['test_data']['header'] == '测试数据'
    assert candidates['test_data']['definition'] == '用于测试的数据'
    assert candidates['test_data']['item_ids'] == ['C1']
    assert candidates['actual_result']['value_source'] == 'manual'
    assert candidates['actual_result']['required'] is False
    assert candidates['attempts']['item_ids'] == ['C2']
    assert detected['head_revision'] == detected['revision'] == 1
    assert d.store.get('profile', d.profile['id']) == d.profile
    assert d.store.get('artifact', 'cases') == d.artifact
    options = d.client.get('/api/artifacts/cases/export-options').json()
    current = next(p for p in options['profiles'] if p['id'] == d.profile['id'])
    assert current['field_drift'] == detected['candidates']
    assert 'test_data' not in {c['field'] for c in options['snapshot_drift']}


def test_shortcut_stages_real_diff_approval_exports_new_values_without_model(drift):
    d = drift
    detected = d.client.get(d.url).json()
    staged = d.client.post(d.url + '/propose', json=proposal_body(detected, ['test_data', 'attempts', 'enabled'])).json()
    assert staged['status'] == 'needs_confirmation', staged
    assert d.store.get('profile', d.profile['id']) == d.profile
    preview = d.client.get('/api/chats/' + d.chat['id'] + '/profile-change',
        params={'prompt_id': staged['pending'][0]['id']}).json()
    assert [row['key'] for row in preview['changes']] == ['excel_columns']
    approved = d.client.post('/api/chats/' + d.chat['id'] + '/profile-change/apply', json={
        'prompt_id': preview['prompt_id'], 'expected_version': preview['expected_version'],
        'selected_keys': ['excel_columns']})
    assert approved.status_code == 200, approved.text
    result = d.client.get('/api/artifacts/cases/export', params={'profile_id': d.profile['id']})
    assert result.status_code == 200, result.text
    sheet = load_workbook(io.BytesIO(result.content)).active
    headers = [cell.value for cell in sheet[1]]
    assert sheet.cell(2, headers.index('测试数据') + 1).value == 'account=alice'
    assert sheet.cell(3, headers.index('attempts') + 1).value == '0'
    assert sheet.cell(3, headers.index('enabled') + 1).value in (False, 'False', 'false')
    assert d.store.get('artifact', 'cases') == d.artifact
    assert [row['field'] for row in d.client.get(d.url).json()['candidates']] == ['actual_result']
    messages = d.store.list('message', chat_id=d.chat['id'])
    assert any(m['role'] == 'assistant' and '测试数据' in m['content'] for m in messages)


def test_aliases_internal_fields_and_deliberately_omitted_core_fields_are_not_drift():
    from tcg.field_drift import detect_field_drift
    artifact = {'type': 'cases', '_profile': {}, 'items': [{
        'id': 'C1', 'title': 'Title', 'type': 'Business', 'priority': 'P1', 'module': 'Login',
        'preconditions': '', 'scenario_id': 'S1', 'requirement_ids': ['R1'], 'refs': ['x'],
        'steps': [], 'expected': '', 'expected_result': 'derived', '_hidden': 1,
        'source_ids': [], 'source_hash': 'hash', 'evidence': {}, 'report': {}, 'profile': {},
        'run_id': 'run', 'chat_id': 'chat', 'project_id': 'project', 'revision': 3,
        'created_at': '', 'updated_at': '', 'case_description': 'Description', 'description': 'Description'}]}
    assert detect_field_drift(artifact, {'excel_columns': [{'field': 'description', 'header': '描述'}]}) == []
    assert [c['field'] for c in detect_field_drift(artifact, {'excel_columns': []})] == ['description']


def test_stale_artifact_profile_unknown_field_and_cross_chat_rejected(drift):
    d = drift
    detected = d.client.get(d.url).json()
    for fields in (['invented'], ['test_data', 'test_data'], ['_profile']):
        bad = d.client.post(d.url + '/propose', json=proposal_body(detected, fields))
        assert bad.status_code == 400, bad.text
    other = d.store.create_chat(d.chat['project_id'], '其他会话')
    cross = d.client.get('/api/chats/' + other['id'] + '/field-drift', params={'artifact_id': 'cases'})
    assert cross.status_code == 404
    d.store.update_profile(d.profile['id'], 'New profile', d.profile['config'], 1)
    assert d.client.post(d.url + '/propose', json=proposal_body(detected, ['test_data'])).status_code == 409
    detected = d.client.get(d.url).json()
    # Save another immutable version through the existing revision commit service.
    updated = copy.deepcopy(d.artifact)
    updated['revision'] = 2
    updated['items'][0]['test_data'] = 'new'
    d.store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
        ('cases', 2, dump(updated), now(), 'fixture', '{}'))
    d.store.put('artifact', updated)
    assert d.client.post(d.url + '/propose', json=proposal_body(detected, ['test_data'])).status_code == 409
    historical = d.client.get(d.url, params={'artifact_id': 'cases', 'revision': 1}).json()
    assert historical['revision'] == 1 and historical['head_revision'] == 2
    assert d.client.post(d.url + '/propose', json=proposal_body(historical, ['test_data'])).status_code == 200
    assert d.store.get('profile', d.profile['id'])['version'] == 2
