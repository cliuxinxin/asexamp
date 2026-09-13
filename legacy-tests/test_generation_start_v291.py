"""Upload and start through real HTTP/Run graphs; only model replies are controlled."""
import copy

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import until
from test_workflow_v25 import FlowModel


class StartModel(FlowModel):
    def __init__(self):
        super().__init__()
        self.actions = []

    async def generate(self, task, context):
        if task == 'conversation_turn':
            self.calls.append((task, copy.deepcopy(context)))
            return {'actions': copy.deepcopy(self.actions)}
        return await super().generate(task, context)


def upload(client, name='login-cases.txt', role='auto'):
    project = client.get('/api/projects').json()[0]
    chat = client.post(f'/api/projects/{project["id"]}/chats', json={}).json()
    response = client.post(f'/api/chats/{chat["id"]}/sources',
        files={'file': (name, b'TC-1: With an existing account, enter valid credentials. Expected: user is authenticated.', 'text/plain')},
        data={'role': role})
    assert response.status_code == 200, response.text
    return chat, response.json()


def turn(client, model, chat, sequence, name, arguments, *, mode='hitp', content='根据我上传的用例生成测试用例'):
    model.actions = [{'name': name, 'arguments': arguments}]
    response = client.post(f'/api/chats/{chat["id"]}/turns', json={
        'client_message_id': f'initial-generation-{sequence}', 'content': content, 'mode': mode})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'succeeded', result
    return result['actions'][0]['result']['run']


@pytest.mark.parametrize('arguments,goal,stop_after', [
    ({'intent': 'generate_cases'}, 'generate_case', 'review'),
    ({'intent': 'generate_scenarios'}, 'generate_scenario', 'scenarios'),
    ({'goal': 'cases'}, 'generate_case', 'cases'),
])
def test_task_and_stage_aliases_start_at_first_human_gate(tmp_path, arguments, goal, stop_after):
    model = StartModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        chat, source = upload(client)
        run = until(client, turn(client, model, chat, 1, 'workflow.start', arguments))
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'strategy_review', run
        assert run['mode'] == 'hitp'
        assert run['goal'] == goal
        assert run['stop_after'] == stop_after
        assert source['id'] in app.state.store.run(run['id'])['_source_ids']
        assert not any(task in ('generate_scenarios', 'generate_cases') for task, _ in model.calls)


def test_uploaded_cases_start_canonical_auto_mainflow(tmp_path):
    model = StartModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        chat, source = upload(client)
        run = until(client, turn(client, model, chat, 1, 'workflow.start', {'intent': 'generate_case'}, mode='auto'))
        assert run['status'] == 'completed', run
        cases = next(app.state.store.get('artifact', aid) for aid in run['artifact_ids']
                     if app.state.store.get('artifact', aid)['type'] == 'cases')
        assert cases['items'][0]['steps'][0]['expected'] == 'User is authenticated'
        assert any(ref.startswith(source['id'] + '#') for ref in cases['items'][0]['refs'])


def test_explicit_generation_command_does_not_require_conversation_planning(tmp_path):
    # This model supports authoring only, so a route call would fail the request.
    model = FlowModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        chat, _ = upload(client)
        response = client.post(f'/api/chats/{chat["id"]}/turns', json={
            'client_message_id': 'explicit-generation', 'content': '根据我上传的用例生成测试用例',
            'intent_hint': 'generate_case', 'mode': 'hitp', 'depth': 'standard',
            'command': {'name': 'workflow.start', 'arguments': {'intent': 'generate_case'}}})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == 'succeeded', result
        run = until(client, result['actions'][0]['result']['run'])
        assert run['mode'] == 'hitp' and run['interrupt']['type'] == 'strategy_review', run
        assert not any(task in ('conversation_turn', 'route') for task, _ in model.calls)


def test_sample_file_requires_role_confirmation_then_chat_continues_same_run(tmp_path):
    model = StartModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        chat, source = upload(client, name='sample-cases.txt')
        assert source['role'] == 'example'
        run = until(client, turn(client, model, chat, 1, 'workflow.start', {'intent': 'generate_case'}))
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'source_review', run
        assert not any(task == 'analyze_requirement' for task, _ in model.calls)
        original_id = run['id']
        continued = turn(client, model, chat, 2, 'workflow.continue',
            {'source_ids': [source['id']]}, content='这份上传用例是本次业务依据，请作为需求使用并继续')
        run = until(client, continued)
        assert run['id'] == original_id and run['interrupt']['type'] == 'strategy_review', run
        assert run['mode'] == 'hitp'
        assert app.state.store.get('source', source['id'])['role'] == 'example'
        assert app.state.store.run(run['id'])['_source_roles'][source['id']] == 'primary'
