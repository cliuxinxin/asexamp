import pytest

from tcg.schemas import DomainError
from test_native_pipeline_v300 import setup as pipeline_setup, settled
from test_native_tools_v300 import setup
from test_native_chat_safety_v300 import store_chat, Gateway, call


@pytest.mark.asyncio
async def test_runtime_keeps_answer_submission_separate_from_stage_approval(tmp_path):
    store, chat, business, runtime = pipeline_setup(tmp_path, questions=True)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        prompt = run['interrupt']['prompt_id']
        with pytest.raises(DomainError, match='澄清答案'):
            await runtime.resume(run['id'], 'approved', prompt)
        assert (await runtime.snapshot(run['id']))['interrupt']['prompt_id'] == prompt
        with pytest.raises(DomainError, match='澄清答案'):
            await runtime.resume(run['id'], 'clarify', prompt)
        await runtime.resume(run['id'], 'clarify', prompt, {'answers': {'Q1': '显示错误提示。'}})
        run = await settled(runtime, run['id'])
        assert run['interrupt']['type'] == 'strategy_review'
        with pytest.raises(DomainError, match='不是澄清'):
            await runtime.resume(run['id'], 'clarify', run['interrupt']['prompt_id'],
                                 {'answers': {'Q1': '再次回答'}})
        assert not any(call[0] == 'scenarios' for call in business.calls)
    finally:
        await runtime.stop()
        store.close()


def test_explicit_empty_questions_do_not_resurrect_legacy_aliases(tmp_path):
    store, _, _, runtime = pipeline_setup(tmp_path)
    try:
        assert runtime._questions({'report': {'questions': [],
            'clarification_questions': ['旧问题'], 'clarifications': ['更旧的问题']}}) == []
        assert runtime._questions({'report': {'clarification_questions': ['旧问题']}})[0]['question'] == '旧问题'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_stage_approval_tool_cannot_implicitly_adopt_clarification_suggestions(setup):
    prompt = {**setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': '锁定多久？', 'suggestion': '5 分钟'}]}
    result = await setup.make(current_prompt=prompt)['resume_pipeline_tool'].ainvoke({})
    assert result['status'] == 'needs_input'
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_answer_tool_matches_question_ids_and_preserves_custom_text_over_adoption(setup):
    prompt = {**setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': '锁定多久？', 'suggestion': '5 分钟'},
        {'id': 'Q2', 'question': '错误提示？', 'suggestion': '请重试'}]}
    tools = setup.make(current_prompt=prompt)
    invalid = await tools['answer_clarification_tool'].ainvoke({'answers': {'未知问题': '答案'}})
    assert invalid['status'] == 'needs_input'
    assert not setup.pipeline.calls
    result = await tools['answer_clarification_tool'].ainvoke({
        'answers': {'锁定多久？': '10 分钟'}, 'adopt_suggestions': True})
    assert result['status'] == 'succeeded'
    assert setup.pipeline.calls[0][3]['answers'] == {'Q1': '10 分钟', 'Q2': '请重试'}


@pytest.mark.parametrize('kind,allowed,forbidden', [
    ('question', ['analyze_artifact_tool', 'read_artifact_tool'],
     ['resume_pipeline_tool', 'answer_clarification_tool', 'modify_artifact_tool', 'start_pipeline_tool']),
    ('clarification', ['answer_clarification_tool', 'read_artifact_tool'],
     ['resume_pipeline_tool', 'modify_artifact_tool', 'start_pipeline_tool']),
    ('confirm', ['resume_pipeline_tool', 'read_artifact_tool'],
     ['answer_clarification_tool', 'modify_artifact_tool', 'start_pipeline_tool']),
])
def test_explicit_quick_reply_exposes_only_the_selected_capabilities(setup, kind, allowed, forbidden):
    tools = setup.make({'reply_kind': kind})
    assert all(name in tools for name in allowed)
    assert all(name not in tools for name in forbidden)


@pytest.mark.asyncio
async def test_question_quick_reply_rejects_model_attempt_to_write_project(store_chat):
    from langchain_core.messages import AIMessage
    from tcg.native_chat import NativeChatAgent
    store, chat = store_chat
    gateway = Gateway([AIMessage(content='', tool_calls=[call('wrong-action', 'add_knowledge_tool',
        {'content': '模型误以为这是一条已确认规则'})]), AIMessage(content='已保存项目规则')])
    agent = NativeChatAgent(store, gateway, object(), object())
    result = await agent.submit(chat['id'], {'content': '先解释，暂不确认',
        'reply_kind': 'question', 'client_message_id': 'read-only-quick-reply'})
    assert result['status'] == 'needs_input'
    assert not store.list('source', chat_id=chat['id'])
    assert not store.runs(chat_id=chat['id'])
