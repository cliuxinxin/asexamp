"""A different chat's fact replacement cannot change an in-flight task's evidence."""
import asyncio
import copy

import pytest

from tcg.conversation import ConversationController
from tcg.model import Settings
from tcg.project_context import shared_sources
from tcg.storage import Store
from tcg.workflow import WorkflowEngine
from test_conversation_workflow_v260 import settled
from test_workflow_v25 import FlowModel


@pytest.mark.asyncio
async def test_prompt_provisional_alias_never_escalates_an_assumption_to_shared_fact(tmp_path):
    from test_conversation_e2e_v260 import ControlledModelEngine
    store = Store(tmp_path)
    chat = store.create_chat(store.list('project')[0]['id'], 'Assumption in chat')
    engine = ControlledModelEngine(store)
    engine.decision = {'actions': [{'name': 'project.confirm_fact', 'arguments': {
        'content': '先假设导出日志保留七天', 'fact_key': 'Audit retention', 'provisional': True}}]}
    controller = ConversationController(store, engine)
    try:
        await controller.submit(chat['id'], {
            'content': '这次先按导出日志保留七天这个假设处理', 'client_message_id': 'provisional-prompt'})
        assert shared_sources(store, chat['project_id']) == []
    finally:
        await controller.close()
        store.close()


