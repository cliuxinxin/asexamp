"""Composer request contracts through real HTTP, SQLite and the authoring graph.

Only model semantics are controlled. These requests exercise /turns, including
the natural-language planner path that older /messages tests do not cover.
"""
import copy
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from tcg.main import create_app
from tcg.conversation import fingerprint
from test_backend_api import setup_chat, until
from test_workflow_v25 import FlowModel


class RoutingModel(FlowModel):
    def __init__(self):
        super().__init__()
        self.arguments = {}
        self.actions = None

    async def generate(self, task, context):
        if task == 'conversation_turn':
            self.calls.append((task, copy.deepcopy(context)))
            return {'actions': copy.deepcopy(self.actions) if self.actions is not None else [
                {'name': 'workflow.start', 'arguments': {'intent': 'generate_case', **self.arguments}}]}
        return await super().generate(task, context)


@pytest.fixture
def http_runtime(tmp_path):
    model = RoutingModel()
    app = create_app(tmp_path, model)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, 'HTTP server did not start'
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=20) as client:
            yield client, app, model
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), 'HTTP server did not stop'


def turn(client, chat, body):
    return client.post('/api/chats/' + chat['id'] + '/turns', json={
        'client_message_id': 'mode-contract', 'content': '请基于登录需求生成测试用例',
        'intent_hint': 'auto', **body})


def created_run(client, app, chat):
    runs = app.state.store.runs(chat_id=chat['id'])
    assert len(runs) == 1, runs
    return until(client, runs[0])


@pytest.mark.parametrize('mode,planned_mode,shortcut', [
    ('hitp', None, False), ('hitp', 'auto', False),
    ('auto', None, False), ('auto', 'hitp', False),
    ('hitp', None, True), ('hitp', 'auto', True),
])
def test_explicit_composer_mode_is_authoritative_on_every_start_path(http_runtime, mode, planned_mode, shortcut):
    client, app, model = http_runtime
    _, chat, _ = setup_chat(client)
    arguments = {'intent': 'generate_case', **({'mode': planned_mode} if planned_mode else {})}
    model.arguments = arguments
    body = {'mode': mode}
    if shortcut:
        body['command'] = {'name': 'workflow.start', 'arguments': arguments}
    response = turn(client, chat, body)
    assert response.status_code == 200, response.text
    run = created_run(client, app, chat)
    stored = app.state.store.run(run['id'])
    assert run['mode'] == stored['_request']['mode'] == mode
    assert stored['experience'] == 'reliable' and stored['graph_version'] == 7
    tasks = [task for task, _ in model.calls]
    assert ('conversation_turn' in tasks) is not shortcut
    if mode == 'hitp':
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'strategy_review', run
        assert 'generate_scenarios' not in tasks and 'generate_cases' not in tasks
    else:
        assert run['status'] == 'completed', run
        assert tasks.count('analyze_requirement') == tasks.count('generate_scenarios') == 1
        assert tasks.count('generate_cases') == tasks.count('review_cases') == 1
        messages = client.get('/api/chats/' + chat['id']).json()['messages']
        assert [m['metadata']['stage'] for m in messages if m.get('metadata', {}).get('stage_artifact_id')] == [
            'understand', 'scenarios', 'cases', 'review']


@pytest.mark.parametrize('mode', ['manual', '', True, 7, {'mode': 'hitp'}, None])
def test_malformed_composer_mode_is_rejected_before_a_turn_or_run(http_runtime, mode):
    client, app, _ = http_runtime
    _, chat, _ = setup_chat(client)
    response = turn(client, chat, {'mode': mode})
    assert response.status_code == 422, response.text
    assert not app.state.store.runs(chat_id=chat['id'])
    assert not app.state.store.list('conversation_turn', chat_id=chat['id'])
    assert not app.state.store.list('message', chat_id=chat['id'])


@pytest.mark.parametrize('planned_mode,expected', [(None, 'auto'), ('hitp', 'hitp')])
def test_http_callers_omitting_mode_keep_the_existing_fallback(http_runtime, planned_mode, expected):
    client, app, model = http_runtime
    _, chat, _ = setup_chat(client)
    model.arguments = {'mode': planned_mode} if planned_mode else {}
    response = turn(client, chat, {})
    assert response.status_code == 200, response.text
    assert created_run(client, app, chat)['mode'] == expected


def test_explicit_profile_and_design_settings_survive_a_conflicting_plan(http_runtime):
    client, app, model = http_runtime
    project, chat, _ = setup_chat(client)
    profiles = client.get('/api/projects/' + project['id'] + '/profiles').json()
    selected = profiles[0]
    other = client.post('/api/projects/' + project['id'] + '/profiles', json={
        'name': 'Competing profile', 'config': {**selected['config'], 'sheet_name': 'Planner sheet'}}).json()
    model.arguments = {'mode': 'auto', 'profile_id': other['id'], 'depth': 'quick',
        'case_types': ['Security'], 'profile_override': {'sheet_name': 'Planner override'}}
    response = turn(client, chat, {'mode': 'hitp', 'profile_id': selected['id'], 'depth': 'deep',
        'case_types': ['Business'], 'profile_override': {'sheet_name': 'Chosen sheet'}})
    assert response.status_code == 200, response.text
    run = created_run(client, app, chat)
    stored = app.state.store.run(run['id'])
    assert stored['_profile_id'] == selected['id']
    assert stored['_profile']['scenario_level'] == stored['_profile']['case_level'] == 'deep'
    assert stored['_profile']['case_types'] == ['Business']
    assert stored['_profile']['sheet_name'] == 'Chosen sheet'


