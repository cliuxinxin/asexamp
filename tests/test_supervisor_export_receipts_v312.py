"""Planned exports commit exact scoped bytes and restart-safe receipts together."""
import base64
import copy
import io
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from tcg.documents import parse_text
from tcg.schemas import DomainError
from tcg.storage import Store, dump, now
from tcg.tool_registry import build_tools


@pytest.fixture
def export_setup(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Planned export')
    profile = store.list('profile', project_id=project['id'])[0]
    text, chunks = parse_text('登录成功显示首页。')
    source = store.add_source(chat['id'], 'Requirement', 'primary', text, chunks)
    rows = [{'id': 'C' + str(index), 'title': '登录' + str(index), 'scenario_id': '',
        'type': 'Business', 'priority': 'P1', 'preconditions': '已登录',
        'steps': [{'action': '打开首页', 'expected': '显示首页'}], 'refs': [source['id'] + '#P1']}
        for index in (1, 2)]
    artifact = store.put('artifact', {'id': 'cases', 'chat_id': chat['id'], 'project_id': project['id'],
        'type': 'cases', 'title': '测试用例', 'revision': 1, '_visible': True, '_profile': profile['config'],
        '_source_ids': [source['id']], '_source_roles': {source['id']: 'primary'}, 'items': rows, 'report': {}})
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
        (artifact['id'], 1, dump(artifact), now(), 'test_fixture', '{}'))
    body = {'content': '导出选中用例', 'artifact_id': artifact['id'], 'selected_ids': ['C2'],
        'profile_id': profile['id'], '_plan_step_id': 'step_export', '_expected_revisions': {'cases': 1},
        '_expected_profile': {'id': profile['id'], 'version': 1}, '_export_artifact_ids': ['cases']}
    value = SimpleNamespace(store=store, chat=chat, profile=profile, artifact=artifact, body=body, path=tmp_path)
    yield value
    value.store.close()


async def export(value, args=None, body=None):
    tools = build_tools(value.store, None, None, value.chat, body or value.body)
    chosen = next(t for t in tools if t.name == 'export_artifact_tool')
    return await chosen.ainvoke(args or {})


@pytest.mark.asyncio
async def test_restart_replays_same_scoped_bytes_after_heads_change(export_setup, monkeypatch):
    c = export_setup
    first = await export(c)
    assert first['status'] == 'succeeded', first
    records = c.store.list('frozen_export')
    assert len(records) == 1
    sheet = load_workbook(io.BytesIO(base64.b64decode(records[0]['_bytes']))).active
    values = list(sheet.values)
    assert len(values) == 2 and 'C2' in values[1] and 'C1' not in values[1]
    receipt = c.store.get('execution_step_receipt', 'receipt:step_export')
    assert receipt['parts'] == first['parts'] and receipt['status'] == 'succeeded'
    c.store.update_profile(c.profile['id'], 'New profile name', c.profile['config'], 1)
    changed = copy.deepcopy(c.artifact['items'])
    changed[0]['title'] = 'Another user changed this case'
    c.store.revise_artifact(c.artifact['id'], 1, changed)
    c.store.close()
    c.store = Store(c.path)
    def never_regenerate(*args, **kwargs):
        raise AssertionError('A committed export must be returned without generating bytes again.')
    monkeypatch.setattr('tcg.tool_registry.export_artifact', never_regenerate)
    replayed = await export(c)
    assert replayed == first
    assert c.store.list('frozen_export') == records


@pytest.mark.asyncio
async def test_same_step_cannot_change_selection_or_chat(export_setup):
    c = export_setup
    assert (await export(c))['status'] == 'succeeded'
    changed = await export(c, {'item_ids': ['C1']})
    assert changed['status'] == 'needs_input' and changed['error_status'] == 409
    c.chat = c.store.create_chat(c.chat['project_id'], 'Other chat')
    other_chat = await export(c)
    assert other_chat['status'] == 'needs_input' and other_chat['error_status'] == 409
    assert len(c.store.list('frozen_export')) == 1


@pytest.mark.asyncio
async def test_bound_profile_cannot_be_substituted_or_updated(export_setup):
    c = export_setup
    other = c.store.create_profile(c.chat['project_id'], 'Other', c.profile['config'])
    substitution = await export(c, {'profile_id': other['id']})
    assert substitution['status'] == 'needs_input' and substitution['error_status'] == 409
    c.store.update_profile(c.profile['id'], 'Updated', c.profile['config'], 1)
    stale = await export(c)
    assert stale['status'] == 'needs_input' and stale['error_status'] == 409
    assert not c.store.list('frozen_export') and not c.store.list('execution_step_receipt')


@pytest.mark.asyncio
@pytest.mark.parametrize('changed_object', ['profile', 'artifact'])
async def test_commit_guard_rechecks_versions_after_render(export_setup, monkeypatch, changed_object):
    c = export_setup
    from tcg.documents import export_artifact
    def change_during_render(artifact, **kwargs):
        content = export_artifact(artifact, **kwargs)
        if changed_object == 'profile':
            c.store.update_profile(c.profile['id'], 'Concurrent profile edit', c.profile['config'], 1)
        else:
            rows = copy.deepcopy(c.artifact['items'])
            rows[0]['title'] = 'Concurrent case edit'
            c.store.revise_artifact(c.artifact['id'], 1, rows)
        return content
    monkeypatch.setattr('tcg.tool_registry.export_artifact', change_during_render)
    result = await export(c)
    assert result['status'] == 'needs_input' and result['error_status'] == 409, result
    assert not c.store.list('frozen_export') and not c.store.list('execution_step_receipt')


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_bytes_and_retry_saves_once(export_setup, monkeypatch):
    c = export_setup
    original = c.store.put
    def fail_receipt(kind, value):
        if kind == 'execution_step_receipt':
            raise DomainError('Simulated interruption before receipt commit', 409)
        return original(kind, value)
    monkeypatch.setattr(c.store, 'put', fail_receipt)
    result = await export(c)
    assert result['status'] == 'needs_input'
    assert not c.store.list('frozen_export') and not c.store.list('execution_step_receipt')
    monkeypatch.setattr(c.store, 'put', original)
    assert (await export(c))['status'] == 'succeeded'
    assert len(c.store.list('frozen_export')) == len(c.store.list('execution_step_receipt')) == 1
