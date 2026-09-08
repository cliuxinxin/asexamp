"""Exercise the documented content-block gateway over real local HTTP."""
import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.model import LangChainGateway, Settings, validate_headers
from tcg.schemas import DomainError
from test_backend_api import setup_chat, start, until
from test_sse_progress import frames


@contextmanager
def strict_chat_server(status=200, redirect=None, finish_reason='stop', malformed=False, minimal=True):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append({'path': self.path, 'body': body, 'headers': dict(self.headers)})
            code = status
            valid_messages = all(isinstance(m.get('content'), list) and m['content'] and
                all(b.get('type') == 'text' and isinstance(b.get('text'), str) for b in m['content']) for m in body.get('messages', []))
            if self.path != '/api/v1/chat/completions':
                code = 404
            elif minimal and (set(body) != {'model', 'messages'} or not valid_messages):
                code = 422
            elif self.headers.get('X-API-Key') != 'LOCAL-TEST-SECRET' or self.headers.get('Authorization'):
                code = 401
            if code != 200:
                payload = {'error': {'message': 'LOCAL-TEST-SECRET must not be echoed'}}
            else:
                content = body['messages'][-1]['content']
                context = json.loads(content[0]['text'] if isinstance(content, list) else content)
                output = {'ok': True}
                if context.get('evidence'):
                    output = {'answer': 'Known credential rules apply.', 'refs': [context['evidence'][0]['id']]}
                payload = {'id': 'chat-local', 'object': 'chat.completion', 'created': 1, 'model': body['model'],
                    'choices': [] if malformed else [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(output)}, 'finish_reason': finish_reason}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
            data = json.dumps(payload).encode()
            self.send_response(code)
            if redirect:
                self.send_header('Location', redirect)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/api/v1', captured
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def settings_body(endpoint, **extra):
    return {'provider': 'openai', 'base_url': endpoint, 'model': 'gpt-5',
            'auth_mode': 'headers', 'headers': {'X-API-Key': 'LOCAL-TEST-SECRET'},
            'timeout_seconds': 3600, 'request_mode': 'minimal', **extra}


@pytest.mark.parametrize('suffix', ['', '/chat/completions', '/chat/completions/'])
@pytest.mark.parametrize('with_callback', [False, True])
def test_minimal_wire_matches_documented_body_headers_and_endpoint(tmp_path, suffix, with_callback):
    with strict_chat_server() as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(settings_body(endpoint + suffix, api_key='UNUSED-BEARER'))
        gateway = LangChainGateway(settings)
        snapshots, pieces = [], []
        gateway.request_recorder = snapshots.append
        async def on_text(text):
            pieces.append(text)
        result = asyncio.run(gateway.generate_stream('connection_test', {}, on_text) if with_callback else gateway.generate('connection_test', {}))
        assert result == {'ok': True}
        assert len(captured) == 1
        request = captured[0]
        assert request['path'] == '/api/v1/chat/completions'
        assert set(request['body']) == {'model', 'messages'}
        assert request['body']['messages'][1]['content'] == [{'type': 'text', 'text': '{}'}]
        headers = {k.lower(): v for k, v in request['headers'].items()}
        assert headers['x-api-key'] == 'LOCAL-TEST-SECRET'
        assert headers['content-type'] == 'application/json'
        assert 'authorization' not in headers
        assert snapshots[0]['http_request']['body'] == request['body']
        assert snapshots[0]['messages'][1]['content'] == '{}'
        assert [m['role'] for m in request['body']['messages']] == ['system', 'user']
        assert snapshots[0]['request_mode'] == 'minimal'
        assert snapshots[0]['endpoint'] == endpoint + '/chat/completions'
        assert snapshots[0]['parameters'] == {}
        assert 'LOCAL-TEST-SECRET' not in json.dumps(snapshots)
        assert pieces == ([json.dumps({'ok': True})] if with_callback else [])


