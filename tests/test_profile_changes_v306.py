"""Profile approval previews the actual merge and applies only selected server values."""
import copy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tcg.native_api import register_routes
from tcg.native_chat import NativeChatAgent
from tcg.documents import parse_text
from tcg.profile_changes import apply_profile_change, preview_profile_change
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.tool_registry import build_tools


@pytest.fixture
def proposal(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '模板学习')
    profile = store.list('profile')[0]
    template = store.put('template', {'id': 'template-one', 'chat_id': chat['id'],
        'project_id': project['id'], 'profile_id': profile['id'], 'base_profile_version': 1,
        'summary': '调整场景表头与工作表名称。', 'template_kinds': ['scenarios'], 'config': {
            'scenario_sheet_name': '业务场景', 'scenario_excel_columns': [
                {'field': 'title', 'header': '场景标题'}, {'field': 'id', 'header': '场景编号'}],
            'sheet_name': '不能覆盖用例设置'}})
    pending = {'id': 'template:one:1', 'kind': 'profile', 'template_ids': [template['id']],
        'profile_id': profile['id'], 'expected_version': 1, 'message': template['summary']}
    store.put('chat', {**chat, '_native_template_prompt': pending})
    yield SimpleNamespace(store=store, chat=chat, profile=profile, template=template, pending=pending)
    store.close()


def test_preview_legacy_proposal_and_subset_approval_preserves_every_other_setting(proposal):
    p = proposal
    preview = preview_profile_change(p.store, p.chat['id'], p.pending['id'])
    changes = {row['key']: row for row in preview['changes']}
    assert set(changes) == {'scenario_sheet_name', 'scenario_excel_columns'}
    assert changes['scenario_excel_columns']['after'][0]['field'] == 'title'
    assert changes['scenario_excel_columns']['before'] == p.profile['config']['scenario_excel_columns']
    assert all(row['before_present'] and row['after_present'] for row in changes.values())
    result = apply_profile_change(p.store, p.chat['id'], p.pending['id'], 1, ['scenario_sheet_name'], write_messages=True)
    expected = {**p.profile['config'], 'scenario_sheet_name': '业务场景'}
    assert result['profile']['config'] == expected
    assert result['profile']['version'] == 2
    assert p.store.get('chat', p.chat['id'])['_native_template_prompt'] is None
    assert p.store.get('template', p.template['id'])['_applied_keys'] == ['scenario_sheet_name']
    assert {row['role'] for row in p.store.list('message', chat_id=p.chat['id'])} == {'user', 'assistant'}
    with pytest.raises(DomainError, match='提示已改变'):
        apply_profile_change(p.store, p.chat['id'], p.pending['id'], 1, ['scenario_excel_columns'])


@pytest.mark.parametrize('keys', [[], ['scope'], ['scenario_sheet_name', 'scenario_sheet_name']])
def test_empty_unknown_or_duplicate_selections_do_not_write(proposal, keys):
    p = proposal
    with pytest.raises(DomainError):
        apply_profile_change(p.store, p.chat['id'], p.pending['id'], 1, keys)
    assert p.store.get('profile', p.profile['id']) == p.profile
    assert p.store.get('chat', p.chat['id'])['_native_template_prompt'] == p.pending


def test_stale_version_superseded_prompt_and_cross_chat_are_rejected(proposal):
    p = proposal
    other = p.store.create_chat(p.chat['project_id'], '另一个对话')
    p.store.put('chat', {**other, '_native_template_prompt': p.pending})
    with pytest.raises(DomainError, match='不属于当前对话'):
        preview_profile_change(p.store, other['id'], p.pending['id'])
    with pytest.raises(DomainError, match='提示已改变'):
        preview_profile_change(p.store, p.chat['id'], 'old-prompt')
    with pytest.raises(DomainError, match='版本'):
        apply_profile_change(p.store, p.chat['id'], p.pending['id'], 2, ['scenario_sheet_name'])
    p.store.update_profile(p.profile['id'], '编辑后的 Profile', p.profile['config'], 1)
    with pytest.raises(DomainError, match='已改变'):
        preview_profile_change(p.store, p.chat['id'], p.pending['id'])


