import copy

import pytest

from tcg.diagnostics import Diagnostics
from tcg.native_model import NativeResult
from tcg.schemas import DomainError
from tcg.server_capacity import ContextCapacityError
from test_native_business_v300 import setup


async def upstream(setup):
    store, run, service, model = setup
    analysis = await service.understand(run)
    scenarios = await service.scenarios(run, analysis)
    return store, run, service, model, analysis, scenarios


def grounded(context):
    evidence = context['evidence'][0]
    return {'items': [{'id': row['id'], 'refs': [evidence['id']],
        'support': [{'ref': evidence['id'], 'quote': evidence['text']}],
        'reason': 'The quoted requirement describes this case behavior.'} for row in context['items']]}


@pytest.mark.asyncio
async def test_invalid_extras_are_filtered_without_model_retry_and_are_reported(setup):
    store, run, service, model, analysis, scenarios = await upstream(setup)
    original = model.generate_native
    generated = None

    async def mixed(task, context, schema, instruction):
        nonlocal generated
        assert task != 'repair_evidence_refs'
        result = await original(task, context, schema, instruction)
        if task == 'generate_cases':
            result['items'][0]['refs'] += ['string', 'sample#P1']
            generated = copy.deepcopy(result['items'])
        return result

    model.generate_native = mixed
    cases = await service.cases(run, analysis, scenarios)
    assert cases['items'][0]['refs'] == scenarios['items'][0]['refs']
    assert cases['items'][0]['steps'] == generated[0]['steps']
    assert cases['items'][1]['refs'] == generated[1]['refs']
    issues = cases['report']['issues']
    assert issues[-1]['code'] == 'reference_filtered'
    assert issues[-1]['item_ids'] == [generated[0]['id']]
    assert issues[-1]['removed_count'] == 2


@pytest.mark.asyncio
async def test_only_ungrounded_rows_receive_refs_only_repair_and_correct_rows_stay_unchanged(setup):
    store, run, service, model, analysis, scenarios = await upstream(setup)
    original = model.generate_native
    generated, repair_contexts = [], []

    async def invalid(task, context, schema, instruction):
        if task == 'repair_evidence_refs':
            repair_contexts.append(copy.deepcopy(context))
            assert set(schema['properties']['items']['items']['properties']) == {'id', 'refs', 'support', 'reason'}
            return grounded(context)
        result = await original(task, context, schema, instruction)
        if task == 'generate_cases':
            result['items'][0]['refs'] = ['invented']
            generated.extend(copy.deepcopy(result['items']))
        return result

    model.generate_native = invalid
    cases = await service.cases(run, analysis, scenarios)
    assert len(repair_contexts) == 1
    assert [r['id'] for r in repair_contexts[0]['items']] == [generated[0]['id']]
    for actual, before in zip(cases['items'], generated):
        assert {k: v for k, v in actual.items() if k != 'refs'} == {k: v for k, v in before.items() if k not in ('refs', 'tester')}
    assert cases['report']['issues'][-1]['code'] == 'reference_repaired'
    assert len([task for task, _ in model.calls if task == 'generate_cases']) == 1


@pytest.mark.asyncio
async def test_unresolved_repair_stops_with_diagnostics_then_retries_only_retained_candidate(setup):
    store, run, service, model, analysis, scenarios = await upstream(setup)
    original = model.generate_native
    model.diagnostics = Diagnostics(store)
    repairs = []

    async def invalid(task, context, schema, instruction):
        if task == 'repair_evidence_refs':
            repairs.append(copy.deepcopy(context))
            if len(repairs) == 1:
                return NativeResult({'items': [{'id': row['id'], 'refs': [], 'support': [],
                    'reason': 'No supplied source supports this behavior.'} for row in context['items']]}, 'call_ref_unresolved')
            return NativeResult(grounded(context), 'call_ref_fixed')
        result = await original(task, context, schema, instruction)
        if task == 'generate_cases':
            result['items'][0]['refs'] = ['string']
            return NativeResult(result, 'call_generation')
        return result

    model.generate_native = invalid
    with pytest.raises(DomainError, match='证据引用.*未解决') as caught:
        await service.cases(run, analysis, scenarios)
    assert caught.value.category == 'invalid_reference'
    assert caught.value.call_id == 'call_ref_unresolved'
    assert not [a for a in store.list('artifact', chat_id=run['chat_id']) if a['type'] == 'cases']
    failed = [event['data'] for event in store.events(run['id']) if event['data'].get('event') == 'batch.validation_failed']
    assert failed[-1]['call_id'] == 'call_ref_unresolved'
    assert failed[-1]['item_ids'] == [repairs[0]['items'][0]['id']]
    cases = await service.cases(run, analysis, scenarios)
    assert len(cases['items']) == 2
    assert len(repairs) == 2
    assert repairs[0]['items'] == repairs[1]['items']
    assert len([task for task, _ in model.calls if task == 'generate_cases']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['invented_ref', 'wrong_quote', 'business_edit', 'wrong_id'])
