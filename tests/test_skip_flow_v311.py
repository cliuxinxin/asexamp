"""Explicit direct-case routing keeps real evidence and native approvals intact."""
import asyncio
import copy

import pytest

from tcg.native_business import NativeBusiness
from tcg.pipeline import PipelineRuntime
from tcg.schemas import DomainError
from tcg.storage import Store
from test_native_business_v300 import NativeModel
from test_native_pipeline_v300 import agree, settled
from test_native_journey_v300 import native_journey


class DirectModel(NativeModel):
    questions = False

    async def generate_native(self, task, context, schema, instruction):
        if task == 'generate_cases' and context.get('generation_mode') == 'direct_requirements':
            self.calls.append((task, copy.deepcopy(context)))
            assert context['scenarios'] == []
            assert 'requirement_ids' in schema['properties']['items']['items']['required']
            return {'items': [{'id': 'C' + row['id'], 'title': row['title'],
                'description': row['description'], 'scenario_id': '', 'requirement_ids': [row['id']],
                'type': 'Business', 'priority': 'P1', 'preconditions': '功能可用',
                'steps': [{'action': '检查' + row['title'], 'expected': row['description']}],
                'refs': row['refs']} for row in context['analysis']], 'report': {'summary': '直接根据需求生成'}}
        result = await super().generate_native(task, context, schema, instruction)
        if task == 'understand_requirements' and not self.questions:
            result['report']['questions'] = []
        return result


@pytest.fixture
def direct_setup(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('Direct test')
    chat = store.create_chat(project['id'], 'Direct cases')
    store.add_source(chat['id'], 'req', 'primary', '登录成功显示首页。', [{'text': '登录成功显示首页。'}])
    model = DirectModel()
    yield store, chat, model, NativeBusiness(store, model)
    store.close()


@pytest.mark.asyncio
async def test_explicit_skip_at_understanding_restores_and_waits_for_review(direct_setup):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        analysis = store.get('artifact', run['current_artifact_id'])
        await runtime.stop()
        runtime = PipelineRuntime(store, business)
        await runtime.start()
        restored = await runtime.snapshot(run['id'])
        await runtime.resume(run['id'], 'skip_to_cases', restored['interrupt']['prompt_id'])
        waiting = await settled(runtime, run['id'])
        assert waiting['interrupt']['type'] == 'case_result_review', waiting.get('error')
        assert not [a for a in store.list('artifact', chat_id=chat['id']) if a['type'] == 'scenarios']
        cases = store.get('artifact', waiting['current_artifact_id'])
        assert cases['revision'] == 1
        assert cases['report']['lineage']['analysis_artifact_id'] == analysis['id']
        assert cases['report']['lineage']['generation_mode'] == 'direct_requirements'
        assert not cases['report']['lineage'].get('scenario_artifact_id')
        assert cases['items'][0]['scenario_id'] == ''
        assert cases['items'][0]['requirement_ids'] == [analysis['items'][0]['id']]
        assert cases['items'][0]['_independent_origin']['mode'] == 'direct_requirements'
        assert store.get('artifact', analysis['id']) == analysis
        from tcg.workspace_coverage import lineage_rows
        ancestry = lineage_rows(store, cases)[0]
        assert ancestry['scenario_skipped'] and not ancestry['stale']
        assert ancestry['status'] == 'linked' and ancestry['requirements'][0]['item'] == analysis['items'][0]
        assert (await agree(runtime, waiting))['status'] == 'completed'
        assert store.get('artifact', cases['id'])['revision'] == 2
        assert [t for t, _ in model.calls] == ['understand_requirements', 'generate_cases', 'review_cases']
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['auto', 'hitp'])
async def test_start_skip_and_draft_only_finishes_without_draft_or_review_gate(direct_setup, mode):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await runtime.start_run(chat['id'], {'mode': mode, 'skip_scenarios': True, 'stop_after': 'cases'})
        run = await settled(runtime, run['id'])
        if mode == 'hitp':
            assert run['interrupt']['type'] == 'strategy_review'
            run = await agree(runtime, run)
        assert run['status'] == 'completed', run.get('error')
        assert [t for t, _ in model.calls] == ['understand_requirements', 'generate_cases']
        assert store.get('artifact', run['current_artifact_id'])['revision'] == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_skip_cannot_bypass_clarification_or_wrong_gate(direct_setup):
    store, chat, model, business = direct_setup
    model.questions = True
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        assert run['interrupt']['type'] == 'clarification'
        with pytest.raises(DomainError, match='只有确认需求'):
            await runtime.resume(run['id'], 'skip_to_cases', run['interrupt']['prompt_id'])
        assert len(model.calls) == 1
        run = await agree(runtime, await agree(runtime, run))
        assert run['interrupt']['type'] == 'scenario_review'
        with pytest.raises(DomainError, match='只有确认需求'):
            await runtime.resume(run['id'], 'skip_to_cases', run['interrupt']['prompt_id'])
        assert not any(t == 'generate_cases' for t, _ in model.calls)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_normal_flow_and_explicit_review_rejection_preserve_cases(direct_setup):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        run = await agree(runtime, await agree(runtime, run))
        assert run['interrupt']['type'] == 'case_result_review'
        before = store.get('artifact', run['current_artifact_id'])
        proposal_id = run['interrupt']['proposal_id']
        await runtime.resume(run['id'], 'rejected', run['interrupt']['prompt_id'])
        done = await settled(runtime, run['id'])
        assert done['status'] == 'completed'
        assert store.get('artifact', before['id']) == before
        assert store.get('artifact_proposal', proposal_id)['status'] == 'rejected'
        assert [t for t, _ in model.calls] == ['understand_requirements', 'generate_scenarios', 'generate_cases', 'review_cases']
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_direct_generation_validates_real_requirement_references(direct_setup):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        analysis = store.get('artifact', run['current_artifact_id'])
        sources, _, evidence = business._evidence(analysis)
        context = business._context(store.run(run['id']), evidence, analysis=analysis['items'], scenarios=[],
                                    generation_mode='direct_requirements', previous_items=[])
        result = await model.generate_native('generate_cases', context,
            {'properties': {'items': {'items': {'required': ['requirement_ids']}}}}, '')
        result['items'][0]['requirement_ids'] = ['R-INVENTED']
        with pytest.raises(DomainError, match='不属于当前需求'):
            business._validate_submission('generate_cases', context, result)
        assert not [a for a in store.list('artifact', chat_id=chat['id']) if a['type'] == 'cases']
    finally:
        await runtime.stop()


