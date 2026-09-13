import copy
import time

from fastapi.testclient import TestClient
from test_backend_api import Model, setup_chat, start, until
from tcg.main import create_app


class ReliableModel(Model):
    def __init__(self, bad_analysis=False, fail_case=False):
        super().__init__()
        self.bad_analysis, self.fail_case = bad_analysis, fail_case

    async def generate(self, task, context):
        result = await super().generate(task, context)
        if task == 'analyze_requirement':
            refs = [e['id'] for e in context['evidence'] if e['role'] != 'example']
            result['items'] = [{'id': f'R{i}', 'title': f'Rule {i}', 'description': e['text'],
                                'refs': [e['id']]} for i, e in enumerate(context['evidence']) if e['role'] != 'example']
            if context.get('clarification'):
                result['items'] = copy.deepcopy(context['analysis'])
            if self.bad_analysis and not context.get('validation_repair'):
                for item in result['items']:
                    item['refs'] = ['invented']
        if task == 'generate_scenarios':
            result['items'] = [{'id': f'S{i}', 'title': r['title'], 'description': r['description'],
                                'priority': 'P1', 'refs': r['refs'], 'requirement_ids': [r['id']]}
                               for i, r in enumerate(context['analysis'])]
        if task == 'generate_cases':
            if self.fail_case:
                raise RuntimeError('test-only transient outage')
            result['items'] = [{'id': f'C{i}', 'title': s['title'], 'scenario_id': s['id'],
                                'type': context['profile']['case_types'][0], 'priority': 'P1',
                                'preconditions': 'Given account', 'steps': [{'action': 'Submit', 'expected': 'Accepted'}],
                                'refs': s['refs']} for i, s in enumerate(context['scenarios'])]
        if task == 'review_cases':
            result = {'operations': [], 'report': {'summary': 'Reviewed current batch'}}
        return result


def test_reliable_full_flow_has_progress_and_immutable_configuration(tmp_path):
    model = ReliableModel()
    with TestClient(create_app(tmp_path, model)) as client:
        project, chat, source = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', depth='quick', case_types=['Boundary']))
        assert run['status'] == 'completed', run
        assert run['graph_version'] == 4
        assert run['progress']['completed'] == run['progress']['total']
        contexts = [c for t, c in model.calls if t == 'generate_cases']
        assert contexts[0]['profile']['case_types'] == ['Boundary']
        assert contexts[0]['profile']['case_level'] == 'quick'
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert artifact['items'][0]['type'] == 'Boundary'
        export = client.get('/api/artifacts/' + artifact['id'] + '/export')
        assert export.status_code == 200 and export.content[:2] == b'PK'


def test_all_bad_refs_are_reported_in_one_repair(tmp_path):
    model = ReliableModel(bad_analysis=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, 'Rule one.\n\nRule two.\n\nRule three.')
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run
        repaired = [c for t, c in model.calls if t == 'analyze_requirement' and c.get('validation_repair')]
        assert len(repaired) == 1
        errors = repaired[0]['validation_repair']['errors']
        assert {e['path'] for e in errors} >= {'items[0].refs[0]', 'items[1].refs[0]', 'items[2].refs[0]'}


def test_long_document_is_partitioned_and_retry_preserves_analysis(tmp_path):
    model = ReliableModel(fail_case=True)
    text = '\n\n'.join(f'Rule {i}: ' + 'A' * 1300 for i in range(24))
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, text)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'failed', run
        analyses = [c for t, c in model.calls if t == 'analyze_requirement']
        assert len(analyses) > 1
        expected = {c['id'] for c in client.get('/api/sources/' + source['id']).json()['chunks']}
        seen = {e['id'] for c in analyses for e in c['evidence']}
        assert expected <= seen
        count = len(analyses)
    model.fail_case = False
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry', json={}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert len([c for t, c in model.calls if t == 'analyze_requirement']) == count


def test_confirmed_memory_cross_chat_and_project_isolation(tmp_path):
    model = ReliableModel()
    with TestClient(create_app(tmp_path, model)) as client:
        project, chat, _ = setup_chat(client)
        response = client.post('/api/projects/' + project['id'] + '/memory', json={'content': '本项目步骤使用中文', 'kind': 'preference'})
        assert response.status_code == 200, response.text
        memory_id = response.json()['id']
        _, other_chat, _ = setup_chat(client)
        run = until(client, start(client, other_chat, experience='reliable'))
        assert run['status'] == 'completed', run
        context = [c for t, c in model.calls if t == 'generate_cases'][-1]
        assert any(n['content'] == '本项目步骤使用中文' for n in context['confirmed_memory'])
        other = client.post('/api/projects', json={'name': 'Isolated'}).json()
        assert client.get('/api/projects/' + other['id'] + '/memory').json() == []
        assert client.delete('/api/projects/' + project['id'] + '/memory/' + memory_id).status_code == 200


