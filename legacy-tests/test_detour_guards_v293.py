"""Targeted guards for a Human edit detour around an existing graph gate."""
import copy

import pytest

from test_conversation_artifacts_v260 import Engine as ModelEngine, data
from tcg.artifact_actions import rebind_waiting_runs
from tcg.conversation_workflow import execute
from tcg.graph import Engine as GraphEngine
from tcg.schemas import DomainError
from tcg.workflow_bindings import bind_interrupt


@pytest.fixture
def branch(tmp_path):
    fixture = data(tmp_path)
    yield next(fixture)
    next(fixture, None)


class Engine(ModelEngine):
    def __init__(self, store, respond):
        super().__init__(respond)
        self.store, self.tasks, self.edit_tasks = store, {}, {}

    request_boundary = GraphEngine.request_boundary
    cancel = GraphEngine.cancel
    resume = GraphEngine.resume

    def trace(self, *args, **kwargs):
        pass

    def schedule(self, rid):
        raise AssertionError('A detour must not schedule or consume the native checkpoint.')


def gate(store, chat, artifact_id, kind):
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'hitp',
        'content': 'Generate', 'artifact_id': artifact_id})
    return store.update_run(run['id'], status='waiting', stage=kind,
        interrupt=bind_interrupt(store, {'type': kind, 'artifact_id': artifact_id}),
        _interrupt_id='native-gate', interrupt_id='native-gate',
        control_version=0, _control_version=0, stop_after='complete')


def edit(store, artifact, item_id, **fields):
    updated = store.revise_artifact(artifact['id'], artifact['revision'],
        [{**row, **fields} if row['id'] == item_id else row for row in artifact['items']])
    rebind_waiting_runs(store, [updated])
    return updated


def answer_args(store, rid):
    run = store.run(rid)
    return {'run_id': rid, 'approved': True, 'interrupt_id': run['_interrupt_id'],
        'expected_control_version': run['control_version'],
        'expected_revision': run['interrupt']['artifact_revision']}


@pytest.mark.asyncio
async def test_start_alias_reuses_detour_and_stops_at_next_confirmation(branch):
    store, chat, analysis, scenarios, cases, _, _ = branch
    run = gate(store, chat, 'scenarios', 'scenario_review')
    edit(store, analysis, 'R1', description='Lock for twenty minutes')
    calls = []

    def respond(task, context):
        calls.append(task)
        assert task == 'artifact_sync_scenarios'
        return {'operations': [{'op': 'update', 'id': 'S1', 'item': {
            'description': context['analysis'][0]['description']}}], 'summary': 'Updated scenario'}

    result = await execute(store, Engine(store, respond), chat, 'workflow.start',
        {'content': '继续生成用例', 'intent': 'generate_case'})
    current = store.run(run['id'])
    assert result['status'] == 'succeeded'
    assert current['status'] == 'waiting'
    assert current['interrupt']['type'] == 'scenario_review'
    assert current['_interrupt_id'] == 'native-gate'
    assert not current.get('_upstream_confirmation')
    assert store.get('artifact', 'scenarios')['revision'] == scenarios['revision'] + 1
    assert store.get('artifact', 'cases') == cases
    assert calls == ['artifact_sync_scenarios']


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'cancel', 'scope'])
async def test_late_reassessment_cannot_replace_newer_pause_or_cancel(branch, control):
    store, chat, _, scenarios, _, _, _ = branch
    run = gate(store, chat, 'cases', 'case_result_review')
    edit(store, scenarios, 'S1', description='Lock for twenty minutes')
    calls = []

    def respond(task, context):
        calls.append(task)
        if task == 'artifact_sync':
            return {'operations': [{'op': 'update', 'id': 'C1', 'item': {
                'steps': [{'action': 'Log in', 'expected': 'Lock for twenty minutes'}]}}],
                'summary': 'Updated case'}
        assert task == 'artifact_review_readonly'
        if control == 'pause':
            engine.request_boundary(run['id'], reason='pause')
        elif control == 'scope':
            from tcg.conversation_workflow import _apply_scope
            _apply_scope(store, store.run(run['id']), {'scope': 'Only login', 'stop_after': 'cases'})
        else:
            engine.cancel(run['id'])
        return {'report': {'summary': 'The updated steps are aligned.', 'issues': [], 'coverage': []}}

    engine = Engine(store, respond)
    await execute(store, engine, chat, 'workflow.continue', answer_args(store, run['id']))
    waiting = store.run(run['id'])
    assert waiting['interrupt']['type'] == 'case_draft_review'
    before = copy.deepcopy(store.get('artifact', 'cases'))
    with pytest.raises(DomainError) as exc:
        await execute(store, engine, chat, 'workflow.continue', answer_args(store, run['id']))
    assert exc.value.status == 409
    assert store.get('artifact', 'cases') == before
    current = store.run(run['id'])
    assert current.get('_upstream_confirmation')
    assert not current.get('_edit_token')
    assert current['status'] == ('cancelled' if control == 'cancel' else 'waiting')
    if control in ('pause', 'scope'):
        assert current['_interrupt_id'] == waiting['_interrupt_id']
        assert current['interrupt']['type'] == 'case_draft_review'
    assert calls == ['artifact_sync', 'artifact_review_readonly']


@pytest.mark.asyncio
async def test_reassessment_restores_native_result_gate_without_approving_it(branch):
    store, chat, _, scenarios, _, _, _ = branch
    run = gate(store, chat, 'cases', 'case_result_review')
    edit(store, scenarios, 'S1', description='Lock for twenty minutes')
    calls = []

    def respond(task, context):
        calls.append(task)
        if task == 'artifact_sync':
            return {'operations': [{'op': 'update', 'id': 'C1', 'item': {
                'steps': [{'action': 'Log in', 'expected': 'Lock for twenty minutes'}]}}],
                'summary': 'Updated case'}
        assert task == 'artifact_review_readonly'
        assert context['cases'][0]['steps'][0]['expected'] == 'Lock for twenty minutes'
        return {'report': {'summary': 'Reviewed the new case version.', 'issues': [], 'coverage': []}}

    engine = Engine(store, respond)
    await execute(store, engine, chat, 'workflow.continue', answer_args(store, run['id']))
    old_reply = answer_args(store, run['id'])
    await execute(store, engine, chat, 'workflow.continue', old_reply)
    current = store.run(run['id'])
    assert current['status'] == 'waiting'
    assert current['_interrupt_id'] == 'native-gate'
    assert current['interrupt']['type'] == 'case_result_review'
    assert not current.get('_upstream_confirmation')
    artifact = store.get('artifact', 'cases')
    assert artifact['report']['review_reports'][0]['summary'] == 'Reviewed the new case version.'
    assert artifact['items'][0]['actual_result'] == 'Human result'
    rejected = await execute(store, engine, chat, 'workflow.continue', old_reply)
    assert rejected['status'] == 'cancelled'
    assert store.run(run['id']) == current
    assert calls == ['artifact_sync', 'artifact_review_readonly']