async def test_bad_repair_cannot_publish_ungrounded_rows_or_overwrite_business_fields(setup, fault):
    store, run, service, model, analysis, scenarios = await upstream(setup)
    original = model.generate_native

    async def invalid(task, context, schema, instruction):
        if task == 'repair_evidence_refs':
            result = grounded(context)
            row = result['items'][0]
            if fault == 'invented_ref':
                row['refs'] = ['invented']
            elif fault == 'wrong_quote':
                row['support'][0]['quote'] = 'This statement is absent from the requirement.'
            elif fault == 'business_edit':
                row['title'] = 'Injected new title'
            else:
                row['id'] = 'other-case'
            return result
        result = await original(task, context, schema, instruction)
        if task == 'generate_cases':
            result['items'][0]['refs'] = []
        return result

    model.generate_native = invalid
    with pytest.raises(DomainError):
        await service.cases(run, analysis, scenarios)
    assert not [a for a in store.list('artifact', chat_id=run['chat_id']) if a['type'] == 'cases']


@pytest.mark.asyncio
async def test_valid_split_batch_is_reused_after_other_batch_reference_failure(setup):
    store, run, service, model, analysis, scenarios = await upstream(setup)
    original = model.generate_native
    generated_groups, repairs = [], []
    good_id, bad_id = [row['id'] for row in scenarios['items']]

    async def split(task, context, schema, instruction):
        if task == 'repair_evidence_refs':
            repairs.append(copy.deepcopy(context))
            if len(repairs) == 1:
                return {'items': [{'id': row['id'], 'refs': [], 'support': [],
                    'reason': 'No grounding yet.'} for row in context['items']]}
            return grounded(context)
        if task == 'generate_cases':
            ids = [row['id'] for row in context['scenarios']]
            generated_groups.append(ids)
            if len(ids) > 1:
                raise ContextCapacityError(16000)
        result = copy.deepcopy(await original(task, context, schema, instruction))
        if task == 'generate_cases' and ids == [bad_id]:
            result['items'][0]['refs'] = ['invalid']
        return result

    model.generate_native = split
    with pytest.raises(DomainError, match='证据引用'):
        await service.cases(run, analysis, scenarios)
    cases = await service.cases(run, analysis, scenarios)
    assert {row['scenario_id'] for row in cases['items']} == {good_id, bad_id}
    assert generated_groups.count([good_id]) == 1
    assert generated_groups.count([bad_id]) == 1
    assert len(repairs) == 2


@pytest.mark.asyncio
async def test_examples_cannot_survive_filter_or_become_repair_candidates():
    from tcg.reference_repair import repair_reference_fields
    evidence = [{'id': 'business#P1', 'role': 'primary', 'text': '登录成功显示首页。'},
                {'id': 'sample#P1', 'role': 'example', 'text': '无关样例。'}]
    context = {'evidence': evidence}
    original = NativeResult({'items': [{'id': 'C1', 'refs': ['business#P1', 'sample#P1']}],
                             'report': {'summary': ''}}, 'call_initial')
    calls = []

    async def repair(task, context, schema, instruction):
        calls.append(context)
        assert [item['id'] for item in context['evidence']] == ['business#P1']
        assert schema['properties']['items']['items']['properties']['refs']['items']['enum'] == ['business#P1']
        return grounded(context)

    filtered = await repair_reference_fields(repair, 'generate_cases', context, original)
    assert filtered['items'][0]['refs'] == ['business#P1']
    assert filtered.call_id == 'call_initial'
    assert not calls
    original['items'][0]['refs'] = ['sample#P1']
    fixed = await repair_reference_fields(repair, 'generate_cases', context, original)
    assert fixed['items'][0]['refs'] == ['business#P1']
    assert len(calls) == 1
