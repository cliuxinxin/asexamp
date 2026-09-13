import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from tcg.model import LangChainGateway, Settings
from tcg.schemas import DomainError
from tcg.server_capacity import ContextCapacityError


SCHEMA = {'type': 'object', 'properties': {'title': {'type': 'string'}},
          'required': ['title'], 'additionalProperties': False}


def settings(tmp_path, provider='openai', **extra):
    value = Settings(tmp_path)
    value.save({'provider': provider, 'base_url': 'http://model/api/v1', 'model': 'local',
                'timeout_seconds': 5, 'api_key': 'PRIVATE-KEY', **extra})
    return value


def completion(arguments, name='submit_understand'):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
            'content': None, 'tool_calls': [{'id': 'call-1', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 6, 'total_tokens': 18}}


def run_native(tmp_path, handler, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(settings(tmp_path), http_client=client)
            result = await gateway.generate_native('understand', {'text': '需求'}, SCHEMA, 'Understand the input.')
            return result
    return asyncio.run(run())


def test_pipeline_uses_native_tool_schema_without_json_response_format(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion({'title': '理解'}))
    result = run_native(tmp_path, handler)
    assert result == {'title': '理解'}
    assert result.call_id.startswith('call_')
    assert json.loads(json.dumps(result)) == {'title': '理解'}
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == '/api/v1/chat/completions'
    assert requests[0].headers['X-API-Key'] == 'PRIVATE-KEY'
    assert 'authorization' not in requests[0].headers
    assert payload['tools'][0]['function']['parameters'] == SCHEMA
    assert payload['tool_choice']['function']['name'] == 'submit_understand'
    assert 'response_format' not in payload and 'format' not in payload
    assert payload['max_tokens'] == 8192
    assert 'Return one JSON' not in payload['messages'][0]['content']


def test_bound_chat_model_preserves_conversation_tool_results(tmp_path):
    @tool
    def read_artifact_tool(artifact_id: str) -> str:
        """Read the artifact identified by artifact_id."""
        return artifact_id
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=completion({'artifact_id': 'art-1'}, 'read_artifact_tool'))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': '这条用例检查登录。'}}]})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(settings(tmp_path), http_client=client)
            model = gateway.chat_model().bind_tools([read_artifact_tool])
            messages = [HumanMessage(content='解释用例')]
            first = await model.ainvoke(messages)
            assert first.tool_calls[0]['args'] == {'artifact_id': 'art-1'}
            assert first.usage_metadata['total_tokens'] == 18
            second = await model.ainvoke([*messages, first, ToolMessage(
                tool_call_id=first.tool_calls[0]['id'], content='{"title":"登录"}')])
            assert second.content == '这条用例检查登录。'
    asyncio.run(run())
    assert requests[1]['messages'][-1]['role'] == 'tool'
    assert requests[1]['messages'][-1]['tool_call_id'] == 'call-1'
    assert 'tool_choice' not in requests[0]


def test_invalid_native_arguments_get_one_correction_and_no_partial_result(tmp_path):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion({'title': 42} if len(requests) == 1 else {'title': 'corrected'}))
    assert run_native(tmp_path, handler) == {'title': 'corrected'}
    assert len(requests) == 2
    assert requests[1]['messages'][-1]['role'] == 'tool'
    assert 'title' in requests[1]['messages'][-1]['content']


def test_json_in_assistant_text_is_never_used_as_a_tool_call(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': '{"title":"must not accept"}'}}]})
    with pytest.raises(DomainError, match='Tool Calling') as error:
        run_native(tmp_path, handler)
    assert error.value.category == 'tool_calling'
    assert len(requests) == 2


@pytest.mark.parametrize('status,body,category', [
    (400, {'error': {'message': 'tools is not supported by this model PRIVATE-KEY'}}, 'tool_calling'),
    (400, {'error': {'code': 'context_length_exceeded', 'message': 'maximum context length is 4096 tokens PRIVATE-KEY'}}, 'context_capacity'),
    (401, {'error': 'bad PRIVATE-KEY'}, 'authentication'),
])
def test_provider_failures_are_safe_actionable_and_never_blindly_retried(tmp_path, status, body, category):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=body)
    with pytest.raises(DomainError) as error:
        run_native(tmp_path, handler)
    assert error.value.category == category
    assert 'PRIVATE-KEY' not in str(error.value)
    assert len(requests) == 1


def test_ollama_uses_native_tools_without_format_or_context_override(tmp_path, monkeypatch):
    import langchain_ollama
    actual = langchain_ollama.ChatOllama
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={'model': 'local', 'message': {'role': 'assistant',
            'content': '', 'tool_calls': [{'function': {'name': 'submit_understand',
            'arguments': {'title': 'understood'}}}]}, 'done': True, 'done_reason': 'stop',
            'prompt_eval_count': 2, 'eval_count': 2})
    def construct(**kwargs):
        kwargs.setdefault('async_client_kwargs', {})['transport'] = httpx.MockTransport(handler)
        return actual(**kwargs)
    monkeypatch.setattr(langchain_ollama, 'ChatOllama', construct)
    gateway = LangChainGateway(settings(tmp_path, provider='ollama', base_url='http://model'))
    assert asyncio.run(gateway.generate_native('understand', {}, SCHEMA, 'Understand')) == {'title': 'understood'}
    assert requests[0]['tools'][0]['function']['name'] == 'submit_understand'
    assert 'format' not in requests[0]
    assert 'num_ctx' not in requests[0]['options']


