"""Supplement adoption and clarification refresh against real versioned artifacts."""
import copy
import json

import pytest

from tcg import conversation_project
from tcg.artifact_actions import apply_action
from tcg.documents import parse_text
from tcg.schemas import DomainError, MessageInput
from tcg.storage import Store


@pytest.fixture
def domain(tmp_path):
    store = Store(tmp_path)
    chat = store.create_chat(store.list('project')[0]['id'], 'requirements refresh')
    text, chunks = parse_text('Five failures lock the account. Notifications are optional.')
    base = store.add_source(chat['id'], 'base', 'primary', text, chunks)
    _, run = store.create_run(chat['id'], MessageInput(content='Understand requirements',
        intent='generate_scenario', mode='hitp').model_dump())
    ref = base['id'] + '#P1'
    analysis = store.artifact(run['id'], 'v7:analysis:artifact', 'analysis', 'Understanding', [
        {'id': 'R1', 'title': 'Lockout', 'description': 'Five failures lock the account.',
         'reviewer_note': 'Keep the manually reviewed title.', 'refs': [ref]},
        {'id': 'R2', 'title': 'Notifications', 'description': 'Notifications are optional.',
         'reviewer_note': 'Independent manual note', 'refs': [ref]},
    ], {'summary': 'Lockout duration is unknown.', 'questions': ['How long is the lockout?'],
        'diagrams': [{'title': 'Flow', 'mermaid': 'flowchart TD\nA-->B'}],
        'analysis_signature': {'digest': 'before'}})
    store.put('artifact', {**analysis, '_visible': True})
    scenarios = store.artifact(run['id'], 'scenarios', 'scenarios', 'Scenarios', [
        {'id': 'S1', 'title': 'Lockout', 'description': 'Verify lockout', 'priority': 'P1',
         'requirement_ids': ['R1'], 'refs': [ref]},
        {'id': 'S2', 'title': 'Notifications', 'description': 'Verify optional notifications',
         'priority': 'P2', 'requirement_ids': ['R2'], 'refs': [ref]},
    ], {'lineage': {'analysis_artifact_id': analysis['id'], 'analysis_revision': 1}})
    store.put('artifact', {**scenarios, '_visible': True})
    store.update_run(run['id'], status='waiting', stop_after='analysis',
        interrupt={'type': 'strategy_review', 'artifact_id': analysis['id']}, _interrupt_id='gate')
    text, chunks = parse_text('Administrators may revoke sessions. Lockout lasts ten minutes.')
    change = store.add_source(chat['id'], 'Supplement', 'change', text, chunks)
    try:
        yield store, chat, run['id'], store.get('artifact', analysis['id']), scenarios, change
    finally:
        store.close()


class Model:
    def __init__(self, store, *, affected=(), addition=True, hook=None):
        self.store, self.calls = store, []
        self.affected, self.addition, self.hook = list(affected), addition, hook

    def fits(self, task, context):
        return True

    async def invoke_model(self, task, context, run_id=None):
        self.calls.append((task, copy.deepcopy(context)))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if task == 'project_source_impact':
            ref = context['new_evidence'][0]['id']
            return {'requirement_ids': self.affected, 'summary': 'Session revocation is a new independent rule.',
                'global_impact': False, 'uncertain': False, 'refs': [ref],
                'new_requirements': [{'title': 'Session revocation',
                    'description': 'Administrators may revoke sessions.', 'refs': [ref]}] if self.addition else []}
        ref = next(e['id'] for e in context['evidence'] if e['role'] in ('change', 'clarification'))
        if task == 'artifact_modify':
            operations = [{'op': 'add', 'item': {'id': 'R3', 'title': 'Session revocation',
                'description': 'Administrators may revoke sessions.', 'refs': [ref]}}] if self.addition else []
            if self.affected:
                operations.insert(0, {'op': 'update', 'id': 'R1', 'item': {
                    'description': 'Five failures lock the account for ten minutes.', 'refs': [ref]}})
            return {'operations': operations, 'summary': 'Understanding includes the confirmed supplement.',
                'report_patch': {'summary': 'Lockout lasts ten minutes; administrators may revoke sessions.',
                    'diagrams': [{'title': 'Confirmed flow', 'mermaid': 'flowchart TD\nA-->C'}]}}
        if task == 'artifact_sync_scenarios':
            return {'operations': [{'op': 'add', 'item': {'id': 'S3', 'title': 'Revoke sessions',
                'description': 'Verify administrator session revocation', 'priority': 'P1',
                'requirement_ids': ['R3'], 'refs': [ref]}}], 'summary': 'Added new rule scenario.'}
        raise AssertionError('Unexpected whole generation: ' + task)


