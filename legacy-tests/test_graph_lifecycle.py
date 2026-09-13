import time

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until


def test_hitp_real_interrupt_survives_restart_and_uses_latest_scenarios(tmp_path):
    model = Model(questions=['How many failed attempts lock the account?'])
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, mode='hitp'))
        assert run['status'] == 'waiting'
        assert run['interrupt']['type'] == 'clarification'
        assert client.post('/api/runs/' + run['id'] + '/resume', json={}).status_code == 400
    model.questions = []
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.get('/api/runs/' + run['id']).json()['status'] == 'waiting'
        response = client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Lock after five failed attempts.'})
        assert response.status_code == 200, response.text
        run = until(client, run)
        assert run['interrupt']['type'] == 'scenario_review'
        aid = run['interrupt']['artifact_id']
        artifact = client.get('/api/artifacts/' + aid).json()
        artifact['items'][0]['title'] = 'Human-reviewed authentication'
        updated = client.put('/api/artifacts/' + aid, json={'expected_revision': artifact['revision'], 'items': artifact['items']})
        assert updated.status_code == 200
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        case_context = [context for task, context in model.calls if task == 'generate_cases'][0]
        assert case_context['scenarios'][0]['title'] == 'Human-reviewed authentication'
        assert any(e['role'] == 'clarification' for e in case_context['evidence'])
    import sqlite3
    with sqlite3.connect(tmp_path / 'checkpoints.sqlite3') as connection:
        assert connection.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0] > 0


