import copy
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from tcg.native_migration import migrate_legacy_runs
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
@pytest.mark.parametrize(('gate', 'mode'), [
    ('strategy_review', 'hitp'), ('scenario_review', 'hitp'),
    ('case_draft_review', 'hitp'), ('case_result_review', 'hitp'),
    ('scenario_review', 'auto'), ('clarification', 'hitp'),
])
async def test_legacy_gate_migrates_to_real_interrupt_without_generating_or_changing_artifacts(tmp_path, gate, mode):
    store, chat, business, runtime = setup(tmp_path, questions=(gate == 'clarification'))
    original = await legacy_waiting(store, chat, business, gate, mode)
    artifact_snapshots = copy.deepcopy(store.list('artifact', chat_id=chat['id']))
    original_calls = copy.deepcopy(business.calls)
    await runtime.start()
    outcomes = await migrate_legacy_runs(store, runtime)
    assert len(outcomes) == 1
    migrated = await runtime.snapshot(original['id'])
    assert migrated['id'] == original['id'] and migrated['mode'] == mode
    assert migrated['status'] == 'waiting' and migrated['interrupt']['type'] == gate
    assert migrated['interrupt']['artifact_id'] == original['interrupt']['artifact_id']
    assert migrated['interrupt']['artifact_revision'] == original['interrupt']['artifact_revision']
    assert migrated['interrupt']['id'] != 'old-id'
    stored = store.run(original['id'])
    assert stored['graph_version'] == 8 and stored['runtime'] == 'native'
    assert stored['_profile'] == original['_profile']
    assert stored['_source_ids'] == original['_source_ids']
    assert stored['artifact_ids'] == original['artifact_ids']
    assert all(key not in stored for key in ('_edit_token', '_interrupt_id', '_control_hold', '_boundary_requested', 'boundary_again'))
    checkpoint = await runtime.graph.aget_state(runtime._config(original['id']))
    assert checkpoint.tasks[0].interrupts[0].id == migrated['interrupt']['id']
    assert business.calls == original_calls
    assert store.list('artifact', chat_id=chat['id']) == artifact_snapshots
    assert await migrate_legacy_runs(store, runtime) == []
    assert await runtime.snapshot(original['id']) == migrated
    if gate == 'scenario_review' and mode == 'hitp':
        continued = await agree(runtime, migrated)
        assert continued['interrupt']['type'] == 'case_draft_review'
        assert business.calls[-1][0] == 'cases'
        assert len(business.calls) == len(original_calls) + 1
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'running'])
async def test_unfinished_legacy_generation_is_preserved_and_never_replayed(tmp_path, status):
    store, chat, business, runtime = setup(tmp_path)
    old = await legacy_waiting(store, chat, business, 'scenario_review')
    store.update_run(old['id'], status=status, stage='cases', interrupt=None)
    calls = copy.deepcopy(business.calls)
    artifacts = copy.deepcopy(store.list('artifact', chat_id=chat['id']))
    await runtime.start()
    result = (await migrate_legacy_runs(store, runtime))[0]
    assert result['status'] == 'failed' and result['stage'] == 'migration_required'
    assert '已有资料和成果版本已保留' in result['error']
    assert '重新开始' in result['error']
    assert business.calls == calls and runtime.tasks == {}
    assert store.list('artifact', chat_id=chat['id']) == artifacts
    assert await migrate_legacy_runs(store, runtime) == []
    with pytest.raises(DomainError, match='需要重新开始'):
        await runtime.retry(old['id'])
    await runtime.stop()
    store.close()


@pytest.mark.asyncio
async def test_unknown_legacy_confirmation_fails_visibly_without_guessing(tmp_path):
    store, chat, business, runtime = setup(tmp_path)
    old = await legacy_waiting(store, chat, business, 'scenario_review')
    store.update_run(old['id'], interrupt={'type': 'workflow_paused', 'node': 'boundary_cases'})
    calls = copy.deepcopy(business.calls)
    await runtime.start()
    result = (await migrate_legacy_runs(store, runtime))[0]
    assert result['status'] == 'failed'
    assert '没有可可靠恢复' in result['error']
    assert business.calls == calls
    assert store.run(old['id'])['artifact_ids'] == old['artifact_ids']
    await runtime.stop()
    store.close()
