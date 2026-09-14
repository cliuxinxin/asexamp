import copy
import json

import httpx
import pytest

from tcg.native_model import NativeResult
from tcg.schemas import DomainError
from tcg.server_capacity import ContextCapacityError
from test_native_business_v300 import setup


@pytest.mark.asyncio
async def test_coverage_repair_contains_exact_missing_ids_and_previous_response(setup):
    store, run, service, model = setup
    analysis = await service.understand(run)
    original = model.generate_native
    calls, first = [], None

    async def omission(task, context, schema, instruction):
        nonlocal first
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios':
            calls.append(copy.deepcopy(context))
            if len(calls) == 1:
                result['items'] = result['items'][:1]
                first = copy.deepcopy(result)
            return NativeResult(result, f'call_scenarios_{len(calls)}')
        return result

    model.generate_native = omission
    scenarios = await service.scenarios(run, analysis)
    assert len(calls) == 2
    assert calls[1]['validation_repair']['previous_result'] == first
    assert calls[1]['validation_repair']['details']['missing_input_ids'] == [analysis['items'][1]['id']]
    assert calls[1]['validation_repair']['validation_error']
    assert len(scenarios['items']) == 2


@pytest.mark.asyncio
async def test_three_corrections_preserve_all_candidates_and_never_cache_failed_success(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    analysis = await service.understand(run)
    original, candidates = model.generate_native, []

    async def omission(task, context, schema, instruction):
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios':
            result['items'] = result['items'][:1]
            result['items'][0]['title'] += f' revision {len(candidates)}'
            candidates.append(copy.deepcopy(result))
            return NativeResult(result, f'call_candidate_{len(candidates)}')
        return result

    model.generate_native = omission
    with pytest.raises(GenerationRepairExhausted) as caught:
        await service.scenarios(run, analysis)
    error = caught.value
    assert len(candidates) == error.attempts == 4
    assert error.retry_count == 3
    assert error.category == 'coverage'
    assert error.call_id == 'call_candidate_4'
    assert error.details['missing_input_ids'] == [analysis['items'][1]['id']]
    assert [entry['result'] for entry in error.candidate_history] == candidates
    assert store.cache_get(run['id'], error.candidate_key)['history'] == error.candidate_history
    assert not [a for a in store.list('artifact', chat_id=run['chat_id']) if a['type'] == 'scenarios']
    assert store.cache_get(run['id'], error.candidate_key.removesuffix(':repair_history')) is None


@pytest.mark.asyncio
async def test_schema_failure_is_one_shared_correction_budget_and_raw_fields_are_retained(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    original, calls = model.generate_native, []

    async def invalid(task, context, schema, instruction):
        calls.append(copy.deepcopy(context))
        result = await original(task, context, schema, instruction)
        result['unexpected_metadata'] = {'raw': 'keep exactly'}
        return NativeResult(result, f'call_schema_{len(calls)}')

    model.generate_native = invalid
    with pytest.raises(GenerationRepairExhausted) as caught:
        await service.understand(run)
    assert len(calls) == 4
    assert caught.value.candidate['unexpected_metadata'] == {'raw': 'keep exactly'}
    assert 'unexpected_metadata' in calls[1]['validation_repair']['details']['unexpected_fields']


@pytest.mark.asyncio
async def test_transport_and_permission_errors_do_not_trigger_content_retries(setup):
    store, run, service, model = setup
    calls = []

    async def unavailable(task, context, schema, instruction):
        calls.append(task)
        error = DomainError('connection unavailable')
        error.category = 'connection'
        raise error

    model.generate_native = unavailable
    with pytest.raises(DomainError, match='connection unavailable'):
        await service.understand(run)
    assert calls == ['understand_requirements']


@pytest.mark.asyncio
async def test_prior_valid_split_leaf_is_reused_after_later_leaf_exhausts(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    analysis = await service.understand(run)
    original, groups, fail = model.generate_native, [], True
    bad_id = analysis['items'][1]['id']

    async def split(task, context, schema, instruction):
        if task == 'generate_scenarios':
            ids = [row['id'] for row in context['analysis']]
            groups.append(ids)
            if len(ids) > 1:
                raise ContextCapacityError(16000)
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios' and ids == [bad_id] and fail:
            result['items'] = []
        return result

    model.generate_native = split
    with pytest.raises(GenerationRepairExhausted):
        await service.scenarios(run, analysis)
    fail = False
    store.update_run(run['id'], _content_retry_round=1)
    scenarios = await service.scenarios(run, analysis)
    assert len(scenarios['items']) == 2
    assert groups.count([analysis['items'][0]['id']]) == 1


@pytest.mark.asyncio
async def test_native_schema_retries_use_same_four_request_limit(setup, tmp_path):
    from tcg.generation_repair import GenerationRepairExhausted
    from tcg.model import LangChainGateway
    from test_native_model_v300 import completion, settings
    store, run, service, _ = setup
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion({'items': [], 'report': {'summary': 'raw'},
            'extra_root_field': 'keep me'}, 'submit_understand_requirements'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.gateway = LangChainGateway(settings(tmp_path), http_client=client)
        with pytest.raises(GenerationRepairExhausted) as caught:
            await service.understand(run)
    assert len(requests) == caught.value.attempts == 4
    assert all(entry['result']['extra_root_field'] == 'keep me' for entry in caught.value.candidate_history)
    assert json.loads(requests[-1]['messages'][-1]['content'])['validation_repair']['attempt'] == 3


@pytest.mark.asyncio
async def test_unparseable_tool_arguments_are_retained_unchanged(setup, tmp_path):
    from tcg.generation_repair import GenerationRepairExhausted
    from tcg.model import LangChainGateway
    from test_native_model_v300 import completion, settings
    store, run, service, _ = setup
    raw = '{"items": [{"id": "R1", "title": "unfinished'
    requests = []

    def handler(request):
        requests.append(request)
        envelope = completion({}, 'submit_understand_requirements')
        envelope['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = raw
        return httpx.Response(200, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service.gateway = LangChainGateway(settings(tmp_path), http_client=client)
        with pytest.raises(GenerationRepairExhausted) as caught:
            await service.understand(run)
    assert len(requests) == 4
    assert caught.value.candidate['invalid_tool_calls'][0]['args'] == raw


@pytest.mark.asyncio
async def test_reference_corrections_share_budget_and_never_filter_to_pass(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    analysis = await service.understand(run)
    original, calls = model.generate_native, []

    async def ungrounded(task, context, schema, instruction):
        calls.append(task)
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios':
            result['items'][0]['refs'].append('invented-ref')
        return result

    model.generate_native = ungrounded
    with pytest.raises(GenerationRepairExhausted) as caught:
        await service.scenarios(run, analysis)
    assert calls == ['generate_scenarios'] + ['repair_evidence_refs'] * 3
    assert 'invented-ref' in caught.value.candidate['items'][0]['refs']
    assert caught.value.category == 'invalid_reference'
    assert caught.value.candidate_history[0]['result']['items'][0]['refs'][-1] == 'invented-ref'


@pytest.mark.asyncio
async def test_recovery_does_not_reset_budget_and_explicit_retry_carries_previous_failure(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    analysis = await service.understand(run)
    original, contexts = model.generate_native, []

    async def omission(task, context, schema, instruction):
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios':
            contexts.append(copy.deepcopy(context))
            if len(contexts) <= 4:
                result['items'] = result['items'][:1]
        return result

    model.generate_native = omission
    with pytest.raises(GenerationRepairExhausted):
        await service.scenarios(run, analysis)
    with pytest.raises(GenerationRepairExhausted):
        await service.scenarios(run, analysis)
    assert len(contexts) == 4
    store.update_run(run['id'], _content_retry_round=1)
    scenarios = await service.scenarios(run, analysis)
    assert len(scenarios['items']) == 2
    assert len(contexts) == 5
    assert contexts[-1]['validation_repair']['details']['missing_input_ids'] == [analysis['items'][1]['id']]


@pytest.mark.asyncio
async def test_recovery_validates_archived_last_response_without_a_fifth_request(setup):
    from tcg.generation_repair import GenerationRepairExhausted
    store, run, service, model = setup
    analysis = await service.understand(run)
    original, calls, complete = model.generate_native, [], None

    async def omission(task, context, schema, instruction):
        nonlocal complete
        result = await original(task, context, schema, instruction)
        if task == 'generate_scenarios':
            calls.append(task)
            complete = copy.deepcopy(result)
            result['items'] = result['items'][:1]
        return result

    model.generate_native = omission
    with pytest.raises(GenerationRepairExhausted) as caught:
        await service.scenarios(run, analysis)
    state = store.cache_get(run['id'], caught.value.candidate_key)
    # Simulate a process stopping immediately after archiving the fourth reply.
    state['history'][-1] = {'attempt': 4, 'task': 'generate_scenarios',
        'call_id': 'call_saved_before_validation', 'result': complete}
    store.cache_set(run['id'], caught.value.candidate_key, state)
    scenarios = await service.scenarios(run, analysis)
    assert len(scenarios['items']) == 2
    assert len(calls) == 4
