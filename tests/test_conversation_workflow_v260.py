import asyncio
import copy

import pytest

from tcg.documents import parse_text
from tcg.model import Settings
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.workflow import WorkflowEngine
from tcg.clarification import get_draft, update_draft, save_draft, share_draft
from tcg.conversation_workflow import execute
from test_workflow_v25 import FlowModel


def setup(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Workflow controls')
    text, chunks = parse_text('Users authenticate. The lock duration requires confirmation.')
    source = store.add_source(chat['id'], 'Requirements', 'primary', text, chunks)
    return store, chat, source


def waiting_draft(store, chat, source):
    _, run = store.create_run(chat['id'], {'content': 'Generate tests', 'intent': 'generate_case',
        'mode': 'hitp', 'experience': 'reliable'})
    store.update_run(run['id'], status='waiting', stage='clarification', _interrupt_id='interrupt-1',
        interrupt={'type': 'clarification', 'questions': ['Lock duration?', 'Who can unlock?'],
            'question_suggestions': [
                {'question': 'Lock duration?', 'answer': '5 minutes', 'basis': 'Unconfirmed assumption',
                    'refs': [], 'confidence': 'assumption'},
                {'question': 'Who can unlock?', 'answer': 'Administrator', 'basis': 'Unconfirmed assumption',
                    'refs': [], 'confidence': 'assumption'}]})
    return store.run(run['id'])


def test_shared_draft_adopt_edit_reload_save_share_without_resume(tmp_path):
    store, chat, source = setup(tmp_path)
    run = waiting_draft(store, chat, source)
    draft = get_draft(store, run['id'])
    adopted = update_draft(store, run['id'], {'expected_revision': draft['revision'], 'adopt_all': True})
    qid = adopted['questions'][0]['id']
    edited = update_draft(store, run['id'], {'expected_revision': adopted['revision'], 'answers': {qid: '10 minutes'}})
    store.close()
    store = Store(tmp_path)
    restored = get_draft(store, run['id'])
    assert restored == edited
    assert restored['questions'][0]['adopted'] and restored['questions'][0]['answer'] == '10 minutes'
    saved = save_draft(store, run['id'], {'expected_revision': restored['revision']})
    assert saved['submitted'] and not saved['shared']
    shared = share_draft(store, run['id'], {'expected_revision': saved['revision']})
    assert shared['shared'] and store.run(run['id'])['status'] == 'waiting'
    again = save_draft(store, run['id'], {})
    assert again['source_id'] == shared['source_id']
    facts = [s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification']
    assert len(facts) == 1 and '10 minutes' in facts[0]['_text']
    assert facts[0]['_project_shared'] and facts[0]['_clarification_draft']['questions'][0]['suggestion']['confidence'] == 'assumption'
    store.close()


def test_stale_draft_and_question_set_rejected_and_clear_restores_suggestion(tmp_path):
    store, chat, source = setup(tmp_path)
    run = waiting_draft(store, chat, source)
    draft = get_draft(store, run['id'])
    adopted = update_draft(store, run['id'], {'expected_revision': draft['revision'], 'adopt_all': True})
    with pytest.raises(DomainError):
        update_draft(store, run['id'], {'expected_revision': draft['revision'], 'answer': 'old tab'})
    cleared = update_draft(store, run['id'], {'expected_revision': adopted['revision'],
        'answers': {adopted['questions'][0]['id']: ''}})
    assert not cleared['questions'][0]['adopted']
    store.update_run(run['id'], interrupt={'type': 'clarification', 'questions': ['New question?']})
    changed = get_draft(store, run['id'])
    assert changed['question_set_version'] != draft['question_set_version']
    with pytest.raises(DomainError):
        update_draft(store, run['id'], {'expected_revision': changed['revision'],
            'question_set_version': draft['question_set_version'], 'adopt_all': True})
    assert changed['questions'][0]['answer'] == ''
    store.close()


async def settled(store, engine, rid):
    for _ in range(500):
        run = store.run(rid)
        if run['status'] in ('waiting', 'failed', 'completed', 'cancelled') and not engine.task_active(rid):
            return run
        await asyncio.sleep(.01)
    raise AssertionError(store.run(rid))


@pytest.mark.asyncio
async def test_continue_saved_draft_reuses_source_and_retains_scenario_stop(tmp_path):
    store, chat, _ = setup(tmp_path)
    model = FlowModel(questions=['Lock duration?'])
    engine = WorkflowEngine(store, model, Settings(tmp_path))
    await engine.start()
    try:
        response = await execute(store, engine, chat, 'workflow.start', {'content': 'Generate tests',
            'intent': 'generate_case', 'mode': 'hitp', 'stop_after': 'scenarios'})
        run = await settled(store, engine, response['run']['id'])
        draft = get_draft(store, run['id'])
        update_draft(store, run['id'], {'expected_revision': draft['revision'], 'answer': '10 minutes'})
        saved = save_draft(store, run['id'], {})
        await execute(store, engine, chat, 'workflow.continue', {'run_id': run['id']})
        run = await settled(store, engine, run['id'])
        assert run['interrupt']['type'] == 'strategy_review'
        assert len([s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification']) == 1
        assert saved['source_id'] in run['_source_ids']
        await execute(store, engine, chat, 'workflow.continue', {'run_id': run['id'], 'approved': True})
        run = await settled(store, engine, run['id'])
        assert run['interrupt']['type'] == 'scenario_review' and run['stop_after'] == 'scenarios'
        assert not any(task == 'generate_cases' for task, _ in model.calls)
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_pause_at_boundary_preserves_completed_stage_and_explicit_resume(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Blocked(FlowModel):
        async def generate(self, task, context):
            if task == 'generate_scenarios':
                entered.set()
                await release.wait()
            return await super().generate(task, context)
    store, chat, _ = setup(tmp_path)
    model = Blocked()
    engine = WorkflowEngine(store, model, Settings(tmp_path))
    await engine.start()
    boundaries = []
    async def at_boundary(run_id):
        boundaries.append(run_id)
    engine.on_safe_boundary = at_boundary
    try:
        response = await execute(store, engine, chat, 'workflow.start', {'content': 'Generate', 'intent': 'generate_case', 'mode': 'auto'})
        rid = response['run']['id']
        await asyncio.wait_for(entered.wait(), 4)
        paused = await execute(store, engine, chat, 'workflow.pause', {'run_id': rid})
        assert paused['status'] == 'deferred'
        release.set()
        run = await settled(store, engine, rid)
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'workflow_paused'
        assert boundaries == [rid]
        assert any(a['type'] == 'scenarios' for a in store.list('artifact', chat_id=chat['id']))
        assert not any(task == 'generate_cases' for task, _ in model.calls)
        stale = await execute(store, engine, chat, 'workflow.continue', {'run_id': rid,
            'expected_control_version': run['control_version'] - 1})
        assert stale['status'] == 'cancelled'
        await execute(store, engine, chat, 'workflow.continue', {'run_id': rid})
        run = await settled(store, engine, rid)
        assert run['status'] == 'completed', run
        assert sum(task == 'generate_scenarios' for task, _ in model.calls) == 1
    finally:
        release.set()
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_boundary_before_clarification_keeps_natural_interrupt_answer_index(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Blocked(FlowModel):
        async def generate(self, task, context):
            if task == 'analyze_requirement':
                entered.set()
                await release.wait()
            return await super().generate(task, context)
    store, chat, _ = setup(tmp_path)
    engine = WorkflowEngine(store, Blocked(questions=['Lock duration?']), Settings(tmp_path))
    await engine.start()
    try:
        result = await execute(store, engine, chat, 'workflow.start', {'content': 'Generate', 'mode': 'hitp'})
        rid = result['run']['id']
        await asyncio.wait_for(entered.wait(), 4)
        await execute(store, engine, chat, 'workflow.pause', {'run_id': rid})
        release.set()
        run = await settled(store, engine, rid)
        assert run['interrupt']['type'] == 'workflow_paused'
        await execute(store, engine, chat, 'workflow.continue', {'run_id': rid})
        run = await settled(store, engine, rid)
        assert run['interrupt']['type'] == 'clarification'
        await execute(store, engine, chat, 'workflow.continue', {'run_id': rid, 'answer': '10 minutes'})
        run = await settled(store, engine, rid)
        assert run['interrupt']['type'] == 'strategy_review', run
        source = next(s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification')
        assert '10 minutes' in source['_text']
    finally:
        release.set()
        await engine.stop()
        store.close()
