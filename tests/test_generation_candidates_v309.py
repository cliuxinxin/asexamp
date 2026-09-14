"""An exhausted native stage exposes quarantined outputs without advancing the graph."""
import copy
import time
from test_native_journey_v300 import native_journey


def test_three_repairs_preserve_readable_candidate_and_correct_failed_stage(native_journey):
    j = native_journey
    original = j.gateway.generate_native
    calls = []
    async def model(task, context, schema, instruction):
        if task == 'understand_requirements':
            result = await original(task, context, schema, instruction)
            second = copy.deepcopy(result['items'][0])
            second.update(id='REQ-2', title='退出登录', description='退出后不能访问账户。')
            result['items'].append(second)
            return result
        if task == 'generate_scenarios':
            calls.append(copy.deepcopy(context))
            return {'items': [{'id': 'SC-1', 'title': '有效凭证登录成功', 'description': '登录后显示首页',
                'priority': 'P1', 'requirement_ids': ['REQ-1'],
                'refs': context['analysis'][0]['refs']}], 'report': {'summary': '只生成了一个场景'}}
        return await original(task, context, schema, instruction)
    j.gateway.generate_native = model
    j.turn('根据需求生成并评审用例', 'start_pipeline_tool', {'mode': 'auto'}, mode='auto')
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = j.snapshot()
        if snapshot['runs'][0]['status'] == 'failed':
            break
        time.sleep(.02)
    run = snapshot['runs'][0]
    assert run['status'] == 'failed'
    assert len(calls) == 4
    assert calls[0] != calls[1]
    assert 'REQ-2' in str(calls[1].get('validation_repair'))
    failure = next(message for message in snapshot['messages'] if message.get('metadata', {}).get('pipeline_failure'))
    assert '连接问题' not in failure['content']
    part = next(part for part in failure['metadata']['turn_response']['parts'] if part['type'] == 'generation_candidate')
    assert part['stage'] == 'scenarios' and part['attempts'] == 3
    path = '/api/runs/' + run['id'] + '/candidates/' + part['candidate_id']
    response = j.client.get(path)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert candidate['items'][0]['id'] == 'SC-1'
    assert candidate['issues'][0]['missing_input_ids'] == ['REQ-2']
    assert len(candidate['history']) == 4
    assert candidate['request_count'] == 4
    assert j.client.get(path + '/download').json()['history'] == candidate['history']
    assert snapshot['conversation_prompt']['stage'] == 'scenarios'
    assert snapshot['conversation_prompt']['candidate_id'] == part['candidate_id']
    assert all(a['type'] == 'analysis' for a in j.app.state.store.list('artifact', chat_id=j.chat['id']))
    assert 'generate_cases' not in j.counts()
    other = j.app.state.store.create_chat(j.project['id'], 'Other')
    _, other_run = j.app.state.store.create_run(other['id'], {'content': '测试', 'intent': 'generate_case', 'mode': 'auto', 'experience': 'native'})
    assert j.client.get('/api/runs/' + other_run['id'] + '/candidates/' + part['candidate_id']).status_code == 404

    # A later transport failure must not inherit this stage's failed-draft pointer.
    from tcg.schemas import DomainError
    async def later_model(task, context, schema, instruction):
        if task == 'generate_scenarios':
            result = await original(task, context, schema, instruction)
            result['items'][0]['requirement_ids'] = [row['id'] for row in context['analysis']]
            return result
        if task == 'generate_cases':
            error = DomainError('模型服务暂时无法连接')
            error.category = 'connection'
            raise error
        return await original(task, context, schema, instruction)
    j.gateway.generate_native = later_model
    j.app.state.store.update_run(other_run['id'], status='cancelled')
    j.turn('重试当前步骤', 'control_pipeline_tool', {'run_id': run['id'], 'action': 'retry'}, mode='auto')
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = j.snapshot()
        if snapshot['runs'][0]['status'] == 'failed':
            break
        time.sleep(.02)
    prompt = snapshot['conversation_prompt']
    assert prompt['stage'] == 'cases'
    assert not prompt.get('candidate_id')
    assert j.client.get(path).json()['items'][0]['id'] == 'SC-1'


def test_reference_patch_never_replaces_full_case_and_stage_uses_current_run(native_journey):
    from tcg.generation_candidates import save_candidate, read_candidate
    from tcg.generation_repair import GenerationRepairExhausted
    from tcg.schemas import DomainError
    j = native_journey
    _, run = j.app.state.store.create_run(j.chat['id'], {'content': '测试', 'intent': 'generate_case',
                                                          'mode': 'auto', 'experience': 'native'})
    run = j.app.state.store.update_run(run['id'], stage='cases', failed_node='scenarios')
    full = {'items': [{'id': 'TC-1', 'title': '完整用例', 'steps': [{'action': '登录', 'expected': '显示首页'}],
                       'refs': ['unknown']}], 'report': {}}
    history = [{'attempt': 1, 'task': 'generate_cases', 'result': copy.deepcopy(full)}]
    history.extend({'attempt': n, 'task': 'repair_evidence_refs', 'result': {'items': [
        {'id': 'TC-1', 'refs': ['unknown'], 'support': 'unsupported', 'reason': '没有依据'}]}}
        for n in range(2, 5))
    error = DomainError('无效引用')
    error.category = 'invalid_reference'
    exc = GenerationRepairExhausted('generate_cases', full, history, error)
    part = save_candidate(j.app.state.store, run, exc)
    candidate = read_candidate(j.app.state.store, run['id'], part['candidate_id'])
    assert candidate['items'] == full['items']
    assert candidate['stage'] == 'cases'
    assert candidate['history'][-1]['task'] == 'repair_evidence_refs'
    assert not j.app.state.store.list('artifact', chat_id=j.chat['id'])
