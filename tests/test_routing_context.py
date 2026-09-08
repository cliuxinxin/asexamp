"""Keep intent classification small while preserving all business evidence."""
import asyncio
import json

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until


class BudgetModel(Model):
    def __init__(self):
        super().__init__()
        self.analyzed = []

    async def generate(self, task, context):
        self.calls.append((task, context))
        if task == 'route':
            if len(json.dumps(context, ensure_ascii=False)) > 12_000:
                raise asyncio.TimeoutError('Simulated oversized routing request')
            return {'intent': 'review_requirement'}
        if task == 'analyze_requirement':
            self.analyzed.extend(context['evidence'])
            return {'items': [{'id': 'REQ-' + e['id'], 'title': 'Requirement', 'description': e['text'], 'refs': [e['id']]} for e in context['evidence']], 'report': {'questions': [], 'assumptions': []}}
        raise AssertionError(task)


def large_requirement():
    return '\n\n'.join(f'Business paragraph {i}: ' + 'PRIVATE-BUSINESS-EVIDENCE-' * 4 for i in range(476))


def test_route_excludes_document_text_and_analysis_receives_every_chunk(tmp_path):
    model = BudgetModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client, large_requirement())
        run = until(client, start(client, chat, intent='auto', content='先分析一下需求'))
        assert run['status'] == 'completed', run
        routing = next(context for task, context in model.calls if task == 'route')
        assert len(json.dumps(routing, ensure_ascii=False)) < 12_000
        assert 'PRIVATE-BUSINESS-EVIDENCE' not in json.dumps(routing)
        assert 'evidence' not in routing
        assert routing['sources']['count'] == 1
        assert routing['request']['content'] == '先分析一下需求'
        expected = client.get('/api/sources/' + source['id']).json()['chunks']
        assert {e['id']: e['text'] for e in model.analyzed} == {e['id']: e['text'] for e in expected}
        assert len(model.analyzed) == 476


def test_routing_preview_bounds_large_history_profile_and_artifact(tmp_path):
    with TestClient(create_app(tmp_path, BudgetModel())) as client:
        project, chat, _ = setup_chat(client)
        store = client.app.state.store
        request = {'content': 'START-' + '\x00' * 90_000 + '-END', 'intent': 'auto', 'mode': 'hitp'}
        _, run = store.create_run(chat['id'], request)
        huge = 'SHOULD-NOT-BE-IN-ROUTING' * 10000
        run['_profile']['additional_rules'] = huge
        run['_conversation'] = [{'role': 'user', 'content': 'older-' + '\x00' * 50000, 'metadata': {'arbitrary': huge}} for _ in range(12)]
        run['_artifact_snapshot'] = {'id':'art-test','type':'cases','title':'Login cases','revision':3,'items':[{'id':'TC-1','description':huge}], 'report':{'body':huge}}
        store.save_run(run)
        context = client.app.state.engine.routing_context(run['id'])
        assert len(json.dumps(context, ensure_ascii=False)) < 12_000
        assert context['request']['content'].startswith('START-') and context['request']['content'].endswith('-END')
        assert context['request']['preview_truncated'] is True
        assert context['artifact']['type'] == 'cases' and context['artifact']['revision'] == 3
        assert context['artifact']['item_count'] == 1 and 'items' not in context['artifact']
        assert 'SHOULD-NOT-BE-IN-ROUTING' not in json.dumps(context)
        assert len(context['conversation']) == 4
        assert store.run(run['id'])['_request']['content'] == request['content']
        client.app.state.engine.cancel(run['id'])


def test_explicit_intent_skips_model_router(tmp_path):
    model = BudgetModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, large_requirement())
        run = until(client, start(client, chat, intent='review_requirement'))
        assert run['status'] == 'completed'
        assert not any(task == 'route' for task, _ in model.calls)
        assert len(model.analyzed) == 476


def test_failed_legacy_route_retries_with_compact_context_after_restart(tmp_path):
    with TestClient(create_app(tmp_path, BudgetModel())) as client:
        engine = client.app.state.engine
        # Emulate the previous release's full-evidence routing call.
        engine.routing_context = engine.context
        _, chat, _ = setup_chat(client, large_requirement())
        run = until(client, start(client, chat, intent='auto'))
        assert run['status'] == 'failed'
    model = BudgetModel()
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry').status_code == 200
        resumed = until(client, run)
        assert resumed['status'] == 'completed'
        assert len(model.analyzed) == 476
        assert len(client.get('/api/chats/' + chat['id']).json()['messages']) == 2