def test_standard_endpoint_custom_auth_and_server_output_limit_are_preserved(tmp_path):
    requests, records = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion({'title': 'ok'}))
    async def run():
        config = settings(tmp_path, base_url='http://model/v1', auth_mode='headers',
                          headers={'Authorization': 'Custom USER-TOKEN', 'X-Tenant': 'tcg'},
                          output_limit_mode='server', server_output_tokens=32768)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(config, http_client=client)
            gateway.request_recorder = records.append
            await gateway.generate_native('understand', {'text': '完整需求' * 20000}, SCHEMA, 'Understand')
    asyncio.run(run())
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == '/v1/chat/completions'
    assert requests[0].headers['authorization'] == 'Custom USER-TOKEN'
    assert 'X-API-Key' not in requests[0].headers
    assert 'max_tokens' not in payload
    assert json.loads(payload['messages'][1]['content'])['text'] == '完整需求' * 20000
    assert records[0]['budget']['context_policy'] == 'server'
    assert 'USER-TOKEN' not in json.dumps(records)


def test_output_truncation_is_not_repaired_or_confused_with_input_capacity(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        result = completion({'title': 'partial'})
        result['choices'][0]['finish_reason'] = 'length'
        return httpx.Response(200, json=result)
    with pytest.raises(DomainError) as error:
        run_native(tmp_path, handler)
    assert error.value.category == 'output_capacity'
    assert not isinstance(error.value, ContextCapacityError)
    assert len(requests) == 1


def test_native_failed_step_archive_has_exact_tool_response_and_safe_request(tmp_path):
    from tcg.diagnostics import Diagnostics
    from tcg.failure_report import failed_step_report
    from tcg.storage import Store
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '原生失败日志')
    _, run = store.create_run(chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'auto'})
    store.update_run(run['id'], status='running', stage='cases')
    raw = completion({'title': 'partial native title'})
    raw['choices'][0]['finish_reason'] = 'length'
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))) as client:
            gateway = LangChainGateway(settings(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with diagnostics.bind(run_id=run['id'], stage='generate_cases'):
                with pytest.raises(DomainError) as raised:
                    await gateway.generate_native('understand', {'text': '需求'}, SCHEMA, 'Understand')
                return raised.value.call_id
    try:
        call_id = asyncio.run(invoke())
        store.update_run(run['id'], status='failed', failed_node='cases', error='输出被截断')
        snapshot = store.model_request(run['id'], call_id)
        assert snapshot['node'] == 'cases'
        assert snapshot['parameters']['tools'][0]['function']['name'] == 'submit_understand'
        assert snapshot['headers']['X-API-Key'] == '••••••'
        assert 'PRIVATE-KEY' not in json.dumps(snapshot)
        output = store.db.execute('SELECT content FROM model_outputs WHERE call_id=?', (call_id,)).fetchone()[0]
        assert json.loads(output) == raw
        report = failed_step_report(store, diagnostics, run['id']).decode()
        assert call_id in report and 'submit_understand' in report and 'partial native title' in report
        assert '没有可用的调用快照' not in report
        assert any(e['event'] == 'model.error' and e['call_id'] == call_id for e in diagnostics.rows(run['id']))
    finally:
        for handler in diagnostics.logger.handlers:
            handler.close()
        store.close()


def test_native_call_publishes_existing_progress_contract_with_archived_request_and_output(tmp_path):
    from tcg.diagnostics import Diagnostics
    from tcg.storage import Store
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'progress contract')
    _, run = store.create_run(chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'auto'})

    async def invoke():
        raw = completion({'title': '消费者可见的原生结果'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=raw))) as client:
            gateway = LangChainGateway(settings(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with diagnostics.bind(run_id=run['id'], node='cases'):
                return await gateway.generate_native('understand', {'text': '需求'}, SCHEMA, 'Understand')

    try:
        result = asyncio.run(invoke())
        progress = [event['data'] for event in store.events(run['id']) if event['kind'] == 'progress']
        start = next(event for event in progress if event['event'] == 'model.start')
        saved = next(event for event in progress if event['event'] == 'model.request_saved')
        complete = next(event for event in progress if event['event'] == 'model.complete')
        deltas = [event['data'] for event in store.events(run['id']) if event['kind'] == 'model_delta']
        assert start['call_id'] == saved['call_id'] == complete['call_id'] == result.call_id
        assert start['node'] == 'cases' and start['streaming'] is False
        assert store.model_request(run['id'], result.call_id)['call_id'] == result.call_id
        returned = json.loads(''.join(delta['text'] for delta in deltas))
        arguments = returned['choices'][0]['message']['tool_calls'][0]['function']['arguments']
        assert json.loads(arguments)['title'] == '消费者可见的原生结果'
    finally:
        diagnostics.close()
        store.close()


def test_progress_persistence_failure_does_not_replace_native_model_result_or_leak_context(tmp_path, monkeypatch):
    from tcg.diagnostics import Diagnostics
    from tcg.storage import Store
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], 'degraded progress')
    _, run = store.create_run(chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'auto'})
    append = store.append_event

    def fail_progress(run_id, kind, data):
        if kind == 'progress':
            raise DomainError('progress unavailable')
        return append(run_id, kind, data)

    monkeypatch.setattr(store, 'append_event', fail_progress)

    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=completion({'title': '仍然成功'})))) as client:
            gateway = LangChainGateway(settings(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with diagnostics.bind(run_id=run['id'], node='cases'):
                return await gateway.generate_native('understand', {'text': '需求'}, SCHEMA, 'Understand')

    try:
        assert asyncio.run(invoke()) == {'title': '仍然成功'}
        assert diagnostics.context.get() == {}
    finally:
        diagnostics.close()
        store.close()
