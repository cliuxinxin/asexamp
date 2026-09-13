"""Regression coverage for editing an HITP artifact before confirmation."""
import asyncio
import concurrent.futures
import threading

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import setup_chat, start, until
from test_workflow_v25 import FlowModel


class BlockingEditModel(FlowModel):
    def __init__(self):
        super().__init__()
        self.edit_started = threading.Event()
        self.release_edit = threading.Event()

    async def generate(self, task, context):
        if task == 'modify':
            self.edit_started.set()
            while not self.release_edit.is_set():
                await asyncio.sleep(.01)
        return await super().generate(task, context)


def test_confirmation_waits_for_ai_edit_then_uses_new_scenario(tmp_path):
    """A concurrent confirmation must not invalidate the in-flight edit."""
    model = BlockingEditModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        assert run['interrupt']['type'] == 'strategy_review'
        client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True})
        run = until(client, run)
        assert run['interrupt']['type'] == 'scenario_review'

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            edit = pool.submit(client.post, '/api/runs/' + run['id'] + '/edit', json={'content': '把场景标题改得更明确'})
            assert model.edit_started.wait(5)
            editing = client.get('/api/runs/' + run['id']).json()
            assert editing['status'] == 'waiting'
            assert editing['edit_in_progress'] is True
            try:
                premature = client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True})
                assert premature.status_code == 409
                assert '正在修改' in premature.json()['detail']
                assert client.get('/api/runs/' + run['id']).json()['status'] == 'waiting'
                blocked_events = [row['event'] for row in client.app.state.engine.diagnostics.rows(run['id'])]
                assert 'resume.blocked_by_edit' in blocked_events
            finally:
                model.release_edit.set()
            edited = edit.result(timeout=5)
        assert edited.status_code == 200, edited.text
        assert edited.json()['status'] == 'waiting'
        assert edited.json()['edit_in_progress'] is False

        artifact = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        assert artifact['revision'] == 2
        assert artifact['items'][0]['title'] == 'Modified through chat'
        events = [row['event'] for row in client.app.state.engine.diagnostics.rows(run['id'])]
        assert 'edit.started' in events
        assert 'edit.saved' in events

        accepted = client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True})
        assert accepted.status_code == 200, accepted.text
        completed = until(client, run)
        assert completed['status'] == 'completed', completed
        case_context = next(context for task, context in model.calls if task == 'generate_cases')
        assert case_context['scenarios'][0]['title'] == 'Modified through chat'
