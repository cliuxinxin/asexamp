import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from tcg import agent_contracts as contract
from tcg.agent import Agent
from tcg.agent_analysis import merge_analyses
from tcg.agent_generation import generate_items
from tcg.agent_repair import repair_fragment
from tcg.document_workspace import DocumentWorkspace
from tcg.main import create_app
from tcg.schemas import DomainError, OutputValidationError
from tcg.storage import Store
from test_agent_workflow import AgentModel
from test_backend_api import setup_chat, start, until


class RepairingAgentModel(AgentModel):
    def __init__(self, defect):
        super().__init__(questions=defect == 'feedback')
        self.defect = defect

    async def generate(self, task, context):
        if task == 'agent_repair':
            self.calls.append((task, copy.deepcopy(context)))
            path = context['repair']['path']
            replacements = {
                'plan[0]': {'id': 'analyze', 'title': 'Analyze authentication'},
                'items[0].assumption': False,
                'proceed': False,
                'has_changes': True,
                'report.business_model.nodes[0].id': 'entry',
            }
            return {'path': path, 'value': replacements[path]}
        result = await super().generate(task, context)
        if task == 'agent_plan' and self.defect == 'plan' and not any(t == 'agent_repair' for t, _ in self.calls):
            result['plan'][0] = 'not-an-object'
        elif task == 'agent_analyze' and self.defect == 'assumption':
            result['items'][0]['assumption'] = True
        elif task == 'agent_analyze' and self.defect == 'graph':
            result['report']['business_model']['nodes'][0]['id'] = 'bad id'
            for edge in result['report']['business_model']['edges']:
                if edge['from'] == 'entry':
                    edge['from'] = 'bad id'
        elif task == 'agent_feedback' and self.defect == 'feedback':
            result = {'proceed': 'no', 'has_changes': 'yes'}
        return result


@pytest.mark.parametrize(
    ('defect', 'expected_path'),
    [('plan', 'plan[0]'), ('assumption', 'items[0].assumption')],
)
def test_symbolic_validator_errors_are_repaired_at_concrete_fields(tmp_path, defect, expected_path):
    model = RepairingAgentModel(defect)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        paths = [context['repair']['path'] for task, context in model.calls if task == 'agent_repair']
        assert paths == [expected_path]


def test_feedback_repairs_each_invalid_boolean_leaf(tmp_path):
    model = RepairingAgentModel('feedback')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'waiting', run
        client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Use five attempts.'}).raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run
        paths = [context['repair']['path'] for task, context in model.calls if task == 'agent_repair']
        assert paths[:2] == ['proceed', 'has_changes']


def test_unique_malformed_node_id_repair_updates_edges_and_preserves_valid_ids(tmp_path):
    model = RepairingAgentModel('graph')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        graph = artifact['report']['business_model']
        assert [node['id'] for node in graph['nodes']] == ['entry', 'result']
        assert [edge['id'] for edge in graph['edges']] == ['accepted', 'rejected']
        assert all(edge['from'] == 'entry' for edge in graph['edges'])
        paths = [context['repair']['path'] for task, context in model.calls if task == 'agent_repair']
        assert paths == ['report.business_model.nodes[0].id']


def test_preservation_rejects_valid_graph_id_changes_and_list_drops():
    original = {'report': {'business_model': {
        'nodes': [{'id': 'entry'}, {'id': 'bad id'}],
        'edges': [{'id': 'accepted'}],
    }}}
    corrected = {'report': {'business_model': {
        'nodes': [{'id': 'renamed'}, {'id': 'fixed'}],
        'edges': [{'id': 'accepted'}],
    }}}
    with pytest.raises(OutputValidationError, match='preserved_graph'):
        Agent.preserve_schema_repair(original, corrected)

    corrected['report']['business_model']['nodes'] = [{'id': 'entry'}]
    with pytest.raises(OutputValidationError, match='preserved_graph_item_count'):
        Agent.preserve_schema_repair(original, corrected)


def test_nonempty_valid_business_collection_cannot_be_whole_field_repair():
    result = {'report': {'business_model': {'nodes': [{'id': 'n'}], 'edges': []}}}
    issue = {'path': 'report.business_model.nodes', 'expected': '1..500_nodes'}
    with pytest.raises(OutputValidationError, match='局部'):
        repair_fragment(result, issue)


