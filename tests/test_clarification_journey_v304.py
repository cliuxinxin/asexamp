import copy
import time

from tcg.schemas import DomainError
from test_native_journey_v300 import native_journey


Q1, Q2 = '账号锁定多久？', '失败后显示什么？'
A1, A2 = '锁定 10 分钟。', '提示请稍后重试。'


def echoing_gateway(j, fail_once=False):
    original = j.gateway.generate_native
    failures = [fail_once]
    async def generate(task, context, schema, instruction):
        if task == 'revise_artifact':
            j.gateway.generations.append((task, copy.deepcopy(context)))
            if failures[0]:
                failures[0] = False
                raise DomainError('受控模型失败，用于验证重试')
            rows = copy.deepcopy(context['items'])
            rows[0]['description'] = '注册用户登录。' + '\n'.join(
                e['text'] for e in context['evidence'] if e.get('role') == 'clarification')
            rows[0]['refs'] = [e['id'] for e in context['evidence']]
            return {'items': rows, 'report': {'summary': '已根据答案更新理解。',
                'questions': [Q1, Q2], 'clarification_questions': [Q1, Q2],
                'clarification_note': '模型仍保留旧问题，用于复现回环。'}}
        result = await original(task, context, schema, instruction)
        if task == 'understand_requirements':
            result['report'].update(questions=[Q1, Q2], question_suggestions=[
                {'question': q, 'answer': a, 'basis': '待确认假设', 'confidence': 'assumption', 'refs': []}
                for q, a in [(Q1, A1), (Q2, A2)]])
        return result
    j.gateway.generate_native = generate


def quick(j, prompt, kind, content, tool_name, arguments=None):
    j.sequence += 1
    j.gateway.next_call = (tool_name, arguments or {})
    response = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json={
        'content': content, 'mode': 'hitp', 'client_message_id': 'quick-' + str(j.sequence),
        'reply_to': prompt['id'], 'reply_kind': kind})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'succeeded', response.text
    return response.json()


def test_same_chat_answers_close_echoed_questions_then_separately_confirm(native_journey):
    j = native_journey
    echoing_gateway(j)
    j.turn('生成用例，每一步确认', 'start_pipeline_tool')
    run, initial, prompt = j.gate('clarification')
    quick(j, prompt, 'clarification', '仅提交第一题答案', 'answer_clarification_tool', {'answers': {'Q1': A1}})
    _, partial, prompt = j.gate('clarification')
    assert partial['id'] == initial['id'] and partial['revision'] == initial['revision'] + 1
    assert [q['question'] for q in prompt['questions']] == [Q2]
    quick(j, prompt, 'question', '解释这些问题，暂不采用答案', 'analyze_artifact_tool',
          {'artifact_id': partial['id'], 'instruction': '解释当前需求理解'})
    _, unchanged, same_prompt = j.gate('clarification')
    assert unchanged['revision'] == partial['revision'] and same_prompt['id'] == prompt['id']
    quick(j, prompt, 'clarification', '采用剩余澄清建议', 'answer_clarification_tool', {'adopt_suggestions': True})
    _, final, prompt = j.gate('strategy_review')
    assert final['report']['questions'] == [] and final['report']['question_suggestions'] == []
    assert j.counts().get('generate_scenarios', 0) == 0
    quick(j, prompt, 'question', '先解释需求理解，暂不确认', 'analyze_artifact_tool',
          {'artifact_id': final['id'], 'instruction': '解释当前需求理解'})
    _, unchanged, same_prompt = j.gate('strategy_review')
    assert unchanged['revision'] == final['revision'] and same_prompt['id'] == prompt['id']
    quick(j, prompt, 'confirm', '确认当前需求理解并继续', 'resume_pipeline_tool')
    _, scenarios, _ = j.gate('scenario_review')
    assert A1 in scenarios['items'][0]['description'] and A2 in scenarios['items'][0]['description']
    assert j.counts()['understand_requirements'] == 1
    shared = j.client.get('/api/projects/' + j.project['id'] + '/shared-context').json()
    assert A1 in str(shared) and A2 in str(shared)
    assert len(j.snapshot()['runs']) == 1


def test_failed_clarification_retries_existing_run_without_duplicate_shared_sources(native_journey):
    j = native_journey
    echoing_gateway(j, fail_once=True)
    j.turn('生成用例，每一步确认', 'start_pipeline_tool')
    run, initial, prompt = j.gate('clarification')
    quick(j, prompt, 'clarification', '采用全部澄清建议', 'answer_clarification_tool', {'adopt_suggestions': True})
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = j.snapshot()
        if state['runs'][0]['status'] == 'failed':
            break
        time.sleep(.01)
    else:
        raise AssertionError(state)
    count = len([s for s in state['sources'] if s['role'] == 'clarification'])
    assert count == 1
    j.turn('重试当前步骤', 'control_pipeline_tool', {'action': 'retry'}, reply=state['conversation_prompt'])
    resumed, artifact, prompt = j.gate('strategy_review')
    assert resumed['id'] == run['id'] and artifact['id'] == initial['id']
    assert artifact['report']['questions'] == []
    assert len([s for s in j.snapshot()['sources'] if s['role'] == 'clarification']) == count
    assert j.counts()['understand_requirements'] == 1
