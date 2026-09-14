import asyncio
import copy

import pytest

from tcg.native_business import NativeBusiness
from tcg.native_views import current_prompt
from tcg.pipeline import PipelineRuntime
from tcg.schemas import DomainError
from tcg.storage import Store
from test_native_business_v300 import NativeModel
from test_native_pipeline_v300 import agree, settled


class ReviewModel(NativeModel):
    async def generate_native(self, task, context, schema, instruction):
        result = await super().generate_native(task, context, schema, instruction)
        if task == 'understand_requirements':
            result['report']['questions'] = []
        if task == 'review_cases':
            result['report']['issues'] = [{'title': '调整标题', 'detail': context.get('review_feedback') or '标题需说明目的'}]
        return result


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('Review proposals')
    chat = store.create_chat(project['id'], '评审')
    store.add_source(chat['id'], 'requirements', 'primary', '支持登录。', [{'text': '支持登录。'}])
    model = ReviewModel()
    business = NativeBusiness(store, model)
    yield store, chat, model, business
    store.close()


async def review_gate(runtime, chat):
    run = await runtime.start_run(chat['id'], {'mode': 'hitp', 'content': '生成用例'})
    run = await settled(runtime, run['id'])
    assert run['interrupt']['type'] == 'strategy_review'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'scenario_review'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'case_result_review'
    return run


