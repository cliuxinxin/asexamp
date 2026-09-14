"""Pipeline control replies reflect saved stages, not model retellings."""
import pytest
from langchain_core.messages import AIMessage

from tcg.native_chat import NativeChatAgent
from tcg.tool_registry import build_tools
from test_native_chat_safety_v300 import Gateway, call, store_chat


class ControlPipeline:
    def __init__(self, store, replacement=False):
        self.store, self.replacement, self.calls = store, replacement, []

    async def snapshot(self, run_id):
        return self.store.run(run_id)

    async def retry(self, run_id):
        self.calls.append(('retry', run_id))
        if self.replacement:
            old = self.store.run(run_id)
            _, run = self.store.create_run(old['chat_id'], {'content': '重新理解当前知识', 'mode': 'hitp', 'intent': 'generate_case'})
            return self.store.update_run(run['id'], status='queued', stage='understand')
        return self.store.update_run(run_id, status='queued', stage='scenarios', error=None)

    async def request_pause(self, run_id):
        self.calls.append(('pause', run_id))
        return self.store.update_run(run_id, pause_after_step=True)

    async def cancel(self, run_id):
        self.calls.append(('cancel', run_id))
        return self.store.update_run(run_id, status='cancelled', stage='cancelled')


def failed_run(store, chat):
    _, run = store.create_run(chat['id'], {'content': '生成用例', 'mode': 'hitp', 'intent': 'generate_case'})
    return store.update_run(run['id'], status='failed', stage='failed', failed_node='scenarios',
                            error='场景没有覆盖全部需求')


@pytest.mark.asyncio
async def test_retry_receipt_overrides_wrong_model_stage_and_completion(store_chat):
    store, chat = store_chat
    run = failed_run(store, chat)
    pipeline = ControlPipeline(store)
    gateway = Gateway([
        AIMessage(content='我将重试需求理解。', tool_calls=[call('retry', 'control_pipeline_tool', {'action': 'retry'})]),
        AIMessage(content='需求理解已完成，所有测试用例已经生成。'),
    ])
    result = await NativeChatAgent(store, gateway, object(), pipeline).submit(chat['id'],
        {'content': '重试当前步骤', 'client_message_id': 'retry-stage'})
    assert result['status'] == 'succeeded'
    assert '生成场景' in result['message'] and '等待执行' in result['message']
    assert '需求理解' not in str(result['parts']) + result['message']
    assert '已经生成' not in result['message']
    receipt = result['actions'][0]['result']['control_receipt']
    assert receipt['action'] == 'retry' and receipt['run_id'] == run['id']
    assert receipt['stage'] == 'scenarios' and receipt['status'] == 'queued'
    assert receipt['message'] == result['message']
    assert pipeline.calls == [('retry', run['id'])]
    assert store.get('message', 'reply:' + result['id'])['content'] == result['message']


@pytest.mark.asyncio
async def test_read_and_control_turn_keeps_explanatory_final_text(store_chat):
    store, chat = store_chat
    failed_run(store, chat)
    pipeline = ControlPipeline(store)
    explanation = '当前 Profile 保留原列配置；场景生成的重试请求已提交，正在等待执行。'
    gateway = Gateway([
        AIMessage(content='', tool_calls=[call('read', 'read_profile_tool', {})]),
        AIMessage(content='', tool_calls=[call('retry', 'control_pipeline_tool', {'action': 'retry'})]),
        AIMessage(content=explanation),
    ])
    result = await NativeChatAgent(store, gateway, object(), pipeline).submit(chat['id'],
        {'content': '先说明当前模板，再重试当前步骤', 'client_message_id': 'explain-and-retry'})
    assert result['status'] == 'succeeded' and result['message'] == explanation
    assert result['actions'][-1]['result']['control_receipt']['stage'] == 'scenarios'


@pytest.mark.asyncio
async def test_knowledge_rebuild_receipt_uses_replacement_run_and_new_stage(store_chat):
    store, chat = store_chat
    run = failed_run(store, chat)
    pipeline = ControlPipeline(store, replacement=True)
    tools = {tool.name: tool for tool in build_tools(store, object(), pipeline, chat, {},
        {'run_id': run['id'], 'kind': 'failed'})}
    result = await tools['control_pipeline_tool'].ainvoke({'action': 'retry'})
    receipt = result['control_receipt']
    assert receipt['run_id'] != run['id'] and result['run_id'] == receipt['run_id']
    assert receipt['stage'] == 'understand' and '重新理解需求' in receipt['message']
    assert '生成场景' not in receipt['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('action,status,expected', [
    ('pause', 'running', '当前步骤完成后暂停'),
    ('cancel', 'cancelled', '已取消'),
])
async def test_pause_and_cancel_receipts_do_not_claim_generation_completed(store_chat, action, status, expected):
    store, chat = store_chat
    run = failed_run(store, chat)
    store.update_run(run['id'], status='running', stage='scenarios')
    pipeline = ControlPipeline(store)
    tools = {tool.name: tool for tool in build_tools(store, object(), pipeline, chat, {},
        {'run_id': run['id'], 'kind': 'busy'})}
    result = await tools['control_pipeline_tool'].ainvoke({'action': action})
    assert expected in result['message']
    assert result['control_receipt']['status'] == status
    assert result['control_receipt']['stage'] == 'scenarios'
