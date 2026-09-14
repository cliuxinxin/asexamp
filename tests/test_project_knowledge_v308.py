"""Project history selection remains explicit, scoped and durable across a rebuild."""
import copy
import json
import time
from types import SimpleNamespace

import pytest

from tcg.conversation_facts import (artifact_projection, ensure_artifact_allowed, record_context_usage,
                                    set_fact_enabled, sources_allowed)
from tcg.documents import parse_text
from tcg.native_business import NativeBusiness
from tcg.native_views import chat_context
from tcg.project_context import share_clarification, shared_context, shared_sources, unshare_clarification
from tcg.schemas import DomainError
from tcg.storage import Store
from test_native_journey_v300 import native_journey, SHARED_RULE


@pytest.fixture
def knowledge(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    origin = store.create_chat(project['id'], '账号模块测试')
    chat = store.create_chat(project['id'], '当前生成')
    other = store.create_chat(project['id'], '其他成员')
    text, chunks = parse_text(SHARED_RULE)
    source = store.add_source(origin['id'], '已确认锁定规则', 'clarification', text, chunks)
    source = share_clarification(store, source['id'], project['id'])
    text, chunks = parse_text('有效账号可以登录。')
    document = store.add_source(chat['id'], '当前需求.md', 'primary', text, chunks)
    yield SimpleNamespace(store=store, project=project, origin=origin, chat=chat, other=other,
                          source=source, document=document)
    store.close()


def test_preference_is_local_versioned_and_never_mutates_evidence(knowledge):
    k = knowledge
    before = copy.deepcopy(k.store.get('source', k.source['id']))
    payload = shared_context(k.store, k.project['id'], chat_id=k.chat['id'])
    row = payload['clarifications'][0]
    assert row['enabled_in_chat'] and not row['excluded']
    assert row['origin_chat_title'] == k.origin['title']
    assert row['source_version'] == before['version'] and row['origin_created_at']
    result = set_fact_enabled(k.store, k.chat['id'], k.source['id'], False, 1)
    assert result['preference_version'] == 2
    assert result['clarifications'][0]['excluded']
    assert not result['clarifications'][0]['enabled_in_chat']
    assert not shared_sources(k.store, k.project['id'], chat_id=k.chat['id'])
    assert shared_sources(k.store, k.project['id'], chat_id=k.other['id'])
    assert k.store.get('source', k.source['id']) == before
    with pytest.raises(DomainError, match='已更新'):
        set_fact_enabled(k.store, k.chat['id'], k.source['id'], True, 1)
    with pytest.raises(DomainError, match='已禁用'):
        sources_allowed(k.store, k.chat['id'], [k.source['id']], strict=True)
    _, run = k.store.create_run(k.chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'hitp'})
    assert run['_source_ids'] == [k.document['id']]
    assert k.source['id'] in sources_allowed(k.store, k.other['id'], [k.source['id']])


def test_busy_scope_and_non_shared_rules_are_rejected_without_mutation(knowledge):
    k = knowledge
    with pytest.raises(DomainError, match='共享澄清'):
        set_fact_enabled(k.store, k.chat['id'], k.document['id'], False, 1)
    _, run = k.store.create_run(k.chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'hitp'})
    with pytest.raises(DomainError, match='正在生成'):
        set_fact_enabled(k.store, k.chat['id'], k.source['id'], False, 1)
    assert k.store.get('chat', k.chat['id']).get('_project_knowledge_version', 1) == 1
    foreign = k.store.create_project('不同项目')
    foreign_chat = k.store.create_chat(foreign['id'], '跨项目')
    with pytest.raises(DomainError, match='本项目'):
        set_fact_enabled(k.store, foreign_chat['id'], k.source['id'], False, 1)
    assert k.store.run(run['id'])['status'] == 'queued'


def test_actual_context_disclosure_and_frozen_origins_survive_unsharing(knowledge):
    k = knowledge
    _, run = k.store.create_run(k.chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'hitp'})
    run = k.store.update_run(run['id'], status='running')
    # Listing documents alone does not claim historical facts were supplied.
    record_context_usage(k.store, run, k.store.evidence([k.document['id']]))
    assert not k.store.run(run['id']).get('shared_facts_used')
    evidence = k.store.evidence([k.source['id']])
    record_context_usage(k.store, run, evidence)
    record_context_usage(k.store, run, evidence)
    used = k.store.run(run['id'])['shared_facts_used']
    assert len(used) == 1 and used[0]['refs'] == [k.source['id'] + '#P1']
    assert used[0]['origin_chat_title'] == k.origin['title']
    note = k.store.get('message', 'project-knowledge:' + run['id'])
    assert note['role'] == 'assistant'
    assert note['metadata']['turn_response']['parts'][0]['facts'] == used
    from tcg.dependencies import manifest
    guard = manifest(k.store, source_ids=[k.source['id']])
    artifact = {'id': 'A', 'chat_id': k.chat['id'], 'project_id': k.project['id'], 'revision': 1,
        'items': [{'id': 'R1', 'refs': evidence and [evidence[0]['id']]}], 'report': {}, '_dependencies': guard}
    frozen = artifact_projection(k.store, artifact)
    unshare_clarification(k.store, k.project['id'], k.source['id'])
    k.store.put('chat', {**k.origin, 'title': '改名后'})
    projected = artifact_projection(k.store, artifact)
    assert projected['report']['source_provenance'] == frozen['report']['source_provenance']
    assert projected['report']['shared_facts_used'][0]['classification'] == 'project_knowledge'