@pytest.mark.asyncio
async def test_human_review_proposes_before_writing_and_approval_applies_after_restart(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        cases = store.get('artifact', run['interrupt']['artifact_id'])
        assert cases['revision'] == 1 and '（评审）' not in cases['items'][0]['title']
        prompt = await current_prompt(store, runtime, store.get('chat', chat['id']))
        assert prompt['review']['issues'][0]['title'] == '调整标题'
        assert prompt['proposal_id'] and prompt['changes'][0]['after']['title'].endswith('（评审）')
        proposal_id = prompt['proposal_id']
        await runtime.stop()
        runtime = PipelineRuntime(store, business)
        await runtime.start()
        restored = await runtime.snapshot(run['id'])
        assert restored['interrupt']['proposal_id'] == proposal_id
        done = await agree(runtime, restored)
        assert done['status'] == 'completed'
        revised = store.get('artifact', cases['id'])
        assert revised['revision'] == 2 and revised['items'][0]['title'].endswith('（评审）')
        assert store.get('review_proposal', proposal_id)['status'] == 'applied'
        assert len([task for task, _ in model.calls if task == 'review_cases']) == 1
        repeated = business.apply_review_proposal(store.run(run['id']), proposal_id)
        assert repeated['revision'] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_auto_uses_same_proposal_path_and_applies(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await runtime.start_run(chat['id'], {'mode': 'auto', 'content': '生成用例'})
        run = await settled(runtime, run['id'])
        assert run['status'] == 'completed'
        proposals = store.list('review_proposal', chat_id=chat['id'])
        assert len(proposals) == 1 and proposals[0]['status'] == 'applied'
        cases = store.get('artifact', proposals[0]['artifact_id'])
        assert cases['revision'] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_review_feedback_creates_new_proposal_without_changing_cases(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        initial = copy.deepcopy(store.get('artifact', run['interrupt']['artifact_id']))
        first = run['interrupt']['proposal_id']
        queued = await runtime.revise_review(run['id'], '请保留原标题，只提出必要意见。')
        assert queued['status'] in ('queued', 'running')
        run = await settled(runtime, run['id'])
        assert run['interrupt']['type'] == 'case_result_review'
        assert run['interrupt']['proposal_id'] != first
        assert store.get('artifact', initial['id']) == initial
        context = [context for task, context in model.calls if task == 'review_cases'][-1]
        assert context['review_feedback'] == '请保留原标题，只提出必要意见。'
        assert context['previous_review']['items'][0]['title'].endswith('（评审）')
        assert store.get('review_proposal', first)['status'] == 'superseded'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stale_proposal_is_rejected_before_resume_without_writing_cases(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        cases = store.get('artifact', run['interrupt']['artifact_id'])
        changed = copy.deepcopy(cases['items'])
        changed[0]['title'] = '人工改名'
        newer = store.revise_artifact(cases['id'], cases['revision'], changed)
        with pytest.raises(DomainError, match='评审建议'):
            await runtime.resume(run['id'], expected_prompt_id=run['interrupt']['prompt_id'])
        assert store.get('artifact', cases['id']) == newer
        assert store.run(run['id'])['status'] == 'waiting'
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_case_edit_schedules_review_without_blocking_and_auto_edit_still_waits(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    blocked, release = asyncio.Event(), asyncio.Event()
    try:
        run = await runtime.start_run(chat['id'], {'mode': 'auto'})
        run = await settled(runtime, run['id'])
        cases = store.get('artifact', run['current_artifact_id'])
        rows = copy.deepcopy(cases['items'])
        rows[0]['title'] = '人工修改后标题'
        original = business.propose_review
        async def delayed(*args, **kwargs):
            blocked.set()
            await release.wait()
            return await original(*args, **kwargs)
        business.propose_review = delayed
        async with runtime.edit_session(chat['id']):
            changed = store.revise_artifact(cases['id'], cases['revision'], rows)
            receipts = await asyncio.wait_for(runtime.on_artifact_changed(changed), timeout=1)
        assert receipts[0]['status'] == 'queued'
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert store.get('artifact', cases['id'])['revision'] == changed['revision']
        release.set()
        waiting = await settled(runtime, run['id'])
        assert waiting['status'] == 'waiting' and waiting['interrupt']['type'] == 'case_result_review'
        assert store.get('artifact', cases['id'])['revision'] == changed['revision']
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancel_review_keeps_generated_cases_and_explicit_drafts_only_does_not_review(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        before = store.get('artifact', run['current_artifact_id'])
        await runtime.cancel(run['id'])
        assert store.get('artifact', before['id']) == before
        with pytest.raises(DomainError, match='取消'):
            business.apply_review_proposal(store.run(run['id']), run['interrupt']['proposal_id'])
        other = store.create_chat(chat['project_id'], '只要草稿')
        source = store.list('source', chat_id=chat['id'])[0]
        count = len([t for t, _ in model.calls if t == 'review_cases'])
        limited = await runtime.start_run(other['id'], {'mode': 'hitp', 'stop_after': 'cases', 'source_ids': [source['id']]})
        limited = await settled(runtime, limited['id'])
        limited = await agree(runtime, await agree(runtime, limited))
        assert limited['status'] == 'completed'
        assert limited.get('interrupt') is None
        assert len([t for t, _ in model.calls if t == 'review_cases']) == count
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_restart_and_retry_after_applied_commit_does_not_apply_twice(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        original = business.apply_review_proposal
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('simulated failure after commit before checkpoint')
        business.apply_review_proposal = interrupted
        await runtime.resume(run['id'], expected_prompt_id=run['interrupt']['prompt_id'])
        await asyncio.gather(runtime.tasks[run['id']], return_exceptions=True)
        assert store.get('artifact', run['interrupt']['artifact_id'])['revision'] == 2
        await runtime.stop()
        business.apply_review_proposal = original
        runtime = PipelineRuntime(store, business)
        await runtime.start()
        await runtime.retry(run['id'])
        restored = await settled(runtime, run['id'])
        assert restored['status'] == 'completed', restored.get('error')
        assert store.get('artifact', run['interrupt']['artifact_id'])['revision'] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_deleting_last_case_finishes_without_reviewing_empty_rows(setup):
    store, chat, model, business = setup
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        cases = store.get('artifact', run['current_artifact_id'])
        before_calls = len(model.calls)
        empty = store.revise_artifact(cases['id'], cases['revision'], [])
        async with runtime.edit_session(chat['id']):
            result = await runtime.on_artifact_changed(empty)
        assert result[0]['status'] == 'completed'
        assert store.get('artifact', cases['id'])['items'] == []
        assert len(model.calls) == before_calls
        with pytest.raises(DomainError, match='没有可评审'):
            await business.propose_review(store.run(run['id']), empty)
        scenarios = next(a for a in store.list('artifact', chat_id=chat['id']) if a['type'] == 'scenarios')
        analysis = next(a for a in store.list('artifact', chat_id=chat['id']) if a['type'] == 'analysis')
        no_scenarios = store.revise_artifact(scenarios['id'], scenarios['revision'], [])
        with pytest.raises(DomainError, match='没有可生成用例的场景'):
            await business.cases(store.run(run['id']), analysis, no_scenarios)
        assert len(model.calls) == before_calls
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_review_respects_case_column_contract_and_does_not_restore_deleted_default(setup):
    store, chat, model, business = setup
    profile = store.list('profile', project_id=chat['project_id'])[0]
    config = copy.deepcopy(profile['config'])
    config['excel_columns'].append({'field': 'legacy_default', 'header': '旧固定列',
                                    'value_source': 'default', 'default_value': '待执行'})
    store.update_profile(profile['id'], profile['name'], config, profile['version'])
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        cases = store.get('artifact', run['current_artifact_id'])
        assert cases['items'][0]['legacy_default'] == '待执行'
        rows = [{key: value for key, value in row.items() if key != 'legacy_default'} for row in cases['items']]
        report = {**cases['report'], 'table_columns': [col for col in config['excel_columns'] if col['field'] != 'legacy_default']}
        changed = store.revise_artifact(cases['id'], cases['revision'], rows, report=report)
        async with runtime.edit_session(chat['id']):
            await runtime.on_artifact_changed(changed)
        run = await settled(runtime, run['id'])
        assert run['interrupt']['type'] == 'case_result_review'
        proposal = store.get('review_proposal', run['interrupt']['proposal_id'])
        assert all('legacy_default' not in row for row in proposal['items'])
        assert (await agree(runtime, run))['status'] == 'completed'
        assert 'legacy_default' not in store.get('artifact', cases['id'])['items'][0]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_chat_context_contains_review_summary_only_and_details_tool_is_scoped(setup):
    from tcg.native_views import chat_context
    from tcg.tool_registry import build_tools
    store, chat, model, business = setup
    generate = model.generate_native
    async def multiple_cases(task, context, schema, instruction):
        result = await generate(task, context, schema, instruction)
        if task == 'generate_cases':
            for index in (2, 3):
                extra = copy.deepcopy(result['items'][0])
                extra['id'] += '-' + str(index)
                result['items'].append(extra)
        return result
    model.generate_native = multiple_cases
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await review_gate(runtime, chat)
        prompt = await current_prompt(store, runtime, store.get('chat', chat['id']))
        original = copy.deepcopy(prompt)
        context = await chat_context(store, runtime, store.get('chat', chat['id']), {}, prompt)
        brief = context['current_prompt']
        assert 'changes' not in brief and 'issues' not in brief['review']
        assert brief['change_counts'] == {'add': 0, 'update': 3, 'delete': 0}
        assert brief['review']['issue_count'] == 1
        assert brief['proposal_id'] == prompt['proposal_id']
        assert prompt == original and prompt['changes'][0]['after']['steps']
        registry = {tool.name: tool for tool in build_tools(store, business, runtime, chat, {}, prompt)}
        detail = await registry['read_review_proposal_tool'].ainvoke({'limit': 1})
        assert detail['changes'] == prompt['changes'][:1] and detail['total_changes'] == 3
        assert detail['next_offset'] == 1
        rest = await registry['read_review_proposal_tool'].ainvoke({'offset': 1, 'limit': 2})
        assert rest['changes'] == prompt['changes'][1:] and rest['next_offset'] is None
        assert detail['issues'][0]['title'] == '调整标题'
        assert store.get('artifact', prompt['artifact_id'])['revision'] == 1
        other = store.create_chat(chat['project_id'], 'another chat')
        foreign = {tool.name: tool for tool in build_tools(store, business, runtime, other, {})}
        denied = await foreign['read_review_proposal_tool'].ainvoke({'run_id': run['id'], 'proposal_id': prompt['proposal_id']})
        assert denied['status'] == 'needs_input' and '不属于' in denied['message']
        listed = await registry['list_context_tool'].ainvoke({})
        assert 'changes' not in listed['prompt']
    finally:
        await runtime.stop()
