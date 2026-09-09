import copy
from fastapi.testclient import TestClient
from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until

class DirectModel(Model):
    def __init__(self, clarify=False, bad=False):
        super().__init__()
        self.clarify, self.bad = clarify, bad

    async def generate(self, task, context):
        if task not in ('direct_cases', 'repair_case_rows', 'document_context'):
            return await super().generate(task, context)
        self.calls.append((task, copy.deepcopy(context)))
        if task == 'document_context':
            return {'summary': 'Login only; document approvers are not application roles.', 'refs': []}
        if self.clarify and not context.get('clarification'):
            return {'items': [], 'questions': ['Which role can log in?'], 'has_more': False}
        refs = [e['id'] for e in context['evidence'] if e['role'] != 'example']
        item = {'id': 'TC-1', 'title': 'Login', 'scenario_id': '', 'type': 'Business', 'priority': 'P1',
                'preconditions': 'Account exists', 'steps': [{'action': 'Log in', 'expected': 'Home opens'}], 'refs': refs[:1]}
        if self.bad and task == 'direct_cases':
            item['steps'] = [{'action': 'Log in'}]
        return {'items': [item], 'has_more': False, 'report': {'summary': 'Login coverage'}}

def test_generate_modify_export(tmp_path):
    model = DirectModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, ('Login requires valid credentials.\n\n' * 150))
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run
        assert [t for t, _ in model.calls] == ['direct_cases']
        artifact_id = run['artifact_ids'][0]
        modified = until(client, start(client, chat, experience='reliable', intent='modify', artifact_id=artifact_id, content='Change title'))
        assert modified['status'] == 'completed', modified
        artifact = client.get('/api/artifacts/' + artifact_id).json()
        assert artifact['items'][0]['title'] == 'Modified through chat'
        assert artifact['revision'] == 2
        response = client.get('/api/artifacts/' + artifact_id + '/export')
        assert response.status_code == 200 and response.content[:2] == b'PK'

def test_clarification_and_local_repair(tmp_path):
    for label, model in [('clarify', DirectModel(clarify=True)), ('repair', DirectModel(bad=True))]:
        with TestClient(create_app(tmp_path / label, model)) as client:
            _, chat, _ = setup_chat(client)
            run = until(client, start(client, chat, experience='reliable', mode='hitp'))
            if label == 'clarify':
                assert run['status'] == 'waiting', run
                client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Registered users'})
                run = until(client, run)
            assert run['status'] == 'completed', run
            if label == 'repair':
                assert [t for t, _ in model.calls] == ['direct_cases', 'repair_case_rows']

def test_capacity_fallback_preserves_all_evidence(tmp_path):
    model = DirectModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, '\n\n'.join(f'Rule {i}: valid credentials required.' for i in range(700)))
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run
        assert run['generation_plan']['input_groups'] > 1
        expected = {e['id'] for e in client.get('/api/sources/' + source['id']).json()['chunks']}
        seen = {e['id'] for task, context in model.calls if task == 'direct_cases' for e in context['evidence']}
        assert seen == expected
