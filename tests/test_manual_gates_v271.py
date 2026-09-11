import copy
import pytest

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import setup_chat, start, until
from test_workflow_v25 import FlowModel


def advance(client, run):
    response = client.post('/api/runs/' + run['id'] + '/resume', json={
        'approved': True, 'interrupt_id': run['interrupt_id'],
        'expected_revision': run['interrupt']['artifact_revision'],
        'expected_control_version': run.get('control_version', 0)})
    assert response.status_code == 200, response.text
    return until(client, response.json())


def test_human_confirms_all_four_artifacts_and_latest_case_draft(tmp_path):
    model = FlowModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        assert run['interrupt']['type'] == 'strategy_review'
        run = advance(client, run)
        assert run['interrupt']['type'] == 'scenario_review'
        run = advance(client, run)
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'case_draft_review', run
        assert not any(task == 'review_cases' for task, _ in model.calls)
        draft = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        items = copy.deepcopy(draft['items'])
        items[0]['title'] = '人工检查后的用例草稿'
        changed = client.put('/api/artifacts/' + draft['id'], json={
            'expected_revision': draft['revision'], 'items': items})
        assert changed.status_code == 200, changed.text
        run = client.get('/api/runs/' + run['id']).json()
        assert run['interrupt']['artifact_revision'] == changed.json()['revision']
        run = advance(client, run)
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'case_result_review', run
        assert run['interrupt']['confirm_label']
        reviewed = [context for task, context in model.calls if task == 'review_cases']
        assert reviewed[0]['cases'][0]['title'] == items[0]['title']
        artifact = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        assert artifact['report'].get('review_reports')
        run = advance(client, run)
        assert run['status'] == 'completed', run
        assert len([task for task, _ in model.calls if task == 'review_cases']) == 1


def test_auto_runs_same_stages_without_human_stops(tmp_path):
    model = FlowModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='auto'))
        assert run['status'] == 'completed', run
        assert [task for task, _ in model.calls] == [
            'analyze_requirement', 'generate_scenarios', 'generate_cases', 'review_cases', 'summarize']


def test_case_draft_checkpoint_survives_service_restart(tmp_path):
    first = FlowModel()
    with TestClient(create_app(tmp_path, first)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        run = advance(client, advance(client, run))
        assert run['interrupt']['type'] == 'case_draft_review'
        binding = copy.deepcopy(run['interrupt'])
    second = FlowModel()
    with TestClient(create_app(tmp_path, second)) as client:
        resumed = client.get('/api/runs/' + run['id']).json()
        assert resumed['status'] == 'waiting'
        assert resumed['interrupt'] == binding
        assert second.calls == []
        reviewed = advance(client, resumed)
        assert reviewed['interrupt']['type'] == 'case_result_review'
        assert advance(client, reviewed)['status'] == 'completed'
        assert [task for task, _ in second.calls] == ['review_cases', 'summarize']


def test_existing_v7_checkpoint_keeps_its_original_confirmation_contract(tmp_path):
    model = FlowModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        # A pre-2.7.1 stored Run has no pause_contract; its existing LangGraph
        # checkpoint and interrupt id must continue without a graph migration.
        saved = app.state.store.run(run['id'])
        saved.pop('pause_contract')
        app.state.store.save_run(saved)
        run = advance(client, run)
        assert run['interrupt']['type'] == 'scenario_review'
        assert advance(client, run)['status'] == 'completed'


@pytest.mark.parametrize('reports', [[{'summary': '人工修订后的评审说明'}], []])
def test_confirm_result_keeps_the_review_report_the_user_approved(tmp_path, reports):
    class CorrectReportModel(FlowModel):
        async def generate(self, task, context):
            if task == 'artifact_modify':
                return {'operations': [], 'report_patch': {'review_reports': reports},
                    'summary': '仅修改评审说明'}
            return await super().generate(task, context)
    model = CorrectReportModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='hitp'))
        for _ in range(3):
            run = advance(client, run)
        assert run['interrupt']['type'] == 'case_result_review'
        artifact = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        response = client.post('/api/chats/' + chat['id'] + '/turns', json={
            'client_message_id': 'correct-review', 'content': '仅修订评审说明，仍等待确认。',
            'command': {'name': 'artifact.revise', 'arguments': {
                'artifact_id': artifact['id'], 'expected_revision': artifact['revision']}}})
        assert response.status_code == 200 and response.json()['status'] == 'succeeded', response.text
        revised = client.get('/api/artifacts/' + artifact['id']).json()
        assert revised['report']['review_reports'] == reports
        run = client.get('/api/runs/' + run['id']).json()
        assert advance(client, run)['status'] == 'completed'
        final = client.get('/api/artifacts/' + artifact['id']).json()
        assert final['revision'] == revised['revision']
        assert final['report']['review_reports'] == reports
        summary_context = next(context for task, context in model.calls if task == 'summarize')
        assert summary_context['review_reports'] == reports


def test_changed_source_rejects_old_generation_with_actionable_recovery(tmp_path):
    class ChangedSourceModel(FlowModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                source = app.state.store.list('source')[0]
                app.state.store.put('source', {**source, '_text': source['_text'] + '\nUpdated rule'})
            return result
    model = ChangedSourceModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='reliable', mode='auto'))
        assert run['status'] == 'failed' and run['failed_node'] == 'cases', run
        assert run['recovery']['category'] == 'dependency'
        assert '最新场景' in ''.join(run['recovery']['suggestions'])
        assert not [value for value in app.state.store.list('artifact', chat_id=chat['id'])
            if value['type'] == 'cases']
        details = [event for event in app.state.engine.diagnostics.rows(run['id'], 500)
            if event.get('dependency_changes')]
        assert any(event.get('dependency_phase') == 'after_model' for event in details)
        assert details[-1]['dependency_changes'][0]['category'] == 'source'
        calls = len(model.calls)
        response = client.post('/api/runs/' + run['id'] + '/retry')
        assert response.status_code == 200
        retried = until(client, response.json())
        assert retried['status'] == 'failed'
        assert len(model.calls) == calls  # Existing batches cannot silently rebind.
