"""Regression probes for progress visibility and cross-run conversation context."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until


def records(directory):
    path = directory / 'logs' / 'tcg.log'
    assert path.is_file(), 'Application must write a local diagnostic log'
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_completed_run_has_correlated_logs_without_business_text(tmp_path):
    secret_text = 'PRIVATE-REQUIREMENT-DO-NOT-LOG'
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client, secret_text)
        run = until(client, start(client, chat, content='PRIVATE-USER-MESSAGE'))
        assert run['status'] == 'completed'
        response = client.get('/api/runs/' + run['id'] + '/diagnostics')
        assert response.status_code == 200
        report = response.json()
        assert report['runtime']['graph_thread_id'] == run['id']
        assert report['context']['conversation_messages'] == 1
        assert report['context']['history_limit'] == 12
        events = report['events']
        for event in ['run.scheduled', 'checkpoint.loaded', 'node.start', 'model.start', 'model.response', 'model.complete', 'node.complete', 'run.completed']:
            assert any(row['event'] == event for row in events), event
        assert all(row['run_id'] == run['id'] and row['chat_id'] == chat['id'] for row in events)
        wire = json.dumps(report) + json.dumps(records(tmp_path))
        assert secret_text not in wire and 'PRIVATE-USER-MESSAGE' not in wire
        exported = client.get('/api/runs/' + run['id'] + '/diagnostics?download=true')
        assert 'attachment' in exported.headers['content-disposition']


def test_waiting_model_reports_heartbeat_and_cancel_is_visible(tmp_path):
    with TestClient(create_app(tmp_path, Model(delay_task='analyze_requirement'))) as client:
        client.app.state.engine.heartbeat_seconds = .03
        _, chat, _ = setup_chat(client)
        run = start(client, chat, intent='review_requirement')
        deadline = time.monotonic() + 3
        while True:
            value = client.get('/api/runs/' + run['id']).json()
            if value.get('diagnostic', {}).get('event') == 'model.waiting':
                break
            assert time.monotonic() < deadline, value
            time.sleep(.01)
        assert value['stage'] == 'requirement_analysis'
        detail = value['diagnostic']
        assert detail['batch_index'] == 1 and detail['batch_count'] == 1
        assert detail['elapsed_ms'] >= 0 and detail['timeout_seconds'] == 300
        assert detail['attempt'] == 1 and detail['max_attempts'] == 2
        assert client.post('/api/runs/' + run['id'] + '/cancel').status_code == 200
        events = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        assert any(row['event'] == 'run.cancelled' for row in events)


@pytest.mark.parametrize('failure', ['timeout', 'exception'])
def test_failed_model_records_attempts_and_safe_error_metadata(tmp_path, failure):
    class Failing(Model):
        async def generate(self, task, context):
            if failure == 'timeout':
                raise asyncio.TimeoutError('SECRET-PROVIDER-ERROR')
            raise RuntimeError('Bearer SECRET-API-KEY PRIVATE-REQUIREMENT')
    with TestClient(create_app(tmp_path, Failing())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, intent='review_requirement'))
        assert run['status'] == 'failed'
        response = client.get('/api/runs/' + run['id'] + '/diagnostics')
        assert response.status_code == 200
        rows = response.json()['events']
        assert len([r for r in rows if r['event'] == 'model.start']) == 2
        assert any(r['event'] == 'model.retry' for r in rows)
        errors = [r for r in rows if r['event'] == 'model.error']
        assert len(errors) == 2 and errors[0]['error_types']
        assert 'SECRET-' not in json.dumps(rows) + json.dumps(records(tmp_path))


def test_two_runs_share_chat_history_and_latest_revision_across_restart(tmp_path):
    model = Model()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        first = until(client, start(client, chat, content='First turn requirement generation'))
        artifact_id = first['artifact_ids'][0]
    followup = Model()
    with TestClient(create_app(tmp_path, followup)) as client:
        second = until(client, start(client, chat, intent='modify', artifact_id=artifact_id, content='Second turn modify title'))
        assert second['status'] == 'completed' and second['id'] != first['id']
        context = next(c for task, c in followup.calls if task == 'modify')
        assert context['artifact']['id'] == artifact_id
        assert [m['role'] for m in context['conversation']] == ['user', 'assistant', 'user']
        assert context['conversation'][0]['content'] == 'First turn requirement generation'
        assert context['artifact']['revision'] == 2
        assert client.get('/api/artifacts/' + artifact_id).json()['revision'] == 3
        assert client.app.state.engine.config(first['id'])['configurable']['thread_id'] != client.app.state.engine.config(second['id'])['configurable']['thread_id']
        assert len(client.get('/api/chats/' + chat['id']).json()['messages']) == 4


def test_history_snapshot_is_chronological_and_reports_truncation(tmp_path, monkeypatch):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        store = client.app.state.store
        for index in range(15):
            store.put('message', {'id': f'old-{index}', 'chat_id': chat['id'], 'project_id': chat['project_id'], 'role': 'user', 'content': f'old message {index}', 'metadata': {}, 'created_at': f'2026-01-01T00:00:{index:02d}+00:00'})
        original = store.list
        def unordered(kind, **kwargs):
            values = original(kind, **kwargs)
            return list(reversed(values)) if kind == 'message' else values
        monkeypatch.setattr(store, 'list', unordered)
        run = until(client, start(client, chat, intent='review_requirement', content='Current message'))
        snapshot = store.run(run['id'])['_conversation']
        assert snapshot[-1]['content'] == 'Current message'
        assert snapshot[0]['content'] == 'old message 4'
        report = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        assert report['context']['conversation_messages'] == 12
        assert report['context']['history_omitted'] == 4


def test_diagnostic_write_failure_cannot_leave_run_unscheduled_or_skip_cancel(tmp_path):
    with TestClient(create_app(tmp_path, Model(delay_task='analyze_requirement')), raise_server_exceptions=False) as client:
        _, chat, _ = setup_chat(client)
        store = client.app.state.store
        store.db.execute("CREATE TRIGGER reject_diagnostic_write BEFORE INSERT ON diagnostics BEGIN SELECT RAISE(ABORT, 'diagnostic-only failure'); END")
        response = client.post('/api/chats/' + chat['id'] + '/messages', json={'content':'Analyze','intent':'review_requirement'})
        assert response.status_code == 200, response.text
        run = response.json()['run']
        assert run['id'] in client.app.state.engine.tasks
        assert client.post('/api/runs/' + run['id'] + '/cancel').status_code == 200
        assert client.get('/api/runs/' + run['id']).json()['status'] == 'cancelled'
        report = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        assert report['runtime']['diagnostic_storage_degraded'] is True


def test_http_request_id_correlates_with_background_task_events(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        response = client.post('/api/chats/' + chat['id'] + '/messages', json={'content':'Analyze','intent':'review_requirement'})
        request_id = response.headers['x-request-id']
        run = until(client, response.json()['run'])
        events = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        assert events and all(event.get('request_id') == request_id for event in events)


def test_unavailable_log_directory_does_not_prevent_startup_or_generation(tmp_path):
    (tmp_path / 'logs').write_text('A pre-existing file prevents log directory creation')
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, intent='review_requirement'))
        assert run['status'] == 'completed'
        report = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        assert report['runtime']['diagnostic_file_degraded'] is True
        deadline = time.monotonic() + 4
        while report['runtime']['task_active']:
            assert time.monotonic() < deadline
            report = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        assert any(row['event'] == 'run.completed' for row in report['events'])


def test_error_metadata_distinguishes_local_timeout_from_provider_http_status():
    from tcg.diagnostics import error_details
    from tcg.schemas import DomainError
    try:
        raise asyncio.TimeoutError()
    except asyncio.TimeoutError:
        local = error_details(DomainError('Local timeout', 504))
    assert local['application_status'] == 504
    assert local['provider_http_status'] is None
    assert local['http_status_source'] == 'application'
    upstream = RuntimeError('Secret provider response')
    upstream.status_code = 503
    wrapped = DomainError('Model failure', 400)
    wrapped.__cause__ = upstream
    provider = error_details(wrapped)
    assert provider['application_status'] == 400
    assert provider['provider_http_status'] == 503
    assert provider['http_status_source'] == 'provider'
    assert 'Secret provider response' not in json.dumps(provider)
