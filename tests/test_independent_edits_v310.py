"""Independent additions and explicit unlinking never rewrite earlier artifacts."""
import copy

import pytest

from tcg.schemas import DomainError
from tcg.workspace_coverage import lineage_rows
from test_native_business_v300 import setup, generated


def independent_model(model):
    original = model.generate_native

    async def respond(task, context, schema, instruction):
        if task != 'revise_artifact' or not context.get('add_only'):
            return await original(task, context, schema, instruction)
        model.calls.append((task, copy.deepcopy(context)))
        row = {'id': 'NEW-' + context['artifact_type'], 'title': '并发登录',
               'refs': context['dialogue_evidence_ids']}
        if context['artifact_type'] == 'cases':
            row.update(scenario_id=context['addition_parent_id'] or '', type='Business', priority='P1',
                       preconditions='登录功能可用', purpose='并发验证',
                       steps=[{'action': '并发登录', 'expected': '行为待明确'}])
        else:
            row.update(description='按用户要求验证并发登录', priority='P1',
                       requirement_ids=[context['addition_parent_id']] if context['addition_parent_id'] else [])
        return {'items': [row], 'report': {'summary': '已准备新增行'}}
    model.generate_native = respond


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['scenarios', 'cases'])
async def test_add_independent_item_preserves_upstream_and_all_existing_rows(setup, kind):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    original_artifacts = store.list('artifact')
    target = scenarios if kind == 'scenarios' else cases
    independent_model(model)
    original_sources = store.list('source')
    proposal = await service.revise(target, instruction='新增并发登录', add=True,
        dialogue_content='请增加并发登录验证，结果暂未定义，不修改前面的成果。')
    assert proposal['dialogue']['parent_changes'] == []
    assert len(proposal['changes']) == 1
    assert store.list('source') == original_sources
    assert store.list('artifact') == original_artifacts
    updated = service.apply_revision_preview(proposal)
    added = updated['items'][-1]
    assert updated['items'][:-1] == target['items']
    assert (added['requirement_ids'] if kind == 'scenarios' else added['scenario_id']) == ([] if kind == 'scenarios' else '')
    assert added['_independent_origin']['reason']
    assert len(store.list('artifact')) == len(original_artifacts)
    for artifact in (analysis, scenarios, cases):
        if artifact['id'] != target['id']:
            assert store.get('artifact', artifact['id']) == artifact
    lineage = lineage_rows(store, updated)[-1]
    assert lineage['status'] == 'independent'
    assert lineage['stale'] is False
    assert not lineage['requirements'] and lineage['scenario'] is None
    if kind == 'scenarios':
        store.update_run(run['id'], _source_ids=updated['_source_ids'], _source_roles=updated['_source_roles'])
        new_cases = await service.cases(run, analysis, updated)
        assert added['id'] in {row['scenario_id'] for row in new_cases['items']}
        child = next(row for row in new_cases['items'] if row['scenario_id'] == added['id'])
        child_lineage = next(row for row in lineage_rows(store, new_cases) if row['item_id'] == child['id'])
        assert child_lineage['status'] == 'independent'
        assert child_lineage['scenario']['item']['id'] == added['id']
        assert child_lineage['requirements'] == []
        assert not child_lineage['stale']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,field,empty', [('scenarios', 'requirement_ids', []), ('cases', 'scenario_id', '')])
async def test_manual_explicit_unlink_and_invalid_nonempty_ids(setup, kind, field, empty):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    target = scenarios if kind == 'scenarios' else cases
    updated = await service.revise(target, ids=[target['items'][0]['id']], new_values={field: empty})
    assert updated['items'][0][field] == empty
    assert updated['items'][0]['_independent_origin']['reason']
    assert updated['items'][1] == target['items'][1]
    assert store.get('artifact', analysis['id']) == analysis
    with pytest.raises(DomainError):
        await service.revise(updated, ids=[updated['items'][0]['id']],
            new_values={field: ['NOT-A-REQ'] if kind == 'scenarios' else 'NOT-A-SCENARIO'})


