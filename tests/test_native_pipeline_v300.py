import asyncio
import copy

import pytest

from tcg.pipeline import PipelineRuntime, PipelineState
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.diagnostics import Diagnostics


class Business:
    def __init__(self, store, questions=False):
        self.store = store
        self.calls = []
        self.questions = questions
        self.started = asyncio.Event()
        self.release = None
        self.fail_cases_once = False

    def save(self, run, kind, items, report=None):
        existing = next((self.store.get('artifact', aid) for aid in run.get('artifact_ids', [])
                         if self.store.get('artifact', aid)['type'] == kind), None)
        if existing:
            return self.store.revise_artifact(existing['id'], existing['revision'], items, report=report)
        return self.store.artifact(run['id'], 'native:' + kind, kind, kind, items, report)

    def ref(self, run):
        return self.store.evidence(run['_source_ids'])[0]['id']

    async def understand(self, run):
        self.calls.append(('understand', None))
        self.started.set()
        if self.release:
            await self.release.wait()
        report = {'questions': [{'id': 'Q1', 'question': '失败如何处理？', 'suggestion': '显示错误提示。'}]} if self.questions else {}
        return self.save(run, 'analysis', [{'id': 'R1', 'title': '登录需求', 'description': '需要登录', 'refs': [self.ref(run)]}], report)

    async def clarify(self, run, artifact, answers):
        self.calls.append(('clarify', answers))
        return self.store.revise_artifact(artifact['id'], artifact['revision'], artifact['items'], report={'questions': []})

    async def scenarios(self, run, analysis):
        self.calls.append(('scenarios', analysis['items'][0]['title']))
        return self.save(run, 'scenarios', [{'id': 'S1', 'title': analysis['items'][0]['title'] + '场景',
            'description': '登录成功', 'priority': 'P1', 'requirement_ids': ['R1'], 'refs': [self.ref(run)]}])

    async def cases(self, run, analysis, scenarios):
        self.calls.append(('cases', scenarios['items'][0]['title']))
        if self.fail_cases_once:
            self.fail_cases_once = False
            raise RuntimeError('server unavailable')
        return self.save(run, 'cases', [{'id': 'C1', 'title': scenarios['items'][0]['title'] + '用例',
            'scenario_id': 'S1', 'type': 'Business', 'priority': 'P1', 'preconditions': '',
            'steps': [{'action': '登录', 'expected': '成功'}], 'refs': [self.ref(run)]}])

    async def propose_review(self, run, cases, feedback=''):
        from tcg.dependencies import manifest
        from tcg.review_proposals import save_review_proposal
        self.calls.append(('review', cases['items'][0]['title']))
        report = {'review_reports': [{'summary': '已检查', 'issues': []}]}
        return save_review_proposal(self.store, run, cases, cases['items'], report,
            manifest(self.store, artifact_ids=[cases['id']], source_ids=run['_source_ids']),
            run['_source_ids'], run.get('_source_roles', {}), feedback)

    def apply_review_proposal(self, run, proposal_id):
        from tcg.native_business import NativeBusiness
        return NativeBusiness(self.store, None).apply_review_proposal(run, proposal_id)


def setup(tmp_path, **business_options):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'native pipeline')
    store.add_source(chat['id'], 'requirements', 'primary', '支持登录。', [{'text': '支持登录。'}])
    business = Business(store, **business_options)
    return store, chat, business, PipelineRuntime(store, business)


async def settled(runtime, run_id):
    for _ in range(1000):
        task = runtime.tasks.get(run_id)
        if task is not None:
            await asyncio.wait({task}, timeout=0.01)
        run = await runtime.snapshot(run_id)
        if run['status'] not in ('queued', 'running') and run_id not in runtime.tasks:
            return run
    raise AssertionError('pipeline did not settle')


async def agree(runtime, run):
    clarification = run['interrupt']['type'] == 'clarification'
    payload = {'answers': {q['id']: q['suggestion'] for q in run['interrupt']['questions']}} if clarification else None
    await runtime.resume(run['id'], action='clarify' if clarification else 'approved',
                         expected_prompt_id=run['interrupt']['prompt_id'], payload=payload)
    return await settled(runtime, run['id'])


