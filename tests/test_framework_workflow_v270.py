"""Behavioral integration checks for the approved authoring framework."""
import copy

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import setup_chat, start, until
from test_workflow_v25 import FlowModel


def test_report_annotation_preserves_the_consumed_revision(tmp_path):
    app = create_app(tmp_path, FlowModel())
    with TestClient(app) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run
        artifact = app.state.store.get('artifact', run['artifact_ids'][0])
        revision = artifact['revision']
        historical = copy.deepcopy(app.state.store.revision(artifact['id'], revision))
        app.state.engine.save_report(artifact, {'summary': 'Additional review explanation'})
        assert app.state.store.revision(artifact['id'], revision) == historical
        latest = app.state.store.get('artifact', artifact['id'])
        assert latest['revision'] > revision
        assert latest['report']['summary'] == 'Additional review explanation'


def test_analysis_reuse_obeys_semantic_scope(tmp_path):
    model = FlowModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        first = until(client, start(client, chat, experience='reliable', intent='generate_scenario',
                                   profile_override={'scope': 'Registered user login'}))
        assert first['status'] == 'completed', first
        second = until(client, start(client, chat, experience='reliable', intent='generate_scenario',
                                    profile_override={'scope': 'Administrator login'}))
        assert second['status'] == 'completed', second
        assert sum(task == 'analyze_requirement' for task, _ in model.calls) == 2


def test_confirmation_is_bound_to_artifact_version(tmp_path):
    model = FlowModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        assert run['interrupt']['type'] == 'strategy_review', run
        run = until(client, client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).json())
        assert run['interrupt']['type'] == 'scenario_review', run
        before = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        items = copy.deepcopy(before['items'])
        items[0]['title'] = 'Revised scene requiring fresh confirmation'
        changed = client.put('/api/artifacts/' + before['id'],
                             json={'expected_revision': before['revision'], 'items': items})
        assert changed.status_code == 200, changed.text
        stale = client.post('/api/runs/' + run['id'] + '/resume', json={
            'approved': True, 'expected_revision': before['revision'],
            'interrupt_id': run['interrupt_id'], 'expected_control_version': run.get('control_version', 0)})
        assert stale.status_code == 409, stale.text
        waiting = client.get('/api/runs/' + run['id']).json()
        assert waiting['status'] == 'waiting'
        assert waiting['interrupt']['artifact_revision'] == changed.json()['revision']
        continued = client.post('/api/runs/' + run['id'] + '/resume', json={
            'approved': True, 'expected_revision': changed.json()['revision'],
            'interrupt_id': waiting['interrupt_id'], 'expected_control_version': waiting['control_version']})
        assert continued.status_code == 200, continued.text
        done = until(client, continued.json())
        assert done['status'] == 'waiting' and done['interrupt']['type'] == 'case_draft_review', done
        calls = [context for task, context in model.calls if task == 'generate_cases']
        assert calls[0]['scenarios'][0]['title'] == items[0]['title']


def test_waiting_ai_edit_preserves_new_confirmation_binding(tmp_path):
    class EditingModel(FlowModel):
        async def generate(self, task, context):
            if task == 'modify':
                return {'operations': [{'op': 'update', 'id': context['artifact']['items'][0]['id'],
                    'item': {'title': 'AI修改后的最新场景'}}]}
            return await super().generate(task, context)
    with TestClient(create_app(tmp_path, EditingModel())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        run = until(client, client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).json())
        before = copy.deepcopy(run)
        response = client.post('/api/runs/' + run['id'] + '/edit', json={'content': '修改场景标题'})
        assert response.status_code == 200, response.text
        waiting = client.get('/api/runs/' + run['id']).json()
        latest = client.get('/api/artifacts/' + waiting['interrupt']['artifact_id']).json()
        assert waiting['status'] == 'waiting'
        assert waiting['interrupt']['artifact_revision'] == latest['revision']
        assert waiting['control_version'] > before.get('control_version', 0)
        assert latest['items'][0]['title'] == 'AI修改后的最新场景'
