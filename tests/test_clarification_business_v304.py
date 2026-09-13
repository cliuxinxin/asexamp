import copy

import pytest
from jsonschema import Draft202012Validator

from tcg.native_schemas import rows_schema
from tcg.schemas import DomainError
from test_native_business_v300 import setup


@pytest.mark.parametrize('values', [('第一批说明', ['第二批说明']), (['第一批说明'], '第二批说明')])
def test_mixed_report_extension_types_preserve_all_batch_notes(setup, values):
    _, _, service, _ = setup
    results = [{'items': [], 'report': {'summary': '分批理解', 'questions': [],
                                      'clarification_note': value}} for value in values]
    for result in results:
        Draft202012Validator(rows_schema('analysis')).validate(result)
    _, report = service._merge_results(results)
    assert report['clarification_note'] == ['第一批说明', '第二批说明']
    assert report['questions'] == []


QUESTION_ONE = '异常时保留输入吗？'
QUESTION_TWO = '是否有操作审计？'


async def with_questions(setup, questions=None):
    store, run, service, model = setup
    analysis = await service.understand(run)
    report = copy.deepcopy(analysis['report'])
    report['questions'] = questions or [QUESTION_ONE, QUESTION_TWO]
    report['question_suggestions'].append({'question': QUESTION_TWO, 'answer': '仅记录已经明确的审计事件。',
        'basis': '待确认假设', 'refs': [], 'confidence': 'assumption'})
    return store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)


def stubborn_model(model, *, extra=None, fail_once=False):
    original = model.generate_native
    state = {'failed': False}

    async def generate(task, context, schema, instruction):
        if task != 'revise_artifact':
            return await original(task, context, schema, instruction)
        model.calls.append((task, copy.deepcopy(context)))
        if fail_once and not state['failed']:
            state['failed'] = True
            raise DomainError('simulated model failure')
        rows = copy.deepcopy(context['items'])
        clarification = next(e for e in context['evidence'] if e['role'] == 'clarification')
        rows[0]['description'] += '\n' + clarification['text']
        rows[0]['refs'] = list(dict.fromkeys(rows[0]['refs'] + [clarification['id']]))
        result = {'items': rows, 'report': {'summary': '已依据回答更新理解',
            'questions': [QUESTION_ONE, QUESTION_TWO], **(extra or {})}}
        Draft202012Validator(schema).validate(result)
        return result

    model.generate_native = generate