@pytest.mark.asyncio
async def test_native_three_gates_and_edit_survive_restart(tmp_path):
    store, chat, first_business, runtime = setup(tmp_path)
    await runtime.start()
    run = await runtime.start_run(chat['id'], {'mode': 'hitp'})
    run = await settled(runtime, run['id'])
    assert run['interrupt']['type'] == 'strategy_review'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'scenario_review'
    native_before = await runtime.graph.aget_state(runtime._config(run['id']))
    assert native_before.tasks[0].interrupts[0].id == run['interrupt']['id']
    assert set(native_before.values) <= PipelineState.__annotations__.keys()
    assert not any(k in store.run(run['id']) for k in ('_edit_token', '_interrupt_id', '_control_hold'))
    artifact = store.get('artifact', run['interrupt']['artifact_id'])
    items = copy.deepcopy(artifact['items'])
    items[0]['title'] = '暂停期间修改后的场景'
    previous_prompt = run['interrupt']['prompt_id']
    async with runtime.edit_session(chat['id']):
        changed = store.revise_artifact(artifact['id'], artifact['revision'], items)
        await runtime.on_artifact_changed(changed)
    run = await runtime.snapshot(run['id'])
    assert run['interrupt']['type'] == 'scenario_review'
    assert run['interrupt']['prompt_id'] != previous_prompt
    with pytest.raises(DomainError, match='提示已更新'):
        await runtime.resume(run['id'], expected_prompt_id=previous_prompt)
    await runtime.stop()

    second_business = Business(store)
    runtime = PipelineRuntime(store, second_business)
    await runtime.start()
    recovered = await runtime.snapshot(run['id'])
    assert recovered['interrupt'] == run['interrupt']
    assert second_business.calls == []
    reviewed = await agree(runtime, recovered)
    assert reviewed['interrupt']['type'] == 'case_result_review'
    assert second_business.calls[0] == ('cases', '暂停期间修改后的场景')
    final = await agree(runtime, reviewed)
    assert final['status'] == 'completed'
    assert [c[0] for c in first_business.calls] == ['understand', 'scenarios']
    assert [c[0] for c in second_business.calls] == ['cases', 'review']
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_upstream_edit_rewinds_native_gate_and_preserves_confirmation_order(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    analysis = store.get('artifact', run['interrupt']['artifact_id'])
    run = await agree(runtime, await agree(runtime, run))
    assert run['interrupt']['type'] == 'case_result_review'
    items = copy.deepcopy(analysis['items'])
    items[0]['title'] = '补充白名单后的登录需求'
    async with runtime.edit_session(chat['id']):
        await runtime.on_artifact_changed(store.revise_artifact(analysis['id'], analysis['revision'], items))
    run = await runtime.snapshot(run['id'])
    assert run['interrupt']['type'] == 'strategy_review'
    assert [c[0] for c in business.calls] == ['understand', 'scenarios', 'cases', 'review']
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'scenario_review'
    assert business.calls[-1] == ('scenarios', '补充白名单后的登录需求')
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'case_result_review'
    assert business.calls[-2] == ('cases', '补充白名单后的登录需求场景')
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_clarification_agreement_only_reaches_understanding_gate(tmp_path):
    store, chat, business, runtime = setup(tmp_path, questions=True)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    assert run['interrupt']['type'] == 'clarification'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'strategy_review'
    assert business.calls[-1] == ('clarify', {'失败如何处理？': '显示错误提示。'})
    assert not any(c[0] == 'scenarios' for c in business.calls)
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_edit_and_old_confirmation_are_serialized_without_durable_edit_locks(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    original_prompt = run['interrupt']['prompt_id']
    artifact = store.get('artifact', run['interrupt']['artifact_id'])
    async with runtime.edit_session(chat['id']):
        continuation = asyncio.create_task(runtime.resume(run['id'], expected_prompt_id=original_prompt))
        await asyncio.sleep(0)
        assert not continuation.done()
        changed_rows = [{**row, 'title': '新确认版本'} for row in artifact['items']]
        await runtime.on_artifact_changed(store.revise_artifact(artifact['id'], artifact['revision'], changed_rows))
    with pytest.raises(DomainError, match='提示已更新'):
        await continuation
    current = await runtime.snapshot(run['id'])
    assert current['status'] == 'waiting' and current['interrupt']['type'] == 'strategy_review'
    assert len(business.calls) == 1
    assert not any(k in store.run(run['id']) for k in ('_edit_token', '_interrupt_id', '_control_hold'))
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_restore_waiting_uses_actual_native_checkpoint_without_generation(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    run = await agree(runtime, run)
    state = await runtime.graph.aget_state(runtime._config(run['id']))
    calls = copy.deepcopy(business.calls)
    restored = await runtime.restore_waiting(run['id'], state.values, 'scenario_review')
    assert restored['interrupt']['type'] == 'scenario_review'
    assert business.calls == calls
    checkpoint = await runtime.graph.aget_state(runtime._config(run['id']))
    assert checkpoint.tasks[0].interrupts[0].id == restored['interrupt']['id']
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_completed_task_can_reenter_the_same_flow_after_supplement_edit(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    analysis = store.get('artifact', run['interrupt']['artifact_id'])
    for _ in range(3):
        run = await agree(runtime, run)
    assert run['status'] == 'completed'
    assert len(business.calls) == 4
    source = store.add_source(chat['id'], '补充需求', 'supplement', '需要白名单。', [{'text': '需要白名单。'}])
    rows = [{**row, 'title': '完成后补充的白名单需求'} for row in analysis['items']]
    async with runtime.edit_session(chat['id']):
        revised = store.revise_artifact(analysis['id'], analysis['revision'], rows,
            source_ids=[source['id']], source_roles={source['id']: 'supplement'})
        await runtime.on_artifact_changed(revised)
    reopened = await runtime.snapshot(run['id'])
    assert reopened['status'] == 'waiting' and reopened['interrupt']['type'] == 'strategy_review'
    assert len(business.calls) == 4
    assert source['id'] in store.run(run['id'])['_source_ids']
    assert (await agree(runtime, reopened))['interrupt']['type'] == 'scenario_review'
    assert business.calls[-1] == ('scenarios', '完成后补充的白名单需求')
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'intent', 'called', 'gate'), [
    ('analysis', 'generate_scenario', 'scenarios', 'scenario_review'),
    ('scenarios', 'generate_case', 'cases', 'case_result_review'),
    ('cases', 'review_case', 'review', 'case_result_review'),
])
async def test_start_from_existing_artifact_only_executes_requested_downstream_stage(tmp_path, kind, intent, called, gate):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    original = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
    assert original['status'] == 'completed'
    artifacts = {a['type']: a for a in store.list('artifact', chat_id=chat['id'])}
    scenario = artifacts['scenarios']
    artifacts['scenarios'] = store.revise_artifact(scenario['id'], scenario['revision'], scenario['items'],
        report={'lineage': {'analysis_artifact_id': artifacts['analysis']['id'],
                            'analysis_revision': artifacts['analysis']['revision']}})
    source = artifacts[kind]
    previous_calls = len(business.calls)
    started = await runtime.start_run(chat['id'], {'mode': 'hitp', 'intent': intent, 'artifact_id': source['id']})
    current = await settled(runtime, started['id'])
    assert current['interrupt']['type'] == gate
    assert [call[0] for call in business.calls[previous_calls:]] == ([called, 'review'] if called == 'cases' else [called])
    state = await runtime.graph.aget_state(runtime._config(current['id']))
    assert state.values[{'analysis': 'analysis_ref', 'scenarios': 'scenario_ref', 'cases': 'cases_ref'}[kind]] == source['id']
    assert source['id'] in current['artifact_ids']
    assert current['start_context']['artifact_id'] == source['id']
    if kind == 'cases':
        assert current['interrupt']['artifact_id'] == source['id']
        assert store.get('artifact', source['id'])['revision'] == source['revision']
    else:
        assert store.get('artifact', source['id'])['revision'] == source['revision']
        assert current['interrupt']['artifact_id'] != artifacts['cases' if kind == 'scenarios' else 'scenarios']['id']
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_partial_clarification_waits_for_remaining_questions_before_understanding(tmp_path):
    store, chat, business, runtime = setup(tmp_path, questions=True)
    original_understand = business.understand

    async def understand_two(run):
        artifact = await original_understand(run)
        questions = artifact['report']['questions'] + [{'id': 'Q2', 'question': '失败是否重试？', 'suggestion': '暂不增加自动重试规则。'}]
        return store.revise_artifact(artifact['id'], artifact['revision'], artifact['items'], report={'questions': questions})

    async def clarify_partial(run, artifact, answers):
        business.calls.append(('clarify', answers))
        remaining = [question for question in artifact['report']['questions'] if question['question'] not in answers]
        return store.revise_artifact(artifact['id'], artifact['revision'], artifact['items'], report={'questions': remaining})

    business.understand = understand_two
    business.clarify = clarify_partial
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    original_prompt = run['interrupt']['prompt_id']
    await runtime.resume(run['id'], 'clarify', original_prompt, {'answers': {'Q1': '显示错误提示。'}})
    current = await settled(runtime, run['id'])
    assert current['interrupt']['type'] == 'clarification'
    assert current['interrupt']['prompt_id'] != original_prompt
    assert [question['id'] for question in current['interrupt']['questions']] == ['Q2']
    current = await agree(runtime, current)
    assert current['interrupt']['type'] == 'strategy_review'
    assert [call[0] for call in business.calls] == ['understand', 'clarify', 'clarify']
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_auto_pause_uses_existing_native_gate_then_resumes_auto(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    business.release = asyncio.Event()
    await runtime.start()
    run = await runtime.start_run(chat['id'], {'mode': 'auto'})
    await business.started.wait()
    assert (await runtime.snapshot(run['id']))['status'] == 'running'
    with pytest.raises(DomainError, match='当前步骤正在生成'):
        async with runtime.edit_session(chat['id']):
            pass
    await runtime.request_pause(run['id'])
    business.release.set()
    run = await settled(runtime, run['id'])
    assert run['mode'] == 'auto' and run['interrupt']['type'] == 'strategy_review'
    run = await agree(runtime, run)
    assert run['status'] == 'completed' and run['mode'] == 'auto'
    assert [c[0] for c in business.calls] == ['understand', 'scenarios', 'cases', 'review']
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_retry_only_failed_node_and_stop_after_never_expands_scope(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    business.fail_cases_once = True
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
    assert run['status'] == 'failed' and run['failed_node'] == 'cases'
    await runtime.retry(run['id'])
    run = await settled(runtime, run['id'])
    assert run['status'] == 'completed'
    assert [c[0] for c in business.calls] == ['understand', 'scenarios', 'cases', 'cases', 'review']
    other = store.create_chat(chat['project_id'], 'scenarios only')
    source = store.list('source', chat_id=chat['id'])[0]
    limited = await settled(runtime, (await runtime.start_run(other['id'], {
        'mode': 'hitp', 'source_ids': [source['id']], 'stop_after': 'scenarios'}))['id'])
    limited = await agree(runtime, limited)
    assert limited['interrupt']['type'] == 'scenario_review'
    count = len(business.calls)
    limited = await agree(runtime, limited)
    assert limited['status'] == 'completed' and len(business.calls) == count
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_retry_does_not_start_a_second_active_pipeline_in_the_same_chat(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    business.fail_cases_once = True
    failed = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
    assert failed['status'] == 'failed'
    current = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    with pytest.raises(DomainError, match='另一个活动任务'):
        await runtime.retry(failed['id'])
    assert (await runtime.snapshot(current['id']))['status'] == 'waiting'
    assert store.run(failed['id'])['status'] == 'failed'
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_native_pipeline_emits_canonical_lifecycle_once_across_snapshot_polling(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    diagnostics = Diagnostics(store)
    runtime.diagnostics = diagnostics
    try:
        await runtime.start()
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        first = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        assert any(event['event'] == 'node.start' and event['node'] == 'understand' for event in first)
        assert any(event['event'] == 'node.complete' and event['node'] == 'understand' for event in first)
        assert any(event['event'] == 'node.interrupted' and event['node'] == 'strategy_review' for event in first)
        before = len(first)
        await runtime.snapshot(run['id'])
        await runtime.snapshot(run['id'])
        assert len([event for event in store.events(run['id']) if event['kind'] == 'progress']) == before

        for _ in range(3):
            run = await agree(runtime, run)
        events = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        starts = [event['node'] for event in events if event['event'] == 'node.start']
        completes = [event['node'] for event in events if event['event'] == 'node.complete']
        assert starts == completes == ['understand', 'scenarios', 'cases', 'review', 'apply_review']
        assert sum(event['event'] == 'run.resumed' for event in events) == 3
        assert sum(event['event'] == 'run.completed' for event in events) == 1
        terminal_count = len(events)
        await runtime.snapshot(run['id'])
        assert len([event for event in store.events(run['id']) if event['kind'] == 'progress']) == terminal_count
    finally:
        await runtime.stop()
        diagnostics.close()
        store.close()


@pytest.mark.asyncio
async def test_progress_persistence_failure_does_not_block_native_pipeline_or_leak_context(tmp_path, monkeypatch):
    store, chat, business, runtime = setup(tmp_path)
    diagnostics = Diagnostics(store)
    runtime.diagnostics = diagnostics
    append = store.append_event

    def fail_progress(run_id, kind, data):
        if kind == 'progress':
            raise DomainError('progress unavailable')
        return append(run_id, kind, data)

    monkeypatch.setattr(store, 'append_event', fail_progress)
    try:
        await runtime.start()
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'strategy_review'
        assert diagnostics.context.get() == {}
    finally:
        await runtime.stop()
        diagnostics.close()
        store.close()


@pytest.mark.asyncio
async def test_native_auto_pipeline_emits_saved_nodes_and_one_completed_transition(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    diagnostics = Diagnostics(store)
    runtime.diagnostics = diagnostics
    try:
        await runtime.start()
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
        events = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        completed_nodes = {event['node'] for event in events if event['event'] == 'node.complete'}
        assert completed_nodes >= {'understand', 'scenarios', 'cases', 'review'}
        assert not any(event['event'] == 'node.interrupted' for event in events)
        assert sum(event['event'] == 'run.completed' for event in events) == 1
    finally:
        await runtime.stop()
        diagnostics.close()
        store.close()


@pytest.mark.asyncio
async def test_native_pipeline_cancellation_emits_node_and_run_cancellation(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    diagnostics = Diagnostics(store)
    runtime.diagnostics = diagnostics
    business.release = asyncio.Event()
    try:
        await runtime.start()
        run = await runtime.start_run(chat['id'], {'mode': 'auto'})
        await business.started.wait()
        await runtime.cancel(run['id'])
        events = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        assert any(event['event'] == 'node.cancelled' and event['node'] == 'understand' for event in events)
        assert sum(event['event'] == 'run.cancelled' for event in events) == 1
    finally:
        await runtime.stop()
        diagnostics.close()
        store.close()


@pytest.mark.asyncio
async def test_native_pipeline_failure_emits_node_error_and_run_failed(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    diagnostics = Diagnostics(store)
    runtime.diagnostics = diagnostics
    business.fail_cases_once = True
    try:
        await runtime.start()
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
        events = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        assert any(event['event'] == 'node.error' and event['node'] == 'cases' for event in events)
        assert sum(event['event'] == 'run.failed' for event in events) == 1
        failure = next(m for m in store.list('message', chat_id=chat['id'])
                       if m.get('metadata', {}).get('pipeline_failure'))
        assert failure['role'] == 'assistant'
        assert failure['metadata']['turn_response']['parts'][0]['type'] == 'diagnostic'
        assert '已有成果' in failure['content']
    finally:
        await runtime.stop()
        diagnostics.close()
        store.close()
