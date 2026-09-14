"""Current-control routing may consume only an existing, unchanged user confirmation."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_supervisor_http_v312 import QueueGateway, QueueModel, begin, post, seed


@pytest.fixture
def control_setup(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        yield SimpleNamespace(gateway=gateway, app=app, client=client, chat=chat, cases=cases,
            prompt=initial['pending'][0], initial_plan_id=part['plan_id'])


def current_control_plan(instruction):
    return {'title': '处理已展示的修改', 'steps': [{'capability': 'current_control', 'instruction': instruction}]}


def assert_original_untouched(c):
    assert c.app.state.store.get('artifact', c.cases['id'])['revision'] == c.cases['revision']
    assert not c.app.state.store.list('frozen_export', chat_id=c.chat['id'])


def test_planner_cannot_schedule_confirmation_after_a_new_edit(control_setup):
    c = control_setup
    c.gateway.plans.append({'title': '模型错误地安排了后置确认', 'steps': [
        {'capability': 'artifact_edit', 'instruction': '再改一次标题'},
        {'capability': 'current_control', 'instruction': '自动确认刚改好的新预览'}]})
    result, _ = post(c.client, c.chat, 'reject-future-approval', '修改标题，然后展示新的结果。')
    assert result['status'] in ('failed', 'needs_input'), result
    assert_original_untouched(c)
    assert c.gateway.executed == ['modify_artifact_tool']
    assert c.client.get('/api/chats/' + c.chat['id']).json()['conversation_prompt']['id'] == c.prompt['id']


def test_conditional_assent_is_not_an_approval(control_setup):
    c = control_setup
    c.gateway.plans.append(current_control_plan('模型误将附带修改条件的回复当作批准'))
    result, _ = post(c.client, c.chat, 'conditional-assent', '同意，但前置条件需要再改成已登录且有权限。')
    assert result['status'] in ('failed', 'needs_input'), result
    assert_original_untouched(c)
    assert c.gateway.executed == ['modify_artifact_tool']
    assert c.client.get('/api/chats/' + c.chat['id']).json()['conversation_prompt']['id'] == c.prompt['id']


def test_natural_rejection_cancels_inherited_export(control_setup):
    c = control_setup
    c.gateway.plans.append(current_control_plan('拒绝当前展示的修改预览，并取消原计划剩余操作'))
    c.gateway.calls.insert(0, ('discard_artifact_preview_tool', {}))
    result, _ = post(c.client, c.chat, 'natural-rejection', '我看过了，这份修改不合适，放弃这份预览。')
    assert result['status'] == 'succeeded', result
    assert_original_untouched(c)
    part = next(p for p in result['parts'] if p['type'] == 'execution_plan')
    state = c.client.get('/api/chats/' + c.chat['id'] + '/plans/' + part['plan_id']).json()
    assert state['status'] == 'cancelled', state
    assert state['steps'][-1]['status'] == 'cancelled'
    previous = c.client.get('/api/chats/' + c.chat['id'] + '/plans/' + c.initial_plan_id).json()
    assert previous['status'] == 'cancelled'
    assert 'export_artifact_tool' not in c.gateway.executed


def test_prompt_changed_during_planning_cannot_be_approved(control_setup, monkeypatch):
    c = control_setup
    c.gateway.plans.append(current_control_plan('接受当前已展示的修改预览'))
    original = QueueModel._agenerate
    replacement_id = c.prompt['id'] + ':replacement'
    async def change_prompt_during_planning(model, messages, stop=None, run_manager=None, **kwargs):
        result = await original(model, messages, stop, run_manager, **kwargs)
        if 'submit_execution_plan' in model.names:
            await asyncio.sleep(0)
            chat = c.app.state.store.get('chat', c.chat['id'])
            pending = copy.deepcopy(chat['_native_artifact_prompt'])
            pending['id'] = replacement_id
            c.app.state.store.put('chat', {**chat, '_native_artifact_prompt': pending})
        return result
    monkeypatch.setattr(QueueModel, '_agenerate', change_prompt_during_planning)
    result, _ = post(c.client, c.chat, 'stale-natural-approval', '我已经看过这份修改，请按它保存。')
    assert result['status'] in ('failed', 'needs_input'), result
    assert_original_untouched(c)
    assert c.gateway.executed == ['modify_artifact_tool']
    assert c.client.get('/api/chats/' + c.chat['id']).json()['conversation_prompt']['id'] == replacement_id
    plan = c.app.state.store.list('execution_plan', chat_id=c.chat['id'])[-1]
    assert plan['status'] in ('failed', 'blocked'), plan