@pytest.mark.parametrize('request_at_start', [False, True])
def test_http_conversation_skip_and_draft_request_stays_in_native_tools(native_journey, request_at_start):
    j = native_journey
    generate = j.gateway.generate_native
    async def direct(task, context, schema, instruction):
        if task == 'generate_cases' and context.get('generation_mode') == 'direct_requirements':
            j.gateway.generations.append((task, copy.deepcopy(context)))
            row = context['analysis'][0]
            return {'items': [{'id': 'TC-DIRECT', 'title': row['title'], 'description': row['description'],
                'scenario_id': '', 'requirement_ids': [row['id']], 'type': 'Business', 'priority': 'P1',
                'preconditions': '功能可用', 'steps': [{'action': '登录', 'expected': '登录成功'}],
                'refs': row['refs']}], 'report': {'summary': '从需求直接生成'}}
        return await generate(task, context, schema, instruction)
    j.gateway.generate_native = direct
    args = {'skip_scenarios': True, 'stop_after': 'cases'} if request_at_start else {}
    j.turn('根据需求直接给我用例草稿，不需要场景和评审' if request_at_start else '生成测试用例', 'start_pipeline_tool', args)
    _, analysis, prompt = j.gate('strategy_review')
    j.turn('同意' if request_at_start else '跳过场景，只根据需求给我用例草稿，不评审', 'resume_pipeline_tool',
        {} if request_at_start else {'action': 'skip_to_cases', 'draft_only': True}, reply=prompt)
    j.completed()
    artifacts = j.app.state.store.list('artifact', chat_id=j.chat['id'])
    cases = next(a for a in artifacts if a['type'] == 'cases')
    assert [a['type'] for a in artifacts].count('scenarios') == 0
    assert cases['items'][0]['requirement_ids'] == ['REQ-1']
    assert cases['report']['lineage']['analysis_artifact_id'] == analysis['id']
    assert not any(task == 'review_cases' for task, _ in j.gateway.generations)
    if request_at_start:
        from io import BytesIO
        from openpyxl import load_workbook
        before_analysis = j.app.state.store.get('artifact', analysis['id'])
        rows = copy.deepcopy(cases['items'])
        rows[0]['title'] = '人工编辑直接需求用例'
        manual = j.client.post('/api/artifacts/' + cases['id'] + '/workspace-grid/save', json={
            'expected_revision': cases['revision'], 'items': rows, 'client_request_id': 'manual-case-edit'})
        assert manual.status_code == 200, manual.text
        edited = manual.json()['artifact']
        assert edited['items'][0]['requirement_ids'] == ['REQ-1']
        assert edited['report']['lineage'] == cases['report']['lineage']
        proposal = j.turn('把当前用例标题改为 AI 编辑直接需求用例', 'modify_artifact_tool', {
            'artifact_id': cases['id'], 'item_id': cases['items'][0]['id'],
            'new_values': {'title': 'AI 编辑直接需求用例'}}, status='needs_confirmation')
        j.turn('接受这项修改', 'workspace_save', reply=proposal['pending'][0])
        changed = j.artifact(cases['id'])
        assert changed['items'][0]['title'] == 'AI 编辑直接需求用例'
        assert changed['items'][0]['requirement_ids'] == ['REQ-1']
        assert j.app.state.store.get('artifact', analysis['id']) == before_analysis
        profile = j.app.state.store.list('profile', project_id=j.project['id'])[0]
        config = copy.deepcopy(profile['config'])
        config['excel_columns'] = [{'field': 'title', 'header': '用例标题'},
            {'field': 'requirement_ids', 'header': '直接关联需求'}, {'field': 'scenario_id', 'header': '场景'}]
        j.app.state.store.update_profile(profile['id'], profile['name'], config, profile['version'])
        exported = j.client.get('/api/artifacts/' + cases['id'] + '/export', params={'profile_id': profile['id']})
        assert exported.status_code == 200, exported.text
        values = list(load_workbook(BytesIO(exported.content)).active.values)
        assert values[1][0] == 'AI 编辑直接需求用例'
        assert 'REQ-1' in str(values[1][1]) and values[1][2] is None
        assert not any(task == 'review_cases' for task, _ in j.gateway.generations)


