import copy
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from tcg.pipeline import PipelineRuntime
from tcg.schemas import DomainError
from test_native_pipeline_v300 import Business, agree, setup


class OldState(TypedDict, total=False):
    run_id: str
    analysis_ref: str
    scenario_ref: str
    cases_ref: str


async def legacy_waiting(store, chat, business, gate, mode='hitp'):
    _, run = store.create_run(chat['id'], {'mode': mode, 'intent': 'generate_case',
        'experience': 'reliable', 'content': '生成测试用例'})
    analysis = await business.understand(run)
    store.update_run(run['id'], artifact_ids=[analysis['id']])
    scenario = await business.scenarios(store.run(run['id']), analysis)
    store.update_run(run['id'], artifact_ids=[analysis['id'], scenario['id']])
    cases = await business.cases(store.run(run['id']), analysis, scenario)
    store.update_run(run['id'], artifact_ids=[analysis['id'], scenario['id'], cases['id']])
    current = {'clarification': analysis, 'strategy_review': analysis,
               'scenario_review': scenario, 'case_draft_review': cases,
               'case_result_review': cases}[gate]
    pending = {'type': gate, 'artifact_id': current['id'], 'artifact_revision': current['revision']}
    store.update_run(run['id'], graph_version=7, runtime='legacy', experience='reliable',
        status='waiting', stage=gate, interrupt=pending,
        _interrupt_id='old-id', interrupt_id='old-id', _edit_token='dead-process',
        _control_hold=True, _boundary_requested={'reason': 'edit'}, boundary_again=True)
    pointers = {'run_id': run['id'], 'analysis_ref': analysis['id'],
                'scenario_ref': scenario['id'], 'cases_ref': cases['id']}
    # A real old SQLite checkpoint is enough to recover pointers; no legacy
    # controller or production generation module is loaded by the migration.
    async with AsyncSqliteSaver.from_conn_string(str(store.directory / 'checkpoints.sqlite3')) as saver:
        await saver.setup()
        builder = StateGraph(OldState)
        builder.add_node('saved', lambda state: state)
        builder.add_node('old_gate', lambda state: interrupt(pending))
        builder.add_edge(START, 'saved')
        builder.add_edge('saved', 'old_gate')
        builder.add_edge('old_gate', END)
        graph = builder.compile(checkpointer=saver)
        config = {'configurable': {'thread_id': run['id']}}
        await graph.aupdate_state(config, pointers, as_node='saved')
        await graph.ainvoke(None, config)
    return store.run(run['id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'running', 'waiting'])
@pytest.mark.parametrize('version', [6, 7])
async def test_active_legacy_runs_require_restart_without_replaying_or_modifying_artifacts(tmp_path, status, version):
    store, chat, business, runtime = setup(tmp_path)
    original = await legacy_waiting(store, chat, business, 'scenario_review')
    store.update_run(original['id'], status=status, graph_version=version)
    artifacts = copy.deepcopy(store.list('artifact', chat_id=chat['id']))
    calls = copy.deepcopy(business.calls)
    await runtime.start()
    result = await runtime.snapshot(original['id'])
    assert result['status'] == 'failed' and result['stage'] == 'restart_required'
    assert '已有资料和成果版本已保留' in result['error']
    assert '重新开始' in result['error']
    assert business.calls == calls and runtime.tasks == {}
    assert store.list('artifact', chat_id=chat['id']) == artifacts
    assert store.run(original['id'])['artifact_ids'] == original['artifact_ids']
    with pytest.raises(DomainError, match='需要重新开始'):
        await runtime.retry(original['id'])
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_completed_legacy_history_is_not_rewritten(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    old = await legacy_waiting(store, chat, business, 'scenario_review')
    old = store.update_run(old['id'], status='completed')
    await runtime.start()
    assert store.run(old['id']) == old
    assert runtime.tasks == {}
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_editing_legacy_native_artifact_does_not_reanchor_or_read_checkpoint(tmp_path, monkeypatch):
    from test_native_pipeline_v300 import settled
    store, chat, business, runtime = setup(tmp_path)
    await runtime.start()
    run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'hitp'}))['id'])
    run = await agree(runtime, run)
    run = await agree(runtime, run)
    cases = store.get('artifact', run['interrupt']['artifact_id'])
    checkpoint = await runtime.graph.aget_state(runtime._config(run['id']))
    await runtime.stop()
    store.update_run(run['id'], graph_version=7, runtime='native')
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        stopped = store.run(run['id'])
        assert stopped['status'] == 'failed' and stopped['stage'] == 'restart_required'
        calls = copy.deepcopy(business.calls)
        reads = []
        original_read = runtime.graph.aget_state

        async def record_read(config, **kwargs):
            reads.append(config)
            return await original_read(config, **kwargs)

        monkeypatch.setattr(runtime.graph, 'aget_state', record_read)
        rows = copy.deepcopy(cases['items'])
        rows[0]['title'] = '人工更新保留的历史用例'
        # Workspace and restore both finish with this real artifact commit/reanchor boundary.
        changed = store.revise_artifact(cases['id'], cases['revision'], rows)
        async with runtime.edit_session(chat['id']):
            updates = await runtime.on_artifact_changed(changed)
        assert updates == []
        assert reads == []
        assert runtime.tasks == {} and business.calls == calls
        assert store.run(run['id']) == stopped
        assert store.get('artifact', cases['id']) == changed
        assert store.revision(cases['id'], cases['revision'])['items'] == cases['items']
        persisted = await original_read(runtime._config(run['id']))
        assert persisted.values == checkpoint.values and persisted.next == checkpoint.next
        with pytest.raises(DomainError, match='需要重新开始'):
            await runtime.retry(run['id'])
    finally:
        await runtime.stop()
        store.close()