@pytest.mark.asyncio
async def test_automatic_chat_context_filters_disabled_fact_text_and_stale_artifact_is_guarded(knowledge):
    k = knowledge
    _, run = k.store.create_run(k.chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'hitp'})
    run = k.store.update_run(run['id'], status='running')
    record_context_usage(k.store, run, k.store.evidence([k.source['id']]))
    k.store.update_run(run['id'], status='waiting')
    set_fact_enabled(k.store, k.chat['id'], k.source['id'], False, 1)
    class Pipeline:
        async def snapshot(self, rid):
            return k.store.run(rid)
    context = await chat_context(k.store, Pipeline(), k.store.get('chat', k.chat['id']), {}, None)
    assert SHARED_RULE not in json.dumps(context, ensure_ascii=False)
    assert all(row['id'] != k.source['id'] for row in context['sources'])
    artifact = {'id': 'old-analysis', 'type': 'analysis', 'chat_id': k.chat['id'], 'project_id': k.project['id'],
        '_source_ids': [k.source['id']], 'items': [], 'report': {}}
    with pytest.raises(DomainError, match='知识库选择'):
        ensure_artifact_allowed(k.store, artifact)
    with pytest.raises(DomainError):
        NativeBusiness(k.store, None)._evidence(artifact)
    # Re-enabling still requires fresh understanding; changing eligibility is not approval.
    set_fact_enabled(k.store, k.chat['id'], k.source['id'], True, 2)
    with pytest.raises(DomainError, match='知识库选择'):
        ensure_artifact_allowed(k.store, artifact)
    assert k.store.run(run['id'])['knowledge_rebuild_required']


def wait_rebuilt(j, old_run_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = j.snapshot()
        current = [run for run in snapshot['runs'] if run['id'] != old_run_id]
        if current and current[-1]['status'] == 'waiting':
            assert snapshot['conversation_prompt']['kind'] == 'strategy_review'
            return current[-1], j.artifact(current[-1]['current_artifact_id']), snapshot
        assert not current or current[-1]['status'] != 'failed', current
        time.sleep(.02)
    raise AssertionError(j.snapshot())


def test_real_http_gate_selection_rebuilds_only_after_approval_and_keeps_adopted_sources(native_journey):
    j = native_journey
    store = j.app.state.store
    origin = store.create_chat(j.project['id'], '历史锁定规则')
    text, chunks = parse_text(SHARED_RULE)
    fact = store.add_source(origin['id'], '锁定规则', 'clarification', text, chunks)
    fact = share_clarification(store, fact['id'], j.project['id'])
    # Explicit initial selection is distinct from later adopted clarifications.
    j.turn('根据需求生成用例，逐步确认。', 'start_pipeline_tool', {'mode': 'hitp', 'stop_after': 'review'}, mode='hitp')
    run, original, gate = j.gate('strategy_review')
    assert SHARED_RULE in original['items'][0]['description']
    assert original['report']['shared_facts_used'][0]['source_id'] == fact['id']
    # Simulate the already-supported clarification adoption before a later gate.
    text, chunks = parse_text('已确认登录页支持键盘回车。')
    adopted = store.add_source(j.chat['id'], '本轮已确认说明', 'clarification', text, chunks)
    current = store.run(run['id'])
    store.update_run(run['id'], _request={**current['_request'], 'source_ids': [j.source['id'], fact['id']]},
        _source_ids=current['_source_ids'] + [adopted['id']])
    before = len(j.gateway.generations)
    response = j.client.patch('/api/chats/' + j.chat['id'] + '/project-knowledge/' + fact['id'],
        json={'enabled': False, 'expected_version': 1})
    assert response.status_code == 200, response.text
    assert response.json()['requires_rebuild'] and len(j.gateway.generations) == before
    prompt = j.snapshot()['conversation_prompt']
    assert prompt['kind'] == 'strategy_review' and prompt['id'] != gate['id']
    assert '重新理解' in prompt['message']
    j.turn('同意，按照当前知识选择重新理解。', 'resume_pipeline_tool', {'run_id': run['id']}, reply=prompt)
    rebuilt, artifact, snapshot = wait_rebuilt(j, run['id'])
    assert rebuilt['id'] != run['id']
    assert SHARED_RULE not in artifact['items'][0]['description']
    assert artifact['id'] != original['id']
    assert store.run(run['id'])['status'] == 'cancelled'
    new_inputs = store.run(rebuilt['id'])['_source_ids']
    assert fact['id'] not in new_inputs and adopted['id'] in new_inputs
    assert SHARED_RULE not in json.dumps(j.gateway.generations[-1][1], ensure_ascii=False)
    assert j.artifact(original['id'])['items'] == original['items']
    assert original['report']['shared_facts_used'][0]['origin_chat_title'] == '历史锁定规则'
    assert shared_sources(store, j.project['id'], chat_id=origin['id'])

    explanation = j.turn('解释一下原来那版理解，仅回看历史。', 'analyze_artifact_tool',
        {'artifact_id': original['id'], 'instruction': '解释这份历史理解，仅回看历史。'})
    answer = next(part['text'] for part in explanation['parts'] if part['type'] == 'answer')
    assert '历史内容' in answer and '不表示' in answer
    assert j.snapshot()['conversation_prompt']['id'] == snapshot['conversation_prompt']['id']
    assert not store.run(rebuilt['id']).get('shared_facts_used')
    assert fact['id'] not in store.run(rebuilt['id'])['_source_ids']