def test_routing_metadata_distinguishes_requested_settings_from_actual_run_mode(http_runtime):
    client, app, model = http_runtime
    project, chat, _ = setup_chat(client)
    profile = client.get('/api/projects/' + project['id'] + '/profiles').json()[0]
    response = turn(client, chat, {'mode': 'hitp', 'depth': 'deep', 'case_types': ['Business'],
        'profile_id': profile['id'], 'profile_override': {'additional_rules': 'PRIVATE FORMAT RULE'}})
    assert response.status_code == 200, response.text
    run = created_run(client, app, chat)
    first = [context for task, context in model.calls if task == 'conversation_turn'][0]
    assert first['requested_settings'] == {'mode': 'hitp', 'depth': 'deep', 'case_types': ['Business'],
        'profile_id': profile['id'], 'profile_override_fields': ['additional_rules']}
    assert 'PRIVATE FORMAT RULE' not in str(first)
    model.actions = []
    response = turn(client, chat, {'client_message_id': 'actual-mode', 'mode': 'auto', 'content': '解释当前任务状态'})
    assert response.status_code == 200, response.text
    latest = [context for task, context in model.calls if task == 'conversation_turn'][-1]
    assert latest['requested_settings']['mode'] == 'auto'
    assert latest['runs'][0]['id'] == run['id'] and latest['runs'][0]['mode'] == 'hitp'
    assert app.state.store.run(run['id'])['mode'] == 'hitp'


@pytest.mark.parametrize('arguments', [{'depth': 'unsupported'}, {'case_types': ['InventedType']}])
def test_workflow_start_validates_the_constructed_message_before_persisting_a_run(http_runtime, arguments):
    client, app, model = http_runtime
    _, chat, _ = setup_chat(client)
    model.arguments = arguments
    response = turn(client, chat, {'mode': 'hitp'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'failed', response.json()
    assert not app.state.store.runs(chat_id=chat['id'])
    assert not any(task == 'analyze_requirement' for task, _ in model.calls)


def test_stop_policy_changes_do_not_invalidate_semantic_inputs(http_runtime):
    client, app, _ = http_runtime
    _, chat, _ = setup_chat(client)
    assert turn(client, chat, {'mode': 'hitp'}).status_code == 200
    run = created_run(client, app, chat)
    before = app.state.store.run(run['id'])
    response = turn(client, chat, {'client_message_id': 'change-stop', 'command': {
        'name': 'workflow.update_scope', 'arguments': {'run_id': run['id'], 'stop_after': 'scenarios', 'goal': 'generate_scenario'}}})
    assert response.status_code == 200 and response.json()['status'] == 'succeeded', response.text
    controlled = app.state.store.run(run['id'])
    assert controlled['stop_after'] == 'scenarios' and controlled['goal'] == 'generate_scenario'
    assert controlled['input_version'] == before['input_version']
    assert controlled['_request']['stop_after'] == 'scenarios'
    response = turn(client, chat, {'client_message_id': 'change-scope', 'command': {
        'name': 'workflow.update_scope', 'arguments': {'run_id': run['id'], 'scope': 'Only registered account login'}}})
    assert response.status_code == 200 and response.json()['status'] == 'succeeded', response.text
    changed = app.state.store.run(run['id'])
    assert changed['input_version'] == before['input_version'] + 1
    assert changed['_profile']['scope'] == 'Only registered account login'


def test_retry_of_a_legacy_turn_receipt_survives_new_setting_metadata(http_runtime):
    client, app, _ = http_runtime
    _, chat, _ = setup_chat(client)
    response = turn(client, chat, {'mode': 'hitp'})
    assert response.status_code == 200, response.text
    run = created_run(client, app, chat)
    stored = app.state.store.get('conversation_turn', response.json()['id'])
    stored['_body'].pop('_explicit_run_settings', None)
    stored['_request_hash'] = fingerprint(stored['_body'])
    app.state.store.put('conversation_turn', stored)
    repeated = turn(client, chat, {'mode': 'hitp'})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == response.json()
    assert [r['id'] for r in app.state.store.runs(chat_id=chat['id'])] == [run['id']]


def test_new_turn_deduplication_keeps_explicit_and_omitted_modes_distinct(http_runtime):
    client, app, model = http_runtime
    _, chat, _ = setup_chat(client)
    model.arguments = {'mode': 'hitp'}
    assert turn(client, chat, {}).status_code == 200
    run = created_run(client, app, chat)
    repeated = turn(client, chat, {'mode': 'auto'})
    assert repeated.status_code == 409, repeated.text
    assert app.state.store.run(run['id'])['mode'] == 'hitp'


@pytest.mark.parametrize('depth,ui_settings,expected_levels,expected_sheet', [
    ('deep', {}, ('deep', 'deep'), 'Planner sheet'),
    ('deep', {'profile_override': None}, ('deep', 'deep'), 'Planner sheet'),
    ('auto', {}, ('quick', 'quick'), 'Planner sheet'),
    ('deep', {'profile_override': {'case_level': 'quick', 'scenario_level': 'standard', 'sheet_name': 'Chosen sheet'}},
        ('quick', 'standard'), 'Chosen sheet'),
])
def test_explicit_depth_survives_model_profile_override(http_runtime, depth, ui_settings, expected_levels, expected_sheet):
    client, app, model = http_runtime
    _, chat, _ = setup_chat(client)
    model.arguments = {'profile_override': {
        'case_level': 'quick', 'scenario_level': 'quick', 'sheet_name': 'Planner sheet'}}
    body = {'mode': 'hitp', 'depth': depth, **ui_settings}
    response = turn(client, chat, body)
    assert response.status_code == 200, response.text
    run = app.state.store.run(created_run(client, app, chat)['id'])
    assert run['_request']['depth'] == depth
    assert (run['_profile']['case_level'], run['_profile']['scenario_level']) == expected_levels
    assert run['_profile']['sheet_name'] == expected_sheet
