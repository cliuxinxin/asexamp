import copy

import pytest

from tcg.documents import parse_text
from tcg.native_business import NativeBusiness
from tcg.schemas import DomainError
from tcg.server_capacity import ContextCapacityError
from tcg.storage import Store


class NativeModel:
    def __init__(self):
        self.calls = []
        self.reject_multi = False
        self.invalid_ref = False

    async def generate_native(self, task, context, schema, instruction):
        self.calls.append((task, copy.deepcopy(context)))
        assert 'operations' not in schema['properties']
        assert 'Return one JSON object' not in instruction
        evidence = context.get('evidence', [])
        ref = evidence[0]['id'] if evidence else None
        if task == 'understand_requirements':
            if self.reject_multi and len(evidence) > 1:
                if self.reject_multi == 'output':
                    error = DomainError('output limit')
                    error.category = 'output_capacity'
                    raise error
                raise ContextCapacityError(16000)
            return {'items': [{'id': 'R' + e['id'].rsplit('P', 1)[-1], 'title': e['text'],
                'description': e['text'], 'refs': [e['id']]} for e in evidence],
                'report': {'summary': '业务需求已理解', 'questions': ['异常时保留输入吗？']}}
        if task == 'generate_scenarios':
            return {'items': [{'id': 'S' + row['id'], 'title': row['title'], 'description': row['description'],
                'priority': 'P1', 'requirement_ids': [row['id']], 'refs': row['refs']}
                for row in context['analysis']], 'report': {'summary': '场景已生成'}}
        if task == 'generate_cases':
            return {'items': [{'id': 'C' + row['id'], 'title': row['title'], 'scenario_id': row['id'],
                'type': 'Business', 'priority': 'P1', 'preconditions': '功能可用',
                'steps': [{'action': '检查' + row['title'], 'expected': row['description']}],
                'refs': row['refs'], 'purpose': row['description'], 'tester': '模型编造的执行人'}
                for row in context['scenarios']], 'report': {'summary': '用例已生成'}}
        if task == 'review_cases':
            rows = copy.deepcopy(context['cases'])
            for row in rows:
                row['title'] += '（评审）'
                row['tester'] = '模型试图覆盖'
                row['actual_result'] = '模型编造的执行结果'
            return {'items': rows, 'report': {'summary': '已评审步骤及预期', 'issues': []}}
        if task == 'revise_artifact':
            rows = copy.deepcopy(context['items'])
            for row in rows:
                row['title'] = '修订后的' + row['title']
                if self.invalid_ref:
                    row['refs'] = ['invented#P1']
            return {'items': rows, 'report': {'summary': '修改完成', 'questions': []}}
        if task == 'repair_evidence_refs':
            return {'items': [{'id': row['id'], 'refs': [], 'support': [],
                'reason': 'The simulated model cannot ground this row.'} for row in context['items']]}
        if task == 'estimate_workload':
            return {'summary': '估算范围', 'scenarios': [{'scenario_id': r['id'], 'min_count': 2,
                'max_count': 4, 'rationale': '正向及异常', 'assumptions': ['组合量待核实']}
                for r in context['scenarios']]}
        if task == 'explain_artifact':
            return {'answer': '这些用例检查当前需求的正常和异常行为。', 'refs': [ref] if ref else []}
        raise AssertionError(task)


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('Native Test')
    chat = store.create_chat(project['id'], '测试生成')
    text, chunks = parse_text('登录成功显示首页。\n\n退出登录后不能访问账户。')
    source = store.add_source(chat['id'], '需求', 'primary', text, chunks)
    profile = store.list('profile', project_id=project['id'])[0]
    config = copy.deepcopy(profile['config'])
    config['excel_columns'] += [{'field': 'purpose', 'header': '验证目的', 'required': True},
                               {'field': 'tester', 'header': '执行人', 'value_source': 'manual'}]
    store.update_profile(profile['id'], profile['name'], config, profile['version'])
    _, run = store.create_run(chat['id'], {'content': '生成测试用例', 'intent': 'generate_case',
        'mode': 'hitp', 'experience': 'native', 'source_ids': [source['id']]})
    model = NativeModel()
    yield store, run, NativeBusiness(store, model), model
    store.close()