@pytest.mark.asyncio
async def test_impact_identifies_grounded_new_requirements_without_old_ids(domain):
    store, chat, _, analysis, _, source = domain
    before = store.revisions(analysis['id'])
    result = await conversation_project.execute(store, Model(store), chat, 'project.source_impact',
        {'artifact_id': analysis['id'], 'source_ids': [source['id']]})
    impact = result['parts'][0]['data']
    assert impact['requirement_ids'] == []
    assert impact['new_requirements'][0]['title'] == 'Session revocation'
    assert impact['new_requirements'][0]['refs'] == [source['id'] + '#P1']
    assert impact['has_additions'] is True
    assert store.revisions(analysis['id']) == before


@pytest.mark.asyncio
async def test_impact_rejects_ungrounded_additions(domain):
    store, chat, _, analysis, _, source = domain
    class Ungrounded(Model):
        async def invoke_model(self, task, context, run_id=None):
            result = await super().invoke_model(task, context, run_id)
            result['new_requirements'][0]['refs'] = ['unprovided#P1']
            return result
    with pytest.raises(DomainError, match='新增需求|引用'):
        await conversation_project.execute(store, Ungrounded(store), chat, 'project.source_impact',
            {'artifact_id': analysis['id'], 'source_ids': [source['id']]})


@pytest.mark.asyncio
async def test_source_preview_preserves_old_rows_and_new_rule_reaches_scenarios(domain):
    store, chat, rid, analysis, scenarios, source = domain
    before_run = store.run(rid)
    before_sources = store.list('source', chat_id=chat['id'])
    assert hasattr(conversation_project, 'preview_from_sources'), 'Sources require a reusable preview path'
    preview = await conversation_project.preview_from_sources(store, Model(store), chat,
        {'artifact_id': analysis['id'], 'source_ids': [source['id']], 'sync_targets': ['scenarios']})
    assert store.get('artifact', analysis['id']) == analysis
    assert {k: v for k, v in store.run(rid).items() if k not in ('updated_at', '_edit_token')} == {
        k: v for k, v in before_run.items() if k not in ('updated_at', '_edit_token')}
    assert store.list('source', chat_id=chat['id']) == before_sources
    assert preview['changes'][0]['diff'] == {'added': ['R3'], 'updated': [], 'deleted': []}
    assert preview['changes'][0]['items'][:2] == analysis['items']
    apply_action(store, analysis['id'], preview['id'], emit_message=False)
    updated = store.get('artifact', analysis['id'])
    assert [r['id'] for r in updated['items']] == ['R1', 'R2', 'R3']
    assert updated['report']['analysis_signature'] != analysis['report']['analysis_signature']
    assert source['id'] in updated['_source_ids']
    assert store.get('artifact', scenarios['id'])['items'][-1]['requirement_ids'] == ['R3']
    assert store.run(rid)['status'] == 'waiting'


@pytest.mark.asyncio
async def test_no_impact_preview_still_allows_explicit_source_adoption(domain):
    store, chat, _, analysis, scenarios, source = domain
    assert hasattr(conversation_project, 'preview_from_sources'), 'No-impact evidence needs an adoption preview'
    model = Model(store, addition=False)
    preview = await conversation_project.preview_from_sources(store, model, chat,
        {'artifact_id': analysis['id'], 'source_ids': [source['id']]})
    assert store.get('artifact', analysis['id']) == analysis
    assert [task for task, _ in model.calls] == ['project_source_impact']
    apply_action(store, analysis['id'], preview['id'], emit_message=False)
    updated = store.get('artifact', analysis['id'])
    assert updated['items'] == analysis['items']
    assert source['id'] in updated['_source_ids']
    assert store.get('artifact', scenarios['id'])['revision'] == 1