def test_failed_node_retries_from_checkpoint_with_original_profile(tmp_path):
    model = Model(fail_task='generate_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        project, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        profile = client.get('/api/projects/' + project['id'] + '/profiles').json()[0]
        changed = dict(profile['config'], language='English')
        assert client.put('/api/profiles/' + profile['id'], json={'name': profile['name'], 'config': changed, 'expected_version': profile['version']}).status_code == 200
    before = len([task for task, _ in model.calls if task == 'analyze_requirement'])
    model.fail_task = None
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry', json={}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert len([task for task, _ in model.calls if task == 'analyze_requirement']) == before
        case_context = [c for t, c in model.calls if t == 'generate_cases'][-1]
        assert case_context['profile']['language'] == '中文'


def test_running_work_recovers_after_engine_restart(tmp_path):
    model = Model(delay_task='generate_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat)
        deadline = time.monotonic() + 8
        while not any(task == 'generate_cases' for task, _ in model.calls):
            assert time.monotonic() < deadline
            time.sleep(.02)
    model.delay_task = None
    with TestClient(create_app(tmp_path, model)) as client:
        result = until(client, run)
        assert result['status'] == 'completed', result
        assert len([task for task, _ in model.calls if task == 'analyze_requirement']) == 1


def test_active_run_conflict_and_cancellation_reject_late_results(tmp_path):
    model = Model(delay_task='generate_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat)
        response = client.post('/api/chats/' + chat['id'] + '/messages', json={'content': 'Again', 'intent': 'generate_case'})
        assert response.status_code == 409
        response = client.post('/api/runs/' + run['id'] + '/cancel', json={})
        assert response.status_code == 200
        assert response.json()['status'] == 'cancelled'
        time.sleep(.1)
        assert client.get('/api/runs/' + run['id']).json()['artifact_ids'] == []
        assert len(client.get('/api/chats/' + chat['id']).json()['messages']) == 1


def test_invented_evidence_and_example_business_evidence_are_rejected(tmp_path):
    with TestClient(create_app(tmp_path, Model(bad_refs=True))) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        assert not run['artifact_ids']
    with TestClient(create_app(tmp_path / 'examples', Model())) as client:
        _, chat, source = setup_chat(client)
        client.delete('/api/sources/' + source['id'])
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Sample', 'text': 'A sample title and example expected result.', 'role': 'example'})
        run = until(client, start(client, chat))
        assert run['status'] == 'completed'
        result = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert result['type'] == 'answer'


def test_query_template_and_modification_keep_evidence_and_versions(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        case_run = until(client, start(client, chat))
        aid = case_run['artifact_ids'][0]
        query = until(client, start(client, chat, content='When is login allowed?', intent='query', artifact_id=aid))
        assert query['status'] == 'completed'
        answer = client.get('/api/artifacts/' + query['artifact_ids'][0]).json()
        assert answer['items'][0]['refs']
        modify = until(client, start(client, chat, content='Improve the title', intent='modify', artifact_id=aid))
        assert modify['status'] == 'completed'
        assert client.get('/api/artifacts/' + aid).json()['items'][0]['title'] == 'Modified through chat'
        template = until(client, start(client, chat, content='Learn this format', intent='learn_template', artifact_id=aid))
        assert template['status'] == 'completed'
        messages = client.get('/api/chats/' + chat['id']).json()['messages']
        assert messages[-1]['metadata']['proposal']['case_types'] == ['Business']


def test_invalid_output_can_be_repaired_without_restarting_whole_graph(tmp_path):
    model = Model(bad_refs=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        model.bad_refs = False
        assert client.post('/api/runs/' + run['id'] + '/retry', json={}).status_code == 200
        repaired = until(client, run)
        assert repaired['status'] == 'completed', repaired


def test_review_uploaded_cases_finishes_with_revision_and_report(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Existing cases', 'text': 'TC-1: Login; enter valid credentials; user authenticates.', 'role': 'example'})
        run = until(client, start(client, chat, intent='review_case'))
        assert run['status'] == 'completed', run
        results = [client.get('/api/artifacts/' + aid).json() for aid in run['artifact_ids']]
        assert {result['type'] for result in results} == {'cases', 'review'}


def test_paused_scenario_can_be_modified_without_a_second_active_run(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, mode='hitp'))
        assert run['interrupt']['type'] == 'scenario_review'
        response = client.post('/api/runs/' + run['id'] + '/edit', json={'content': 'Improve scenario title', 'selected_ids': ['SC-1']})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'waiting'
        artifact = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        assert artifact['items'][0]['title'] == 'Modified through chat'
        assert artifact['revision'] == 2
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).status_code == 200
        assert until(client, run)['status'] == 'completed'


def test_repeated_content_with_fresh_ids_does_not_loop_forever(tmp_path):
    class Repeating(Model):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                page = len(context['previous_items'])
                result['items'][0]['id'] = f'TC-{page}'
                result.update(has_more=True, next_cursor=f'next-{page}')
            return result
    with TestClient(create_app(tmp_path, Repeating())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed', run
        assert '重复' in run['error']
        assert not run['artifact_ids']


class BlockingModel(Model):
    def __init__(self):
        super().__init__()
        import threading
        self.block_task = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.ignore_cancel = False

    async def generate(self, task, context):
        if task == self.block_task:
            import asyncio
            self.started.set()
            try:
                while not self.release.is_set():
                    await asyncio.sleep(.01)
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                while not self.release.is_set():
                    await asyncio.sleep(.01)
            self.finished.set()
        return await super().generate(task, context)


def test_provider_ignoring_cancellation_cannot_publish_a_late_result(tmp_path):
    model = BlockingModel()
    model.block_task, model.ignore_cancel = 'generate_cases', True
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat)
        assert model.started.wait(5)
        assert client.post('/api/runs/' + run['id'] + '/cancel', json={}).json()['status'] == 'cancelled'
        model.release.set()
        assert model.finished.wait(5)
        time.sleep(.05)
        result = client.get('/api/runs/' + run['id']).json()
        assert result['status'] == 'cancelled'
        assert result['artifact_ids'] == []
        assert len(client.get('/api/chats/' + chat['id']).json()['messages']) == 1


def test_manual_edit_wins_over_stale_review_result(tmp_path):
    model = BlockingModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        generated = until(client, start(client, chat))
        aid = generated['artifact_ids'][0]
        model.block_task = 'review_cases'
        review = start(client, chat, intent='review_case', artifact_id=aid)
        assert model.started.wait(5)
        artifact = client.get('/api/artifacts/' + aid).json()
        artifact['items'][0]['title'] = 'A newer human edit'
        edited = client.put('/api/artifacts/' + aid, json={'expected_revision': artifact['revision'], 'items': artifact['items']})
        assert edited.status_code == 200
        model.release.set()
        failed = until(client, review)
        assert failed['status'] == 'failed'
        assert client.get('/api/artifacts/' + aid).json()['items'][0]['title'] == 'A newer human edit'


def test_cancelling_paused_edit_rejects_late_revision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    model = BlockingModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, mode='hitp'))
        aid = run['interrupt']['artifact_id']
        model.block_task = 'modify'
        with ThreadPoolExecutor(1) as pool:
            response = pool.submit(client.post, '/api/runs/' + run['id'] + '/edit', json={'content': 'Change title'})
            assert model.started.wait(5)
            assert client.post('/api/runs/' + run['id'] + '/cancel', json={}).status_code == 200
            model.release.set()
            assert response.result(timeout=5).status_code == 409
        artifact = client.get('/api/artifacts/' + aid).json()
        assert artifact['revision'] == 1
        assert artifact['items'][0]['title'] == 'Login'


def test_abrupt_process_exit_recovers_checkpointed_job(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = '''
import json, os, sys, time
from pathlib import Path
from fastapi.testclient import TestClient
from tcg.main import create_app
from test_backend_api import Model, setup_chat, start
model = Model(delay_task='generate_cases')
with TestClient(create_app(Path(sys.argv[1]), model)) as client:
    _, chat, _ = setup_chat(client)
    run = start(client, chat)
    deadline = time.monotonic() + 5
    while not any(t == 'generate_cases' for t, _ in model.calls):
        assert time.monotonic() < deadline
        time.sleep(.01)
    Path(sys.argv[1], 'crashed-run.json').write_text(json.dumps(run))
    os._exit(17)
'''
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'backend'), str(root / 'tests'))))
    process = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=env, capture_output=True, text=True, timeout=15)
    assert process.returncode == 17, process.stderr
    run = json.loads((tmp_path / 'crashed-run.json').read_text())
    model = Model()
    with TestClient(create_app(tmp_path, model)) as client:
        recovered = until(client, run)
        assert recovered['status'] == 'completed', recovered
        assert not any(task == 'analyze_requirement' for task, _ in model.calls)


def test_multiple_case_pages_keep_all_cases_and_review_once(tmp_path):
    class Paged(Model):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                if context['cursor'] is None:
                    result.update(has_more=True, next_cursor='negative-paths')
                else:
                    result['items'][0].update(id='TC-2', title='Reject invalid password', type='Negative', steps=[{'action': 'Enter wrong password', 'expected': 'Authentication fails safely'}])
            return result
    model = Paged()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert [item['id'] for item in artifact['items']] == ['TC-1', 'TC-2']
        assert artifact['items'][0]['title'] == 'Reviewed login case'
        assert artifact['items'][1]['type'] == 'Negative'
        assert len([task for task, _ in model.calls if task == 'review_cases']) == 1
