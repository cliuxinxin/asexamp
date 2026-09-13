import copy

import pytest
from jsonschema import Draft202012Validator

from tcg.analysis_diagrams import complete_analysis_diagrams
from tcg.native_schemas import rows_schema
from test_native_business_v300 import setup


VIEWS = [
    {'title': '业务流程图', 'mermaid': 'flowchart TD\n  A[登录] --> B[显示首页]'},
    {'title': '领域思维导图', 'mermaid': 'mindmap\n  root((账户))\n    登录\n    退出'},
    {'title': '状态转换图', 'mermaid': 'stateDiagram-v2\n  未登录 --> 已登录: 登录成功'},
]


def types(report):
    return [view['mermaid'].split()[0] for view in report['diagrams']]


@pytest.mark.parametrize('diagrams', [[], VIEWS[:1]])
def test_omitted_views_do_not_fail_business_output_or_invent_transitions(diagrams):
    rows = [{'id': 'R1', 'title': '账户查询', 'description': '展示账户信息', 'refs': ['s#P1']}]
    report = {'summary': '理解完成', 'diagrams': copy.deepcopy(diagrams)}
    complete_analysis_diagrams(report, rows)
    assert types(report) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    assert '账户查询' in report['diagrams'][1]['mermaid']
    assert '-->' not in report['diagrams'][2]['mermaid']
    assert '未提供' in report['diagrams'][2]['mermaid']
    if diagrams:
        assert report['diagrams'][0] == diagrams[0]
    Draft202012Validator(rows_schema('analysis')).validate({'items': rows, 'report': report})


def test_three_model_views_are_preserved_and_mermaid_remains_typed():
    report = {'summary': '理解完成', 'diagrams': copy.deepcopy(VIEWS)}
    complete_analysis_diagrams(report, [{'id': 'R1', 'title': '登录'}])
    assert report['diagrams'] == VIEWS
    schema = rows_schema('analysis')
    description = schema['properties']['report']['properties']['diagrams']['description']
    assert all(kind in description for kind in ('flowchart', 'mindmap', 'stateDiagram-v2'))
    report['diagrams'][0]['mermaid'] = {'accidental': 'object'}
    assert list(Draft202012Validator(schema).iter_errors({'items': [], 'report': report}))


@pytest.mark.parametrize('rule', ['in_scope', 'out_of_scope', 'assumptions'])
def test_report_only_rule_changes_retire_echoed_diagrams(rule):
    rows = [{'id': 'R1', 'title': '登录', 'description': '展示账户首页', 'refs': ['s#P1']}]
    previous = {'items': copy.deepcopy(rows), 'report': {'summary': '理解完成',
        'diagrams': copy.deepcopy(VIEWS), rule: ['旧业务规则']}}
    report = copy.deepcopy(previous['report'])
    report[rule] = ['只覆盖白名单用户']
    complete_analysis_diagrams(report, rows, previous=previous)
    assert types(report) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    assert all(view != old for view, old in zip(report['diagrams'], VIEWS))
    assert previous['report']['diagrams'] == VIEWS


def test_unchanged_rows_and_rules_preserve_previous_valid_views():
    rows = [{'id': 'R1', 'title': '登录', 'description': '展示账户首页', 'refs': ['s#P1']}]
    previous = {'items': copy.deepcopy(rows), 'report': {'summary': '理解完成',
        'diagrams': copy.deepcopy(VIEWS), 'in_scope': ['登录'], 'assumptions': None}}
    report = copy.deepcopy(previous['report'])
    report.update(summary='只更新说明', assumptions=[], questions=[])
    complete_analysis_diagrams(report, rows, previous=previous)
    assert report['diagrams'] == VIEWS


@pytest.mark.asyncio
async def test_initial_and_direct_edits_always_use_three_current_views_without_extra_calls(setup):
    _, run, service, model = setup
    analysis = await service.understand(run)
    assert types(analysis['report']) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    calls = len(model.calls)
    edited = await service.revise(analysis, ids=[analysis['items'][0]['id']],
        new_values={'title': '新增二次确认', 'description': '展示确认页后由用户确认'})
    assert types(edited['report']) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    assert '新增二次确认' in edited['report']['diagrams'][1]['mermaid']
    assert len(model.calls) == calls


@pytest.mark.asyncio
async def test_ai_echoed_old_diagrams_cannot_outlive_changed_understanding(setup):
    _, run, service, model = setup
    analysis = await service.understand(run)
    original = model.generate_native

    async def revise(task, context, schema, instruction):
        result = await original(task, context, schema, instruction)
        if task == 'revise_artifact':
            assert all(kind in instruction for kind in ('flowchart', 'mindmap', 'stateDiagram-v2'))
            result['report']['diagrams'] = copy.deepcopy(analysis['report']['diagrams'])
        return result

    model.generate_native = revise
    revised = await service.clarify(run, analysis, {'异常时保留输入吗？': '保留输入'})
    assert revised['report']['questions'] == []
    assert types(revised['report']) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    assert '修订后的' in revised['report']['diagrams'][1]['mermaid']
    assert revised['report']['diagrams'] != analysis['report']['diagrams']
    assert [task for task, _ in model.calls] == ['understand_requirements', 'revise_artifact']


@pytest.mark.asyncio
async def test_split_understanding_has_one_complete_set_instead_of_duplicate_partial_views(setup):
    _, run, service, model = setup
    model.reject_multi = True
    original = model.generate_native

    async def generate(task, context, schema, instruction):
        result = await original(task, context, schema, instruction)
        result['report']['diagrams'] = copy.deepcopy(VIEWS)
        return result

    model.generate_native = generate
    analysis = await service.understand(run)
    assert types(analysis['report']) == ['flowchart', 'mindmap', 'stateDiagram-v2']
    assert all(row['title'] in analysis['report']['diagrams'][1]['mermaid'] for row in analysis['items'])
