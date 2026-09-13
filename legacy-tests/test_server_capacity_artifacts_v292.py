"""Server-led capacity recovery must preserve scope and atomic artifact edits."""
import asyncio
import copy

import pytest

from tcg.artifact_actions import invoke_groups, preview_action
from tcg.server_capacity import ContextCapacityError
from test_conversation_artifacts_v260 import data as artifact_data
from test_framework_dialogue_v270 import setup as dialogue_setup


class ServerEngine:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def fits(self, *_):
        raise AssertionError('Local capacity admission must not run')

    async def invoke_model(self, task, context, run_id=None):
        self.calls.append((task, copy.deepcopy(context), run_id))
        return self.respond(task, context)


def collect(engine, rows, build):
    async def run():
        return [value async for value in invoke_groups(engine, 'artifact_explain', rows, build, 'run-read')]
    return asyncio.run(run())


def test_full_selected_payload_reaches_server_without_local_capacity_admission():
    rows = [{'id': 'C1', 'detail': '完整用例' * 140000}, {'id': 'C2', 'detail': '保留第二条'}]
    engine = ServerEngine(lambda *_: {'answer': '已解释', 'refs': []})
    results = collect(engine, rows, lambda group: {'items': group})
    assert len(engine.calls) == 1
    assert engine.calls[0][1]['items'] == rows
    assert engine.calls[0][2] == 'run-read'
    assert results[0][0] == rows


def test_provider_overflow_bisects_rows_with_exact_evidence_ids():
    rows = [{'id': f'C{i}', 'refs': [f'src#P{i}']} for i in range(4)]
    def build(group):
        return {'items': group, 'evidence': [{'id': row['refs'][0], 'text': row['id']} for row in group]}
    def respond(task, context):
        if len(context['items']) > 2:
            raise ContextCapacityError(8192)
        return {'answer': ','.join(row['id'] for row in context['items']),
                'refs': [row['id'] for row in context['evidence']]}
    engine = ServerEngine(respond)
    results = collect(engine, rows, build)
    assert [[row['id'] for row in call[1]['items']] for call in engine.calls] == [
        ['C0', 'C1', 'C2', 'C3'], ['C0', 'C1'], ['C2', 'C3']]
    assert [row for group, _, _ in results for row in group] == rows
    for group, context, result in results:
        assert result['refs'] == [row['refs'][0] for row in group]
        assert result['refs'] == [row['id'] for row in context['evidence']]


def test_unrelated_model_failure_does_not_trigger_smaller_requests():
    def respond(*_):
        raise RuntimeError('Model unavailable')
    engine = ServerEngine(respond)
    with pytest.raises(RuntimeError, match='Model unavailable'):
        collect(engine, [{'id': 'C1'}, {'id': 'C2'}], lambda group: {'items': group})
    assert len(engine.calls) == 1


def test_single_item_server_rejection_stops_with_actionable_capacity_error():
    def respond(*_):
        raise ContextCapacityError(4096)
    engine = ServerEngine(respond)
    with pytest.raises(ContextCapacityError) as caught:
        collect(engine, [{'id': 'C1'}], lambda group: {'items': group})
    assert len(engine.calls) == 1
    assert caught.value.category == 'context_capacity'
    assert '单个' in str(caught.value)
    assert '原成果' in str(caught.value)


@pytest.fixture
def saved_artifacts(tmp_path):
    fixture = artifact_data(tmp_path)
    try:
        yield next(fixture)
    finally:
        next(fixture, None)


