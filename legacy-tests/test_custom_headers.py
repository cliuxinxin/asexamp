import json
import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from tcg.main import create_app
from tcg.model import Settings, LangChainGateway
from tcg.schemas import DomainError


def config(**overrides):
    return {'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture', 'timeout_seconds': 3600, **overrides}


def test_headers_are_encrypted_retained_masked_and_cleared_on_endpoint_change(tmp_path):
    settings = Settings(tmp_path)
    settings.save(config(headers={'X-Api-Key': 'private-header-secret'}, auth_mode='headers'))
    assert settings.public()['header_names'] == ['X-Api-Key']
    assert 'private-header-secret' not in (tmp_path / 'settings.json').read_text()
    settings = Settings(tmp_path)
    assert settings.headers() == {'X-Api-Key': 'private-header-secret'}
    settings.save(config(auth_mode='headers'))
    assert settings.headers()
    settings.save(config(base_url='http://localhost:23456/v1', auth_mode='headers'))
    assert settings.headers() == {}


@pytest.mark.parametrize('headers', [{'Authorization': 'secret'}, {'Host': 'secret'}, {'Transfer-Encoding': 'secret'}, {'Upgrade': 'secret'}, {'Trailer': 'secret'}, {'X-Token': 'secret\r\nInjected: secret'}, {'bad name': 'secret'}, {'X-Key': 'one', 'x-key': 'two'}])
def test_unsafe_or_conflicting_headers_rejected_without_values(tmp_path, headers):
    with pytest.raises(DomainError) as error:
        Settings(tmp_path).save(config(headers=headers))
    assert 'secret' not in str(error.value)