@pytest.mark.asyncio
async def test_other_chat_supersedes_fact_while_model_runs_preserving_original_task(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedModel(FlowModel):
        decisions = {}

        async def generate(self, task, context):
            if task == 'conversation_turn':
                return copy.deepcopy(self.decisions[context['content']])
            if task == 'generate_scenarios':
                entered.set()
                await release.wait()
            return await super().generate(task, context)

    store = Store(tmp_path)
    project = store.list('project')[0]
    confirmer = store.create_chat(project['id'], 'Project rule owner')
    consumer = store.create_chat(project['id'], 'Existing task')
    model = PausedModel()
    engine = WorkflowEngine(store, model, Settings(tmp_path))
    controller = ConversationController(store, engine)
    engine.on_safe_boundary = controller.safe_boundary
    await engine.start()

    async def turn(chat, text, name, arguments):
        model.decisions[text] = {'actions': [{'name': name, 'arguments': arguments}]}
        return await controller.submit(chat['id'], {'content': text, 'client_message_id': text})

    try:
        confirmed = await turn(confirmer, '确认 v2 移出白名单立即撤权', 'project.confirm_fact', {
            'content': 'Removing an account from the allowlist immediately revokes access.',
            'fact_key': 'Access revocation', 'scope': {'module': 'Access', 'version': 'v2'}})
        assert confirmed['status'] == 'succeeded'
        old_id = confirmed['actions'][0]['result']['fact']['id']
        original_source = store.get('source', old_id)
        started = await turn(consumer, '根据已确认规则自动完成场景和用例', 'workflow.start', {
            'content': 'Generate tests from the confirmed access rule.', 'mode': 'auto',
            'stop_after': 'complete', 'source_ids': [old_id]})
        rid = started['actions'][0]['result']['run']['id']
        await asyncio.wait_for(entered.wait(), 4)
        assert store.run(rid)['status'] == 'running'
        original_evidence = store.evidence_version(old_id, original_source['version'])

        replaced = await turn(confirmer, '从 v3 开始改为十分钟后撤权', 'project.confirm_fact', {
            'content': 'From v3, access is revoked ten minutes after allowlist removal.',
            'fact_key': 'Access revocation', 'scope': {'module': 'Access', 'version': 'v3'},
            'supersedes': [old_id]})
        assert replaced['status'] == 'succeeded'
        new_id = replaced['actions'][0]['result']['fact']['id']
        assert store.run(rid)['_source_ids'] == [old_id]
        assert [s['id'] for s in shared_sources(store, project['id'])] == [new_id]
        assert store.evidence_version(old_id, original_source['version']) == original_evidence
        release.set()
        finished = await settled(store, engine, rid)
        assert finished['status'] == 'completed', finished.get('error')
        assert finished['_source_ids'] == [old_id]
        scenarios = next(a for a in store.list('artifact', chat_id=consumer['id']) if a['type'] == 'scenarios')
        assert old_id + '#P1' in scenarios['items'][0]['refs']
        assert new_id not in scenarios['_source_ids']
    finally:
        release.set()
        await controller.close()
        await engine.stop()
        store.close()


@pytest.mark.parametrize('change', ['text', 'chunks', 'scope', 'role'])
def test_supersession_bridge_still_rejects_changed_business_evidence(tmp_path, change):
    from tcg.dependencies import DependencyConflict, assert_manifest, manifest
    from tcg.documents import parse_text
    from tcg.project_context import share_clarification
    store = Store(tmp_path)
    try:
        project = store.list('project')[0]
        chat = store.create_chat(project['id'], 'Strict evidence checks')
        text, chunks = parse_text('Access is revoked immediately.')
        old = store.add_source(chat['id'], 'Revocation', 'clarification', text, chunks)
        share_clarification(store, old['id'], project['id'],
            scope={'module': 'Access', 'version': 'v2'}, fact_key='Revocation')
        captured = manifest(store, source_ids=[old['id']])
        text, chunks = parse_text('From v3, access is revoked after ten minutes.')
        new = store.add_source(chat['id'], 'Revocation v3', 'clarification', text, chunks)
        share_clarification(store, new['id'], project['id'],
            scope={'module': 'Access', 'version': 'v3'}, fact_key='Revocation', supersedes=[old['id']])
        assert_manifest(store, captured)
        current = store.get('source', old['id'])
        if change == 'chunks':
            chunk = store.get('chunk', old['id'] + '#P1')
            store.put('chunk', {**chunk, 'text': 'A different unsupported business rule.'})
        else:
            patch = {'text': {'_text': 'Changed old evidence body.'},
                     'scope': {'scope': {'module': 'Billing', 'version': 'v2'}},
                     'role': {'role': 'example'}}[change]
            store.put('source', {**current, **patch})
        with pytest.raises(DependencyConflict):
            assert_manifest(store, captured)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unrelated_fact_conflict_does_not_pause_running_auto_task(tmp_path):
    from tcg.documents import parse_text
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedModel(FlowModel):
        decisions = {}

        async def generate(self, task, context):
            if task == 'conversation_turn':
                return copy.deepcopy(self.decisions[context['content']])
            if task == 'generate_scenarios':
                entered.set()
                await release.wait()
            return await super().generate(task, context)

    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'Independent facts and generation')
    text, chunks = parse_text('Registered users can log in with valid credentials.')
    base = store.add_source(chat['id'], 'Login requirements', 'primary', text, chunks)
    model = PausedModel()
    engine = WorkflowEngine(store, model, Settings(tmp_path))
    controller = ConversationController(store, engine)
    engine.on_safe_boundary = controller.safe_boundary
    await engine.start()

    async def turn(text, name, arguments):
        model.decisions[text] = {'actions': [{'name': name, 'arguments': arguments}]}
        return await controller.submit(chat['id'], {'content': text, 'client_message_id': text})

    try:
        confirmed = await turn('确认导出日志保留九十天', 'project.confirm_fact', {
            'content': 'Export audit logs are retained ninety days.', 'fact_key': 'Audit retention',
            'scope': {'module': 'Export', 'version': 'v2'}})
        assert confirmed['status'] == 'succeeded'
        started = await turn('只按登录需求自动完成测试用例', 'workflow.start', {
            'content': 'Generate login tests.', 'mode': 'auto', 'stop_after': 'complete',
            'source_ids': [base['id']]})
        rid = started['actions'][0]['result']['run']['id']
        await asyncio.wait_for(entered.wait(), 4)
        conflicting = await turn('确认导出日志保留七天', 'project.confirm_fact', {
            'content': 'Export audit logs are retained seven days.', 'fact_key': 'Audit retention',
            'scope': {'module': 'Export', 'version': 'v2'}})
        release.set()
        finished = await settled(store, engine, rid)
        assert finished['status'] == 'completed', finished.get('interrupt') or finished.get('error')
        current_turn = controller.get(chat['id'], conflicting['id'])
        assert current_turn['status'] == 'needs_input'
        assert current_turn['pending'][0]['field'] == 'supersedes'
        assert finished['_source_ids'] == [base['id']]
        assert len(shared_sources(store, project['id'])) == 1
    finally:
        release.set()
        await controller.close()
        await engine.stop()
        store.close()
