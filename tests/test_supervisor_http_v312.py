"""Supervisor contracts over HTTP, including actual saved revisions and XLSX bytes."""
import asyncio
import copy
import io
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from openpyxl import load_workbook

from tcg.main import create_app
from tcg.native_business import NativeBusiness
from test_native_business_v300 import NativeModel


class QueueModel(BaseChatModel):
    gateway: Any = Field(exclude=True)
    names: frozenset[str] = frozenset()

    @property
    def _llm_type(self):
        return 'supervisor-http-contract'

    def bind_tools(self, tools, **kwargs):
        names = frozenset(t.name if hasattr(t, 'name') else t['function']['name'] for t in tools)
        self.gateway.bindings.append(names)
        return self.model_copy(update={'names': names})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if 'submit_execution_plan' in self.names:
            self.gateway.planner_inputs.append(copy.deepcopy(messages))
            assert self.gateway.plans, 'Unexpected planning call during a bound confirmation.'
            plan = self.gateway.plans.pop(0)
            message = AIMessage(content='', tool_calls=[{'name': 'submit_execution_plan',
                'args': plan, 'id': 'plan-' + str(len(self.gateway.planner_inputs)), 'type': 'tool_call'}])
        elif isinstance(messages[-1], ToolMessage):
            message = AIMessage(content='本步已处理。')
        else:
            assert self.gateway.calls, 'Unexpected specialist call.'
            name, arguments = self.gateway.calls.pop(0)
            assert name in self.names, (name, self.names)
            self.gateway.executed.append(name)
            message = AIMessage(content='', tool_calls=[{'name': name, 'args': arguments,
                'id': 'exec-' + str(len(self.gateway.executed)), 'type': 'tool_call'}])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


class QueueGateway:
    def __init__(self):
        self.business_model = NativeModel()
        self.plans, self.calls, self.bindings, self.planner_inputs, self.executed = [], [], [], [], []
        self.model = QueueModel(gateway=self)

    def chat_model(self):
        return self.model

    async def generate_native(self, *args, **kwargs):
        return await self.business_model.generate_native(*args, **kwargs)


async def seed(store, gateway):
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Supervisor acceptance')
    source = store.add_source(chat['id'], '需求.md', 'primary', '登录成功显示首页。\n退出登录后禁止访问。',
        [{'text': '登录成功显示首页。'}, {'text': '退出登录后禁止访问。'}])
    _, run = store.create_run(chat['id'], {'content': '准备已有成果', 'intent': 'generate_case',
        'mode': 'auto', 'experience': 'native', 'source_ids': [source['id']]})
    business = NativeBusiness(store, gateway)
    analysis = await business.understand(run)
    scenarios = await business.scenarios(run, analysis)
    cases = await business.cases(run, analysis, scenarios)
    for artifact in (analysis, scenarios, cases):
        store.put('artifact', {**artifact, '_visible': True})
    store.update_run(run['id'], status='completed', stage='completed', current_artifact_id=cases['id'])
    gateway.calls = []
    return chat, cases


def plan(*capabilities):
    instructions = {'artifact_edit': "只将选中用例的前置条件改为'已登录'，先给出预览。",
        'export': '导出前一步已确认版本的原选中用例为Excel。', 'answer': '只解释这份修改，不进行确认或修改。'}
    return {'title': '按顺序处理本次要求', 'steps': [
        {'capability': name, 'instruction': instructions[name]} for name in capabilities]}


