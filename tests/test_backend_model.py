import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.model import LangChainGateway, Settings
from tcg.schemas import DomainError


@contextmanager
def completion_server(payload, finish_reason='stop', provider='openai'):
    captured = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append(request)
            if provider == 'openai':
                response = {'id': 'chatcmpl-test', 'object': 'chat.completion', 'created': 1, 'model': 'test-local', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(payload)}, 'finish_reason': finish_reason}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
            else:
                response = {'model': 'test-local', 'created_at': '2026-01-01T00:00:00Z', 'message': {'role': 'assistant', 'content': json.dumps(payload)}, 'done': True, 'done_reason': finish_reason, 'total_duration': 1, 'eval_count': 1, 'prompt_eval_count': 1}
            data = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if provider == 'openai' else 'application/x-ndjson')
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


def saved(tmp_path, endpoint, provider='openai'):
    settings = Settings(tmp_path)
    settings.save({'provider': provider, 'base_url': endpoint, 'model': 'test-local', 'timeout_seconds': 5})
    return settings


def test_real_langchain_openai_gateway_requests_json_and_rejects_truncation(tmp_path):
    with completion_server({'ok': True}) as (endpoint, captured):
        gateway = LangChainGateway(saved(tmp_path, endpoint))
        asyncio.run(gateway.test())
        assert set(captured[0]) == {'model', 'messages'}
        assert all(isinstance(message['content'], list) for message in captured[0]['messages'])
    with completion_server({'ok': True}, finish_reason='length') as (endpoint, _):
        gateway = LangChainGateway(saved(tmp_path, endpoint))
        with pytest.raises(DomainError):
            asyncio.run(gateway.test())


def test_real_langchain_ollama_gateway_and_false_connection_test(tmp_path):
    with completion_server({'ok': True}, provider='ollama') as (endpoint, captured):
        gateway = LangChainGateway(saved(tmp_path, endpoint, provider='ollama'))
        asyncio.run(gateway.test())
        assert captured[0]['format'] == 'json'
    with completion_server({'ok': False}) as (endpoint, _):
        gateway = LangChainGateway(saved(tmp_path, endpoint))
        with pytest.raises(DomainError):
            asyncio.run(gateway.test())


def test_secret_is_encrypted_and_never_reused_for_changed_endpoint(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        body = {'provider': 'openai', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'local', 'api_key': 'test-sensitive-secret'}
        result = client.put('/api/settings', json=body)
        assert result.status_code == 200
        assert result.json()['has_api_key'] is True
        assert 'test-sensitive-secret' not in (tmp_path / 'settings.json').read_text()
        assert 'test-sensitive-secret' not in client.get('/api/settings').text
        body.pop('api_key')
        assert client.put('/api/settings', json=body).json()['has_api_key'] is True
        body['base_url'] = 'http://127.0.0.1:5678/v1'
        assert client.put('/api/settings', json=body).json()['has_api_key'] is False
        assert client.put('/api/settings', json={**body, 'base_url': 'file:///tmp/model'}).status_code == 400
