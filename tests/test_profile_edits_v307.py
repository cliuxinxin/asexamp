"""Direct native Profile edits share the saved preview/approval contract."""
import copy
from types import SimpleNamespace

import pytest

from tcg.profile_changes import apply_profile_change, preview_profile_change
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.tool_registry import build_tools


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '对话修改模板')
    profile = store.list('profile')[0]
    yield SimpleNamespace(store=store, chat=chat, profile=profile)
    store.close()


def registry(c, prompt=None, **body):
    return {tool.name: tool for tool in build_tools(c.store, SimpleNamespace(), SimpleNamespace(),
        c.chat, {'reply_to': (prompt or {}).get('id'), **body}, prompt)}


@pytest.mark.asyncio
async def test_add_column_without_file_or_secondary_model_call_then_approve(context):
    c = context
    result = await registry(c)['modify_profile_tool'].ainvoke({
        'upsert_columns': [{'field': 'status', 'header': '执行状态'}],
        'summary': '增加执行状态列，供实际执行后填写。'})
    assert result['status'] == 'needs_confirmation'
    assert '查看 Profile 更改' in result['message']
    assert c.store.get('profile', c.profile['id']) == c.profile
    assert not c.store.list('source', chat_id=c.chat['id'])
    pending = result['pending'][0]
    preview = preview_profile_change(c.store, c.chat['id'], pending['id'])
    assert [change['key'] for change in preview['changes']] == ['excel_columns']
    columns = preview['changes'][0]['after']
    assert columns[:-1] == c.profile['config']['excel_columns']
    assert columns[-1] == {'field': 'status', 'header': '执行状态', 'value_source': 'manual', 'required': False}
    applied = await registry(c, pending)['apply_profile_tool'].ainvoke({})
    assert applied['status'] == 'succeeded'
    assert applied['profile']['config']['excel_columns'] == columns


@pytest.mark.asyncio
async def test_amend_pending_edit_keeps_previous_changes_and_stale_token_cannot_apply(context):
    c = context
    first = await registry(c)['modify_profile_tool'].ainvoke({
        'upsert_columns': [{'field': 'status', 'header': '状态', 'value_source': 'manual'}]})
    old = first['pending'][0]
    second = await registry(c, old)['modify_profile_tool'].ainvoke({
        'upsert_columns': [{'field': 'status', 'header': '执行状态'}, {'field': 'tester', 'header': '执行人'}],
        'preferences': {'language': 'English', 'additional_rules': ''}})
    new = second['pending'][0]
    read = await registry(c, new)['read_profile_tool'].ainvoke({})
    assert read['profile']['version'] == 1
    assert read['pending_config']['language'] == 'English'
    assert read['pending_config']['excel_columns'][-2]['header'] == '执行状态'
    assert read['pending_config']['excel_columns'][-1]['field'] == 'tester'
    stale = await registry(c, old)['apply_profile_tool'].ainvoke({})
    assert stale['status'] == 'needs_input' and stale['error_status'] == 409
    applied = apply_profile_change(c.store, c.chat['id'], new['id'], 1, ['excel_columns'])
    assert applied['profile']['config']['language'] == c.profile['config']['language']
    assert applied['profile']['config']['excel_columns'][-1]['value_source'] == 'manual'
    assert c.store.get('chat', c.chat['id'])['_native_template_prompt'] is None


@pytest.mark.asyncio
async def test_remove_reorder_and_clear_preferences_preserves_other_template_and_defaults(context):
    c = context
    config = copy.deepcopy(c.profile['config'])
    config['additional_rules'] = '旧写作规则'
    config['excel_columns'].append({'field': 'attempts', 'header': '次数',
        'value_source': 'default', 'default_value': 0, 'required': False})
    c.profile = c.store.update_profile(c.profile['id'], c.profile['name'], config, 1)
    desired = [column['field'] for column in config['scenario_excel_columns'] if column['field'] != 'refs'][::-1]
    result = await registry(c)['modify_profile_tool'].ainvoke({'kind': 'scenarios',
        'remove_columns': ['refs'], 'column_order': desired,
        'upsert_columns': [{'field': 'title', 'header': '业务场景'}],
        'preferences': {'additional_rules': ''}})
    pending = result['pending'][0]
    applied = apply_profile_change(c.store, c.chat['id'], pending['id'], 2)['profile']['config']
    assert applied['excel_columns'] == config['excel_columns']
    assert [column['field'] for column in applied['scenario_excel_columns']] == desired
    assert next(column for column in applied['scenario_excel_columns'] if column['field'] == 'title')['header'] == '业务场景'
    assert applied['additional_rules'] == ''


@pytest.mark.asyncio
async def test_invalid_edits_leave_profile_and_pending_unchanged(context):
    c = context
    first = await registry(c)['modify_profile_tool'].ainvoke({'upsert_columns': [{'field': 'tester', 'header': '执行人'}]})
    pending = first['pending'][0]
    for args in [{'preferences': {'sample_cases': []}}, {'remove_columns': ['unknown']},
                 {'upsert_columns': [{'field': '_profile', 'header': 'internal'}]},
                 {'column_order': ['title']}, {'upsert_columns': [{'field': 'steps', 'value_source': 'ai'}]}]:
        result = await registry(c, pending)['modify_profile_tool'].ainvoke(args)
        assert result['status'] == 'needs_input', args
        assert c.store.get('chat', c.chat['id'])['_native_template_prompt'] == pending
        assert c.store.get('profile', c.profile['id']) == c.profile


@pytest.mark.asyncio
async def test_cross_project_and_changed_profile_cannot_reuse_saved_suggestion(context):
    c = context
    other_project = c.store.create_project('其他项目')
    other_profile = c.store.list('profile', project_id=other_project['id'])[0]
    cross = await registry(c)['modify_profile_tool'].ainvoke({'profile_id': other_profile['id'],
        'preferences': {'language': 'English'}})
    assert cross['status'] == 'needs_input' and cross['error_status'] == 404
    staged = await registry(c)['modify_profile_tool'].ainvoke({'preferences': {'language': 'English'}})
    pending = staged['pending'][0]
    c.store.update_profile(c.profile['id'], '更改后的配置', c.profile['config'], 1)
    with pytest.raises(DomainError, match='已改变'):
        preview_profile_change(c.store, c.chat['id'], pending['id'])
    amended = await registry(c, pending)['modify_profile_tool'].ainvoke({'preferences': {'case_level': 'deep'}})
    assert amended['status'] == 'needs_input' and amended['error_status'] == 409


@pytest.mark.asyncio
async def test_reverting_pending_edits_clears_suggestion_without_version_change(context):
    c = context
    first = await registry(c)['modify_profile_tool'].ainvoke({'preferences': {'language': 'English'}})
    second = await registry(c, first['pending'][0])['modify_profile_tool'].ainvoke({
        'preferences': {'language': c.profile['config']['language']}})
    assert second['status'] == 'succeeded' and '无需修改' in second['message']
    assert c.store.get('chat', c.chat['id'])['_native_template_prompt'] is None
    assert c.store.get('profile', c.profile['id'])['version'] == 1