@pytest.mark.asyncio
async def test_clarification_updates_current_rows_report_and_evidence_idempotently(domain):
    from tcg import requirement_refresh
    store, _, rid, analysis, _, source = domain
    store.put('source', {**source, 'role': 'clarification'})
    store.update_run(rid, status='running')
    model = Model(store, affected=['R1'])
    updated = await requirement_refresh.refresh_clarification(model, rid, analysis, source['id'],
        'Lockout lasts ten minutes. Administrators may revoke sessions.')
    assert updated['revision'] == 2
    assert updated['items'][0]['description'].endswith('ten minutes.')
    assert updated['items'][0]['reviewer_note'] == analysis['items'][0]['reviewer_note']
    assert updated['items'][1] == analysis['items'][1]
    assert updated['items'][2]['id'] == 'R3'
    assert updated['report']['summary'].startswith('Lockout lasts ten minutes')
    assert updated['report']['diagrams'] != analysis['report']['diagrams']
    assert updated['report']['questions'] == []
    assert updated['report']['analysis_signature'] != analysis['report']['analysis_signature']
    assert source['id'] in updated['_source_ids']
    assert any(s['id'] == source['id'] for s in updated['_dependencies']['sources'])
    assert store.revision(analysis['id'], 1)['items'] == analysis['items']
    again = await requirement_refresh.refresh_clarification(model, rid, analysis, source['id'],
        'Lockout lasts ten minutes. Administrators may revoke sessions.')
    assert again == updated
    assert [task for task, _ in model.calls] == ['artifact_modify']
    assert store.cache_get(rid, 'v6:requirement_map')['confirmed_requirements'] == updated['items']


@pytest.mark.asyncio
async def test_clarification_does_not_commit_after_evidence_changes(domain):
    from tcg import requirement_refresh
    store, _, rid, analysis, _, source = domain
    store.put('source', {**source, 'role': 'clarification'})
    store.update_run(rid, status='running')
    def change_source():
        store.put('source', {**store.get('source', source['id']), '_text': 'Changed while generating'})
    with pytest.raises(DomainError, match='依赖已改变|资料已变化'):
        await requirement_refresh.refresh_clarification(Model(store, affected=['R1'], hook=change_source),
            rid, analysis, source['id'], 'Ten minutes')
    assert store.get('artifact', analysis['id']) == analysis


@pytest.mark.asyncio
async def test_replayed_clarification_returns_later_manual_revision(domain):
    from tcg.requirement_refresh import refresh_clarification
    store, _, rid, analysis, _, source = domain
    store.put('source', {**source, 'role': 'clarification'})
    store.update_run(rid, status='running')
    model = Model(store, affected=['R1'])
    refreshed = await refresh_clarification(model, rid, analysis, source['id'], 'Ten minutes')
    rows = copy.deepcopy(refreshed['items'])
    rows[0]['reviewer_note'] = 'A newer manual review'
    manual = store.revise_artifact(analysis['id'], refreshed['revision'], rows)
    replay = await refresh_clarification(model, rid, analysis, source['id'], 'Ten minutes')
    assert replay == manual
    assert replay['items'][0]['reviewer_note'] == 'A newer manual review'
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_inline_supplement_preview_saves_evidence_then_waits_for_adoption(domain):
    store, chat, rid, analysis, _, _ = domain
    before_count = len(store.list('source', chat_id=chat['id']))
    result = await conversation_project.update_from_sources(store, Model(store), chat,
        {'artifact_id': analysis['id'], 'content': 'Administrators may revoke sessions.',
         'preview': True, 'sync_targets': []})
    assert result['status'] == 'needs_confirmation'
    assert len(store.list('source', chat_id=chat['id'])) == before_count + 1
    assert store.get('artifact', analysis['id']) == analysis
    assert store.run(rid)['status'] == 'waiting'
    proposal_id = next(part['proposal_id'] for part in result['parts'] if part['type'] == 'diff')
    apply_action(store, analysis['id'], proposal_id, emit_message=False)
    assert len(store.get('artifact', analysis['id'])['items']) == 3


@pytest.mark.asyncio
async def test_replayed_source_preview_reuses_the_saved_proposal(domain):
    store, chat, _, analysis, _, source = domain
    command_id = 'source-preview-replay'
    store.put('conversation_command', {'id': command_id, 'chat_id': chat['id'],
        'project_id': chat['project_id'], 'status': 'pending'})
    model = Model(store)
    args = {'artifact_id': analysis['id'], 'source_ids': [source['id']], 'preview': True, 'sync_targets': []}
    first = await conversation_project.execute(store, model, chat, 'project.update_from_sources', args, command_id)
    second = await conversation_project.execute(store, model, chat, 'project.update_from_sources', args, command_id)
    assert second == first
    assert len(model.calls) == 2
    assert store.get('artifact', analysis['id']) == analysis