def test_preview_shows_normalized_rules_and_protected_manual_policy(proposal):
    p = proposal
    config = copy.deepcopy(p.profile['config'])
    config['excel_columns'].append({'field': 'actual_result', 'header': '实际结果', 'value_source': 'manual', 'required': False})
    profile = p.store.update_profile(p.profile['id'], p.profile['name'], config, 1)
    p.store.put('template', {**p.template, 'base_profile_version': 2, 'template_kinds': ['cases'], 'config': {
        'template_rules': ['步骤清楚', '预期可验证'], 'excel_columns': [
            {'field': 'title', 'header': '用例标题'},
            {'field': 'actual_result', 'header': '执行结果', 'value_source': 'ai', 'required': True}]}})
    p.store.put('chat', {**p.chat, '_native_template_prompt': {**p.pending, 'expected_version': 2}})
    changes = {row['key']: row for row in preview_profile_change(p.store, p.chat['id'], p.pending['id'])['changes']}
    assert changes['template_rules']['after'] == '步骤清楚\n预期可验证'
    manual = changes['excel_columns']['after'][1]
    assert manual['value_source'] == 'manual' and manual['required'] is False
    applied = apply_profile_change(p.store, p.chat['id'], p.pending['id'], 2)
    assert applied['profile']['config']['excel_columns'] == changes['excel_columns']['after']
    assert applied['profile']['config']['scenario_excel_columns'] == profile['config']['scenario_excel_columns']


@pytest.mark.asyncio
async def test_http_preview_and_manual_confirmation_persist_chat_history(proposal):
    p = proposal
    app = FastAPI()
    app.state.store = p.store
    app.state.conversation = NativeChatAgent(p.store, object(), object(), object())
    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({'detail': exc.message}, status_code=exc.status)
    register_routes(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://tcg.test') as client:
        url = f'/api/chats/{p.chat["id"]}/profile-change'
        response = await client.get(url, params={'prompt_id': p.pending['id']})
        assert response.status_code == 200
        preview = response.json()
        body = {'prompt_id': preview['prompt_id'], 'expected_version': preview['expected_version'],
            'selected_keys': ['scenario_sheet_name']}
        rejected = await client.post(url + '/apply', json={**body, 'config': {'scope': 'injected'}})
        assert rejected.status_code == 422
        response = await client.post(url + '/apply', json=body)
        assert response.status_code == 200 and response.json()['status'] == 'succeeded'
        assert len(p.store.list('message', chat_id=p.chat['id'])) == 2
        assert (await client.post(url + '/apply', json=body)).status_code == 409


@pytest.mark.asyncio
async def test_learning_returns_summary_and_chat_approval_uses_same_selective_service(proposal):
    p = proposal
    source = p.store.add_source(p.chat['id'], '场景模板', 'example', *parse_text('场景标题\t场景编号'))
    async def generate_native(task, context, schema, instruction):
        assert task == 'learn_template' and '不逐列罗列' in instruction
        return {key: p.template[key] for key in ('summary', 'template_kinds', 'config')}
    business = SimpleNamespace(gateway=SimpleNamespace(generate_native=generate_native))
    def tools(prompt=None):
        return {tool.name: tool for tool in build_tools(p.store, business, SimpleNamespace(), p.chat,
            {'reply_to': (prompt or {}).get('id')}, prompt)}
    learned = await tools()['learn_template_tool'].ainvoke({'source_ids': [source['id']], 'kind': 'scenarios'})
    assert learned['status'] == 'needs_confirmation'
    assert '查看 Profile 更改' in learned['message'] and '按导出顺序' not in learned['message']
    assert '场景标题' not in learned['message']
    assert 'config' not in learned['proposal']
    pending = learned['pending'][0]
    preview = preview_profile_change(p.store, p.chat['id'], pending['id'])
    assert len(preview['changes']) == 2
    approved = await tools(pending)['apply_profile_tool'].ainvoke({'selected_keys': ['scenario_sheet_name']})
    assert approved['status'] == 'succeeded'
    assert approved['profile']['config']['scenario_sheet_name'] == '业务场景'
    assert approved['profile']['config']['scenario_excel_columns'] == p.profile['config']['scenario_excel_columns']
    assert not p.store.list('message', chat_id=p.chat['id'])


@pytest.mark.asyncio
async def test_learning_identical_template_clears_old_pending_without_changing_profile(proposal):
    p = proposal
    source = p.store.add_source(p.chat['id'], '现有场景模板', 'example', *parse_text('内容'))
    async def generate_native(*args):
        return {'summary': '模板与现有配置一致。', 'template_kinds': ['scenarios'],
            'config': {'scenario_sheet_name': p.profile['config']['scenario_sheet_name']}}
    business = SimpleNamespace(gateway=SimpleNamespace(generate_native=generate_native))
    tools = {tool.name: tool for tool in build_tools(p.store, business, SimpleNamespace(), p.chat, {}, {})}
    learned = await tools['learn_template_tool'].ainvoke({'source_ids': [source['id']], 'kind': 'scenarios'})
    assert learned['status'] == 'succeeded' and '无需修改' in learned['message']
    assert p.store.get('profile', p.profile['id'])['version'] == 1
    assert p.store.get('chat', p.chat['id'])['_native_template_prompt'] is None
