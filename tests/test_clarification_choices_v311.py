import copy

import pytest
from jsonschema import Draft202012Validator

from tcg.native_schemas import report_schema
from test_clarification_business_v304 import QUESTION_ONE, QUESTION_TWO, stubborn_model, with_questions
from test_native_business_v300 import setup
from test_native_tools_v300 import setup as tool_setup
from test_native_journey_v300 import native_journey
from test_clarification_journey_v304 import echoing_gateway, Q1, Q2


OPTIONS = [
    {'id': 'keep', 'label': '是，保留', 'answer': '发生错误后保留用户已输入的账号。'},
    {'id': 'clear', 'label': '否，清空', 'answer': '发生错误后清空用户已输入的账号。'},
]


def test_schema_accepts_explicit_options_without_making_them_required(setup):
    _, _, service, _ = setup
    report = {'summary': '待确认', 'questions': [QUESTION_ONE, QUESTION_TWO], 'question_suggestions': [
        {'question': QUESTION_ONE, 'answer': '暂按保留处理。', 'basis': '待确认假设',
         'refs': [], 'confidence': 'assumption', 'options': OPTIONS}]}
    Draft202012Validator(report_schema('analysis')).validate(report)
    normalized = service._suggestions(copy.deepcopy(report), [])
    assert normalized['question_suggestions'][0]['options'] == OPTIONS
    assert 'options' not in normalized['question_suggestions'][1]


@pytest.mark.parametrize('options', [OPTIONS[:1], [OPTIONS[0], OPTIONS[0]],
    [OPTIONS[0], {'id': 'clear', 'label': '否', 'answer': ''}]])
def test_invalid_option_groups_do_not_create_one_sided_choices(setup, options):
    _, _, service, _ = setup
    report = {'questions': [QUESTION_ONE], 'question_suggestions': [
        {'question': QUESTION_ONE, 'options': options}]}
    normalized = service._suggestions(report, [])
    assert 'options' not in normalized['question_suggestions'][0]


@pytest.mark.asyncio
async def test_one_selected_answer_retires_only_its_question_and_preserves_other_options(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    report = copy.deepcopy(analysis['report'])
    report['question_suggestions'][0]['options'] = copy.deepcopy(OPTIONS)
    analysis = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    stubborn_model(model)
    revised = await service.clarify(run, analysis, {QUESTION_TWO: '无需操作审计。'})
    assert revised['report']['questions'] == [QUESTION_ONE]
    assert revised['report']['question_suggestions'][0]['options'] == OPTIONS
    final = await service.clarify(run, revised, {QUESTION_ONE: OPTIONS[1]['answer']})
    assert final['report']['questions'] == []
    assert final['report']['question_suggestions'] == []
    sources = [s for s in store.list('source', project_id=run['project_id']) if s.get('_project_shared')]
    assert OPTIONS[1]['answer'] in str(sources)
    assert OPTIONS[0]['answer'] not in str(sources)


def test_projected_options_are_answered_individually_through_chat(native_journey):
    j = native_journey
    echoing_gateway(j)
    original = j.gateway.generate_native

    async def generate(task, context, schema, instruction):
        result = await original(task, context, schema, instruction)
        if task == 'understand_requirements':
            result['report']['question_suggestions'][1]['options'] = copy.deepcopy(OPTIONS)
        return result

    j.gateway.generate_native = generate
    j.turn('生成用例，每一步确认', 'start_pipeline_tool')
    _, _, prompt = j.gate('clarification')
    assert prompt['questions'][1]['options'] == OPTIONS
    j.gateway.next_call = ('answer_clarification_tool', {'adopt_suggestions': True})
    selected = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json={
        'content': '仅选择第二题：' + OPTIONS[1]['answer'], 'reply_to': prompt['id'],
        'reply_kind': 'clarification', 'client_message_id': 'choose-second-option', 'mode': 'hitp',
        'command': {'name': 'clarification.answer', 'arguments': {
            'answers': {prompt['questions'][1]['id']: OPTIONS[1]['answer']}}}})
    assert selected.status_code == 200 and selected.json()['status'] == 'succeeded', selected.text
    _, updated, remaining = j.gate('clarification')
    assert [question['question'] for question in remaining['questions']] == [Q1]
    assert updated['report']['questions'] == [Q1]
    assert j.counts().get('generate_scenarios', 0) == 0
    j.gateway.next_call = ('answer_clarification_tool', {'answers': {Q1: '锁定 15 分钟。'}})
    stale = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json={
        'content': '再选择旧题答案', 'reply_to': prompt['id'], 'reply_kind': 'clarification',
        'client_message_id': 'stale-option', 'mode': 'hitp'})
    assert stale.status_code == 200
    assert stale.json()['status'] == 'needs_input'
    assert j.gate('clarification')[1]['revision'] == updated['revision']


@pytest.mark.asyncio
async def test_button_command_cannot_be_broadened_to_all_answers_by_model(tool_setup):
    prompt = {**tool_setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': QUESTION_ONE, 'suggestion': '暂按保留处理。', 'options': OPTIONS},
        {'id': 'Q2', 'question': QUESTION_TWO, 'suggestion': '无需审计。'}]}
    tools = tool_setup.make({'reply_to': prompt['id'], 'reply_kind': 'clarification', 'command': {
        'name': 'clarification.answer', 'arguments': {'answers': {'Q1': OPTIONS[1]['answer']}}}},
        current_prompt=prompt)
    result = await tools['answer_clarification_tool'].ainvoke({
        'adopt_suggestions': True, 'answers': {'Q1': '模型改写用户答案', 'Q2': '模型额外采用'}})
    assert result['status'] == 'succeeded'
    assert tool_setup.pipeline.calls[0][3]['answers'] == {'Q1': OPTIONS[1]['answer']}


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [None, {'Q1': '不是当前选项'},
    {'Q1': OPTIONS[0]['answer'], 'Q2': '无需审计。'}])
async def test_malformed_button_command_never_falls_back_to_model_answers(tool_setup, answer):
    prompt = {**tool_setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': QUESTION_ONE, 'suggestion': '暂按保留处理。', 'options': OPTIONS},
        {'id': 'Q2', 'question': QUESTION_TWO, 'suggestion': '无需审计。'}]}
    tools = tool_setup.make({'reply_to': prompt['id'], 'reply_kind': 'clarification', 'command': {
        'name': 'clarification.answer', 'arguments': {'answers': answer}}}, current_prompt=prompt)
    result = await tools['answer_clarification_tool'].ainvoke({'adopt_suggestions': True})
    assert result['status'] == 'needs_input'
    assert not tool_setup.pipeline.calls
