"""Default case generation, visible review feedback and a second human review."""
import copy

from test_native_journey_v300 import native_journey
from tcg.native_views import review_opinions


def test_review_questions_and_scope_exclusions_are_visible_with_evidence():
    artifact = {'report': {'summary': '旧的草稿摘要', 'review_reports': [{
        'summary': '有待核实项和范围调整', 'issues': [], 'questions': ['锁定时长是多少？'],
        'excluded_scenarios': [{'scenario_id': 'SC-2', 'reason': '需求明确排除密码重置',
                                'refs': ['src#P3']}]}]}}
    visible = review_opinions(artifact)
    assert any('锁定时长是多少？' in item.get('detail', '') for item in visible['issues'])
    exclusion = next(item for item in visible['issues'] if 'SC-2' in item['title'])
    assert exclusion['detail'] == '需求明确排除密码重置'
    assert exclusion['refs'] == ['src#P3']


def test_auto_recognized_generation_includes_review_without_explicit_stop(native_journey):
    j = native_journey
    j.turn('根据上传的需求生成测试用例。', 'start_pipeline_tool', mode='auto')
    run = j.completed()
    assert run['stop_after'] == 'review'
    assert j.counts() == {'understand_requirements': 1, 'generate_scenarios': 1,
                          'generate_cases': 1, 'review_cases': 1}
    messages = [m for m in j.snapshot()['messages']
                if m.get('metadata', {}).get('stage') == 'reviewed']
    assert '已核对步骤与预期结果。' in messages[-1]['content']
    assert '根据本项目登录需求完成当前阶段。' not in messages[-1]['content']


def test_human_review_exposes_opinions_and_comments_require_fresh_confirmation(native_journey):
    j = native_journey
    generate = j.gateway.generate_native
    feedback = '登录成功，并显示登录后的首页。'
    review_inputs = []

    async def model(task, context, schema, instruction):
        result = await generate(task, context, schema, instruction)
        if task == 'review_cases':
            review_inputs.append(copy.deepcopy(context['cases']))
            result['report']['issues'] = [{'title': '预期结果需明确',
                'detail': '请确认成功登录后应展示的页面。',
                'case_ids': [context['cases'][0]['id']], 'refs': context['cases'][0]['refs']}]
        return result

    j.gateway.generate_native = model
    j.turn('根据上传需求生成测试用例，每一步等我确认。', 'start_pipeline_tool')
    for kind in ('strategy_review', 'scenario_review', 'case_draft_review'):
        run, artifact, prompt = j.gate(kind)
        j.turn('同意，继续', 'resume_pipeline_tool', {'run_id': run['id']}, reply=prompt)
    run, reviewed, prompt = j.gate('case_result_review')
    assert prompt['review']['summary'] == '已核对步骤与预期结果。'
    assert prompt['review']['issues'][0]['case_ids'] == ['TC-1']
    assert prompt['review']['scope']['reviewed_count'] == 1
    assert prompt['review']['scope']['total_count'] == 1
    assert prompt['artifact_revision'] == reviewed['revision']
    assert run['status'] == 'waiting'

    j.turn('先解释这条用例的步骤和预期。', 'analyze_artifact_tool',
           {'artifact_id': reviewed['id'], 'instruction': '解释步骤和预期'}, reply=prompt)
    assert j.gate('case_result_review')[2]['id'] == prompt['id']

    j.turn('我的补充意见：预期写明登录成功并显示首页，先修改，不要完成。',
           'modify_artifact_tool', {'artifact_id': reviewed['id'], 'item_id': 'TC-1',
            'new_values': {'steps': [{'action': '输入有效账号密码并登录', 'expected': feedback}]}},
           reply=prompt)
    run, changed, changed_prompt = j.gate('case_draft_review')
    assert changed['revision'] == reviewed['revision'] + 1
    assert changed['items'][0]['steps'][0]['expected'] == feedback
    assert 'review' not in changed_prompt
    assert j.counts()['review_cases'] == 1
    j.turn('确认修改后的草稿，继续评审。', 'resume_pipeline_tool',
           {'run_id': run['id']}, reply=changed_prompt)
    run, rereviewed, fresh_prompt = j.gate('case_result_review')
    assert review_inputs[-1][0]['steps'][0]['expected'] == feedback
    assert fresh_prompt['id'] != prompt['id']
    assert fresh_prompt['artifact_revision'] == rereviewed['revision']
    assert run['status'] == 'waiting'
    assert j.counts() == {'understand_requirements': 1, 'generate_scenarios': 1,
                          'generate_cases': 1, 'review_cases': 2, 'explain_artifact': 1}
    j.turn('评审结果确认，完成。', 'resume_pipeline_tool', {'run_id': run['id']}, reply=fresh_prompt)
    assert j.completed()['id'] == run['id']
