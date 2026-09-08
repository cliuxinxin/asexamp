import copy
import json

import pytest
from fastapi.testclient import TestClient

from tcg import agent_contracts as contract
from tcg.documents import parse_text
from tcg.agent_generation import compact_previous
from tcg.main import create_app
from tcg.schemas import OutputValidationError
from test_agent_workflow import AgentModel, GoalModel
from test_backend_api import setup_chat, start, until


def test_aggregate_previews_fit_budget_without_mutating_full_facts():
    from tcg.agent_context import compact_items, model_view

    refs = ['src_' + 'a' * 32 + f'#P{i}' for i in range(1, 121)]
    prose = 'detail ' * 1000
    items = [{'id': f'R{i}', 'title': 'title ' * 500, 'refs': refs[i:i + 4],
              'requirement_ids': ['R1'], 'branch_ids': ['B1']} for i in range(40)]
    context = {
        'request': {'content': 'Review'},
        'memory': {'decisions': [{'id': f'run_{i}:0', 'summary': prose, 'refs': refs[:12],
                                 'status': 'confirmed'} for i in range(12)],
                   'scope': [prose] * 12, 'open_questions': [prose] * 12,
                   'assumptions': [prose] * 12, 'source_refs': refs},
        'strategy': {'depth': 'standard', 'rationale': prose, 'scope': [prose] * 12,
                     'techniques': [prose] * 12},
        'assumptions': [prose] * 12, 'deferred_questions': [prose] * 12, 'questions': [prose] * 12,
        'business_model': {'node_count': 30, 'edge_count': 30,
                           'nodes': [{'id': f'N{i}', 'label': 'L' * 300} for i in range(30)],
                           'edges': [{'id': f'E{i}', 'label': 'L' * 300, 'from': 'N1', 'to': 'N2'} for i in range(30)]},
        'evidence': [{'id': ref, 'source_id': 'src_' + 'a' * 32, 'role': 'primary', 'location': 'paragraph'} for ref in refs[:40]],
        'clarification_decision': {'mode': 'proceed', 'epoch': 0, 'content': prose},
    }
    for field in ('analysis', 'scenarios', 'cases'):
        context[field] = compact_items(items)
        context[field + '_count'] = len(items)
    original = copy.deepcopy(context)
    plan = model_view(context, 'plan')
    summary = model_view({**plan, 'output': {'type': 'cases', 'title': 'Final', 'item_count': 40,
                                           'items': compact_items(items, 12)}}, 'summary')
    assert all(len(json.dumps(view, ensure_ascii=False)) <= 30_000 for view in (plan, summary))
    assert context == original
    for field in ('assumptions', 'deferred_questions', 'questions'):
        assert plan[field + '_projection']['total_count'] == 12
        assert plan[field + '_projection']['truncated']
    assert summary['clarification_decision']['content_characters'] == len(prose)
    assert summary['clarification_decision']['content_truncated']


def test_planner_accepts_citation_from_visible_finding_beyond_metadata_preview(tmp_path):
    class FindingModel(AgentModel):
        cited = None

        async def generate(self, task, context):
            if task == 'agent_integrate':
                self.calls.append((task, copy.deepcopy(context)))
                return {'edges': [], 'limitations': []}
            result = await super().generate(task, context)
            if task == 'agent_plan' and context.get('analysis'):
                self.cited = context['analysis'][-1]['refs'][-1]
                result['insight']['refs'] = [self.cited]
            return result

    model = FindingModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='\n\n'.join(
            f'Rule {index}: ' + 'Valid credentials are accepted. ' * 35 for index in range(60)))
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        plan = [c for task, c in model.calls if task == 'agent_plan' and c.get('analysis')][-1]
        assert model.cited in {ref for item in plan['analysis'] for ref in item['refs']}
        assert model.cited not in {e['id'] for e in plan['evidence']}


def configured(model):
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    return model


def test_query_planner_uses_captured_artifact_scope_after_source_deactivation(tmp_path):
    model = configured(GoalModel())
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert initial['status'] == 'completed', initial
        client.app.state.store.deactivate_source(source['id'])
        run = until(client, start(client, chat, experience='agent', intent='query', source_ids=[],
                                  artifact_id=initial['artifact_ids'][-1], content='When are valid credentials accepted?'))
        assert run['status'] == 'completed', run
        query = [context for task, context in model.calls if task == 'query'][-1]
        assert source['id'] in {e['source_id'] for e in query['evidence']}


