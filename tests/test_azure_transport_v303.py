import asyncio
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from tcg.diagnostics import Diagnostics
from tcg.model import LangChainGateway, Settings
from tcg.schemas import DomainError
from tcg.storage import Store


SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
TOOL = {'type': 'function', 'function': {'name': 'read_artifact_tool',
        'description': 'Read an artifact.', 'parameters': {'type': 'object',
        'properties': {'artifact_id': {'type': 'string'}}, 'required': ['artifact_id']}}}


def configured(tmp_path, **extra):
    settings = Settings(tmp_path)
    settings.save({'provider': 'openai', 'base_url': 'https://legacy.test/v1', 'model': 'old',
                   'api_key': 'AZURE-TEST-KEY', 'headers': {'X-Tenant': 'OLD-TENANT'}})
    # Exercise the transport contract independently from env parsing.
    settings.value.update(provider='azure', base_url='https://resource.openai.azure.com',
                          model='tcg-deployment', api_version='2025-01-01-preview', **extra)
    return settings


def completion(name='submit_connection_test', args=None):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'role': 'assistant',
            'content': None, 'tool_calls': [{'id': 'tool-1', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args if args is not None else {'ok': True})}}]}}]}


@pytest.mark.parametrize('output_mode', ['request', 'server'])
def test_azure_native_submission_routes_deployment_and_isolates_auth(tmp_path, output_mode):
    requests, snapshots = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=completion())
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(configured(tmp_path, output_limit_mode=output_mode,
                server_output_tokens=32768), http_client=client)
            gateway.request_recorder = snapshots.append
            assert await gateway.generate_native('connection_test', {}, SCHEMA, 'Check connection.') == {'ok': True}
    asyncio.run(invoke())
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == ('https://resource.openai.azure.com/openai/deployments/'
                               'tcg-deployment/chat/completions?api-version=2025-01-01-preview')
    assert request.headers['api-key'] == 'AZURE-TEST-KEY'
    assert all(name not in request.headers for name in ('X-API-Key', 'Authorization', 'X-Tenant'))
    payload = json.loads(request.content)
    assert payload['model'] == 'tcg-deployment'
    assert payload['tool_choice']['function']['name'] == 'submit_connection_test'
    assert payload['tools'][0]['function']['parameters']['required'] == ['ok']
    assert 'max_tokens' not in payload and 'response_format' not in payload
    assert ('max_completion_tokens' in payload) == (output_mode == 'request')
    if output_mode == 'request':
        assert payload['max_completion_tokens'] == 8192
        assert snapshots[0]['parameters']['max_completion_tokens'] == 8192
    assert snapshots[0]['api_version'] == '2025-01-01-preview'
    assert snapshots[0]['endpoint'] == str(request.url)
    assert 'AZURE-TEST-KEY' not in json.dumps(snapshots)


def test_azure_chat_tool_result_round_trip(tmp_path):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json=completion('read_artifact_tool', {'artifact_id': 'art-1'}))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '这组用例检查登录。'}}]})
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(configured(tmp_path), http_client=client)
            model = gateway.chat_model().bind_tools([TOOL])
            messages = [HumanMessage(content='总结用例')]
            first = await model.ainvoke(messages)
            assert first.tool_calls[0]['args'] == {'artifact_id': 'art-1'}
            second = await model.ainvoke([*messages, first,
                ToolMessage(content='登录用例', tool_call_id=first.tool_calls[0]['id'])])
            assert second.content == '这组用例检查登录。'
    asyncio.run(invoke())
    assert len(requests) == 2
    assert requests[1]['messages'][-1]['tool_call_id'] == 'tool-1'


@pytest.mark.parametrize('status,code,category', [
    (404, 'DeploymentNotFound', 'configuration'),
    (400, 'InvalidApiVersionParameter', 'configuration'),
    (401, 'invalid_api_key', 'authentication'),
])
def test_azure_failure_logs_effective_route_without_keys(tmp_path, status, code, category):
    store = Store(tmp_path)
    diagnostics = Diagnostics(store)
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={'error': {'code': code,
            'message': 'AZURE-TEST-KEY OLD-TENANT'}}, headers={'apim-request-id': 'azure-request-42'})
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            gateway = LangChainGateway(configured(tmp_path), http_client=client)
            gateway.diagnostics = diagnostics
            with pytest.raises(DomainError) as raised:
                await gateway.test()
            assert raised.value.category == category
            assert 'AZURE-TEST-KEY' not in str(raised.value)
            if category == 'configuration':
                assert '部署名称' in str(raised.value) and 'API 版本' in str(raised.value)
    try:
        asyncio.run(invoke())
        assert len(requests) == 1
        events = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        start = next(e for e in events if e['event'] == 'model.transport_start')
        assert start['provider'] == 'azure'
        assert start['api_version'] == '2025-01-01-preview'
        assert start['deployment'] == 'tcg-deployment'
        assert start['auth_header_names'] == ['api-key']
        failure = next(e for e in events if e['event'] == 'model.transport_error')
        assert failure['provider_request_id'] == 'azure-request-42'
        assert failure['provider_error_code'] == code
        assert 'AZURE-TEST-KEY' not in diagnostics.path.read_text()
        assert 'OLD-TENANT' not in diagnostics.path.read_text()
    finally:
        diagnostics.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['auto', 'hitp'])
