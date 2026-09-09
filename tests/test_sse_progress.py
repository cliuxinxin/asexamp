import asyncio
import json
import threading
import time
import socket
import sqlite3
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import httpx
import uvicorn
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.model import LangChainGateway
from tcg.storage import Store
from test_backend_api import Model, setup_chat, start, until
from test_backend_model import saved


def frames(text):
    result = []
    for block in text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line and not line.startswith(':'))
        if 'data' in fields:
            fields['data'] = json.loads(fields['data'])
            result.append(fields)
    return result


def test_sse_contains_all_stage_events_and_model_output_without_polling(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        response = client.get('/api/runs/' + run['id'] + '/events')
        events = frames(response.text)
        assert response.headers['content-type'].startswith('text/event-stream')
        progress = [e['data'] for e in events if e['event'] == 'progress']
        assert {e['node'] for e in progress if e['event'] == 'node.complete'} >= {'analysis','scenarios','cases','review','finish'}
        starts = [e for e in progress if e['event'] == 'model.start']
        assert len({e['call_id'] for e in starts}) == 4
        deltas = [e['data'] for e in events if e['event'] == 'model_delta']
        assert 'Login correctly' in ''.join(e['text'] for e in deltas)
        assert events[-1]['event'] == 'done'
        diagnostics = client.get('/api/runs/' + run['id'] + '/diagnostics').text
        assert 'Login correctly' not in diagnostics
        assert 'Login correctly' not in (tmp_path / 'logs' / 'tcg.log').read_text()


def test_terminal_sse_drains_more_than_one_page_and_resumes_by_event_id(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, intent='review_requirement'))
        store = client.app.state.store
        with store.transaction():
            for _ in range(450):
                store.event(store.run(run['id']))
        all_events = frames(client.get('/api/runs/' + run['id'] + '/events').text)
        ids = [int(e['id']) for e in all_events if 'id' in e]
        assert len(ids) >= 450
        assert ids == sorted(set(ids))
        cursor = ids[215]
        replay = frames(client.get('/api/runs/' + run['id'] + '/events', headers={'Last-Event-ID': str(cursor)}).text)
        assert [int(e['id']) for e in replay if 'id' in e] == [i for i in ids if i > cursor]
        query_replay = frames(client.get('/api/runs/' + run['id'] + f'/events?after={cursor}').text)
        assert [e.get('id') for e in query_replay] == [e.get('id') for e in replay]


@contextmanager
def streaming_server(provider):
    release = threading.Event()
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if provider == 'openai' else 'application/x-ndjson')
            self.end_headers()
            def send(content, done=False):
                if provider == 'openai':
                    payload = {'id':'stream-test','object':'chat.completion.chunk','created':1,'model':'test-local','choices':[{'index':0,'delta':{'content':content,**({'role':'assistant'} if not done else {}),'reasoning_content':'PRIVATE-THOUGHT'},'finish_reason':'stop' if done else None}]}
                    data = 'data: ' + json.dumps(payload) + '\n\n'
                else:
                    payload = {'model':'test-local','message':{'role':'assistant','content':content,'thinking':'PRIVATE-THOUGHT'},'done':done,'done_reason':'stop' if done else None}
                    data = json.dumps(payload) + '\n'
                self.wfile.write(data.encode())
                self.wfile.flush()
            send('{"ok":')
            release.wait(5)
            send('true}', True)
            if provider == 'openai':
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}' + ('/v1' if provider == 'openai' else ''), release, captured
    finally:
        release.set()
        server.shutdown()
        thread.join()
        server.server_close()


def test_real_ollama_stream_delivers_text_before_response_completes(tmp_path):
    with streaming_server('ollama') as (endpoint, release, captured):
        gateway = LangChainGateway(saved(tmp_path, endpoint, 'ollama'))
        async def exercise():
            first = asyncio.Event()
            pieces = []
            async def on_text(text):
                pieces.append(text)
                first.set()
            task = asyncio.create_task(gateway.generate_stream('connection_test', {}, on_text))
            try:
                await asyncio.wait_for(first.wait(), 4)
                assert not task.done()
                assert ''.join(pieces) == '{"ok":'
                release.set()
                assert await task == {'ok': True}
                assert ''.join(pieces) == '{"ok":true}'
                assert captured[0]['stream'] is True
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        asyncio.run(exercise())


@contextmanager
def app_server(app):
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started:
            assert time.monotonic() < deadline, 'Test server did not start'
            time.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(8)
        assert not thread.is_alive(), 'Test server did not shut down'


class SlowStreamingModel(Model):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()
    async def generate_stream(self, task, context, on_text):
        result = await super().generate(task, context)
        text = json.dumps(result)
        await on_text(text[:20])
        try:
            while not self.release.is_set():
                await asyncio.sleep(.01)
        except asyncio.CancelledError:
            # Test late transport output after cancellation.
            pass
        await on_text(text[20:])
        return result