@pytest.mark.asyncio
async def test_ai_omission_cannot_silently_unlink_existing_rows(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    original = model.generate_native

    async def dropping(task, context, schema, instruction):
        output = await original(task, context, schema, instruction)
        if task == 'revise_artifact':
            output['items'][0]['requirement_ids'] = []
            output['items'][0]['_independent_origin'] = {'reason': 'forged permission'}
        return output
    model.generate_native = dropping
    with pytest.raises(DomainError):
        await service.revise(scenarios, instruction='只修改标题')
    assert store.get('artifact', scenarios['id']) == scenarios
    proposal = await service.revise(scenarios, ids=[scenarios['items'][0]['id']],
        instruction='这个场景需求改为N/A', independent=True, preview=True)
    assert proposal['items'][0]['requirement_ids'] == []
    assert proposal['items'][0]['_independent_origin']['reason'] != 'forged permission'


@pytest.mark.asyncio
async def test_manual_whole_table_save_can_unlink_but_rejects_unknown_ids(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    items = copy.deepcopy(scenarios['items'])
    items[0]['requirement_ids'] = []
    updated = store.revise_artifact(scenarios['id'], scenarios['revision'], items)
    assert updated['items'][0]['_independent_origin']['reason']
    assert store.get('artifact', analysis['id']) == analysis
    items[0]['requirement_ids'] = ['invented']
    with pytest.raises(DomainError):
        store.revise_artifact(updated['id'], updated['revision'], items)


@pytest.mark.asyncio
async def test_review_keeps_independent_markers_even_if_model_omits_metadata(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    independent_model(model)
    proposal = await service.revise(cases, instruction='独立增加', add=True, dialogue_content='独立并发登录验证')
    updated = service.apply_revision_preview(proposal)
    original = model.generate_native

    async def omit_marker(task, context, schema, instruction):
        output = await original(task, context, schema, instruction)
        if task == 'review_cases':
            for row in output['items']:
                row.pop('_independent_origin', None)
        return output
    model.generate_native = omit_marker
    review = await service.review(run, updated)
    assert review['items'][-1]['scenario_id'] == ''
    assert review['items'][-1]['_independent_origin'] == updated['items'][-1]['_independent_origin']
    assert store.get('artifact', analysis['id']) == analysis
    assert store.get('artifact', scenarios['id']) == scenarios


@pytest.mark.asyncio
async def test_delete_rows_is_exact_preview_without_model_or_upstream_writes(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    before_calls = len(model.calls)
    with pytest.raises(DomainError, match='明确选择'):
        await service.revise(cases, delete=True)
    chosen = cases['items'][0]['id']
    proposal = await service.revise(cases, ids=[chosen], delete=True)
    assert len(model.calls) == before_calls
    assert proposal['items'] == cases['items'][1:]
    assert store.get('artifact', cases['id']) == cases
    updated = service.apply_revision_preview(proposal)
    assert updated['items'] == cases['items'][1:]
    assert store.revision(cases['id'], 1)['items'] == cases['items']
    assert store.get('artifact', analysis['id']) == analysis
    assert store.get('artifact', scenarios['id']) == scenarios
    empty = await service.revise(updated, ids=[updated['items'][0]['id']], delete=True)
    assert service.apply_revision_preview(empty)['items'] == []


@pytest.mark.asyncio
async def test_refresh_other_scenarios_preserves_independent_case_and_its_evidence(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    independent_model(model)
    proposal = await service.revise(cases, instruction='独立增加', add=True, dialogue_content='独立并发登录验证')
    changed_cases = service.apply_revision_preview(proposal)
    independent_case = changed_cases['items'][-1]
    changed_scenarios = await service.revise(scenarios, ids=[scenarios['items'][0]['id']],
        new_values={'title': '更新场景标题'})
    refreshed = await service.cases(run, analysis, changed_scenarios)
    assert independent_case in refreshed['items']
    assert independent_case['_independent_origin']['source_id'] in refreshed['_source_ids']
    independent_lineage = next(row for row in lineage_rows(store, refreshed) if row['item_id'] == independent_case['id'])
    assert independent_lineage['status'] == 'independent'
    assert not independent_lineage['stale']


@pytest.mark.asyncio
async def test_removed_default_column_does_not_reappear_during_edit_or_completion(setup):
    store, run, service, model = setup
    historical_profile = copy.deepcopy(run['_profile'])
    historical_profile['excel_columns'].append({'field': 'obsolete_flag', 'header': '旧列',
                                               'value_source': 'default', 'default_value': False})
    store.update_run(run['id'], _profile=historical_profile)
    analysis, scenarios, cases = await generated(setup)
    columns = [column for column in cases['_profile']['excel_columns'] if column['field'] != 'obsolete_flag']
    rows = [{key: value for key, value in row.items() if key != 'obsolete_flag'} for row in cases['items']]
    changed = store.revise_artifact(cases['id'], cases['revision'], rows,
        reason='native_tool_edit', report={**cases['report'], 'table_columns': columns})
    revised = await service.revise(changed, instruction='只修改标题')
    context = [context for task, context in model.calls if task == 'revise_artifact'][-1]
    assert 'obsolete_flag' not in {column['field'] for column in context['profile']['excel_columns']}
    assert all('obsolete_flag' not in row for row in revised['items'])
    completed = await service.complete_fields(revised)
    assert all('obsolete_flag' not in row for row in completed['items'])
    assert store.get('artifact', cases['id'])['_profile'] == historical_profile
    changed_scenarios = await service.revise(scenarios, ids=[scenarios['items'][0]['id']],
        new_values={'title': '新场景标题'})
    refreshed = await service.cases(run, analysis, changed_scenarios)
    assert refreshed['report']['table_columns'] == columns
    assert all('obsolete_flag' not in row for row in refreshed['items'])
    reviewed = await service.review(run, refreshed)
    assert all('obsolete_flag' not in row for row in reviewed['items'])
    assert reviewed['_profile'] == historical_profile
