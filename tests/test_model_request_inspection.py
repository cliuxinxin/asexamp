import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.model import LangChainGateway, Settings
from test_backend_api import Model, setup_chat, start, until
from test_backend_model import saved
from test_sse_progress import streaming_server


@pytest.mark.parametrize('source', ['default', 'saved', 'env', 'process'])
def test_legacy_timeout_configuration_runs_with_sixty_minutes(tmp_path, monkeypatch, source):
    if source == 'saved':
        (tmp_path / 'settings.json').write_text(json.dumps({'timeout_seconds':120, 'model':'existing'}))
    if source == 'env':
        (tmp_path / '.env').write_text('TCG_MODEL_TIMEOUT_SECONDS=120\nTCG_MODEL_NAME=existing\n')
    if source == 'process':
        monkeypatch.setenv('TCG_MODEL_TIMEOUT_SECONDS', '300')
    settings = Settings(tmp_path)
    assert settings.value['timeout_seconds'] == 3600
    assert settings.public()['timeout_policy'] == 'fixed_60_minutes'
    if source in ('saved', 'env'):
        assert settings.public()['model'] == 'existing'


def test_settings_api_accepts_hour_and_normalizes_old_ui_payload(tmp_path):
    with TestClient(create_app(tmp_path, Model())) as client:
        for seconds in (3600,120):
            response = client.put('/api/settings', json={'provider':'ollama','base_url':'http://127.0.0.1:11434','model':'local','timeout_seconds':seconds})
            assert response.status_code == 200, response.text
            assert response.json()['timeout_seconds'] == 3600
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, intent='query'))
        events = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        assert all(e['timeout_seconds'] == 3600 for e in events if e['event'] == 'model.start')


@pytest.mark.parametrize('provider', ['openai','ollama'])
def test_gateway_records_exact_messages_before_stream_finishes_and_uses_hour_timeouts(tmp_path, monkeypatch, provider):
    with streaming_server(provider) as (endpoint, release, captured):
        settings = saved(tmp_path, endpoint, provider)
        gateway = LangChainGateway(settings)
        snapshots = []
        gateway.request_recorder = lambda snapshot: snapshots.append(json.loads(json.dumps(snapshot)))
        constructors = []
        if provider == 'openai':
            import langchain_openai as module
            name = 'ChatOpenAI'
        else:
            import langchain_ollama as module
            name = 'ChatOllama'
        original = getattr(module, name)
        def construct(**kwargs):
            constructors.append(kwargs)
            return original(**kwargs)
        monkeypatch.setattr(module, name, construct)
        async def exercise():
            first = asyncio.Event()
            async def on_text(text):
                first.set()
            context = {'request':{'content':'VERIFY-EXACT-INPUT'},'validation_repair':{'previous_response':{'invalid':True}}}
            call = asyncio.create_task(gateway.generate_stream('connection_test', context, on_text))
            try:
                await asyncio.wait_for(first.wait(), 4)
                assert not call.done()
                assert len(snapshots) == 1
                assert snapshots[0]['messages'] == captured[0]['messages']
                assert json.loads(snapshots[0]['messages'][1]['content']) == context
                assert snapshots[0]['timeout_seconds'] == 3600
                kwargs = constructors[0]
                assert (kwargs['timeout'] if provider == 'openai' else kwargs['client_kwargs']['timeout']) == 3600
                context['request']['content'] = 'LATER-CHANGE'
                assert 'LATER-CHANGE' not in json.dumps(snapshots[0])
            finally:
                release.set()
                await asyncio.gather(call, return_exceptions=True)
        asyncio.run(exercise())


def test_request_inspection_is_durable_scoped_and_excluded_from_diagnostics(tmp_path):
    with streaming_server('openai') as (endpoint, release, captured):
        with TestClient(create_app(tmp_path)) as client:
            client.put('/api/settings', json={'provider':'openai','base_url':endpoint,'model':'test-local','api_key':'SECRET-AUTH-KEY','timeout_seconds':3600}).raise_for_status()
            _, chat, _ = setup_chat(client, text='PRIVATE-REQUIREMENT-BODY')
            run = start(client, chat, intent='query', content='PRIVATE-REQUEST-CONTENT')
            deadline = time.monotonic() + 4
            try:
                while True:
                    events = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
                    stored = [e for e in events if e['event'] == 'model.request_saved']
                    if stored:
                        break
                    assert time.monotonic() < deadline, 'Request inspection was never made available'
                    time.sleep(.02)
                call_id = stored[0]['call_id']
                url = '/api/runs/' + run['id'] + '/model-calls/' + call_id + '/request'
                response = client.get(url)
                assert response.status_code == 200
                assert response.headers['cache-control'] == 'no-store'
                assert 'PRIVATE-REQUIREMENT-BODY' in response.text
                assert 'PRIVATE-REQUEST-CONTENT' in response.text
                assert 'SECRET-AUTH-KEY' not in response.text
                original = response.json()
                _, other_chat, _ = setup_chat(client)
                # A real, different run cannot retrieve this call's snapshot.
                other = client.app.state.store
                fake = {**other.run(run['id']), 'id':'run-other', 'chat_id':other_chat['id'], 'status':'failed'}
                with other.transaction():
                    other.db.execute('INSERT INTO runs(id,chat_id,project_id,status,payload) VALUES(?,?,?,?,?)',(fake['id'],fake['chat_id'],fake['project_id'],fake['status'],json.dumps(fake)))
                assert client.get(url.replace(run['id'],'run-other')).status_code == 404
                assert client.get(url.replace(call_id,'missing-call')).status_code == 404
                assert client.get(url+'?download=true').headers['content-disposition'].endswith('.json"')
                diagnostics = client.get('/api/runs/' + run['id'] + '/diagnostics').text
                log = (tmp_path/'logs'/'tcg.log').read_text()
                for secret in ('PRIVATE-REQUIREMENT-BODY','PRIVATE-REQUEST-CONTENT','SECRET-AUTH-KEY'):
                    assert secret not in diagnostics and secret not in log
                assert client.get('/api/runs/' + run['id']).json()['status'] == 'running'
            finally:
                release.set()
            until(client, run)
        with TestClient(create_app(tmp_path, Model())) as restarted:
            assert restarted.get(url).json() == original