async def generated(setup):
    store, run, service, model = setup
    analysis = await service.understand(run)
    scenarios = await service.scenarios(run, analysis)
    cases = await service.cases(run, analysis, scenarios)
    return analysis, scenarios, cases


@pytest.mark.asyncio
async def test_native_generation_preserves_grounded_lineage_and_manual_execution_data(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    assert len(cases['items']) == 2
    assert scenarios['report']['lineage']['analysis_artifact_id'] == analysis['id']
    assert cases['report']['lineage']['scenario_artifact_id'] == scenarios['id']
    assert all(row['purpose'] for row in cases['items'])
    assert all('tester' not in row for row in cases['items'])
    chosen = cases['items'][0]['id']
    revised = await service.revise(cases, ids=[chosen], new_values={'tester': '实际测试员', 'actual_result': '人工记录'})
    reviewed = await service.review(run, revised)
    assert reviewed['items'][0]['tester'] == '实际测试员'
    assert reviewed['items'][0]['actual_result'] == '人工记录'
    assert 'actual_result' not in reviewed['items'][1]
    assert reviewed['report']['review_reports'][0]['summary']
    assert reviewed['report']['lineage'] == cases['report']['lineage']


@pytest.mark.asyncio
async def test_changed_scenario_regenerates_only_related_cases_without_touching_other_rows(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    cases = await service.revise(cases, ids=[cases['items'][0]['id']], new_values={'tester': '实际测试员'})
    untouched = copy.deepcopy(cases['items'][1])
    revised = await service.revise(scenarios, ids=[scenarios['items'][0]['id']],
                                   new_values={'title': '修订登录校验', 'description': '成功后显示首页'})
    refreshed = await service.cases(run, analysis, revised)
    context = [c for task, c in model.calls if task == 'generate_cases'][-1]
    assert [r['id'] for r in context['scenarios']] == [scenarios['items'][0]['id']]
    rows = {r['id']: r for r in refreshed['items']}
    assert rows[untouched['id']] == untouched
    assert rows[cases['items'][0]['id']]['tester'] == '实际测试员'
    assert refreshed['id'] == cases['id']


@pytest.mark.asyncio
async def test_native_read_tools_are_read_only_and_template_draft_uses_cas(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    before_run = store.run(run['id'])
    before = store.get('artifact', cases['id'])
    estimate = await service.estimate(scenarios)
    assert (estimate['min_count'], estimate['max_count']) == (4, 8)
    answer = await service.analyze(cases, '解释这些用例')
    assert answer['answer']
    assert store.run(run['id']) == before_run
    assert store.get('artifact', cases['id']) == before
    proposal = await service.revise(cases, ids=[cases['items'][0]['id']], instruction='修改标题', preview=True)
    assert store.get('artifact', cases['id']) == before
    await service.revise(cases, ids=[cases['items'][0]['id']], new_values={'title': '更新后的标题'})
    with pytest.raises(DomainError):
        service.apply_revision_preview(proposal)
    assert store.get('artifact', cases['id'])['items'][0]['title'] == '更新后的标题'


@pytest.mark.asyncio
async def test_invalid_model_evidence_does_not_mutate_artifact(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    model.invalid_ref = True
    with pytest.raises(DomainError, match='证据引用'):
        await service.revise(cases, instruction='修改标题')
    assert store.get('artifact', cases['id']) == cases


@pytest.mark.asyncio
@pytest.mark.parametrize('rejection', [True, 'output'])
async def test_server_rejection_splits_only_after_request_and_reuses_saved_leaves(setup, rejection):
    store, run, service, model = setup
    model.reject_multi = rejection
    analysis = await service.understand(run)
    calls = [c for task, c in model.calls if task == 'understand_requirements']
    assert [len(c['evidence']) for c in calls] == [2, 1, 1]
    assert len(analysis['items']) == 2
    assert len({r['id'] for r in analysis['items']}) == 2
    again = await service.understand(run)
    assert again['id'] == analysis['id']
    assert len(model.calls) == 3


@pytest.mark.asyncio
async def test_every_clarification_has_suggestion_and_confirmed_answers_are_project_shared(setup):
    store, run, service, model = setup
    analysis = await service.understand(run)
    suggestions = analysis['report']['question_suggestions']
    assert len(suggestions) == len(analysis['report']['questions']) == 1
    assert suggestions[0]['confidence'] == 'assumption'
    assert '假设' in suggestions[0]['basis']
    revised = await service.clarify(run, analysis, {'异常时保留输入吗？': '保留已输入的内容。'})
    assert revised['report']['questions'] == []
    shared = [s for s in store.list('source', project_id=run['project_id']) if s.get('_project_shared')]
    assert len(shared) == 1
    assert '保留已输入' in shared[0]['_text']
    another = store.create_chat(run['project_id'], '另一个成员的对话')
    _, another_run = store.create_run(another['id'], {'content': '根据项目需求生成场景', 'intent': 'generate_scenario',
        'mode': 'hitp', 'experience': 'native'})
    assert shared[0]['id'] in another_run['_source_ids']


@pytest.mark.asyncio
async def test_learned_template_completion_keeps_rows_and_exports_custom_values(setup):
    from io import BytesIO
    from openpyxl import load_workbook
    from tcg.documents import export_artifact
    store, run, service, model = setup
    _, _, cases = await generated(setup)
    chosen_id = cases['items'][0]['id']
    cases = await service.revise(cases, ids=[chosen_id], new_values={'tester': '真实执行人'})
    profile = store.list('profile', project_id=run['project_id'])[0]
    config = copy.deepcopy(profile['config'])
    config['excel_columns'] += [{'field': 'validation_goal', 'header': '验证目标', 'value_source': 'ai', 'required': True},
                               {'field': 'input_data', 'header': '输入数据', 'value_source': 'ai', 'required': True},
                               {'field': 'build_no', 'header': '构建号', 'value_source': 'default', 'default_value': 0}]
    profile = store.update_profile(profile['id'], profile['name'], config, profile['version'])
    original_generate = model.generate_native
    async def complete(task, context, schema, instruction):
        if task == 'complete_case_fields':
            model.calls.append((task, copy.deepcopy(context)))
            assert context['missing'] == [{'id': chosen_id, 'fields': ['input_data', 'validation_goal']}]
            return {'items': [{'id': row['id'], 'fields': {'validation_goal': row['purpose'], 'input_data': '按当前前置条件准备'},
                              'refs': row['refs'], 'unresolved': []} for row in context['cases']]}
        return await original_generate(task, context, schema, instruction)
    model.generate_native = complete
    before_run = store.run(run['id'])
    updated = await service.complete_fields(cases, profile, ids=[chosen_id])
    assert updated['items'][1] == cases['items'][1]
    assert updated['items'][0]['tester'] == '真实执行人'
    assert updated['items'][0]['steps'] == cases['items'][0]['steps']
    assert updated['_profile'] == cases['_profile']
    assert updated['report']['lineage'] == cases['report']['lineage']
    assert store.run(run['id']) == before_run
    calls_before = len(model.calls)
    assert (await service.complete_fields(updated, profile, ids=[chosen_id]))['revision'] == updated['revision']
    assert len(model.calls) == calls_before
    workbook = load_workbook(BytesIO(export_artifact({**updated, '_profile': profile['config']}, selected=[chosen_id])))
    sheet = workbook.active
    values = dict(zip([cell.value for cell in sheet[1]], [cell.value for cell in sheet[2]]))
    assert values['验证目标'] == cases['items'][0]['purpose']
    assert values['输入数据'] == '按当前前置条件准备'
    assert str(values['构建号']) == '0'
    assert values['执行人'] == '真实执行人'


@pytest.mark.asyncio
async def test_template_completion_records_unknown_facts_and_rejects_existing_field_changes(setup):
    from tcg.case_fields import template_check
    store, run, service, model = setup
    _, _, cases = await generated(setup)
    config = copy.deepcopy(cases['_profile'])
    config['excel_columns'].append({'field': 'test_account', 'header': '测试账号', 'value_source': 'ai', 'required': True})
    async def unknown(task, context, schema, instruction):
        assert task == 'complete_case_fields'
        return {'items': [{'id': row['id'], 'fields': {}, 'refs': [],
            'unresolved': [{'field': 'test_account', 'reason': '需求未提供真实测试账号，需由环境维护者补充。'}]}
            for row in context['cases']]}
    model.generate_native = unknown
    updated = await service.complete_fields(cases, config)
    assert all('test_account' not in row for row in updated['items'])
    assert all('需求未提供真实测试账号' in gap['reason'] for gap in template_check(config, updated['items'])['missing'])
    async def unsafe(task, context, schema, instruction):
        return {'items': [{'id': row['id'], 'fields': {'purpose': '模型试图覆盖已有内容'},
            'refs': row['refs'], 'unresolved': []} for row in context['cases']]}
    model.generate_native = unsafe
    with pytest.raises(DomainError, match='purpose') as rejected:
        await service.complete_fields(updated, config)
    assert rejected.value.details['unexpected_fields'] == ['purpose']
    assert store.get('artifact', updated['id']) == updated


@pytest.mark.asyncio
async def test_partial_clarification_keeps_other_questions_and_can_remain_local(setup):
    store, run, service, model = setup
    analysis = await service.understand(run)
    report = copy.deepcopy(analysis['report'])
    report['questions'].append('是否有操作审计？')
    analysis = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    store.update_run(run['id'], clarification_save_to_project=False)
    revised = await service.clarify(run, analysis, {'异常时保留输入吗？': '保留输入。'})
    assert revised['report']['questions'] == ['是否有操作审计？']
    assert revised['report']['question_suggestions'][0]['question'] == '是否有操作审计？'
    assert not any(source.get('_project_shared') for source in store.list('source', project_id=run['project_id']))
    with pytest.raises(DomainError, match='具体澄清答案'):
        await service.clarify(run, revised, {'是否有操作审计？': ''})


@pytest.mark.asyncio
async def test_uploaded_requirement_change_updates_only_linked_scenarios_and_cases(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    old_scenario, old_case = copy.deepcopy(scenarios['items'][1]), copy.deepcopy(cases['items'][1])
    text, chunks = parse_text('登录成功后显示首页，并显示登录时间。')
    source = store.add_source(run['chat_id'], '登录补充规则', 'supplement', text, chunks)
    changed = await service.revise(analysis, ids=[analysis['items'][0]['id']], instruction='采用登录补充规则',
                                  source_ids=[source['id']], source_roles={source['id']: 'supplement'})
    refreshed_scenarios = await service.scenarios(run, changed)
    refreshed_cases = await service.cases(run, changed, refreshed_scenarios)
    assert refreshed_scenarios['items'][1] == old_scenario
    assert refreshed_cases['items'][1] == old_case
    assert source['id'] in refreshed_cases['_source_ids']
    assert refreshed_cases['report']['lineage']['analysis_revision'] == changed['revision']
    last_analysis_context = [c for task, c in model.calls if task == 'revise_artifact'][-1]
    assert source['id'] + '#P1' in {e['id'] for e in last_analysis_context['evidence']}
    assert [r['id'] for r in [c for task, c in model.calls if task == 'generate_scenarios'][-1]['analysis']] == [analysis['items'][0]['id']]


@pytest.mark.asyncio
async def test_review_honors_selected_case_scope_and_passes_complete_steps(setup):
    store, run, service, model = setup
    _, _, cases = await generated(setup)
    original_second = copy.deepcopy(cases['items'][1])
    request = {**run['_request'], 'intent': 'review_case', 'artifact_id': cases['id'],
               'selected_ids': [cases['items'][0]['id']]}
    store.update_run(run['id'], _request=request)
    reviewed = await service.review(run, cases)
    assert reviewed['items'][1] == original_second
    review_context = [context for task, context in model.calls if task == 'review_cases'][-1]
    assert [row['id'] for row in review_context['cases']] == request['selected_ids']
    assert review_context['cases'][0]['steps'] == cases['items'][0]['steps']
    assert reviewed['report']['review_reports'][-1]['scope'] == {
        'case_ids': request['selected_ids'], 'all': False, 'reviewed_count': 1, 'total_count': 2}


@pytest.mark.asyncio
async def test_incomplete_case_batch_is_corrected_before_it_can_be_cached_as_success(setup):
    from tcg.diagnostics import Diagnostics
    from tcg.native_model import NativeResult
    store, run, service, model = setup
    analysis = await service.understand(run)
    scenarios = await service.scenarios(run, analysis)
    original = model.generate_native
    model.diagnostics = Diagnostics(store)
    first = True
    async def incomplete(task, context, schema, instruction):
        nonlocal first
        result = await original(task, context, schema, instruction)
        if task == 'generate_cases' and first:
            first = False
            result['items'] = result['items'][:1]
            return NativeResult(result, call_id='call_invalid_batch')
        return result
    model.generate_native = incomplete
    cases = await service.cases(run, analysis, scenarios)
    assert len(cases['items']) == 2
    assert len([task for task, _ in model.calls if task == 'generate_cases']) == 2
    events = [event['data'] for event in store.events(run['id']) if event['data'].get('event') == 'batch.validation_failed']
    assert len(events) == 1
    assert events[0]['call_id'] == 'call_invalid_batch'
    assert events[0]['errors'] and events[0]['run_id'] == run['id']


@pytest.mark.asyncio
async def test_global_understanding_rules_are_inputs_even_if_scenario_wording_is_unchanged(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    report = copy.deepcopy(analysis['report'])
    report.update(in_scope=['登录与退出'], out_of_scope=['不执行生产环境压测'],
                  assumptions=['没有用户授权时不修改账户数据'])
    changed = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    refreshed_scenarios = await service.scenarios(run, changed)
    assert refreshed_scenarios['items'] == scenarios['items']
    scenario_context = [c for task, c in model.calls if task == 'generate_scenarios'][-1]
    assert scenario_context['parent_rules']['out_of_scope'] == ['不执行生产环境压测']
    refreshed_cases = await service.cases(run, changed, refreshed_scenarios)
    case_contexts = [c for task, c in model.calls if task == 'generate_cases']
    assert len(case_contexts) == 2
    assert len(case_contexts[-1]['scenarios']) == 2
    assert case_contexts[-1]['understanding_rules']['assumptions'] == ['没有用户授权时不修改账户数据']
    assert refreshed_cases['report']['lineage']['analysis_revision'] == changed['revision']
    modified_title = await service.revise(changed, ids=[changed['items'][0]['id']], instruction='只修改标题')
    assert modified_title['report']['assumptions'] == changed['report']['assumptions']
    assert modified_title['report']['out_of_scope'] == changed['report']['out_of_scope']


@pytest.mark.asyncio
async def test_requirement_row_change_reaches_only_linked_cases_even_when_scenarios_stay_equal(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    changed = await service.revise(analysis, ids=[analysis['items'][0]['id']], new_values={'description': '登录成功还需展示时间'})
    original = model.generate_native
    async def keep_scenarios(task, context, schema, instruction):
        if task == 'generate_scenarios':
            model.calls.append((task, copy.deepcopy(context)))
            return {'items': copy.deepcopy(context['previous_items']), 'report': {'summary': '现有场景粒度足够'}}
        return await original(task, context, schema, instruction)
    model.generate_native = keep_scenarios
    refreshed_scenarios = await service.scenarios(run, changed)
    assert refreshed_scenarios['items'] == scenarios['items']
    updated = await service.cases(run, changed, refreshed_scenarios)
    context = [c for task, c in model.calls if task == 'generate_cases'][-1]
    assert len(context['scenarios']) == 1
    assert context['analysis'][0]['description'] == '登录成功还需展示时间'
    assert updated['items'][1] == cases['items'][1]