def test_process_header_object_replaces_file_and_new_endpoint_clears(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('TCG_MODEL_NAME=fixture\nTCG_MODEL_AUTH_MODE=headers\nTCG_MODEL_HEADERS_JSON=\'{"X-Key":"file-secret"}\'\n')
    monkeypatch.setenv('TCG_MODEL_HEADERS_JSON', '{"X-Other":"process-secret"}')
    assert Settings(tmp_path).headers() == {'X-Other': 'process-secret'}
    monkeypatch.delenv('TCG_MODEL_HEADERS_JSON')
    monkeypatch.setenv('TCG_MODEL_BASE_URL', 'http://localhost:32123')
    assert Settings(tmp_path).headers() == {}


@contextmanager
def header_server(provider='openai', status=200, redirect=None):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append({'headers': dict(self.headers), 'body': body})
            if status != 200:
                payload = {'error': {'message': 'PRIVATE-HEADER-SECRET', 'type': 'authentication_error'}}
            elif provider == 'openai':
                payload = {'id': 'local', 'object': 'chat.completion', 'created': 1, 'model': 'fixture', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
            else:
                payload = {'model': 'fixture', 'created_at': '2026-01-01T00:00:00Z', 'message': {'role': 'assistant', 'content': '{"ok":true}'}, 'done': True, 'done_reason': 'stop'}
            data = json.dumps(payload).encode()
            self.send_response(status)
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
        yield f'http://127.0.0.1:{server.server_port}' + ('/v1' if provider == 'openai' else ''), captured
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize('provider,custom', [('openai', {'X-Api-Key': 'PRIVATE-HEADER-SECRET'}), ('openai', {'authorization': 'Custom PRIVATE-HEADER-SECRET'}), ('ollama', {'X-Api-Key': 'PRIVATE-HEADER-SECRET'})])
def test_real_wire_header_only_auth_and_masked_inspection(tmp_path, provider, custom):
    with header_server(provider) as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(config(provider=provider, base_url=endpoint, headers=custom, auth_mode='headers', api_key='UNUSED-BEARER-SECRET'))
        gateway = LangChainGateway(settings)
        snapshots = []
        gateway.request_recorder = snapshots.append
        asyncio.run(gateway.test())
        headers = {k.lower(): v for k, v in captured[0]['headers'].items()}
        assert all(headers[k.lower()] == v for k, v in custom.items())
        assert headers.get('authorization') == custom.get('authorization')
        assert snapshots[0]['headers'] == {k: '••••••' for k in custom}
        assert 'PRIVATE-HEADER-SECRET' not in json.dumps(snapshots)
        assert 'UNUSED-BEARER-SECRET' not in json.dumps(captured)


def test_header_auth_error_is_actionable_redacted_and_nonretryable(tmp_path):
    with header_server(status=401) as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(config(base_url=endpoint, headers={'X-Key': 'PRIVATE-HEADER-SECRET'}, auth_mode='headers'))
        with pytest.raises(DomainError) as error:
            asyncio.run(LangChainGateway(settings).test())
        assert error.value.retryable is False
        assert error.value.category == 'authentication'
        assert 'PRIVATE-HEADER-SECRET' not in str(error.value)
        assert len(captured) == 1


def test_invalid_settings_values_and_saved_headers_never_echo_secrets(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.put('/api/settings', json=config(headers={'X-Key': ['PRIVATE-HEADER-SECRET']}))
        assert response.status_code == 422
        assert 'PRIVATE-HEADER-SECRET' not in response.text
        response = client.put('/api/settings', json=config(headers={'Authorization': 'PRIVATE-HEADER-SECRET'}))
        assert response.status_code == 400
        assert 'headers' in response.text
        assert 'PRIVATE-HEADER-SECRET' not in response.text
        client.put('/api/settings', json=config(headers={'X-Key': 'PRIVATE-HEADER-SECRET'}, auth_mode='headers')).raise_for_status()
        assert 'PRIVATE-HEADER-SECRET' not in client.get('/api/settings').text
    assert 'PRIVATE-HEADER-SECRET' not in (tmp_path / 'logs' / 'tcg.log').read_text()


def test_ollama_bearer_and_custom_header_share_saved_configuration(tmp_path):
    with header_server('ollama') as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(config(provider='ollama', base_url=endpoint, headers={'X-Region': 'local'}, api_key='PRIVATE-BEARER-SECRET'))
        asyncio.run(LangChainGateway(settings).test())
        headers = {k.lower(): v for k, v in captured[0]['headers'].items()}
        assert headers['authorization'] == 'Bearer PRIVATE-BEARER-SECRET'
        assert headers['x-region'] == 'local'


def test_ollama_header_only_never_inherits_ambient_auth(tmp_path, monkeypatch):
    monkeypatch.setenv('OLLAMA_API_KEY', 'UNRELATED-AMBIENT-SECRET')
    with header_server('ollama') as (endpoint, captured):
        settings = Settings(tmp_path)
        settings.save(config(provider='ollama', base_url=endpoint, headers={'X-Key': 'EXPLICIT-HEADER-SECRET'}, auth_mode='headers'))
        asyncio.run(LangChainGateway(settings).test())
        headers = {k.lower(): v for k, v in captured[0]['headers'].items()}
        assert 'authorization' not in headers
        assert headers['x-key'] == 'EXPLICIT-HEADER-SECRET'


@pytest.mark.parametrize('provider,auth_mode', [('openai', 'bearer'), ('openai', 'headers'), ('ollama', 'bearer'), ('ollama', 'headers')])
def test_custom_secrets_never_follow_cross_origin_redirect(tmp_path, provider, auth_mode):
    with header_server(provider) as (target, received):
        with header_server(provider, 307, target) as (endpoint, captured):
            settings = Settings(tmp_path)
            settings.save(config(provider=provider, base_url=endpoint, headers={'X-Key': 'PRIVATE-REDIRECT-SECRET'}, auth_mode=auth_mode))
            with pytest.raises(DomainError):
                asyncio.run(LangChainGateway(settings).test())
            assert received == []
            assert len(captured) == 1