def test_scenario_estimate_retries_rejected_scope_without_saving_cases(saved_artifacts):
    store, chat, _, scenarios, cases, _, _ = saved_artifacts
    def respond(task, context):
        assert task == 'artifact_estimate'
        rows = context['scenarios']
        if len(rows) > 1:
            raise ContextCapacityError(4096)
        return {'scenarios': [{'scenario_id': row['id'], 'min_count': 2, 'max_count': 4,
            'rationale': row['title'], 'assumptions': ['按现有场景估算']} for row in rows]}
    engine = ServerEngine(respond)
    result = asyncio.run(preview_action(store, engine, 'scenarios', {
        'action': 'estimate', 'instruction': '只估算，不生成用例', 'selected_ids': ['S1', 'S2']}))
    assert [len(call[1]['scenarios']) for call in engine.calls] == [2, 1, 1]
    assert [row['scenario_id'] for row in result['estimate']['scenarios']] == ['S1', 'S2']
    assert (result['estimate']['min_count'], result['estimate']['max_count']) == (4, 8)
    assert store.get('artifact', 'scenarios') == scenarios
    assert store.get('artifact', 'cases') == cases
    assert store.list('action_proposal', chat_id=chat['id']) == []


def test_later_split_edit_failure_keeps_all_artifacts_and_releases_lease(saved_artifacts):
    store, chat, analysis, scenarios, cases, _, _ = saved_artifacts
    def respond(task, context):
        assert task == 'artifact_modify'
        ids = context['selected_ids']
        if len(ids) > 1:
            raise ContextCapacityError(8192)
        if ids == ['R2']:
            raise RuntimeError('Second batch failed')
        assert ids == ['R1']
        return {'operations': [{'op': 'update', 'id': 'R1', 'item': {'description': 'Draft only'}}]}
    engine = ServerEngine(respond)
    with pytest.raises(RuntimeError, match='Second batch failed'):
        asyncio.run(preview_action(store, engine, 'analysis', {
            'action': 'modify', 'instruction': '更新两个需求并联动场景用例',
            'selected_ids': ['R1', 'R2'], 'sync_related': True}))
    assert [call[1]['selected_ids'] for call in engine.calls] == [['R1', 'R2'], ['R1'], ['R2']]
    for original in (analysis, scenarios, cases):
        assert store.get('artifact', original['id']) == original
        assert len(store.revisions(original['id'])) == 1
    assert store.list('action_proposal', chat_id=chat['id']) == []
    assert not getattr(store, '_workspace_action_tokens', {})


def test_dialogue_summary_overflow_returns_full_validated_answers_and_coverage(tmp_path):
    from tcg.dialogue_context import answer_dialogue
    store, engine, _, run, source = dialogue_setup(tmp_path, ['第一条规则', '第二条规则'])
    calls = []
    batch_answers = []
    async def respond(task, context, run_id=None):
        calls.append(copy.deepcopy(context))
        if context.get('dialogue_phase') == 'summary' or len(context.get('evidence', [])) > 1:
            raise ContextCapacityError(4096)
        evidence = context['evidence']
        assert len(evidence) == 1
        answer = evidence[0]['text'] + '完整解释' * 500 + '末尾必须保留'
        batch_answers.append(answer)
        return {'answer': answer, 'refs': [evidence[0]['id']]}
    engine.invoke_model = respond
    engine.fits = lambda *_: (_ for _ in ()).throw(AssertionError('No local admission'))
    before = store.run(run['id'])
    try:
        result = asyncio.run(answer_dialogue(engine, run['id'], content='总结所有需求'))
        assert len(calls[0]['evidence']) == 2
        assert any(context.get('dialogue_phase') == 'summary' for context in calls)
        assert len(batch_answers) == 2
        assert all(answer in result['answer'] for answer in batch_answers)
        assert set(result['refs']) == {row['id'] for row in store.evidence([source['id']])}
        assert result['coverage']['included_evidence_count'] == 2
        assert result['coverage']['batch_count'] == 2
        assert result['coverage']['partial'] is False
        assert result['coverage']['synthesis'] == 'batch_answers'
        assert not result['coverage'].get('answer_excerpts')
        assert store.run(run['id']) == before
        assert store.list('artifact', chat_id=run['chat_id']) == []
    finally:
        store.close()
