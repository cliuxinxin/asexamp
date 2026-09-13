"""Clarification submission updates the real graph without approving understanding."""
import asyncio
import copy
from contextlib import asynccontextmanager

import pytest

from tcg.clarification import get_draft
from tcg.conversation_workflow import execute
from tcg.model import Settings
from tcg.schemas import DomainError
from tcg.workflow import WorkflowEngine
from test_conversation_workflow_v260 import setup, settled
from test_workflow_v25 import FlowModel


@asynccontextmanager
async def journey(tmp_path, questions=None, model=None):
    store, chat, _ = setup(tmp_path)
    model = model or FlowModel(questions=questions or ['Lock duration?'])
    engine = WorkflowEngine(store, model, Settings(tmp_path))
    await engine.start()
    try:
        started = await execute(store, engine, chat, 'workflow.start',
            {'content': 'Generate tests', 'mode': 'hitp', 'stop_after': 'complete'})
        run = await settled(store, engine, started['run']['id'])
        assert run['interrupt']['type'] == 'clarification'
        yield store, chat, engine, model, run
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_explicit_adoption_refreshes_once_and_stops_at_understanding(tmp_path):
    async with journey(tmp_path) as (store, chat, engine, model, run):
        before = store.get('artifact', run['interrupt']['artifact_id'])
        draft = get_draft(store, run['id'])
        result = await execute(store, engine, chat, 'clarification.adopt', {
            'run_id': run['id'], 'expected_revision': draft['revision'],
            'expected_control_version': run.get('control_version', 0),
            'answers': {draft['questions'][0]['id']: '10 minutes'}, 'submit': True})
        assert result['status'] == 'succeeded'
        updated = store.get('artifact', before['id'])
        assert updated['revision'] == before['revision'] + 1
        assert '10 minutes' in updated['items'][0]['description']
        assert updated['report']['questions'] == []
        stopped = await settled(store, engine, run['id'])
        assert stopped['interrupt']['type'] == 'strategy_review'
        assert stopped['interrupt']['artifact_revision'] == updated['revision']
        assert sum(task == 'artifact_modify' for task, _ in model.calls) == 1
        assert not any(task == 'generate_scenarios' for task, _ in model.calls)
        with pytest.raises(DomainError):
            engine.resume(run['id'], {'approved': True,
                'expected_control_version': run.get('control_version', 0)})