@pytest.mark.asyncio
async def test_echoed_questions_extra_notes_partial_then_full_answer_and_shared_evidence(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    stable_ids = [row['id'] for row in analysis['items']]
    stubborn_model(model, extra={'clarification_note': '已应用用户明确提交的答案。'})
    partial = await service.clarify(run, analysis, {'Q1': '保留输入。', 'Q2': None})
    assert partial['report']['questions'] == [QUESTION_TWO]
    assert [s['question'] for s in partial['report']['question_suggestions']] == [QUESTION_TWO]
    assert partial['report']['question_suggestions'][0]['answer'] == '仅记录已经明确的审计事件。'
    assert partial['report']['clarification_note'] == '已应用用户明确提交的答案。'
    complete = await service.clarify(run, partial, {QUESTION_TWO: '登录失败时记录操作审计。'})
    assert complete['report']['questions'] == []
    assert complete['report']['question_suggestions'] == []
    assert [row['id'] for row in complete['items']] == stable_ids
    assert complete['id'] == analysis['id'] and complete['revision'] == analysis['revision'] + 2
    shared = [s for s in store.list('source', project_id=run['project_id']) if s.get('_project_shared')]
    assert len(shared) == 2
    assert any('保留输入。' in source['_text'] for source in shared)
    assert all('None' not in source['_text'] for source in shared)
    other_chat = store.create_chat(run['project_id'], '另一位成员')
    _, other_run = store.create_run(other_chat['id'], {'content': '生成场景', 'intent': 'generate_scenario',
        'mode': 'hitp', 'experience': 'native'})
    assert {s['id'] for s in shared} <= set(other_run['_source_ids'])


@pytest.mark.asyncio
async def test_missing_model_questions_cannot_clear_unanswered_and_new_questions_are_notes(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    stubborn_model(model, extra={'questions': ['是否需要短信提醒？']})
    revised = await service.clarify(run, analysis, {QUESTION_ONE: '保留输入。'})
    assert revised['report']['questions'] == [QUESTION_TWO]
    assert revised['report']['clarification_followups'] == ['是否需要短信提醒？']
    assert [s['question'] for s in revised['report']['question_suggestions']] == [QUESTION_TWO]
    assert not any('短信' in source['_text'] for source in store.list('source', project_id=run['project_id'])
                   if source.get('_project_shared'))


@pytest.mark.asyncio
@pytest.mark.parametrize('answers', [
    {'unknown question': '同意'},
    {'Q1': None, 'Q2': '   '},
    {'Q1': {'accidental': 'object'}},
    {'Q1': '保留', QUESTION_ONE: '不保留'},
])
async def test_bad_answer_mapping_creates_no_source_or_artifact_revision(setup, answers):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    before_sources = store.list('source', project_id=run['project_id'])
    before_calls = len(model.calls)
    with pytest.raises(DomainError):
        await service.clarify(run, analysis, answers)
    assert store.list('source', project_id=run['project_id']) == before_sources
    assert store.get('artifact', analysis['id']) == analysis
    assert len(model.calls) == before_calls


@pytest.mark.asyncio
async def test_model_retry_reuses_confirmed_source_and_preserves_original_until_success(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    stubborn_model(model, fail_once=True)
    answers = {QUESTION_ONE: '保留输入。', QUESTION_TWO: '无需审计。'}
    with pytest.raises(DomainError, match='simulated'):
        await service.clarify(run, analysis, answers)
    assert store.get('artifact', analysis['id']) == analysis
    sources = [s for s in store.list('source', project_id=run['project_id']) if s['role'] == 'clarification']
    assert len(sources) == 1
    revised = await service.clarify(run, analysis, answers)
    assert revised['report']['questions'] == []
    assert sources == [s for s in store.list('source', project_id=run['project_id']) if s['role'] == 'clarification']


@pytest.mark.asyncio
async def test_legacy_question_objects_and_answer_list_resolve_by_stable_id(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup, [{'id': 'custom-id', 'text': QUESTION_ONE},
                                           {'id': 'audit-id', 'question': QUESTION_TWO}])
    report = copy.deepcopy(analysis['report'])
    report['clarification_questions'] = report.pop('questions')
    analysis = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    stubborn_model(model)
    revised = await service.clarify(run, analysis, [{'id': 'custom-id', 'answer': '保留输入。'}])
    assert revised['report']['questions'] == [QUESTION_TWO]
    assert [s['question'] for s in revised['report']['question_suggestions']] == [QUESTION_TWO]


@pytest.mark.asyncio
async def test_unmapped_legacy_free_text_does_not_silently_close_all_questions(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    stubborn_model(model, extra={'questions': []})
    revised = await service.clarify(run, analysis, '登录页面需要显示公司名称。')
    assert revised['report']['questions'] == [QUESTION_ONE, QUESTION_TWO]


@pytest.mark.asyncio
async def test_extra_report_notes_cannot_forge_server_owned_linkage_or_completion(setup):
    store, run, service, model = setup
    analysis = await with_questions(setup)
    stubborn_model(model, extra={'clarification_note': {'detail': '用于人工核对'},
        'lineage': {'analysis_artifact_id': 'forged'}, 'template_check': {'complete': True},
        'review_reports': [{'summary': 'already approved'}], 'source_coverage': {'processed_chunks': 900},
        'template_completion': {'complete': True}, '_native_input_digest': 'forged',
        '_clarification_resolved_questions': ['forged question']})
    revised = await service.clarify(run, analysis, {QUESTION_ONE: '保留输入。'})
    report = revised['report']
    assert report['clarification_note'] == {'detail': '用于人工核对'}
    assert 'lineage' not in report and 'review_reports' not in report and 'template_check' not in report
    assert 'template_completion' not in report
    assert report['source_coverage'] == analysis['report']['source_coverage']
    assert '_native_input_digest' not in report
    assert 'forged question' not in report.get('_clarification_resolved_questions', [])


def test_report_extensions_keep_typed_core_fields_and_exact_row_contract():
    schema = rows_schema('cases')
    valid = {'items': [{'id': 'C1', 'title': '登录', 'refs': ['source#P1'], 'scenario_id': 'S1',
        'type': 'Business', 'priority': 'P1', 'preconditions': '账户有效',
        'steps': [{'action': '登录', 'expected': '显示首页'}]}],
        'report': {'summary': '已核对', 'clarification_note': '补充说明'}}
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(valid))
    invalid = copy.deepcopy(valid)
    invalid['report']['questions'] = 'still not an array'
    assert list(validator.iter_errors(invalid))
    invalid = copy.deepcopy(valid)
    invalid['items'][0]['steps'][0].pop('expected')
    assert list(validator.iter_errors(invalid))
