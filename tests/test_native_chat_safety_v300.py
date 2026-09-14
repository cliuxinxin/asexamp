import asyncio
import copy
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tcg.native_chat import NativeChatAgent
from tcg.server_capacity import ContextCapacityError
from tcg.storage import Store


class ScriptedChatModel(BaseChatModel):
    script: list[Any] = Field(exclude=True)
    calls: list[Any] = Field(default_factory=list, exclude=True)
    planning: bool = False

    @property
    def _llm_type(self):
        return 'native-chat-safety'

    def bind_tools(self, tools, **kwargs):
        names = {value.name if hasattr(value, 'name') else value['function']['name'] for value in tools}
        return self.model_copy(update={'planning': 'submit_execution_plan' in names})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.planning:
            first = self.script[0]
            name = first.tool_calls[0]['name'] if isinstance(first, AIMessage) and first.tool_calls else ''
            capability = {'add_knowledge_tool': 'knowledge', 'modify_profile_tool': 'profile_edit',
                'read_profile_tool': 'answer', 'list_context_tool': 'answer'}.get(name, 'answer')
            proposal = AIMessage(content='', tool_calls=[call('plan', 'submit_execution_plan', {
                'title': '处理用户要求', 'steps': [{'capability': capability,
                    'instruction': '按本次用户原文处理指定对象，不扩大范围。'}]})])
            return ChatResult(generations=[ChatGeneration(message=proposal)])
        self.calls.append(copy.deepcopy(messages))
        next_result = self.script.pop(0)
        if isinstance(next_result, Exception):
            raise next_result
        return ChatResult(generations=[ChatGeneration(message=next_result)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


class Gateway:
    def __init__(self, script):
        self.model = ScriptedChatModel(script=script)

    def chat_model(self):
        return self.model


def call(call_id, name, args):
    return {'id': call_id, 'name': name, 'args': args, 'type': 'tool_call'}


@pytest.fixture
def store_chat(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Chat safety')
    yield store, chat
    store.close()


@pytest.mark.asyncio
async def test_invalid_tool_arguments_cannot_be_presented_as_success(store_chat):
    store, chat = store_chat
    gateway = Gateway([AIMessage(content='', tool_calls=[call('bad', 'add_knowledge_tool', {})]),
                       AIMessage(content='已成功保存所有澄清。')])
    agent = NativeChatAgent(store, gateway, object(), object())
    result = await agent.submit(chat['id'], {'content': '保存这条澄清', 'client_message_id': 'missing'})
    assert result['status'] == 'needs_input'
    assert '已成功保存所有澄清' not in result['message']
    assert not store.list('source', chat_id=chat['id'])
    assert result['actions'][0]['status'] == 'needs_input'
    assert any(isinstance(m, ToolMessage) and m.status == 'error' for m in gateway.model.calls[1])


@pytest.mark.asyncio
async def test_duplicate_parallel_mutations_are_applied_once(store_chat):
    store, chat = store_chat
    arguments = {'content': '已确认：登录成功后关闭旧会话。'}
    gateway = Gateway([AIMessage(content='', tool_calls=[call('first', 'add_knowledge_tool', arguments),
        call('duplicate', 'add_knowledge_tool', arguments)]), AIMessage(content='已保存项目澄清。')])
    agent = NativeChatAgent(store, gateway, object(), object())
    result = await agent.submit(chat['id'], {'content': arguments['content'], 'client_message_id': 'duplicate'})
    assert result['status'] == 'succeeded'
    assert len(store.list('source', chat_id=chat['id'])) == 1
    assert len(result['actions']) == 1
    returned = [m for m in gateway.model.calls[1] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in returned] == ['first', 'duplicate']
    assert returned[0].content == returned[1].content


@pytest.mark.asyncio
async def test_context_failure_after_write_preserves_result_without_replaying_turn(store_chat):
    store, chat = store_chat
    gateway = Gateway([AIMessage(content='', tool_calls=[call('save', 'add_knowledge_tool',
        {'content': '登录失败显示错误提示。'})]), ContextCapacityError()])
    agent = NativeChatAgent(store, gateway, object(), object())
    body = {'content': '保存：登录失败显示错误提示。', 'client_message_id': 'capacity'}
    result = await agent.submit(chat['id'], body)
    assert result['status'] == 'failed'
    assert '已完成' in result['message'] and '保留' in result['message']
    assert result['actions'][0]['status'] == 'succeeded'
    assert len(store.list('source', chat_id=chat['id'])) == 1
    assert await agent.submit(chat['id'], body) == result
    assert len(gateway.model.calls) == 2
    assert len(store.list('source', chat_id=chat['id'])) == 1
