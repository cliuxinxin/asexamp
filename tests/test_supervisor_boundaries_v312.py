"""Selected-scope continuation and approval crash-window regression coverage."""
import asyncio
import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tcg.main import create_app
from tcg.storage import now
from test_supervisor_http_v312 import QueueGateway, begin, file_parts, plan, post, seed, wait_files


def test_natural_control_keeps_original_tail_selection_when_ui_selection_changes(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        prompt = initial['pending'][0]
        gateway.plans.append({'title': '接受当前已查看修改', 'steps': [
            {'capability': 'current_control', 'instruction': '仅接受当前修改预览，保留已安排的后续步骤。'}]})
        gateway.calls.insert(0, ('apply_artifact_preview_tool', {}))
        result, _ = post(client, chat, 'natural-accept', '我已经看过了，按这份修改保存吧',
            reply_to=prompt['id'], artifact_id=cases['id'], artifact_revision=cases['revision'],
            selected_ids=[cases['items'][1]['id']])
        files = wait_files(client, chat, result)
        sheet = load_workbook(io.BytesIO(client.get(files[0]['url']).content)).active
        values = list(sheet.values)
        assert len(values) == 2
        assert cases['items'][0]['id'] in values[1], values
        assert cases['items'][1]['id'] not in values[1]
        assert len(app.state.store.list('frozen_export', chat_id=chat['id'])) == 1
        assert len(gateway.planner_inputs) == 2


def test_refining_preview_keeps_original_export_tail_and_selection(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, original, _ = begin(client, app, gateway, chat, cases)
        gateway.plans.append({'title': '调整当前预览', 'steps': [
            {'capability': 'artifact_edit', 'instruction': '将原选择用例的前置条件改为已登录且有权限。'}]})
        gateway.calls.insert(0, ('modify_artifact_tool', {'artifact_id': cases['id'],
            'new_values': {'preconditions': '已登录且有权限'}}))
        refined, _ = post(client, chat, 'refine', '前置条件应该改成已登录且有权限', reply_to=initial['pending'][0]['id'])
        assert refined['status'] == 'needs_confirmation', refined
        state = client.get(f"/api/chats/{chat['id']}/plans/{original['plan_id']}").json()
        assert state['status'] == 'cancelled'
        result, _ = post(client, chat, 'accept-refined', '同意', reply_to=refined['pending'][0]['id'])
        files = wait_files(client, chat, result)
        sheet = load_workbook(io.BytesIO(client.get(files[0]['url']).content)).active
        rows = list(sheet.values)
        assert len(rows) == 2 and '已登录且有权限' in rows[1]
        assert app.state.store.get('artifact', cases['id'])['items'][1] == cases['items'][1]


def test_restart_after_approval_commit_resumes_tail_using_durable_approval_receipt(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        prompt = initial['pending'][0]
        # Deliberately stop between the business approval commit and queue continuation.
        turn = {'id': 'approval-window', 'client_message_id': '', 'project_id': chat['project_id'],
            'chat_id': chat['id'], 'created_at': now(), 'status': 'running',
            'message': '', 'parts': [], 'pending': [], 'actions': [], '_runtime': 'native'}
        client.portal.call(app.state.conversation._execute_direct, chat,
            {'content': '同意', 'reply_to': prompt['id']}, prompt, turn, 'apply_artifact_preview_tool', {})
        assert app.state.store.get('artifact', cases['id'])['revision'] == cases['revision'] + 1
        assert not app.state.store.list('frozen_export', chat_id=chat['id'])
    restarted = create_app(tmp_path, gateway)
    with TestClient(restarted) as client:
        files = wait_files(client, chat, {})
        assert len(files) == 1
        assert len(gateway.planner_inputs) == 1
        state = client.get(f"/api/chats/{chat['id']}/plans/{part['plan_id']}").json()
        assert state['status'] == 'completed'
        assert len(restarted.state.store.list('frozen_export', chat_id=chat['id'])) == 1


def test_uncertain_effect_after_crash_is_not_replayed_by_retry(tmp_path):
    gateway = QueueGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        initial, part, _ = begin(client, app, gateway, chat, cases)
        queued = app.state.store.get('execution_plan', part['plan_id'])
        queued.update(status='running')
        queued['steps'][0].update(status='running', receipts=[])
        app.state.store.put('execution_plan', queued)
    restarted = create_app(tmp_path, gateway)
    with TestClient(restarted) as client:
        state = client.get(f"/api/chats/{chat['id']}/plans/{part['plan_id']}").json()
        assert state['status'] == 'blocked'
        result, _ = post(client, chat, 'retry-uncertain', '重试未完成步骤')
        assert result['status'] == 'failed'
        assert '不能自动重放' in result['message']
        assert len(gateway.planner_inputs) == 1
        assert gateway.executed == ['modify_artifact_tool']
        assert not restarted.state.store.list('frozen_export', chat_id=chat['id'])
