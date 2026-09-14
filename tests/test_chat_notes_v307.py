"""Public assistant speech survives native tool turns and later failures."""
import pytest
from langchain_core.messages import AIMessage

from tcg.native_chat import NativeChatAgent
from tcg.server_capacity import ContextCapacityError
from test_native_chat_safety_v300 import Gateway, call, store_chat


@pytest.mark.asyncio
async def test_public_tool_commentary_is_saved_with_final_reply(store_chat):
    store, chat = store_chat
    note = '我会先记录这条已确认的项目规则。'
    final = '已保存，项目中的其他会话也可以使用这条规则。'
    gateway = Gateway([AIMessage(content=[{'type': 'text', 'text': note},
        {'type': 'reasoning', 'text': 'private reasoning must not be displayed'}],
        tool_calls=[call('save', 'add_knowledge_tool', {'content': '登录失败五次锁定十分钟。'})]),
        AIMessage(content=final)])
    agent = NativeChatAgent(store, gateway, object(), object())
    result = await agent.submit(chat['id'], {'content': '记录规则：登录失败五次锁定十分钟。',
                                            'client_message_id': 'with-commentary'})
    assert result['message'] == final
    assert [p for p in result['parts'] if p['type'] == 'assistant_note'] == [
        {'type': 'assistant_note', 'text': note}]
    saved = store.get('message', 'reply:' + result['id'])
    assert saved['role'] == 'assistant'
    assert saved['metadata']['turn_response']['parts'] == result['parts']
    assert 'private reasoning' not in str(saved)


@pytest.mark.asyncio
async def test_commentary_remains_visible_when_final_model_call_fails(store_chat):
    store, chat = store_chat
    gateway = Gateway([AIMessage(content='先记录你刚才的补充规则。', tool_calls=[
        call('save', 'add_knowledge_tool', {'content': '登录失败五次锁定十分钟。'})]),
        ContextCapacityError()])
    agent = NativeChatAgent(store, gateway, object(), object())
    body = {'content': '保存这条规则。', 'client_message_id': 'commentary-failure'}
    result = await agent.submit(chat['id'], body)
    assert result['status'] == 'failed'
    assert any(p == {'type': 'assistant_note', 'text': '先记录你刚才的补充规则。'}
               for p in result['parts'])
    assert any(p['type'] == 'diagnostic' for p in result['parts'])
    assert '保留' in result['message']
    assert await agent.submit(chat['id'], body) == result
    assert len(store.list('source', chat_id=chat['id'])) == 1


@pytest.mark.asyncio
async def test_successful_read_does_not_hide_prior_mutation_error(store_chat):
    store, chat = store_chat
    gateway = Gateway([
        AIMessage(content='', tool_calls=[call('edit', 'modify_profile_tool', {'remove_columns': ['missing']})]),
        AIMessage(content='我再核对一下现有列。', tool_calls=[call('read', 'read_profile_tool', {})]),
        AIMessage(content='已成功删除列。')])
    agent = NativeChatAgent(store, gateway, object(), object())
    result = await agent.submit(chat['id'], {'content': '删除 missing 列', 'client_message_id': 'read-after-error'})
    assert result['status'] == 'needs_input'
    assert '删除列需使用现有' in result['message']
    assert '已成功删除' not in result['message']
    assert any(p.get('text') == '我再核对一下现有列。' for p in result['parts'])