async def test_azure_env_drives_chat_and_pipeline_through_http(tmp_path, monkeypatch, mode):
    from tcg import environment
    from tcg.main import create_app
    from test_native_journey_v300 import NativeJourneyGateway
    for name in (*environment.MODEL_ENV, 'TCG_ENV_FILE'):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / '.env').write_text(
        'AZURE_OPENAI_ENDPOINT=resource.openai.azure.com/\n'
        'AZURE_OPENAI_API_KEY=AZURE-INTEGRATION-KEY\n'
        'AZURE_API_VERSION=2025-01-01-preview\n'
        'TCG_MODEL_NAME=tcg-deployment\n')
    fixture = NativeJourneyGateway()
    tasks = []
    async def handler(request):
        assert request.url.path == '/openai/deployments/tcg-deployment/chat/completions'
        assert request.url.params['api-version'] == '2025-01-01-preview'
        assert request.headers['api-key'] == 'AZURE-INTEGRATION-KEY'
        payload = json.loads(request.content)
        forced = payload.get('tool_choice', {}).get('function', {}).get('name', '')
        if forced.startswith('submit_'):
            task = forced.removeprefix('submit_')
            tasks.append(task)
            result = await fixture.generate_native(task,
                json.loads(payload['messages'][1]['content']), {}, '')
            return httpx.Response(200, json=completion(forced, result))
        if payload['messages'][-1]['role'] == 'tool':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': '已处理，等待下一阶段。'}}]})
        tool_name = ('resume_pipeline_tool' if '同意' in str(payload['messages'][-1]['content'])
                     else 'start_pipeline_tool')
        return httpx.Response(200, json=completion(tool_name, {}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as model_client:
        gateway = LangChainGateway(Settings(tmp_path), http_client=model_client)
        app = create_app(tmp_path, gateway)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                settings = (await client.get('/api/settings')).json()
                assert settings['provider'] == 'azure' and settings['model'] == 'tcg-deployment'
                assert 'AZURE-INTEGRATION-KEY' not in json.dumps(settings)
                project = (await client.get('/api/projects')).json()[0]
                chat = (await client.post('/api/projects/' + project['id'] + '/chats',
                                         json={'title': 'Azure integration'})).json()
                route = '/api/chats/' + chat['id']
                source = await client.post(route + '/sources', files={'file': (
                    'login.md', '注册用户输入有效账号密码后登录成功。'.encode(), 'text/markdown')},
                    data={'role': 'primary'})
                assert source.status_code == 200
                response = await client.post(route + '/turns', json={
                    'content': '根据上传需求生成测试用例', 'mode': mode, 'client_message_id': 'azure-start'})
                assert response.json()['status'] == 'succeeded', response.text
                gates = []
                for _ in range(800):
                    snapshot = (await client.get(route)).json()
                    run = snapshot['runs'][0]
                    assert run['status'] != 'failed', run
                    if run['status'] == 'completed':
                        break
                    if run['status'] == 'waiting':
                        prompt = snapshot['conversation_prompt']
                        gates.append(prompt['kind'])
                        response = await client.post(route + '/turns', json={
                            'content': '同意，继续', 'mode': mode, 'reply_to': prompt['id'],
                            'client_message_id': 'azure-confirm-' + str(len(gates))})
                        assert response.json()['status'] == 'succeeded', response.text
                    await asyncio.sleep(.01)
                else:
                    pytest.fail('Azure pipeline did not complete')
                assert gates == ([] if mode == 'auto' else [
                    'strategy_review', 'scenario_review', 'case_result_review'])
                assert tasks == ['understand_requirements', 'generate_scenarios', 'generate_cases', 'review_cases']
                artifact = (await client.get('/api/artifacts/' + run['current_artifact_id'])).json()
                assert artifact['items'][0]['steps'][0]['expected'] == '登录成功'
