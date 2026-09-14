"""Real HTTP/SQLite/native graph journey for conversational additions and exports."""
import copy
import io
import json

from openpyxl import load_workbook
from test_native_journey_v300 import native_journey


def test_dialogue_addition_returns_to_gate_and_profile_edit_exports_without_upload(native_journey):
    j = native_journey
    original = j.gateway.generate_native
    request = '增加一个并发登录场景：同账号第二次登录后，第一次登录的会话必须失效。'

    async def model(task, context, schema, instruction):
        if task == 'revise_artifact' and context.get('add_only'):
            j.gateway.generations.append((task, copy.deepcopy(context)))
            return {'items': copy.deepcopy(context['items']) + [{
                'id': 'SC-CONCURRENT', 'title': '第二次登录使旧会话失效', 'description': request,
                'priority': 'P1', 'requirement_ids': [context['addition_parent_id']] if context['addition_parent_id'] else [],
                'refs': context['dialogue_evidence_ids']}], 'report': {'summary': '已准备新增并发登录场景。'}}
        if task == 'generate_cases':
            j.gateway.generations.append((task, copy.deepcopy(context)))
            rows = [{'id': 'TC-' + row['id'], 'title': row['title'],
                'description': row['description'], 'scenario_id': row['id'], 'type': 'Business',
                'priority': 'P1', 'preconditions': '已有注册账号',
                'steps': [{'action': '使用同一账号第二次登录' if row['id'] == 'SC-CONCURRENT' else '使用有效凭证登录',
                           'expected': '第一次登录的会话失效' if row['id'] == 'SC-CONCURRENT' else '登录成功'}],
                'refs': row['refs']} for row in context['scenarios']]
            # Reproduce the reported malformed reference without corrupting other rows.
            rows[-1]['refs'] = ['string']
            return {'items': rows, 'report': {'summary': '生成登录测试用例。'}}
        if task == 'repair_evidence_refs':
            j.gateway.generations.append((task, copy.deepcopy(context)))
            evidence = next(e for e in context['evidence'] if e['text'] == request)
            return {'items': [{'id': row['id'], 'refs': [evidence['id']],
                'support': [{'ref': evidence['id'], 'quote': evidence['text']}],
                'reason': '用户明确要求第二次登录使旧会话失效。'} for row in context['items']]}
        return await original(task, context, schema, instruction)

    j.gateway.generate_native = model
    j.turn('根据上传的登录需求生成用例，逐步确认。', 'start_pipeline_tool', {'mode': 'hitp'})
    run, analysis, prompt = j.gate('strategy_review')
    j.turn('同意，生成场景', 'resume_pipeline_tool', reply=prompt)
    _, scenarios, prompt = j.gate('scenario_review')
    j.turn('先估算这些场景需要多少条用例，不生成。', 'estimate_workload_tool',
           {'artifact_id': scenarios['id']}, reply=prompt)
    assert not j.counts().get('generate_cases')
    sources_before = j.snapshot()['sources']
    staged = j.turn(request, 'modify_artifact_tool',
        {'artifact_id': scenarios['id'], 'instruction': request, 'add': True},
        reply=prompt, status='needs_confirmation')
    preview_prompt = j.snapshot()['conversation_prompt']
    assert preview_prompt['kind'] == 'artifact_proposal'
    assert staged['parts'][0]['type'] == 'artifact_proposal'
    proposal = j.app.state.store.get('artifact_proposal', staged['parts'][0]['proposal_id'])
    assert len(proposal['changes']) == 1
    receipt = json.loads(j.gateway.tool_results[-1].content)
    assert receipt['parts'][0]['type'] == 'artifact_proposal'
    assert 'changes' not in receipt['parts'][0]
    assert proposal['changes'][0]['op'] == 'add'
    assert proposal['changes'][0]['after']['id'] == 'SC-CONCURRENT'
    assert j.artifact(scenarios['id']) == scenarios
    assert j.artifact(analysis['id']) == analysis
    assert j.snapshot()['sources'] == sources_before
    j.turn('同意应用这次新增', 'workspace_save', reply=preview_prompt)
    _, updated, prompt = j.gate('scenario_review')
    assert updated['items'][0] == scenarios['items'][0]
    added = updated['items'][1]
    assert added['id'] == 'SC-CONCURRENT'
    upstream = j.artifact(analysis['id'])
    assert upstream == analysis
    assert added['requirement_ids'] == []
    assert added['_independent_origin']['reason']
    assert added['_independent_origin']['source_id'] + '#P1' in added['refs']
    assert not j.counts().get('generate_cases')
    j.turn('场景确认，继续生成用例', 'resume_pipeline_tool', reply=prompt)
    _, cases, review_prompt = j.gate('case_result_review')
    assert {c['scenario_id'] for c in cases['items']} == {s['id'] for s in updated['items']}
    assert j.counts()['generate_cases'] == 1 and j.counts()['repair_evidence_refs'] == 1
    repaired = next(c for c in cases['items'] if c['scenario_id'] == 'SC-CONCURRENT')
    assert repaired['refs'] == added['refs'] and repaired['steps'][0]['expected'] == '第一次登录的会话失效'
    assert any(issue.get('code') == 'reference_repaired' for issue in cases['report']['issues'])
    assert j.counts()['review_cases'] == 1
    assert cases['revision'] == 1
    assert review_prompt['review']['summary'] == '已核对步骤与预期结果。'
    j.turn('评审结果同意，完成', 'resume_pipeline_tool', reply=review_prompt)
    j.completed()
    reviewed = j.artifact(cases['id'])
    assert reviewed['revision'] == cases['revision'] + 1
    assert j.artifact(analysis['id']) == analysis
    assert j.artifact(scenarios['id']) == updated

    calls_before = len(j.gateway.generations)
    j.turn('导出格式加一列执行状态，人工填写，不上传模板。', 'modify_profile_tool',
        {'upsert_columns': [{'field': 'status', 'header': '执行状态'}],
         'summary': '增加执行状态列，实际执行后人工填写。'}, status='needs_confirmation')
    profile_prompt = j.snapshot()['conversation_prompt']
    preview = j.client.get('/api/chats/' + j.chat['id'] + '/profile-change',
                           params={'prompt_id': profile_prompt['id']}).json()
    assert [c['key'] for c in preview['changes']] == ['excel_columns']
    j.turn('同意应用 Profile 更改', 'apply_profile_tool', reply=profile_prompt)
    assert len(j.gateway.generations) == calls_before
    options = j.client.get('/api/artifacts/' + reviewed['id'] + '/export-options',
                           params={'revision': reviewed['revision']}).json()
    assert options['default_profile_id']
    response = j.client.get('/api/artifacts/' + reviewed['id'] + '/export',
        params={'revision': reviewed['revision'], 'profile_id': options['default_profile_id']})
    assert response.status_code == 200
    cells = list(load_workbook(io.BytesIO(response.content)).active.values)
    assert cells[0][-1] == '执行状态' and all(row[-1] is None for row in cells[1:])
    old_options = j.client.get('/api/artifacts/' + cases['id'] + '/export-options',
                               params={'revision': cases['revision']}).json()
    assert old_options['default_profile_id'] is None
    j.turn('解释并总结这些用例验证了什么', 'analyze_artifact_tool',
           {'artifact_id': reviewed['id'], 'instruction': '解释测试内容'})
    assert j.snapshot()['runs'][0]['status'] == 'completed'
