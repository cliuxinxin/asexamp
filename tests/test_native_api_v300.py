"""Compatibility risks at the native checkpoint / existing React boundary."""
import copy

from test_native_journey_v300 import native_journey


def test_clarification_projection_adoption_and_direct_revision_reanchor(native_journey):
    j = native_journey
    original = j.gateway.generate_native
    question = '账号锁定多长时间？'
    answer = '锁定10分钟，期满自动解锁。'

    async def generate(task, context, schema, instruction):
        if task == 'revise_artifact':
            j.gateway.generations.append((task, copy.deepcopy(context)))
            rows = copy.deepcopy(context['items'])
            rows[0]['description'] += answer
            rows[0]['refs'] = [e['id'] for e in context['evidence']]
            return {'items': rows, 'report': {'summary': '已采用澄清。', 'questions': []}}
        result = await original(task, context, schema, instruction)
        if task == 'understand_requirements':
            result['report'].update(questions=[question], question_suggestions=[{
                'question': question, 'answer': answer, 'basis': '待用户确认的假设。',
                'confidence': 'assumption', 'refs': []}])
        return result

    j.gateway.generate_native = generate
    j.turn('生成测试设计，每一步请我确认。', 'start_pipeline_tool')
    run, analysis, prompt = j.gate('clarification')
    assert run['interrupt']['questions'] == [question]
    assert run['interrupt']['question_suggestions'][0]['answer'] == answer
    assert prompt['questions'][0]['question'] == question
    assert prompt['questions'][0]['suggestion'] == answer
    assert not prompt['questions'][0].get('answer')
    j.turn('同意采用建议，并保存到项目。', 'answer_clarification_tool',
           {'adopt_suggestions': True}, reply=prompt)
    run, updated, understanding = j.gate('strategy_review')
    assert updated['revision'] == analysis['revision'] + 1
    assert not updated['report']['questions']
    assert 'questions' not in understanding
    assert j.counts().get('generate_scenarios', 0) == 0
    shared = j.client.get('/api/projects/' + j.project['id'] + '/shared-context').json()
    assert answer in str(shared)

    # The retained edit/restore API uses the same native gate and optimistic revision.
    rows = copy.deepcopy(updated['items'])
    rows[0]['title'] = '已明确锁定规则的登录需求'
    response = j.client.put('/api/artifacts/' + updated['id'],
        json={'expected_revision': updated['revision'], 'items': rows})
    assert response.status_code == 200, response.text
    _, changed, current = j.gate('strategy_review')
    assert changed['revision'] == updated['revision'] + 1 and current['id'] != understanding['id']
    response = j.client.post('/api/artifacts/' + updated['id'] + '/restore',
        json={'expected_revision': changed['revision'], 'revision': updated['revision']})
    assert response.status_code == 200, response.text
    _, restored, _ = j.gate('strategy_review')
    assert restored['items'] == updated['items']
    assert j.counts().get('generate_scenarios', 0) == 0
