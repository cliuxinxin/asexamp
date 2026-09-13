import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until


def empty_chat(client):
    project = client.get('/api/projects').json()[0]
    return client.post('/api/projects/' + project['id'] + '/chats', json={}).json()


def test_instructions_never_become_evidence_without_explicit_requirement_flag(tmp_path):
    content = 'Please generate a comprehensive test case suite with positive, negative, boundary, integration, and end-to-end cases. The system should review the cases once, then export to Excel.'
    with TestClient(create_app(tmp_path, Model())) as client:
        chat = empty_chat(client)
        run = until(client, start(client, chat, content=content))
        assert run['status'] == 'completed'
        assert client.get('/api/chats/' + chat['id']).json()['sources'] == []
        result = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert result['type'] == 'answer'
        assert '需求' in result['items'][0]['description']


def test_explicit_requirement_flag_accepts_short_text_and_selected_scope(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        chat = empty_chat(client)
        run = until(client, start(client, chat, content='最多尝试五次，随后锁定账户。', as_requirement=True, source_ids=[]))
        assert run['status'] == 'completed', run
        sources = client.get('/api/chats/' + chat['id']).json()['sources']
        assert len(sources) == 1 and sources[0]['role'] == 'primary'
        result = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert result['type'] == 'cases'
        assert result['items'][0]['refs'][0].startswith(sources[0]['id'] + '#')


def test_fresh_auto_generation_honors_excluded_deactivated_sources(tmp_path):
    model = Model()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client)
        original = until(client, start(client, chat))
        assert original['status'] == 'completed'
        client.delete('/api/sources/' + source['id'])
        analysis_calls = sum(task == 'analyze_requirement' for task, _ in model.calls)
        run = until(client, start(client, chat, intent='auto', source_ids=[]))
        assert run['status'] == 'completed', run
        assert client.get('/api/chats/' + chat['id']).json()['sources'] == []
        result = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert result['type'] == 'answer'
        assert sum(task == 'analyze_requirement' for task, _ in model.calls) == analysis_calls


def test_historical_artifact_query_and_edit_keep_provenance_after_deactivation(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, source = setup_chat(client)
        original = until(client, start(client, chat))
        aid = original['artifact_ids'][0]
        client.delete('/api/sources/' + source['id'])
        query = until(client, start(client, chat, intent='query', content='Explain this case', artifact_id=aid, source_ids=[]))
        assert query['status'] == 'completed', query
        answer = client.get('/api/artifacts/' + query['artifact_ids'][0]).json()
        assert answer['items'][0]['refs'][0].startswith(source['id'] + '#')
        modified = until(client, start(client, chat, intent='modify', content='Improve the title', artifact_id=aid, source_ids=[]))
        assert modified['status'] == 'completed', modified
        assert client.get('/api/artifacts/' + aid).json()['items'][0]['title'] == 'Modified through chat'
        reviewed = until(client, start(client, chat, intent='review_case', content='Review this case', artifact_id=aid, source_ids=[]))
        assert reviewed['status'] == 'completed', reviewed
        assert client.get('/api/sources/' + source['id']).json()['chunks']


def test_crash_during_paused_edit_releases_orphaned_token_on_restart(tmp_path):
    script = '''
import json, os, sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until
model = Model(delay_task='modify')
with TestClient(create_app(Path(sys.argv[1]), model)) as client:
    _, chat, _ = setup_chat(client)
    run = until(client, start(client, chat, mode='hitp'))
    pool = ThreadPoolExecutor(1)
    pool.submit(client.post, '/api/runs/' + run['id'] + '/edit', json={'content': 'Improve title'})
    deadline = time.monotonic() + 5
    while not any(t == 'modify' for t, _ in model.calls):
        assert time.monotonic() < deadline
        time.sleep(.01)
    Path(sys.argv[1], 'crashed-edit.json').write_text(json.dumps(run))
    os._exit(17)
'''
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'backend'), str(root / 'tests'))))
    process = subprocess.run([sys.executable, '-c', script, str(tmp_path)], env=env, capture_output=True, text=True, timeout=15)
    assert process.returncode == 17, process.stderr
    run = json.loads((tmp_path / 'crashed-edit.json').read_text())
    aid = run['interrupt']['artifact_id']
    with TestClient(create_app(tmp_path, Model())) as client:
        restarted = client.get('/api/runs/' + run['id']).json()
        assert restarted['status'] == 'waiting'
        assert restarted['interrupt']['artifact_id'] == aid
        assert client.get('/api/artifacts/' + aid).json()['revision'] == 1
        edited = client.post('/api/runs/' + run['id'] + '/edit', json={'content': 'Improve title'})
        assert edited.status_code == 200, edited.text
        assert edited.json()['status'] == 'waiting'
        assert client.get('/api/artifacts/' + aid).json()['revision'] == 2
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).status_code == 200
        assert until(client, run)['status'] == 'completed'


@pytest.mark.parametrize('failure', ['timeout', 'list_output', 'bad_operation', 'provider_error'])
def test_paused_edit_errors_are_actionable_json_and_leave_editable_run(tmp_path, failure):
    class BadEdit(Model):
        broken = True
        async def generate(self, task, context):
            if task == 'modify' and self.broken:
                if failure == 'timeout':
                    await asyncio.sleep(30)
                if failure == 'list_output':
                    return []
                if failure == 'bad_operation':
                    return {'operations': ['invalid-operation']}
                raise RuntimeError('Deliberate provider failure')
            return await super().generate(task, context)
    model = BadEdit()
    with TestClient(create_app(tmp_path, model), raise_server_exceptions=False) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, mode='hitp'))
        # Shorten only this injected-test timeout; public settings retain the 5s minimum.
        client.app.state.settings.value['timeout_seconds'] = .05
        result = client.post('/api/runs/' + run['id'] + '/edit', json={'content': 'Improve title'})
        assert result.status_code in (400, 502, 504), result.text
        detail = result.json()['detail']
        assert isinstance(detail, str) and detail
        if failure == 'timeout':
            assert '超时' in detail
        assert client.get('/api/runs/' + run['id']).json()['status'] == 'waiting'
        assert client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()['revision'] == 1
        model.broken = False
        result = client.post('/api/runs/' + run['id'] + '/edit', json={'content': 'Improve title'})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'waiting'
