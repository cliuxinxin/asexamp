import asyncio
import json

import httpx
import pytest

from tcg.context_budget import request_budget
from tcg.model import LangChainGateway, Settings
from tcg.schemas import DomainError, SettingsInput
from test_backend_model import completion_server, saved


def invoke(settings, handler, context=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(settings, http_client=client)
            return await gateway.generate('query', context or {})
    return asyncio.run(run())


def configured(tmp_path):
    settings = Settings(tmp_path)
    settings.save({'provider': 'openai', 'base_url': 'http://model', 'model': 'test',
                   'context_window': 4096, 'output_tokens': 2048})
    return settings


def test_legacy_limits_do_not_reject_large_final_request(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    monkeypatch.setenv('TCG_MODEL_CONTEXT_TOKENS', '4096')
    context = {'text': '现有需求保持完整' * 80000}
    budget = request_budget(tmp_path, 'query', context, settings)
    assert budget['fits'] is True
    assert budget['context_policy'] == 'server'
    assert budget['input_tokens'] > 500000
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"answer":"ok","refs":[]}'}}]})
    assert invoke(settings, handler, context)['answer'] == 'ok'
    assert len(requests) == 1
    assert json.loads(requests[0]['messages'][1]['content'][0]['text']) == context
    assert requests[0]['max_tokens'] == 2048


def test_context_setting_is_retired_without_changing_output_policy(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    result = settings.save({'provider': 'openai', 'base_url': 'http://model', 'model': 'test',
                            'context_window': 4096, 'output_tokens': 32768})
    assert result['context_window'] == 0
    assert result['output_tokens'] == 32768
    assert SettingsInput(provider='openai', base_url='http://model', model='test',
                         context_window=0).context_window == 0
    monkeypatch.setenv('TCG_MODEL_CONTEXT_TOKENS', 'obsolete-value')
    assert Settings(tmp_path).value['context_window'] == 0
    with pytest.raises(DomainError):
        settings.save({'provider': 'openai', 'base_url': 'http://model', 'model': 'test',
                       'output_limit_mode': 'server', 'server_output_tokens': None})


@pytest.mark.parametrize('status,payload,limit', [
    (400, {'error': {'code': 'context_length_exceeded', 'message':
        'Maximum context length is 4096 tokens; requested 9000 tokens. SECRET-KEY'}}, 4096),
    (413, {'error': 'input length exceeds maximum context length of 8,192 tokens'}, 8192),
    (422, {'error': {'message': '输入超过模型上下文窗口，最大为 32768 tokens'}}, 32768),
    (400, {'error': {'code': 'context_length_exceeded'}}, None),
])
def test_provider_context_error_has_safe_separate_category(tmp_path, status, payload, limit):
    from tcg.server_capacity import ContextCapacityError
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=payload)
    with pytest.raises(ContextCapacityError) as caught:
        invoke(configured(tmp_path), handler)
    assert len(requests) == 1
    assert caught.value.category == 'context_capacity'
    assert caught.value.retryable is False
    assert caught.value.context_limit_tokens == limit
    assert 'SECRET-KEY' not in str(caught.value)
    assert 'SECRET-KEY' not in repr(vars(caught.value))


@pytest.mark.parametrize('status,payload', [
    (400, {'error': {'message': 'unknown model SECRET-KEY'}}),
    (413, {'error': 'Request body too large'}),
    (422, {'error': 'max_tokens must be less than the output token limit'}),
    (401, {'error': {'code': 'context_length_exceeded'}}),
])
def test_unrelated_provider_errors_are_not_context_overflow(tmp_path, status, payload):
    from tcg.server_capacity import ContextCapacityError
    with pytest.raises(DomainError) as caught:
        invoke(configured(tmp_path), lambda request: httpx.Response(status, json=payload))
    assert not isinstance(caught.value, ContextCapacityError)
    assert 'SECRET-KEY' not in str(caught.value)


def test_output_truncation_stays_separate_from_input_overflow(tmp_path):
    from tcg.server_capacity import ContextCapacityError
    with pytest.raises(DomainError) as caught:
        invoke(configured(tmp_path), lambda request: httpx.Response(200, json={
            'choices': [{'finish_reason': 'length', 'message': {'content': '{"answer":'}}]}))
    assert not isinstance(caught.value, ContextCapacityError)


def test_ollama_wire_does_not_override_server_context_window(tmp_path):
    with completion_server({'answer': 'ok'}, provider='ollama') as (endpoint, requests):
        settings = saved(tmp_path, endpoint, 'ollama')
        settings.value['context_window'] = 4096
        records = []
        gateway = LangChainGateway(settings)
        gateway.request_recorder = records.append
        assert asyncio.run(gateway.generate('query', {'text': '需求' * 3000}))['answer'] == 'ok'
    assert len(requests) == 1
    assert 'num_ctx' not in requests[0]['options']
    assert requests[0]['options']['num_predict'] == 8192
    assert 'num_ctx' not in records[0]['parameters']


def test_ollama_explicit_exception_is_classified_before_configuration(tmp_path, monkeypatch):
    import langchain_ollama
    from tcg.server_capacity import ContextCapacityError
    requests = []
    original = langchain_ollama.ChatOllama
    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={'error':
            'prompt too long: exceeds context window of 4096 tokens; SECRET-KEY'})
    def construct(**kwargs):
        kwargs['async_client_kwargs']['transport'] = httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(langchain_ollama, 'ChatOllama', construct)
    gateway = LangChainGateway(saved(tmp_path, 'http://model', 'ollama'))
    with pytest.raises(ContextCapacityError) as caught:
        asyncio.run(gateway.generate('query', {}))
    assert len(requests) == 1
    assert caught.value.context_limit_tokens == 4096
    assert 'SECRET-KEY' not in str(caught.value)