def test_minimal_env_and_older_settings_form_preserve_compatibility(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('TCG_MODEL_PROVIDER=openai\nTCG_MODEL_NAME=gpt-5\nTCG_MODEL_REQUEST_MODE=minimal\n')
    assert Settings(tmp_path).public()['request_mode'] == 'minimal'
    monkeypatch.setenv('TCG_MODEL_REQUEST_MODE', 'standard')
    assert Settings(tmp_path).public()['request_mode'] == 'standard'
    monkeypatch.delenv('TCG_MODEL_REQUEST_MODE')
    (tmp_path / '.env').unlink()
    with TestClient(create_app(tmp_path)) as client:
        body = settings_body('http://127.0.0.1:1234/api/v1')
        client.put('/api/settings', json=body).raise_for_status()
        body.pop('request_mode')
        client.put('/api/settings', json=body).raise_for_status()
        assert client.get('/api/settings').json()['request_mode'] == 'minimal'
    assert Settings(tmp_path).public()['request_mode'] == 'minimal'


def test_json_content_type_can_be_copied_from_gateway_documentation():
    assert validate_headers({'Content-Type': 'application/json', 'X-API-Key': 'local'}, 'headers') == {'Content-Type': 'application/json', 'X-API-Key': 'local'}
    with pytest.raises(DomainError):
        validate_headers({'Content-Type': 'text/plain'}, 'headers')


@pytest.mark.parametrize('status,category', [(401, 'authentication'), (403, 'authentication'), (404, 'configuration'), (405, 'configuration'), (422, 'configuration')])
def test_minimal_errors_identify_http_status_without_secret_echo(tmp_path, status, category):
    with strict_chat_server(status=status) as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(settings_body(endpoint))
        with pytest.raises(DomainError) as error:
            asyncio.run(LangChainGateway(settings).test())
        assert str(status) in str(error.value)
        assert error.value.category == category
        assert error.value.retryable is False
        assert 'LOCAL-TEST-SECRET' not in str(error.value)
        assert len(captured) == 1


def test_minimal_redirect_does_not_forward_headers(tmp_path):
    with strict_chat_server() as (target, received):
        with strict_chat_server(status=307, redirect=target + '/chat/completions') as (endpoint, captured):
            settings = Settings(tmp_path)
            settings.save(settings_body(endpoint))
            with pytest.raises(DomainError):
                asyncio.run(LangChainGateway(settings).test())
            assert len(captured) == 1
            assert received == []


@pytest.mark.parametrize('kwargs', [{'finish_reason': 'length'}, {'malformed': True}])
def test_minimal_rejects_truncated_or_missing_assistant_response(tmp_path, kwargs):
    with strict_chat_server(**kwargs) as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(settings_body(endpoint))
        with pytest.raises(DomainError):
            asyncio.run(LangChainGateway(settings).test())


def test_minimal_gateway_runs_real_graph_and_delivers_sse_result(tmp_path):
    with strict_chat_server() as (endpoint, captured):
        with TestClient(create_app(tmp_path)) as client:
            client.put('/api/settings', json=settings_body(endpoint)).raise_for_status()
            _, chat, _ = setup_chat(client)
            run = until(client, start(client, chat, intent='query'))
            assert run['status'] == 'completed', run
            events = frames(client.get('/api/runs/' + run['id'] + '/events').text)
            assert any(e['event'] == 'model_delta' and 'Known credential rules apply.' in e['data']['text'] for e in events)
            assert events[-1]['event'] == 'done'
            assert 'LOCAL-TEST-SECRET' not in client.get('/api/runs/' + run['id'] + '/diagnostics').text


def test_standard_sdk_accepts_full_chat_url_without_changing_request_mode(tmp_path):
    with strict_chat_server(minimal=False) as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(settings_body(endpoint + '/chat/completions', request_mode='standard'))
        asyncio.run(LangChainGateway(settings).test())
        assert captured[0]['path'] == '/api/v1/chat/completions'
        assert captured[0]['body']['response_format'] == {'type': 'json_object'}
        assert isinstance(captured[0]['body']['messages'][1]['content'], str)


@pytest.mark.parametrize('configuration', [
    'TCG_MODEL_REQUEST_MODE=PRIVATE-INVALID-MODE\n',
    'TCG_MODEL_PROVIDER=ollama\nTCG_MODEL_REQUEST_MODE=minimal\n',
])
def test_invalid_protocol_configuration_fails_locally_without_echoing_values(tmp_path, configuration):
    (tmp_path / '.env').write_text(configuration)
    with pytest.raises(DomainError) as error:
        Settings(tmp_path)
    assert 'PRIVATE-INVALID-MODE' not in str(error.value)


def test_minimal_http_status_reaches_diagnostics_without_error_body(tmp_path):
    with strict_chat_server(status=422) as (endpoint, captured):
        with TestClient(create_app(tmp_path)) as client:
            client.put('/api/settings', json=settings_body(endpoint)).raise_for_status()
            _, chat, _ = setup_chat(client)
            run = until(client, start(client, chat, intent='query'))
            assert run['status'] == 'failed'
            events = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
            assert any(e.get('provider_http_status') == 422 for e in events)
            assert 'LOCAL-TEST-SECRET' not in json.dumps(events)
            assert len(captured) == 1