def test_unselected_case_type_can_be_corrected(tmp_path):
    class WrongType(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases' and not context.get('validation_repair'):
                result['items'][0]['type'] = 'Security'
            return result
    with TestClient(create_app(tmp_path, WrongType())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', case_types=['Boundary']))
        assert run['status'] == 'completed', run.get('error')


def test_reliable_human_confirmation_and_restart(tmp_path):
    model = ReliableModel()
    model.questions = ['How many attempts?']
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        assert run['interrupt']['type'] == 'clarification'
    model.questions = []
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Five attempts.'}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'waiting', run.get('error')
        assert run['interrupt']['type'] == 'scenario_review'
        aid = run['interrupt']['artifact_id']
        artifact = client.get('/api/artifacts/' + aid).json()
        artifact['items'][0]['title'] = 'Confirmed scenario'
        assert client.put('/api/artifacts/' + aid, json={'expected_revision': artifact['revision'], 'items': artifact['items']}).status_code == 200
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error')
        assert any(s['title'] == 'Confirmed scenario' for t, c in model.calls if t == 'generate_cases' for s in c['scenarios'])


def test_running_reliable_work_recovers_automatically(tmp_path):
    model = ReliableModel()
    model.delay_task = 'generate_cases'
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat, experience='reliable')
        deadline = time.monotonic() + 5
        while not any(t == 'generate_cases' for t, _ in model.calls):
            assert time.monotonic() < deadline
            time.sleep(.01)
    count = len([t for t, _ in model.calls if t == 'analyze_requirement'])
    model.delay_task = None
    with TestClient(create_app(tmp_path, model)) as client:
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error')
        assert len([t for t, _ in model.calls if t == 'analyze_requirement']) == count


def test_review_add_ids_are_unique_across_batches(tmp_path):
    class Additions(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                for item in result['items']:
                    item['steps'][0]['action'] = 'A' * 3500
            if task == 'review_cases':
                item = copy.deepcopy(context['cases'][0])
                item['id'] = 'ADDED'
                result['operations'] = [{'op': 'add', 'item': item}]
            return result
    with TestClient(create_app(tmp_path, Additions())) as client:
        _, chat, _ = setup_chat(client, 'Rule one.\n\nRule two.\n\nRule three.')
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run.get('error')
        items = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()['items']
        assert len(items) == 6
        assert len({i['id'] for i in items}) == 6
        assert len([i for i in items if i['id'].startswith('C1-')]) == 3


def test_clarification_repairs_dropped_rules_and_preserves_ids(tmp_path):
    class DropRule(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'analyze_requirement' and context.get('clarification'):
                result['items'] = copy.deepcopy(context['analysis'])
                result['report']['questions'] = []
                if not context.get('validation_repair'):
                    result['items'] = result['items'][:1]
            return result
    model = DropRule()
    model.questions = ['Confirm rules?']
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, 'Rule one.\n\nRule two.\n\nRule three.')
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Keep all rules.'})
        run = until(client, run)
        assert run['status'] == 'waiting', run.get('error')
        scenarios = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()['items']
        assert len(scenarios) == 3
        original_ids = {i['id'] for t, c in model.calls if t == 'analyze_requirement' for i in c.get('analysis', [])}
        assert {r for s in scenarios for r in s['requirement_ids']} == original_ids
        assert any(c.get('validation_repair') for t, c in model.calls if c.get('clarification'))


def test_remaining_clarification_questions_pause_again(tmp_path):
    class Questions(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'analyze_requirement' and context.get('clarification'):
                result['items'] = copy.deepcopy(context['analysis'])
                result['report']['questions'] = ['Which role?'] if context['clarification'] == 'Five attempts.' else []
            return result
    model = Questions()
    model.questions = ['How many attempts?']
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Five attempts.'})
        run = until(client, run)
        assert run['interrupt']['type'] == 'clarification'
        assert run['interrupt']['questions'] == ['Which role?']
        client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'All users.'})
        run = until(client, run)
        assert run['interrupt']['type'] == 'scenario_review', run.get('error')


def test_long_change_document_uses_bounded_grounded_batches(tmp_path):
    model = ReliableModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, '\n\n'.join(f'Change {i}: ' + 'A' * 1000 for i in range(35)))
        assert client.put('/api/sources/' + source['id'] + '/role', json={'role': 'change'}).status_code == 200
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run.get('error')
        items = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()['items']
        expected = {e['id'] for e in client.get('/api/sources/' + source['id']).json()['chunks']}
        assert {ref for item in items for ref in item['refs']} == expected
        from tcg.reliable import encoded_size
        assert all(encoded_size(c) < 32000 for _, c in model.calls)