def test_selected_modification_validates_untouched_items_against_full_scope(tmp_path):
    model = configured(GoalModel())
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, text='Accept valid credentials.\n\nReject invalid credentials.')
        initial = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        artifact = client.get('/api/artifacts/' + initial['artifact_ids'][-1]).json()
        refs = [chunk['id'] for chunk in client.get('/api/sources/' + source['id']).json()['chunks']]
        first = {**artifact['items'][0], 'refs': [refs[0]]}
        second = {**artifact['items'][0], 'id': 'C2', 'title': 'Untouched rejection', 'refs': [refs[1]]}
        client.put('/api/artifacts/' + artifact['id'], json={'expected_revision': 1, 'items': [first, second]}).raise_for_status()

        run = until(client, start(client, chat, experience='agent', intent='modify', artifact_id=artifact['id'],
                                  selected_ids=['C1'], content='Rename selected case'))

        assert run['status'] == 'completed', run
        result = client.get('/api/artifacts/' + artifact['id']).json()
        assert result['items'][1] == second
        modify_context = next(context for task, context in model.calls if task == 'modify')
        assert {item['id'] for item in modify_context['artifact']['items']} == {'C1', 'C2'}
        assert refs[1] not in {item['id'] for item in modify_context['evidence']}


def test_import_cases_exhaustively_batches_input_and_namespaces_outputs(tmp_path):
    class ImportModel(GoalModel):
        async def generate(self, task, context):
            if task == 'import_cases':
                self.calls.append((task, copy.deepcopy(context)))
                return {'items': [{'id': f'C{index}', 'title': evidence['text'][:60], 'scenario_id': '',
                                   'type': 'Business', 'priority': 'P1', 'preconditions': '',
                                   'steps': [{'action': 'Perform', 'expected': 'Accept'}], 'refs': [evidence['id']]}
                                  for index, evidence in enumerate(context['evidence'])], 'has_more': False}
            return await super().generate(task, context)

    model = configured(ImportModel())
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, text='\n\n'.join(
            f'Case {index}: ' + 'Perform the required behavior. ' * 40 for index in range(35)))
        expected = [chunk['id'] for chunk in client.get('/api/sources/' + source['id']).json()['chunks']]

        run = until(client, start(client, chat, experience='agent', intent='review_case', content='Review every case'))

        assert run['status'] == 'completed', run
        calls = [context for task, context in model.calls if task == 'import_cases']
        assert len(calls) > 1
        assert [item['id'] for context in calls for item in context['evidence']] == expected
        assert all(len(json.dumps(context['evidence'], ensure_ascii=False)) <= 12_000 for context in calls)
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert len(artifact['items']) == len(expected)
        assert len({item['id'] for item in artifact['items']}) == len(expected)
        assert set(expected) == {ref for item in artifact['items'] for ref in item['refs']}


def test_summary_metadata_prioritizes_and_accepts_tail_output_citation(tmp_path):
    class TailModel(AgentModel):
        answer_ref = None

        async def generate(self, task, context):
            if task == 'query':
                self.calls.append((task, copy.deepcopy(context)))
                return {'answer': 'The tail rule is retained.',
                        'refs': [e['id'] for e in context['evidence'] if 'tailneedle' in e.get('text', '')]}
            if task == 'agent_summary':
                self.calls.append((task, copy.deepcopy(context)))
                return {'summary': 'The tail rule is retained.', 'refs': [self.answer_ref]}
            return await super().generate(task, context)

    model = TailModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, text='\n\n'.join(
            [f'Unrelated paragraph {index}.' for index in range(49)] + ['The tailneedle rule is retained.']))
        model.answer_ref = client.get('/api/sources/' + source['id']).json()['chunks'][-1]['id']

        run = until(client, start(client, chat, experience='agent', intent='query', content='tailneedle'))

        assert run['status'] == 'completed', run
        summary = next(context for task, context in model.calls if task == 'agent_summary')
        assert model.answer_ref in {item['id'] for item in summary['evidence']}
        assert all('text' not in item for item in summary['evidence'])


