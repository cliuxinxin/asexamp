import asyncio
import copy

from fastapi.testclient import TestClient
from tcg.main import create_app
from tcg.requirement_reconciliation import reconcile_requirements, _validate
from test_backend_api import setup_chat


class FakeModel:
    def __init__(self):
        self.calls = []

    async def generate(self, task, context):
        assert task == 'reconcile_requirements'
        self.calls.append(copy.deepcopy(context))
        ids = [r['id'] for r in context['requirements']]
        linked = 'R-definition' in ids and 'R-exception' in ids
        rule = {'type': 'exception', 'text': 'Verified means completed identity checks; verified customers are exempt from the 100 unit threshold.',
                'requirement_ids': ['R-definition', 'R-exception'], 'refs': ['source#1', 'source#2']}
        return {'relationships': [rule] if linked else [], 'global_rules': [rule] if linked else [], 'conflicts': []}


def test_cross_section_rule_is_linked_and_completed_requests_are_reused(tmp_path):
    model = FakeModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        _, chat, _ = setup_chat(client)
        _, run = app.state.store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'auto',
            'content': 'Analyze', 'experience': 'reliable'})
        app.state.store.update_run(run['id'], status='running')
        rows = [{'id': 'R-definition', 'title': 'Verified customer', 'description': 'Verified means identity checks completed.', 'refs': ['source#1']},
                {'id': 'R-exception', 'title': 'Threshold exception', 'description': 'Transfers above 100 require approval except for verified customers.', 'refs': ['source#2']}]
        original = copy.deepcopy(rows)
        evidence = [{'id': 'source#1', 'source_id': 'source', 'location': 'Definitions', 'role': 'primary', 'text': rows[0]['description']},
                    {'id': 'source#2', 'source_id': 'source', 'location': 'Limits', 'role': 'primary', 'text': rows[1]['description']}]
        async def twice():
            first = await reconcile_requirements(app.state.engine, run['id'], 'analysis-key', rows, [], evidence)
            second = await reconcile_requirements(app.state.engine, run['id'], 'analysis-key', rows, [], evidence)
            return first, second
        first, second = asyncio.run(twice())
        assert first == second
        assert len(model.calls) == 1
        assert first['global_rules'][0]['requirement_ids'] == ['R-definition', 'R-exception']
        assert first['global_rules'][0]['refs'] == ['source#1', 'source#2']
        assert all(e['status'] == 'included' for e in first['reconciliation_coverage']['sections'])
        assert first['reconciliation_coverage']['all_pairs_examined'] is False
        assert rows == original
        assert model.calls[0]['evidence'] == evidence


def test_reconciliation_rejects_invented_ids_and_resolved_conflict():
    rows = [{'id': 'R1', 'refs': ['e1']}]
    value = {'relationships': [], 'global_rules': [{'type': 'rule', 'text': 'Unsupported', 'requirement_ids': ['R2'], 'refs': ['e1']}],
             'conflicts': [{'type': 'conflict', 'text': 'Conflict', 'requirement_ids': ['R1'], 'refs': ['e1'], 'status': 'resolved'}]}
    errors = _validate(value, rows, [{'id': 'e1'}])
    assert len(errors) == 2


def test_interrupted_multigroup_reconciliation_reuses_completed_group(tmp_path, monkeypatch):
    monkeypatch.setenv('TCG_MODEL_CONTEXT_TOKENS', '16384')
    monkeypatch.setenv('TCG_MODEL_OUTPUT_TOKENS', '2048')
    model = FakeModel()
    original_generate = model.generate
    completed = []
    fail = [True]
    async def interrupted(task, context):
        if completed and fail[0]:
            raise RuntimeError('External model interrupted')
        result = await original_generate(task, context)
        completed.append([r['id'] for r in context['requirements']])
        return result
    model.generate = interrupted
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        _, chat, _ = setup_chat(client)
        _, run = app.state.store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'auto',
            'content': 'Analyze', 'experience': 'reliable'})
        app.state.store.update_run(run['id'], status='running')
        rows = [{'id': f'R{i}', 'title': f'Policy {i}', 'description': 'shared verified threshold ' * 120,
                 'refs': [f'e{i}']} for i in range(32)]
        evidence = [{'id': f'e{i}', 'source_id': 'source', 'location': f'Section {i}', 'role': 'primary',
                     'text': rows[i]['description']} for i in range(32)]
        async def retry():
            import pytest
            with pytest.raises(Exception, match='RuntimeError'):
                await reconcile_requirements(app.state.engine, run['id'], 'retry-key', rows, [], evidence)
            first = completed[0]
            fail[0] = False
            result = await reconcile_requirements(app.state.engine, run['id'], 'retry-key', rows, [], evidence)
            assert completed.count(first) == 1
            return result
        result = asyncio.run(retry())
        coverage = result['reconciliation_coverage']
        assert coverage['capacity_group_count'] > 1
        assert coverage['candidate_pair_count'] > 0
        assert coverage['completed_candidate_pair_count'] > 0
        assert coverage['partial'] is True
        assert len(coverage['included_requirement_ids']) == 32