def test_read_rejects_long_refs_without_echoing_model_text(tmp_path):
    store = Store(tmp_path)
    try:
        documents = DocumentWorkspace(store)
        marker = 'PRIVATE-MODEL-REF-' + ('x' * 20_000)
        with pytest.raises(DomainError) as raised:
            documents.read([], [marker])
        assert marker not in str(raised.value)
        assert len(str(raised.value)) < 200
        short_marker = 'PRIVATE-UNKNOWN'
        with pytest.raises(DomainError) as unknown:
            documents.read([], [short_marker])
        assert short_marker not in str(unknown.value)
        assert len(str(unknown.value)) < 200
    finally:
        store.close()


def _analysis(item_id, node_id, edge_id, ref):
    return {
        'items': [{'id': item_id, 'title': 'Rule', 'description': 'Rule', 'refs': [ref]}],
        'report': {
            'summary': 'Summary', 'questions': [], 'assumptions': [], 'evidence_review': [],
            'business_model': {
                'nodes': [{'id': node_id, 'label': 'Node', 'refs': [ref]}],
                'edges': [{'id': edge_id, 'from': node_id, 'to': node_id, 'label': 'Loop', 'refs': [ref]}],
            },
            'strategy': {'depth': 'standard', 'rationale': 'Reason', 'techniques': ['state'], 'scope': ['rule']},
        },
    }


def test_batch_namespacing_keeps_contract_max_ids_valid_and_references_consistent():
    results = [
        _analysis('r' * 200, 'n' * 80, 'e' * 80, 'ref-1'),
        _analysis('r' * 200, 'n' * 80, 'e' * 80, 'ref-2'),
    ]
    merged = merge_analyses(results)
    evidence = {f'ref-{index}': {'role': 'primary'} for index in (1, 2)}
    contract.analysis(merged, evidence, 'standard')
    assert all(len(item['id']) <= 200 for item in merged['items'])
    graph = merged['report']['business_model']
    assert all(len(item['id']) <= 80 for item in graph['nodes'] + graph['edges'])
    assert {edge['from'] for edge in graph['edges']} <= {node['id'] for node in graph['nodes']}
    assert len({item['id'] for item in merged['items']}) == 2


def test_grouped_generation_namespacing_keeps_max_length_item_ids_valid():
    evidence = [{'id': f'ref-{index}', 'role': 'primary', 'source_id': 'source', 'location': 'P1', 'text': 'rule'} for index in (1, 2)]
    analysis = [{'id': f'R{index}', 'title': 'Rule', 'description': 'd' * 9_000, 'refs': [f'ref-{index}']} for index in (1, 2)]
    context = {'analysis': analysis, 'business_model': {'nodes': [], 'edges': []}, 'depth': 'standard'}

    class Documents:
        def evidence(self, source_ids):
            return copy.deepcopy(evidence)

        def read(self, source_ids, refs):
            return {'evidence': [copy.deepcopy(item) for item in evidence if item['id'] in refs]}

    class RunStore:
        def run(self, run_id):
            return {'agent': {'coverage': None}}

    class GenerationAgent:
        documents = Documents()
        store = RunStore()

        def current(self, state):
            return {'_source_ids': ['source']}

        def insight(self, *args):
            pass

        async def call(self, state, task, page_context, validator):
            requirement = page_context['analysis'][0]
            return validator({'items': [{'id': 's' * 200, 'title': 'Scenario', 'description': 'Scenario',
                'priority': 'P1', 'refs': requirement['refs'], 'requirement_ids': [requirement['id']], 'branch_ids': []}],
                'has_more': False})

    items = asyncio.run(generate_items(GenerationAgent(), {'run_id': 'run', 'iteration': 1, 'action': 'scenarios'}, context, 'scenarios', []))
    assert len(items) == 2
    assert all(len(item['id']) <= 200 for item in items)
    assert len({item['id'] for item in items}) == 2


@pytest.mark.parametrize(
    ('step', 'path'),
    [({'id': '', 'title': 'Title'}, 'plan[0].id'), ({'id': 'one', 'title': ''}, 'plan[0].title')],
)
def test_plan_nested_errors_include_reachable_indexes(step, path):
    result = {'depth': 'standard', 'rationale': 'why', 'next_action': 'analyze', 'plan': [step],
              'insight': {'summary': 'summary', 'refs': []}}
    with pytest.raises(OutputValidationError) as raised:
        contract.plan(result, ['analyze'], {}, 'standard')
    assert raised.value.issue['path'] == path