def test_large_durable_prose_is_projected_for_plan_summary_and_route(tmp_path):
    class VerboseModel(AgentModel):
        async def generate(self, task, context):
            if task == 'route':
                self.calls.append((task, copy.deepcopy(context)))
                return {'intent': 'review_requirement'}
            result = await super().generate(task, context)
            if task == 'agent_analyze':
                prose = 'scope details ' * 780
                result['report']['strategy']['scope'] = [f'business {index}: {prose}' for index in range(5)]
                result['report']['strategy']['techniques'] = [f'technique {index}: {prose}' for index in range(5)]
                result['report']['assumptions'] = [f'assumption {index}: {prose}' for index in range(5)]
                result['report']['business_model']['nodes'][0]['label'] = prose
            return result

    model = VerboseModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        contexts = [context for task, context in model.calls if task in ('agent_plan', 'agent_summary')]
        assert contexts
        assert all(len(json.dumps(context, ensure_ascii=False)) <= 30_000 for context in contexts)
        assert any(context.get('memory', {}).get('_projection', {}).get('scope', {}).get('truncated') for context in contexts)

        memory = client.app.state.store.get('chat', chat['id'])['memory']
        memory['decisions'].append({'id': 'long', 'summary': 'remember ' * 15_000, 'refs': [],
                                    'status': 'confirmed', 'run_id': run['id']})
        saved_chat = client.app.state.store.get('chat', chat['id'])
        saved_chat['memory'] = memory
        client.app.state.store.put('chat', saved_chat)
        routed = until(client, start(client, chat, experience='agent', intent='auto', content='Review the requirements',
                                     confirm_strategy=False))
        assert routed['status'] == 'completed', routed
        route = [context for task, context in model.calls if task == 'route'][-1]
        assert len(json.dumps(route, ensure_ascii=False)) <= 12_000
        assert route['confirmed_memory']['_projection']['decisions']['truncated']


def test_analyze_uses_full_role_inventory_beyond_metadata_preview(tmp_path):
    class IntakeRejectingModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_intake':
                raise AssertionError('Primary source exists outside the metadata preview')
            return await super().generate(task, context)

    model = IntakeRejectingModel()
    with TestClient(create_app(tmp_path, model)) as client:
        project = client.get('/api/projects').json()[0]
        chat = client.post('/api/projects/' + project['id'] + '/chats', json={'title': 'Inventory'}).json()
        example_text, example_chunks = parse_text('\n\n'.join(f'Example format {index}' for index in range(50)))
        primary_text, primary_chunks = parse_text('Valid credentials must be accepted.')
        client.app.state.store.add_source(chat['id'], 'Examples', 'example', example_text, example_chunks,
                                          source_id='src_a_examples')
        client.app.state.store.add_source(chat['id'], 'Late primary', 'primary', primary_text, primary_chunks,
                                          source_id='src_z_primary')

        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))

        assert run['status'] == 'completed', run
        assert any(task == 'agent_analyze' for task, _ in model.calls)


def test_metadata_plan_allows_procedural_empty_refs_but_rejects_unknown_refs():
    evidence = {'p1': {'id': 'p1', 'role': 'primary'}}
    base = {'depth': 'standard', 'rationale': 'Plan the requested work.',
            'plan': [{'id': 'analyze', 'title': 'Analyze'}], 'next_action': 'analyze',
            'insight': {'summary': 'I will inspect the authorized sources.', 'refs': []}}
    assert contract.plan(copy.deepcopy(base), ['analyze'], evidence, 'auto', require_insight_refs=False)
    invalid = copy.deepcopy(base)
    invalid['insight']['refs'] = ['invented']
    with pytest.raises(OutputValidationError):
        contract.plan(invalid, ['analyze'], evidence, 'auto', require_insight_refs=False)


def test_long_feedback_is_preserved_as_change_without_unbounded_classifier_call(tmp_path):
    class FeedbackModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_integrate':
                self.calls.append((task, copy.deepcopy(context)))
                return {'edges': [], 'limitations': []}
            return await super().generate(task, context)

    model = FeedbackModel(questions=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'waiting'
        answer = 'The lockout threshold is five failures. ' + 'supporting detail ' * 3000

        client.post('/api/runs/' + run['id'] + '/resume', json={'answer': answer}).raise_for_status()
        run = until(client, run)

        assert run['status'] == 'completed', run
        assert not any(task == 'agent_feedback' for task, _ in model.calls)
        sources = client.get('/api/chats/' + chat['id']).json()['sources']
        clarification = next(source for source in sources if source['role'] == 'clarification')
        stored = client.get('/api/sources/' + clarification['id']).json()['text']
        assert stored.replace('\n\n', '') == answer.strip()
        assert clarification['characters'] == len(stored)
        assert all(len(json.dumps(context, ensure_ascii=False)) <= 30_000
                   for task, context in model.calls if task == 'agent_analyze')


def test_repair_previous_case_projection_has_an_aggregate_bound():
    cases = [{'id': f'C{index}', 'title': 'Case', 'scenario_id': 'S1', 'type': 'Business',
              'priority': 'P1', 'preconditions': 'p' * 500,
              'refs': ['ref'], 'requirement_ids': ['R1'], 'branch_ids': ['B1'],
              'steps': [{'action': 'a' * 500, 'expected': 'e' * 500} for _ in range(8)]}
             for index in range(8)]
    projected = compact_previous(cases, 'cases')
    assert len(json.dumps(projected, ensure_ascii=False)) < 12_000
    assert all(item['step_count'] == 8 and len(item['steps']) == 2 for item in projected)