@pytest.mark.asyncio
async def test_direct_case_ancestry_tracks_only_real_requirement_changes(direct_setup):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {
            'mode': 'auto', 'skip_scenarios': True, 'stop_after': 'cases'}))['id'])
        cases = store.get('artifact', run['current_artifact_id'])
        analysis = store.get('artifact', cases['report']['lineage']['analysis_artifact_id'])
        changed = [{**r, 'title': '变更后的需求'} for r in analysis['items']]
        updated = store.revise_artifact(analysis['id'], analysis['revision'], changed)
        from tcg.workspace_coverage import lineage_rows
        ancestry = lineage_rows(store, cases)[0]
        assert ancestry['stale'] and ancestry['requirements'][0]['stale']
        assert ancestry['requirements'][0]['item'] == analysis['items'][0]
        assert ancestry['requirements'][0]['revision'] == 1
        async with runtime.edit_session(chat['id']):
            await runtime.on_artifact_changed(updated)
        waiting = await runtime.snapshot(run['id'])
        assert waiting['interrupt']['type'] == 'strategy_review'
        completed = await agree(runtime, waiting)
        assert completed['status'] == 'completed'
        regenerated = store.get('artifact', cases['id'])
        assert regenerated['id'] == cases['id'] and regenerated['revision'] == 2
        assert not lineage_rows(store, regenerated)[0]['stale']
    finally:
        await runtime.stop()


def test_http_review_rejection_completes_without_applying_proposal(native_journey):
    j = native_journey
    j.turn('生成测试用例', 'start_pipeline_tool')
    _, _, prompt = j.gate('strategy_review')
    j.turn('同意', 'resume_pipeline_tool', reply=prompt)
    _, _, prompt = j.gate('scenario_review')
    j.turn('同意', 'resume_pipeline_tool', reply=prompt)
    run, cases, prompt = j.gate('case_result_review')
    j.turn('拒绝这份评审建议，保留当前用例', 'resume_pipeline_tool', {'action': 'rejected'}, reply=prompt)
    j.completed()
    assert j.artifact(cases['id'])['revision'] == cases['revision']
    assert j.app.state.store.get('artifact_proposal', prompt['proposal_id'])['status'] == 'rejected'


@pytest.mark.asyncio
async def test_switching_existing_scenario_cases_keeps_previous_manual_values(direct_setup):
    store, chat, model, business = direct_setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        analysis = store.get('artifact', run['current_artifact_id'])
        run = await agree(runtime, await agree(runtime, run))
        old_cases = store.get('artifact', run['current_artifact_id'])
        original = store.revise_artifact(old_cases['id'], old_cases['revision'],
            [{**r, 'tester': '实际人工执行人'} for r in old_cases['items']])
        updated = store.revise_artifact(analysis['id'], analysis['revision'],
            [{**r, 'title': '变更后的需求'} for r in analysis['items']])
        async with runtime.edit_session(chat['id']):
            await runtime.on_artifact_changed(updated)
        run = await runtime.snapshot(run['id'])
        await runtime.resume(run['id'], 'skip_to_cases', run['interrupt']['prompt_id'], {'draft_only': True})
        done = await settled(runtime, run['id'])
        assert done['status'] == 'completed', done.get('error')
        assert done['current_artifact_id'] != original['id']
        assert store.get('artifact', original['id']) == original
        current = store.get('artifact', done['current_artifact_id'])
        assert current['revision'] == 1 and current['report']['lineage']['generation_mode'] == 'direct_requirements'
    finally:
        await runtime.stop()
