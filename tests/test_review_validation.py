"""Exercise malformed model review output against the real graph and SQLite."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.schemas import apply_operations
from test_backend_api import Model, setup_chat, start, until


class InvalidReview(Model):
    def __init__(self, field='preconditions', repair_valid=True, malformed=None):
        super().__init__()
        self.field, self.repair_valid, self.malformed = field, repair_valid, malformed

    async def generate(self, task, context):
        result = await super().generate(task, context)
        if task == 'review_cases' and (not context.get('validation_repair') or not self.repair_valid):
            item = result['operations'][0]['item']
            if self.malformed == 'missing_add':
                item['id'] = 'TC-PRIVATE-ADDITION'
                item.pop('preconditions')
                result['operations'] = [{'op': 'add', 'item': item}]
            elif self.malformed == 'operation':
                result['operations'] = ['PRIVATE-INVALID-OPERATION']
            elif self.malformed == 'scenario':
                item['scenario_id'] = 'PRIVATE-INVENTED-SCENARIO'
            else:
                item[self.field] = ['PRIVATE-INVALID-VALUE']
        return result


@pytest.mark.parametrize('field', ['type', 'priority', 'preconditions', 'scenario_id'])
def test_invalid_review_field_is_repaired_with_feedback_before_commit(tmp_path, field):
    model = InvalidReview(field)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert artifact['revision'] == 2
        assert artifact['items'][0]['preconditions'] == 'Account exists'
        assert artifact['items'][0]['scenario_id'] == 'SC-1'
        assert artifact['items'][0]['title'] == 'Reviewed login case'
        reviews = [c for t, c in model.calls if t == 'review_cases']
        assert len(reviews) == 2
        feedback = reviews[1]['validation_repair']
        assert feedback['validation_error'] == {'code': 'type_mismatch', 'path': 'items[0].' + field, 'expected': 'string', 'actual': 'array'}
        assert feedback['previous_response']['operations'][0]['item'][field] == ['PRIVATE-INVALID-VALUE']
        assert len([t for t, _ in model.calls if t == 'generate_cases']) == 1
        rows = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        failed = next(e for e in rows if e['event'] == 'review.validation_failed')
        assert failed['validation_error'] == feedback['validation_error']
        assert any(e['event'] == 'review.repair_complete' for e in rows)
        assert 'PRIVATE-' not in json.dumps(rows)


@pytest.mark.parametrize('malformed,path,actual', [
    ('missing_add', 'items[1].preconditions', 'missing'),
    ('operation', 'operations[0]', 'string'),
    ('scenario', 'items[0].scenario_id', 'string'),
])
def test_review_repair_validates_added_items_operations_and_scenario_links(tmp_path, malformed, path, actual):
    model = InvalidReview(malformed=malformed)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'completed', run
        reviews = [c for t, c in model.calls if t == 'review_cases']
        assert len(reviews) == 2
        issue = reviews[1]['validation_repair']['validation_error']
        assert issue['path'] == path and issue['actual'] == actual
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert len(artifact['items']) == 1 and artifact['items'][0]['scenario_id'] == 'SC-1'


def test_failed_repair_is_bounded_preserves_draft_and_retries_after_restart(tmp_path):
    model = InvalidReview(repair_valid=False)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        assert len([t for t, _ in model.calls if t == 'review_cases']) == 2
        assert 'items[0].preconditions' in run['error']
        store = client.app.state.store
        aid = store.cache_get(run['id'], 'cases_artifact')['id']
        draft = store.get('artifact', aid)
        assert draft['revision'] == 1 and draft['items'][0]['preconditions'] == 'Account exists'
        assert len(store.revisions(aid)) == 1
        assert client.get('/api/artifacts/' + aid).status_code == 404
        diagnostics = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        error = next(e for e in diagnostics['events'] if e['event'] == 'node.error')
        assert error['validation_error']['path'] == 'items[0].preconditions'
        assert 'PRIVATE-' not in json.dumps(diagnostics)
        assert 'PRIVATE-' not in (tmp_path / 'logs' / 'tcg.log').read_text()
    repaired = InvalidReview()
    with TestClient(create_app(tmp_path, repaired)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry').status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert [t for t, _ in repaired.calls] == ['review_cases']
        artifact = client.get('/api/artifacts/' + aid).json()
        assert artifact['revision'] == 2 and artifact['items'][0]['title'] == 'Reviewed login case'


def test_cancel_during_review_repair_prevents_late_commit(tmp_path):
    class SlowRepair(InvalidReview):
        async def generate(self, task, context):
            if context.get('validation_repair'):
                self.repair_started = True
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    pass  # Simulate a provider returning after cancellation.
            return await super().generate(task, context)
    model = SlowRepair()
    model.repair_started = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat)
        deadline = time.monotonic() + 5
        while not model.repair_started:
            current = client.get('/api/runs/' + run['id']).json()
            assert current['status'] != 'failed', current
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert client.post('/api/runs/' + run['id'] + '/cancel').status_code == 200
        deadline = time.monotonic() + 5
        while client.get('/api/runs/' + run['id'] + '/diagnostics').json()['runtime']['task_active']:
            assert time.monotonic() < deadline
            time.sleep(.01)
        store = client.app.state.store
        aid = store.cache_get(run['id'], 'cases_artifact')['id']
        assert store.get('artifact', aid)['revision'] == 1
        assert store.cache_get(run['id'], 'review_applied') is None
        assert client.get('/api/runs/' + run['id']).json()['status'] == 'cancelled'


def test_legacy_review_failure_resumes_only_review_after_upgrade(tmp_path):
    with TestClient(create_app(tmp_path, InvalidReview())) as client:
        engine = client.app.state.engine
        # Previous release: one raw call followed directly by revision validation.
        async def legacy_review(state):
            run_id = state['run_id']
            engine.stage(run_id, 'case_review')
            artifact = engine.store.get('artifact', state['cases_ref'])
            result = await engine.call(run_id, 'case_review', 'review_cases', engine.grounded_context(run_id, cases=artifact['items']))
            items = apply_operations(artifact['items'], result['operations'])
            engine.store.revise_artifact(artifact['id'], artifact['revision'], items, 'ai_review', run_id, 'review_applied')
        engine.node_review = legacy_review
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        aid = engine.store.cache_get(run['id'], 'cases_artifact')['id']
    model = InvalidReview()
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry').status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert [t for t, _ in model.calls] == ['review_cases', 'review_cases']
        assert client.get('/api/artifacts/' + aid).json()['revision'] == 2


@pytest.mark.parametrize('needs_repair', [False, True])
def test_replay_after_revision_commit_keeps_accepted_report_and_revision(tmp_path, needs_repair):
    class ReportReview(InvalidReview):
        async def generate(self, task, context):
            result = await super().generate(task, context) if needs_repair else await Model.generate(self, task, context)
            if task == 'review_cases':
                result['report'] = {'summary': 'Accepted report' if not needs_repair or context.get('validation_repair') else 'Rejected report'}
            return result
    with TestClient(create_app(tmp_path, Model())) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat))
        aid = initial['artifact_ids'][0]
        client.app.state.engine.gateway = ReportReview()
        store = client.app.state.store
        original_artifact = store.artifact
        def fail_report(run_id, key, *args, **kwargs):
            if key == 'review_report':
                raise RuntimeError('Simulated failure after revision commit')
            return original_artifact(run_id, key, *args, **kwargs)
        store.artifact = fail_report
        run = until(client, start(client, chat, intent='review_case', artifact_id=aid))
        assert run['status'] == 'failed'
        assert store.get('artifact', aid)['revision'] == 3
    model = Model()
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry').status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert client.get('/api/artifacts/' + aid).json()['revision'] == 3
        artifacts = [client.get('/api/artifacts/' + item).json() for item in run['artifact_ids']]
        report = next(a for a in artifacts if a['type'] == 'review')
        assert report['report']['summary'] == 'Accepted report'
        assert model.calls == []