def test_malformed_unhashable_case_id_gets_repaired(tmp_path):
    class Malformed(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases' and not context.get('validation_repair'):
                result['items'][0]['id'] = []
            return result
    with TestClient(create_app(tmp_path, Malformed())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run.get('error')


def test_corrected_example_source_can_generate_cases(tmp_path):
    with TestClient(create_app(tmp_path, ReliableModel())) as client:
        _, chat, _ = setup_chat(client)
        source = client.post('/api/chats/' + chat['id'] + '/sources/text', json={
            'name': 'Misclassified', 'role': 'example', 'text': 'Users must sign in.'}).json()
        assert client.put('/api/sources/' + source['id'] + '/role', json={'role': 'primary'}).status_code == 200
        run = until(client, start(client, chat, experience='reliable', source_ids=[source['id']]))
        assert run['status'] == 'completed', run.get('error')
        assert client.get('/api/artifacts/' + run['artifact_ids'][0]).json()['type'] == 'cases'


def test_oversized_model_item_is_repaired_before_it_is_accepted(tmp_path):
    class Oversized(ReliableModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases' and not context.get('validation_repair'):
                result['items'][0]['steps'][0]['action'] = 'A' * 25000
            return result
    model = Oversized()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run.get('error')
        case = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()['items'][0]
        assert case['steps'][0]['action'] == 'Submit'
        assert any(c.get('validation_repair') for t, c in model.calls if t == 'generate_cases')


def test_accepted_review_additions_survive_restart_retry(tmp_path):
    class RetryReview(ReliableModel):
        fail_review = True
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                for item in result['items']:
                    item['steps'][0]['action'] = 'A' * 3500
            if task == 'review_cases':
                if self.fail_review and context['cases'][0]['id'] != 'C1-C0':
                    raise RuntimeError('Temporary later batch failure')
                item = copy.deepcopy(context['cases'][0])
                item['id'] = 'ADDED'
                result['operations'] = [{'op': 'add', 'item': item}]
            return result
    model = RetryReview()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, 'Rule one.\n\nRule two.\n\nRule three.')
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'failed'
        first_count = sum(t == 'review_cases' and c['cases'][0]['id'] == 'C1-C0' for t, c in model.calls)
        assert first_count == 1
    model.fail_review = False
    with TestClient(create_app(tmp_path, model)) as client:
        client.post('/api/runs/' + run['id'] + '/retry', json={})
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error')
        items = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()['items']
        assert len(items) == len({i['id'] for i in items}) == 6
        assert sum(t == 'review_cases' and c['cases'][0]['id'] == 'C1-C0' for t, c in model.calls) == first_count


def test_reliable_context_retains_bounded_history_and_compact_memory(tmp_path):
    import json
    model = ReliableModel()
    with TestClient(create_app(tmp_path, model)) as client:
        project, chat, _ = setup_chat(client)
        client.post('/api/projects/' + project['id'] + '/memory', json={'content': 'Use Chinese steps.', 'kind': 'preference'})
        first = until(client, start(client, chat, experience='reliable', content='Prior instruction: ' + 'A' * 5000))
        assert first['status'] == 'completed'
        run = until(client, start(client, chat, experience='reliable', content='Generate another version.'))
        assert run['status'] == 'completed', run.get('error')
        context = [c for t, c in model.calls if t == 'generate_cases'][-1]
        assert context['confirmed_memory'] == [{'kind': 'preference', 'content': 'Use Chinese steps.'}]
        assert any('Prior instruction:' in m['content'] for m in context['conversation'])
        assert len(context['conversation']) <= 6
        assert len(json.dumps(context['conversation'], ensure_ascii=False)) <= 3000
        assert context['conversation_context']['partial'] is True
