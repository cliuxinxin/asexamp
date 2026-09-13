import asyncio
import errno
import json
import socket
import ssl

import httpx
import pytest

from tcg.diagnostics import Diagnostics
from tcg.model import LangChainGateway, Settings
from tcg.native_chat import NativeChatAgent
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.model_diagnostics import response_details


def configured(tmp_path):
    config = Settings(tmp_path)
    config.save({'provider': 'openai', 'base_url': 'https://model/v1', 'model': 'local',
                 'api_key': 'PRIVATE-KEY', 'timeout_seconds': 5,
                 'headers': {'X-Tenant-Token': 'PRIVATE-TENANT'}})
    return config


@pytest.mark.parametrize('status,category', [(401, 'authentication'), (429, 'rate_limit'), (502, 'service_unavailable')])
def test_http_failure_retains_status_endpoint_and_correlation_without_secrets(tmp_path, status, category):
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'code': 'upstream_failure',
            'message': 'PRIVATE-KEY PRIVATE-TENANT BUSINESS-TEXT'}}, headers={'x-request-id': 'upstream-42'})
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(configured(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            agent = NativeChatAgent(store, gateway, object(), object())
            chat = store.create_chat(store.list('project')[0]['id'], 'Failure')
            body = {'content': 'BUSINESS-TEXT', 'client_message_id': 'request-one'}
            result = await agent.submit(chat['id'], body)
            assert await agent.submit(chat['id'], body) == result
            return chat, result
    try:
        chat, result = asyncio.run(invoke())
        assert len(calls) == 1
        assert result['status'] == 'failed'
        part = next(p for p in result['parts'] if p['type'] == 'diagnostic')
        assert part['category'] == category and part['reference_id'].startswith('call_')
        assert part['http_status'] == status and part['hints']
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        failure = next(e for e in events if e['event'] == 'model.transport_error')
        assert failure['call_id'] == part['reference_id']
        assert failure['http_status'] == status and failure['provider_request_id'] == 'upstream-42'
        assert failure['endpoint'] == 'https://model/v1/chat/completions'
        assert failure['category'] == category and failure['elapsed_ms'] >= 0
        assert failure['provider_error_code'] == 'upstream_failure'
        start = next(e for e in events if e['event'] == 'model.transport_start')
        assert start['client_source'] == 'injected' and start['trust_env'] is None
        assert start['auth_header_names'] == ['X-API-Key', 'X-Tenant-Token']
        serialized = json.dumps(events) + json.dumps(result)
        assert all(secret not in serialized for secret in ('PRIVATE-KEY', 'PRIVATE-TENANT', 'BUSINESS-TEXT'))
        messages = sorted(store.list('message', chat_id=chat['id']), key=lambda m: (m['created_at'], m['id']))
        assert [m['role'] for m in messages] == ['user', 'assistant']
        assert messages[0]['metadata']['client_message_id'] == 'request-one'
    finally:
        diagnostics.close()
        store.close()


@pytest.mark.parametrize('cause,outer,category', [
    (socket.gaierror(-2, 'PRIVATE-KEY'), httpx.ConnectError, 'dns'),
    (ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE-KEY'), httpx.ConnectError, 'connection'),
    (ssl.SSLCertVerificationError(1, 'PRIVATE-KEY'), httpx.ConnectError, 'tls'),
    (None, httpx.ReadTimeout, 'timeout'),
    (None, httpx.ProxyError, 'proxy'),
])
def test_transport_error_keeps_typed_cause_and_safe_stack(tmp_path, cause, outer, category):
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    def handler(request):
        raise outer('PRIVATE-TENANT PRIVATE-KEY', request=request) from cause
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(configured(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with pytest.raises(DomainError) as raised:
                await gateway.test()
            return raised.value
    try:
        error = asyncio.run(invoke())
        assert error.category == category
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        failure = next(e for e in events if e['event'] == 'model.transport_error')
        assert outer.__name__ in failure['error_types']
        assert failure['call_id'] == error.call_id and failure['stack']
        if cause:
            assert type(cause).__name__ in failure['error_types']
        serialized = diagnostics.path.read_text() + str(error)
        assert 'PRIVATE-KEY' not in serialized and 'PRIVATE-TENANT' not in serialized
    finally:
        diagnostics.close()
        store.close()


def test_success_status_with_html_reports_protocol_details(tmp_path):
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, text='<html>PRIVATE-KEY proxy login</html>', headers={'content-type': 'text/html'}))) as client:
            gateway = LangChainGateway(configured(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with pytest.raises(DomainError) as raised:
                await gateway.test()
            assert raised.value.category == 'protocol'
    try:
        asyncio.run(invoke())
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        failure = next(e for e in events if e['event'] == 'model.transport_error')
        assert failure['http_status'] == 200 and failure['content_type'] == 'text/html'
        assert 'PRIVATE-KEY' not in diagnostics.path.read_text()
    finally:
        diagnostics.close()
        store.close()


def test_provider_error_fields_cannot_echo_business_text():
    fields = response_details(httpx.Response(500, json={}), {}, {'error': {'code': 'BUSINESS-TEXT'}})
    assert 'BUSINESS-TEXT' not in json.dumps(fields)
    assert fields['provider_error_code_sha256']


@pytest.mark.asyncio
async def test_nested_model_tool_failure_preserves_diagnostic(tmp_path):
    from tcg.tool_registry import build_tools
    store = Store(tmp_path)
    try:
        chat = store.create_chat(store.list('project')[0]['id'], 'Estimate failure')
        store.put('artifact', {'id': 'scenes', 'chat_id': chat['id'], 'project_id': chat['project_id'],
            'title': 'Scenes', 'type': 'scenarios', 'revision': 1, 'items': [], '_visible': True})
        class Business:
            async def estimate(self, *args, **kwargs):
                error = DomainError('模型连接失败')
                error.category, error.call_id = 'connection', 'call_nested'
                raise error
        tools = build_tools(store, Business(), object(), chat, {})
        estimate = next(tool for tool in tools if tool.name == 'estimate_workload_tool')
        result = await estimate.ainvoke({'artifact_id': 'scenes'})
        assert result['status'] == 'failed'
        assert result['parts'][0]['reference_id'] == 'call_nested'
    finally:
        store.close()