def post(client, chat, sequence, content, **extra):
    body = {'client_message_id': sequence, 'content': content, 'mode': 'hitp', **extra}
    response = client.post('/api/chats/' + chat['id'] + '/turns', json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def file_parts(response):
    return [file for part in response.get('parts', []) if part.get('type') == 'files' for file in part['files']]


def wait_files(client, chat, response):
    import time
    for _ in range(100):
        files = file_parts(response)
        if files:
            return files
        snapshot = client.get('/api/chats/' + chat['id']).json()
        files = [file for m in snapshot['messages'] for file in file_parts(m.get('metadata', {}).get('turn_response', {}))]
        if files:
            return files
        time.sleep(.03)
    raise AssertionError(response)


def begin(client, app, gateway, chat, cases):
    selected = [cases['items'][0]['id']]
    gateway.plans = [plan('artifact_edit', 'export')]
    gateway.calls = [('modify_artifact_tool', {'artifact_id': cases['id'], 'new_values': {'preconditions': '已登录'}}),
                     ('export_artifact_tool', {'artifact_ids': [cases['id']]})]
    result, body = post(client, chat, 'modify-export', '这几个用例不太对，把前置条件都改成已登录，然后直接导出Excel。',
        artifact_id=cases['id'], artifact_revision=cases['revision'], selected_ids=selected)
    assert result['status'] == 'needs_confirmation', result
    assert not app.state.store.list('frozen_export', chat_id=chat['id'])
    assert app.state.store.get('artifact', cases['id'])['revision'] == cases['revision']
    part = next(p for p in result['parts'] if p['type'] == 'execution_plan')
    snapshot = client.get('/api/chats/' + chat['id'] + '/plans/' + part['plan_id'])
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()['waiting_prompt_id'] == result['pending'][0]['id']
    assert len(gateway.planner_inputs) == 1
    edit_bindings = [names for names in gateway.bindings if 'modify_artifact_tool' in names]
    assert edit_bindings and all('export_artifact_tool' not in names for names in edit_bindings)
    return result, part, body


def test_modify_selected_confirm_exports_exact_new_version_and_duplicate_does_not_repeat(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        prompt = initial['pending'][0]
        result, body = post(client, chat, 'accept', '同意', reply_kind='confirm', reply_to=prompt['id'],
            command={'name': 'artifact.apply', 'arguments': {'proposal_id': prompt['proposal_id']}})
        files = wait_files(client, chat, result)
        assert len(files) == 1
        record = app.state.store.list('frozen_export', chat_id=chat['id'])
        assert len(record) == 1 and record[0]['revision'] == cases['revision'] + 1
        sheet = load_workbook(io.BytesIO(client.get(files[0]['url']).content)).active
        values = list(sheet.values)
        assert len(values) == 2, values
        assert '已登录' in values[1]
        current = app.state.store.get('artifact', cases['id'])
        assert current['items'][1] == cases['items'][1]
        assert len(gateway.planner_inputs) == 1
        again = client.post('/api/chats/' + chat['id'] + '/turns', json=body).json()
        assert again['id'] == result['id']
        assert len(app.state.store.list('frozen_export', chat_id=chat['id'])) == 1
        assert app.state.store.get('artifact', cases['id'])['revision'] == current['revision']
        state = client.get('/api/chats/' + chat['id'] + '/plans/' + part['plan_id']).json()
        assert state['status'] in ('completed', 'succeeded'), state


def test_question_does_not_advance_and_inline_reject_cancels_export(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        prompt = initial['pending'][0]
        gateway.plans.append(plan('answer'))
        gateway.calls.insert(0, ('analyze_artifact_tool', {'artifact_id': cases['id'], 'instruction': '解释这些用例'}))
        answer, _ = post(client, chat, 'question', '为什么需要这样修改？', reply_to=prompt['id'])
        assert answer['status'] == 'succeeded', answer
        assert not app.state.store.list('frozen_export', chat_id=chat['id'])
        assert client.get('/api/chats/' + chat['id']).json()['conversation_prompt']['id'] == prompt['id']
        rejected, _ = post(client, chat, 'reject', '拒绝这份修改', reply_kind='confirm', reply_to=prompt['id'],
            command={'name': 'artifact.discard', 'arguments': {'proposal_id': prompt['proposal_id']}})
        assert rejected['status'] == 'succeeded', rejected
        assert app.state.store.get('artifact', cases['id'])['revision'] == cases['revision']
        assert not app.state.store.list('frozen_export', chat_id=chat['id'])
        state = client.get('/api/chats/' + chat['id'] + '/plans/' + part['plan_id']).json()
        assert state['status'] == 'cancelled', state
        assert len(gateway.planner_inputs) == 2


def test_waiting_plan_restores_after_server_restart_then_exports_without_replanning(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        prompt = initial['pending'][0]
    restarted = create_app(tmp_path, gateway)
    with TestClient(restarted) as client:
        state = client.get('/api/chats/' + chat['id'] + '/plans/' + part['plan_id']).json()
        assert state['waiting_prompt_id'] == prompt['id']
        result, _ = post(client, chat, 'accept-after-restart', '同意', reply_kind='confirm', reply_to=prompt['id'],
            command={'name': 'artifact.apply', 'arguments': {'proposal_id': prompt['proposal_id']}})
        assert len(wait_files(client, chat, result)) == 1
        assert len(gateway.planner_inputs) == 1
        assert len(restarted.state.store.list('frozen_export', chat_id=chat['id'])) == 1


def test_plain_answer_is_preserved_when_a_later_step_exports(tmp_path, monkeypatch):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    answer = '前置条件描述执行用例之前必须满足的状态。'
    original = QueueModel._generate
    def answer_without_a_tool(model, messages, stop=None, run_manager=None, **kwargs):
        if 'analyze_artifact_tool' in model.names:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])
        return original(model, messages, stop, run_manager, **kwargs)
    monkeypatch.setattr(QueueModel, '_generate', answer_without_a_tool)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        gateway.plans = [{'title': '先解答，再导出', 'steps': [
            {'capability': 'answer', 'instruction': '解释前置条件这个术语，不修改用例。'},
            {'capability': 'export', 'instruction': '导出当前用例。'}]}]
        gateway.calls = [('export_artifact_tool', {'artifact_ids': [cases['id']]})]
        result, _ = post(client, chat, 'explain-export', '先解释前置条件是什么意思，然后导出当前用例。',
            artifact_id=cases['id'], artifact_revision=cases['revision'])
        assert result['status'] == 'succeeded', result
        assert {'type': 'assistant_note', 'text': answer} in result['parts']
        assert len(file_parts(result)) == 1
        saved = app.state.store.get('message', 'reply:' + result['id'])
        assert answer in str(saved['metadata']['turn_response']['parts'])
        assert app.state.store.get('artifact', cases['id'])['revision'] == cases['revision']