@pytest.mark.asyncio
async def test_clarification_model_can_amend_existing_requirement_map(domain):
    from tcg.requirement_refresh import refresh_clarification
    store, _, rid, analysis, _, source = domain
    store.put('source', {**source, 'role': 'clarification'})
    store.update_run(rid, status='running')
    rule = {'id': 'lockout-rule', 'text': 'lockout duration unresolved',
        'refs': analysis['items'][0]['refs'], 'requirement_ids': ['R1']}
    report = {**analysis['report'], 'requirement_map': {'rules': [rule]}}
    analysis = store.revise_artifact(analysis['id'], 1, analysis['items'], report=report)
    class MapModel(Model):
        async def invoke_model(self, task, context, run_id=None):
            assert any(value['text'] == 'lockout duration unresolved' for value in context['global_rules']['rules'])
            result = await super().invoke_model(task, context, run_id)
            result['report_patch']['requirement_map'] = {'rules': [{**rule, 'text': 'lockout lasts ten minutes'}]}
            return result
    refreshed = await refresh_clarification(MapModel(store, affected=['R1']), rid,
        analysis, source['id'], 'Ten minutes')
    assert refreshed['report']['requirement_map']['rules'][0]['text'] == 'lockout lasts ten minutes'


@pytest.mark.asyncio
async def test_clarification_retains_remaining_report_uncertainties(domain):
    from tcg.requirement_refresh import refresh_clarification
    store, _, rid, analysis, _, source = domain
    store.put('source', {**source, 'role': 'clarification'})
    store.update_run(rid, status='running')
    class RemainingQuestion(Model):
        async def invoke_model(self, task, context, run_id=None):
            result = await super().invoke_model(task, context, run_id)
            result['report_patch']['questions'] = ['How are administrator session revocations audited?']
            return result
    refreshed = await refresh_clarification(RemainingQuestion(store, affected=['R1']),
        rid, analysis, source['id'], 'Ten minutes')
    assert refreshed['report']['questions'] == ['How are administrator session revocations audited?']


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['sources', 'clarification'])
async def test_large_report_does_not_prevent_small_grounded_refresh(domain, path):
    from tcg.requirement_refresh import refresh_clarification
    store, chat, rid, analysis, _, source = domain
    store.update_run(rid, status='running')
    rule = {'id': 'lockout-rule', 'text': 'Account locks after five failures.',
        'requirement_ids': ['R1'], 'refs': analysis['items'][0]['refs']}
    report = {**analysis['report'], 'requirement_map': {'rules': [rule],
        'states': [{'id': 'state-' + str(i), 'description': 'Existing independent state detail. ' * 12}
            for i in range(60)]},
        'diagrams': [{'title': 'Large existing diagram', 'mermaid': 'flowchart TD\n' + 'A-->B\n' * 3000}]}
    analysis = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    class CapacityModel(Model):
        def fits(self, task, context):
            return len(json.dumps(context, ensure_ascii=False)) <= 12000

        async def invoke_model(self, task, context, run_id=None):
            assert self.fits(task, context)
            if task == 'artifact_modify':
                assert any(value['id'] == rule['id'] for value in context['global_rules']['rules'])
                assert 'state-59' not in context['instruction']
            return await super().invoke_model(task, context, run_id)
    model = CapacityModel(store, affected=['R1'], addition=False)
    if path == 'sources':
        store.update_run(rid, status='waiting')
        preview = await conversation_project.preview_from_sources(store, model, chat,
            {'artifact_id': analysis['id'], 'source_ids': [source['id']], 'sync_targets': []})
        updated = preview['changes'][0]
        assert store.get('artifact', analysis['id']) == analysis
    else:
        store.put('source', {**source, 'role': 'clarification'})
        updated = await refresh_clarification(model, rid, analysis, source['id'], 'Ten minutes')
    assert updated['items'][0]['description'].endswith('ten minutes.')
    assert updated['items'][1] == analysis['items'][1]
    assert updated['report']['requirement_map'] == report['requirement_map']


@pytest.mark.asyncio
async def test_refresh_does_not_drop_mandatory_rules_to_fit_capacity(domain):
    store, chat, rid, analysis, _, source = domain
    store.update_run(rid, status='running')
    report = {**analysis['report'], 'requirement_map': {'rules': [{
        'id': 'mandatory-rule', 'text': 'Required business constraint. ' * 1000,
        'requirement_ids': ['R1'], 'refs': analysis['items'][0]['refs']}]}}
    analysis = store.revise_artifact(analysis['id'], analysis['revision'], analysis['items'], report=report)
    store.update_run(rid, status='waiting')
    class CapacityModel(Model):
        def fits(self, task, context):
            return len(json.dumps(context, ensure_ascii=False)) <= 12000
    model = CapacityModel(store, affected=['R1'], addition=False)
    with pytest.raises(DomainError, match='超过模型容量|必要规则'):
        await conversation_project.preview_from_sources(store, model, chat,
            {'artifact_id': analysis['id'], 'source_ids': [source['id']], 'sync_targets': []})
    assert store.get('artifact', analysis['id']) == analysis
    assert [task for task, _ in model.calls] == ['project_source_impact']