def first_model_delta(client, run_id):
    with client.stream('GET', '/api/runs/' + run_id + '/events') as response:
        block = []
        for line in response.iter_lines():
            if line:
                block.append(line)
            else:
                parsed = frames('\n'.join(block) + '\n\n')
                block = []
                if parsed and parsed[0]['event'] == 'model_delta':
                    return parsed[0]
    raise AssertionError('Stream ended before any model text')


def test_live_http_disconnect_does_not_cancel_run_and_resume_replays_remaining_text(tmp_path):
    model = SlowStreamingModel()
    with app_server(create_app(tmp_path, model)) as address:
        with httpx.Client(base_url=address, trust_env=False, timeout=5) as client:
            _, chat, _ = setup_chat(client)
            run = start(client, chat, intent='review_requirement')
            first = first_model_delta(client, run['id'])
            assert client.get('/api/runs/' + run['id']).json()['status'] == 'running'
            model.release.set()
            assert until(client, run)['status'] == 'completed'
            replay = frames(client.get('/api/runs/' + run['id'] + '/events', headers={'Last-Event-ID':first['id']}).text)
            remaining = ''.join(e['data']['text'] for e in replay if e['event'] == 'model_delta')
            complete = json.loads(first['data']['text'] + remaining)
            assert complete['items'][0]['title'] == 'Authentication'
            assert all(int(e['id']) > int(first['id']) for e in replay if 'id' in e)
            assert replay[-1]['event'] == 'done'


def test_cancel_preserves_partial_output_and_rejects_late_stream_content(tmp_path):
    model = SlowStreamingModel()
    with app_server(create_app(tmp_path, model)) as address:
        with httpx.Client(base_url=address, trust_env=False, timeout=5) as client:
            _, chat, _ = setup_chat(client)
            run = start(client, chat, intent='review_requirement')
            first = first_model_delta(client, run['id'])
            assert client.post('/api/runs/' + run['id'] + '/cancel').status_code == 200
            model.release.set()
            events = frames(client.get('/api/runs/' + run['id'] + '/events').text)
            assert ''.join(e['data']['text'] for e in events if e['event'] == 'model_delta') == first['data']['text']
            assert events[-1]['data']['status'] == 'cancelled'
            assert client.get('/api/chats/' + chat['id']).json()['messages'][-1]['role'] == 'user'


def test_previous_event_schema_is_migrated_without_losing_rows(tmp_path):
    with sqlite3.connect(tmp_path / 'tcg.sqlite3') as db:
        db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)')
        db.execute('INSERT INTO events(run_id,payload,created_at) VALUES(?,?,?)', ('legacy-run','{"status":"failed"}','2026-09-07'))
    store = Store(tmp_path)
    try:
        assert store.events('legacy-run') == [{'id':1,'kind':'update','data':{'status':'failed'}}]
    finally:
        store.close()


@pytest.mark.parametrize('invalid', [False, True])
def test_paused_edit_stream_has_final_validation_state(tmp_path, invalid):
    class EditModel(Model):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'modify' and invalid:
                result['operations'][0]['item']['refs'] = ['not-real-evidence']
            return result
    with TestClient(create_app(tmp_path, EditModel())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, mode='hitp'))
        result = client.post('/api/runs/' + run['id'] + '/edit', json={'content':'Change title'})
        assert result.status_code == (400 if invalid else 200), result.text
        progress = [e['data'] for e in client.app.state.store.events(run['id']) if e['kind'] == 'progress']
        edit_events = [e for e in progress if e.get('node') == 'paused_edit']
        assert edit_events[0]['event'] == 'node.start'
        assert edit_events[-1]['event'] == ('node.error' if invalid else 'node.complete')
        assert any(e['event'] == 'model.complete' for e in edit_events)


def test_cancel_waits_for_paused_edit_final_progress_before_sse_done(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from test_graph_lifecycle import BlockingModel
    model = BlockingModel()
    with app_server(create_app(tmp_path, model)) as base:
        with httpx.Client(base_url=base, timeout=8) as client, ThreadPoolExecutor(2) as pool:
            _, chat, _ = setup_chat(client)
            run = until(client, start(client, chat, mode='hitp'))
            model.block_task, model.ignore_cancel = 'modify', True
            edit = pool.submit(client.post, '/api/runs/' + run['id'] + '/edit', json={'content':'Change title'})
            assert model.started.wait(5)
            client.post('/api/runs/' + run['id'] + '/cancel', json={}).raise_for_status()
            stream = pool.submit(client.get, '/api/runs/' + run['id'] + '/events')
            try:
                time.sleep(.15)
                assert not stream.done(), 'SSE closed before paused edit ended'
            finally:
                model.release.set()
            assert edit.result(5).status_code == 409
            events = frames(stream.result(5).text)
            progress = [e['data'] for e in events if e['event'] == 'progress' and e['data'].get('node') == 'paused_edit']
            assert progress[-1]['event'] == 'node.cancelled'
            assert any(e['event'] == 'model.cancelled' for e in progress)
            assert events[-1]['event'] == 'done'
