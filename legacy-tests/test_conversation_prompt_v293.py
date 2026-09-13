"""The displayed prompt is the only object a short composer assent approves."""
import copy
from concurrent.futures import ThreadPoolExecutor

from test_conversation_journey_v290 import conversation_journey
from test_manual_journey_v271 import upload
from test_backend_api import until


def prompt(j):
    response = j.client.get('/api/chats/' + j.chat['id'])
    assert response.status_code == 200
    return response.json()['conversation_prompt']


def reply(j, text, current, key, **extra):
    result = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json={
        'client_message_id': key, 'content': text, 'reply_to': current['id'], **extra})
    assert result.status_code == 200, result.text
    return result.json()


def test_assent_adopts_answers_then_each_later_prompt_advances_one_gate(conversation_journey):
    j = conversation_journey
    upload(j, 'login-requirements.md')
    started = j.turn('根据需求生成用例，每一步都请我确认。', 'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    drafts = copy.deepcopy(j.store.list('clarification_draft'))
    current = prompt(j)
    assert current['kind'] == 'clarification'
    assert current['questions'][0]['suggestion']
    assert prompt(j) == current
    assert j.store.list('clarification_draft') == drafts
    count = len([call for call in j.model.calls if call[0] == 'conversation_turn'])
    result = reply(j, '同意', current, 'adopt-answers', as_requirement=True)
    assert result['status'] == 'succeeded', result
    assert [a['name'] for a in result['actions']] == ['clarification.adopt']
    run, _ = j.gate(run, 'strategy_review')
    assert len([call for call in j.model.calls if call[0] == 'conversation_turn']) == count
    assert prompt(j)['kind'] == 'strategy_review'
    assert prompt(j)['id'] != current['id']
    old = reply(j, '同意', current, 'duplicate-old-answer')
    assert old['actions'] == []
    j.gate(run, 'strategy_review')
    j.model.turn_decision = {'actions': [], 'message': '刚才的问题用于明确锁定时长。'}
    explanation = reply(j, '解释一下刚才那个问题。', current, 'explain-old-prompt')
    assert explanation['message'] == '刚才的问题用于明确锁定时长。'
    j.gate(run, 'strategy_review')
    current = prompt(j)
    result = reply(j, '可以', current, 'approve-understanding')
    assert [a['name'] for a in result['actions']] == ['workflow.continue']
    run, _ = j.gate(run, 'scenario_review')
    old = reply(j, '继续', current, 'duplicate-old-gate')
    assert old['actions'] == []
    j.gate(run, 'scenario_review')
    assert not [a for a in j.store.list('artifact', chat_id=j.chat['id']) if a['type'] == 'cases']


def test_question_about_prompt_does_not_confirm_it(conversation_journey):
    j = conversation_journey
    upload(j, 'login-requirements.md')
    started = j.turn('根据需求生成测试用例', 'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    current = prompt(j)
    j.model.turn_decision = {'actions': [], 'message': '锁定时长影响边界用例的预期结果。'}
    result = reply(j, '为什么需要确认这个问题？', current, 'why-question')
    assert result['actions'] == []
    assert prompt(j)['id'] == current['id']
    j.gate(run, 'clarification')
    context = next(context for task, context in reversed(j.model.calls) if task == 'conversation_turn')
    assert context['conversation_prompt']['id'] == current['id']


def test_parallel_agreements_cannot_approve_two_stages(conversation_journey):
    j = conversation_journey
    upload(j, 'login-requirements.md')
    started = j.turn('生成用例并逐步人工确认', 'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    reply(j, '同意', prompt(j), 'accept-first')
    run, _ = j.gate(run, 'strategy_review')
    current = prompt(j)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reply, j, '同意', current, 'parallel-' + str(index)) for index in range(2)]
        results = [future.result() for future in futures]
    assert sum(result['status'] == 'succeeded' and any(
        action['name'] == 'workflow.continue' and action['status'] == 'succeeded'
        for action in result['actions']) for result in results) == 1
    j.gate(run, 'scenario_review')
    assert not [a for a in j.store.list('artifact', chat_id=j.chat['id']) if a['type'] == 'cases']


def test_proposal_agreement_only_applies_preview_and_returns_to_gate(conversation_journey):
    j = conversation_journey
    upload(j, 'login-requirements.md')
    started = j.turn('逐阶段确认生成用例', 'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    reply(j, '同意', prompt(j), 'adopt')
    run, _ = j.gate(run, 'strategy_review')
    reply(j, '同意', prompt(j), 'understand')
    run, scenes = j.gate(run, 'scenario_review')
    j.turn('先预览第一个场景的标题修改，不继续生成。', 'artifact.preview',
        artifact_id=scenes['id'], artifact_revision=scenes['revision'], selected_ids=[scenes['items'][0]['id']])
    current = prompt(j)
    assert current['kind'] == 'proposal'
    result = reply(j, '同意', current, 'apply-preview')
    assert [action['name'] for action in result['actions']] == ['artifact.apply']
    run, revised = j.gate(run, 'scenario_review')
    assert revised['revision'] == scenes['revision'] + 1
    assert prompt(j)['kind'] == 'scenario_review'
    assert reply(j, '同意', current, 'stale-preview')['actions'] == []
    assert j.artifact(revised['id'])['revision'] == revised['revision']


def test_source_prompt_does_not_promote_samples_on_vague_agreement(conversation_journey):
    j = conversation_journey
    response = j.client.post('/api/chats/' + j.chat['id'] + '/sources/text', json={
        'name': '用例格式示例', 'text': '标题、步骤和预期结果。', 'role': 'example'})
    assert response.status_code == 200
    source = response.json()
    started = j.turn('生成测试用例', 'workflow.start', {'stop_after': 'complete', 'source_ids': [source['id']]})
    run = until(j.client, started['actions'][0]['result']['run'])
    assert run['interrupt']['type'] == 'source_review'
    current = prompt(j)
    assert current['choices'][0]['id'] == source['id']
    result = reply(j, '同意', current, 'source-ack')
    assert result['actions'] == []
    assert j.store.run(run['id'])['interrupt']['type'] == 'source_review'
    assert j.store.get('source', source['id'])['role'] == 'example'


def test_template_prompt_applies_exact_suggestion_once_and_keeps_workflow_waiting(conversation_journey):
    from test_conversation_project_v260 import dual_template
    j = conversation_journey
    upload(j, 'login-requirements.md')
    started = j.turn('逐阶段确认生成用例', 'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    reply(j, '同意', prompt(j), 'adopt-template-flow')
    run, _ = j.gate(run, 'strategy_review')
    old_gate = prompt(j)
    source = j.client.post('/api/chats/' + j.chat['id'] + '/sources/text', json={
        'name': '场景及用例格式模板', 'role': 'example', 'text': '编号、场景名称、用例标题、步骤、预期结果'}).json()
    original = j.model.generate
    async def generate(task, context):
        if task == 'learn_template':
            j.model.calls.append((task, copy.deepcopy(context)))
            return dual_template()
        return await original(task, context)
    j.model.generate = generate
    j.model.turn_decision = {'actions': [{'name': 'project.learn_template', 'arguments': {
        'source_ids': [source['id']], 'apply': False}}]}
    learned = reply(j, '先学习这个模板，展示建议让我确认。', old_gate, 'learn-template')
    assert learned['status'] == 'needs_confirmation', learned
    current = prompt(j)
    assert current['kind'] == 'profile'
    profile_before = j.store.get('profile', current['profile_id'])
    j.model.turn_decision = {'actions': [], 'message': '模板只影响格式与字段写法。'}
    assert reply(j, '模板会修改哪些设置？', current, 'explain-template')['actions'] == []
    assert j.store.get('profile', current['profile_id'])['version'] == profile_before['version']
    adopted = reply(j, '同意', current, 'apply-template')
    assert adopted['status'] == 'succeeded', adopted
    assert [action['name'] for action in adopted['actions']] == ['project.apply_profile']
    assert j.store.get('profile', current['profile_id'])['version'] == profile_before['version'] + 1
    assert prompt(j)['kind'] == 'strategy_review'
    assert prompt(j)['id'] != old_gate['id']
    assert reply(j, '同意', current, 'repeat-template')['actions'] == []
    assert reply(j, '同意', old_gate, 'repeat-old-gate')['actions'] == []
    j.gate(run, 'strategy_review')
