"""A newly confirmed Profile is offered immediately without rewriting saved cases."""
from test_artifact_export_v306 import viewer, workbook


def test_current_viewer_defaults_to_chat_profile_but_history_keeps_snapshot(viewer):
    client, store, project = viewer
    current = store.get('artifact', 'cases')
    profile = store.create_profile(project['id'], '对话修改后的格式', {
        'excel_columns': [{'field': 'title', 'header': '用例标题'},
                          {'field': 'status', 'header': '执行状态', 'value_source': 'manual'}]})
    chat = store.get('chat', current['chat_id'])
    store.put('chat', {**chat, 'profile_id': profile['id']})
    options = client.get('/api/artifacts/cases/export-options?revision=2').json()
    assert options['default_profile_id'] == profile['id']
    assert options['snapshot']['sheet_name'] == 'New Cases'
    history = client.get('/api/artifacts/cases/export-options?revision=1').json()
    assert history['default_profile_id'] is None
    sheet = workbook(client.get('/api/artifacts/cases/export', params={
        'revision': 2, 'profile_id': options['default_profile_id']}))
    assert list(sheet.values) == [('用例标题', '执行状态'), ('New C1', None), ('New C2', None)]
    assert store.get('artifact', 'cases') == current