@pytest.mark.asyncio
async def test_partial_answers_preserve_unanswered_questions_and_prior_facts(tmp_path):
    async with journey(tmp_path, ['Lock duration?', 'Who can unlock?']) as (store, chat, engine, model, run):
        draft = get_draft(store, run['id'])
        first = await execute(store, engine, chat, 'clarification.adopt', {
            'run_id': run['id'], 'answers': {draft['questions'][0]['id']: '10 minutes'}, 'submit': True})
        assert first['status'] == 'succeeded'
        waiting = store.run(run['id'])
        assert waiting['interrupt']['type'] == 'clarification'
        assert waiting['interrupt']['questions'] == ['Who can unlock?']
        analysis = store.get('artifact', waiting['interrupt']['artifact_id'])
        assert analysis['report']['questions'] == ['Who can unlock?']
        assert '10 minutes' in analysis['items'][0]['description']
        active = get_draft(store, run['id'])
        assert [q['question'] for q in active['questions']] == ['Who can unlock?']
        await execute(store, engine, chat, 'clarification.adopt', {
            'run_id': run['id'], 'answers': {active['questions'][0]['id']: 'Administrator'}, 'submit': True})
        stopped = await settled(store, engine, run['id'])
        assert stopped['interrupt']['type'] == 'strategy_review'
        facts = [s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification']
        assert len(facts) == 2 and all(s['_active'] for s in facts)
        assert sum(task == 'artifact_modify' for task, _ in model.calls) == 2
        assert not any(task == 'generate_scenarios' for task, _ in model.calls)


@pytest.mark.asyncio
async def test_draft_only_does_not_submit_or_refresh(tmp_path):
    async with journey(tmp_path) as (store, chat, engine, model, run):
        before = copy.deepcopy(store.get('artifact', run['interrupt']['artifact_id']))
        await execute(store, engine, chat, 'clarification.adopt',
            {'run_id': run['id'], 'answer': '10 minutes', 'draft_only': True})
        assert not get_draft(store, run['id'])['submitted']
        assert store.get('artifact', before['id']) == before
        assert not any(task == 'artifact_modify' for task, _ in model.calls)


@pytest.mark.asyncio
async def test_provisional_submission_is_used_locally_without_project_sharing(tmp_path):
    from tcg.project_context import shared_sources
    async with journey(tmp_path) as (store, chat, engine, model, run):
        await execute(store, engine, chat, 'clarification.adopt', {
            'run_id': run['id'], 'answer': 'Assume 10 minutes for this task', 'submit': True,
            'status': 'provisional'})
        stopped = await settled(store, engine, run['id'])
        assert stopped['interrupt']['type'] == 'strategy_review'
        source = next(s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification')
        assert source['status'] == 'provisional' and not source.get('_project_shared')
        assert source['id'] in stopped['_source_ids']
        assert source['id'] not in {s['id'] for s in shared_sources(store, chat['project_id'])}
        assert sum(task == 'artifact_modify' for task, _ in model.calls) == 1


@pytest.mark.asyncio
async def test_failed_refresh_can_retry_saved_answer_without_duplicate_source(tmp_path):
    class Broken(FlowModel):
        broken = True
        async def generate(self, task, context):
            if task == 'artifact_modify' and self.broken:
                raise DomainError('Temporary model failure', 502)
            return await super().generate(task, context)
    model = Broken(questions=['Lock duration?'])
    async with journey(tmp_path, model=model) as (store, chat, engine, _, run):
        before = copy.deepcopy(store.get('artifact', run['interrupt']['artifact_id']))
        with pytest.raises(DomainError):
            await execute(store, engine, chat, 'clarification.save',
                {'run_id': run['id'], 'answer': '10 minutes'})
        assert store.get('artifact', before['id']) == before
        assert not store.run(run['id']).get('_edit_token')
        saved = get_draft(store, run['id'])
        assert saved['submitted']
        model.broken = False
        await execute(store, engine, chat, 'clarification.save',
            {'run_id': run['id'], 'expected_revision': saved['revision']})
        stopped = await settled(store, engine, run['id'])
        assert stopped['interrupt']['type'] == 'strategy_review'
        assert store.get('artifact', before['id'])['revision'] == before['revision'] + 1
        assert len([s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification']) == 1


@pytest.mark.asyncio
async def test_edit_blocks_confirmation_and_late_pause_retains_original_understanding(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Blocked(FlowModel):
        async def generate(self, task, context):
            if task == 'artifact_modify':
                entered.set()
                await release.wait()
            return await super().generate(task, context)
    model = Blocked(questions=['Lock duration?'])
    async with journey(tmp_path, model=model) as (store, chat, engine, _, run):
        before = copy.deepcopy(store.get('artifact', run['interrupt']['artifact_id']))
        edit = asyncio.create_task(execute(store, engine, chat, 'clarification.adopt',
            {'run_id': run['id'], 'answer': '10 minutes', 'submit': True}))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert store.run(run['id']).get('_edit_token')
            assert not store.db.in_transaction, 'Do not hold a database transaction across the model await.'
            with pytest.raises(DomainError):
                engine.resume(run['id'], {'answer': '10 minutes'})
            await execute(store, engine, chat, 'workflow.pause', {'run_id': run['id']})
            release.set()
            with pytest.raises(DomainError):
                await edit
            assert store.get('artifact', before['id']) == before
            assert not store.run(run['id']).get('_edit_token')
            assert store.run(run['id'])['interrupt']['type'] == 'clarification'
        finally:
            release.set()
            await asyncio.gather(edit, return_exceptions=True)
