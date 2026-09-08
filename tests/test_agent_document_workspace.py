import copy
import json

from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.schemas import DomainError
from test_agent_workflow import AgentModel
from test_backend_api import setup_chat, start, until


class FragmentRepairModel(AgentModel):
    def __init__(self, wrong_path=False):
        super().__init__()
        self.wrong_path = wrong_path

    async def generate(self, task, context):
        if task == 'agent_repair':
            self.calls.append((task, copy.deepcopy(context)))
            return {'path': 'items' if self.wrong_path else context['repair']['path'],
                    'value': [] if self.wrong_path else 'Accept valid credentials and reject invalid credentials.'}
        result = await super().generate(task, context)
        if task == 'agent_analyze':
            if context.get('validation_repair'):
                result['items'].append({**result['items'][0], 'id': 'R2'})
            else:
                result['items'][0]['description'] = ['PRIVATE-REJECTED-TEXT']
        return result


def test_analysis_schema_error_repairs_only_fragment_without_count_changing_regeneration(tmp_path):
    model = FragmentRepairModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert [i['id'] for i in artifact['items']] == ['R1']
        assert isinstance(artifact['items'][0]['description'], str)
        repairs = [c for t, c in model.calls if t == 'agent_repair']
        assert len(repairs) == 1
        assert repairs[0]['repair']['path'] == 'items[0].description'
        assert len(json.dumps(repairs[0])) < 6000
        assert not repairs[0].get('evidence')
        assert len([t for t, _ in model.calls if t == 'agent_analyze']) == 1
        diagnostics = client.get('/api/runs/' + run['id'] + '/diagnostics').json()
        assert any(e.get('validation_error', {}).get('path') == 'items[0].description' for e in diagnostics['events'])
        assert 'PRIVATE-REJECTED-TEXT' not in json.dumps(diagnostics)


def test_repair_rejects_out_of_scope_path_and_explains_field_without_leaking_value(tmp_path):
    with TestClient(create_app(tmp_path, FragmentRepairModel(wrong_path=True))) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'failed'
        assert run['recovery'].get('validation_error')
        assert 'PRIVATE-REJECTED-TEXT' not in json.dumps(run['recovery'])
        assert any('字段' in s for s in run['recovery']['suggestions'])


def test_planner_and_summary_do_not_resend_source_body(tmp_path):
    model = AgentModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        marker = 'DOCUMENT-CONTENT-MUST-NOT-ENTER-PLANNER'
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Rules', 'role': 'primary', 'text': marker + ' valid authentication rules.'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        for task, context in model.calls:
            if task in ('agent_plan', 'agent_summary'):
                assert marker not in json.dumps(context)
                assert not any(e.get('text') for e in context['evidence'])


def test_full_analysis_reads_all_large_document_batches_once(tmp_path):
    class BatchModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_integrate':
                self.calls.append((task, copy.deepcopy(context)))
                return {'edges': [], 'limitations': ['Cross-module dependencies require business review.']}
            return await super().generate(task, context)
    model = BatchModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        content = '\n\n'.join(f'Rule {i}: ' + 'Authentication accepts valid credentials. ' * 30 for i in range(36))
        source = client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Large', 'role': 'primary', 'text': content}).json()
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        calls = [c for t, c in model.calls if t == 'agent_analyze']
        assert len(calls) > 1
        assert all(len(json.dumps(c['evidence'], ensure_ascii=False)) <= 12000 for c in calls)
        seen = [e['id'] for c in calls for e in c['evidence'] if e['source_id'] == source['id']]
        expected = [c['id'] for c in client.get('/api/sources/' + source['id']).json()['chunks']]
        assert seen == expected
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert set(expected) <= {r for i in artifact['items'] for r in i['refs']}


def test_agent_can_search_then_read_relevant_document_content(tmp_path):
    class ToolModel(AgentModel):
        async def generate(self, task, context):
            if task == 'query':
                self.calls.append((task, copy.deepcopy(context)))
                return {'answer': '退款窗口为30天', 'refs': [e['id'] for e in context['evidence'] if '退款' in e.get('text', '')]}
            result = await super().generate(task, context)
            if task == 'agent_plan':
                observation = context.get('document_observation')
                if not observation:
                    result.update(next_action='search_documents', tool_arguments={'query': '退款窗口'})
                elif observation['tool'] == 'search_documents':
                    result.update(next_action='read_document', tool_arguments={'refs': [m['id'] for m in observation['result']['matches']]})
                else:
                    result.update(next_action='query')
            return result
    model = ToolModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Refund', 'role': 'primary', 'text': '退款窗口为30天。'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='query', content='退款窗口是多少？'))
        assert run['status'] == 'completed', run
        context = next(c for t, c in model.calls if t == 'query')
        assert any('退款' in e['text'] for e in context['evidence'])
        assert len(context['evidence']) == 1
        assert any('读取' in i['summary'] for i in run['agent']['insights'])


class RecoveringBatchModel(AgentModel):
    def __init__(self):
        super().__init__()
        self.fail = True

    async def generate(self, task, context):
        if task == 'agent_integrate':
            self.calls.append((task, copy.deepcopy(context)))
            return {'edges': [], 'limitations': []}
        if task == 'agent_analyze' and context.get('batch_index') == 1 and self.fail:
            self.calls.append((task, copy.deepcopy(context)))
            error = DomainError('Controlled batch failure')
            error.retryable = False
            raise error
        return await super().generate(task, context)


def test_restart_retry_reuses_completed_document_batches(tmp_path):
    model = RecoveringBatchModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        text = '\n\n'.join(f'Rule {i}: ' + 'Reject invalid credentials. ' * 50 for i in range(20))
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Rules', 'role': 'primary', 'text': text}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'failed'
        assert run['agent']['document_progress']['completed_batches'] == 1
    model.fail = False
    with TestClient(create_app(tmp_path, model)) as client:
        client.post('/api/runs/' + run['id'] + '/retry').raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert len([c for t, c in model.calls if t == 'agent_analyze' and c['batch_index'] == 0]) == 1
        assert any('复用' in i['summary'] for i in run['agent']['insights'])


def test_semantic_missing_evidence_is_completed_without_rewriting_existing_requirement(tmp_path):
    class CompletionModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_complete_analysis':
                self.calls.append((task, copy.deepcopy(context)))
                return {'items': [{'id': 'R2', 'title': 'Lockout', 'description': 'Suspend accounts after five failures.', 'refs': [e['id'] for e in context['evidence']]}], 'evidence_review': [], 'nodes': [], 'edges': []}
            result = await super().generate(task, context)
            if task == 'agent_analyze':
                result['items'][0]['refs'] = result['items'][0]['refs'][:1]
            return result
    model = CompletionModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        source = client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Lockout', 'role': 'supplement', 'text': 'Suspend accounts after five failures.'}).json()
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert [i['id'] for i in artifact['items']] == ['R1', 'R2']
        call = next(c for t, c in model.calls if t == 'agent_complete_analysis')
        assert {e['source_id'] for e in call['evidence']} == {source['id']}
        assert not any(t == 'agent_repair' for t, _ in model.calls)


def test_document_heading_is_explicitly_reviewed_without_inventing_a_requirement(tmp_path):
    class HeadingModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_analyze':
                headers = [e for e in context['evidence'] if e['text'] == '目录']
                result['items'][0]['refs'] = [e['id'] for e in context['evidence'] if e not in headers]
                result['report']['evidence_review'] = [{'ref': e['id'], 'classification': 'non_requirement', 'reason': '目录标题，没有业务规则。'} for e in headers]
            return result
    with TestClient(create_app(tmp_path, HeadingModel())) as client:
        _, chat, _ = setup_chat(client)
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Heading', 'role': 'primary', 'text': '目录'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', confirm_strategy=False))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert len(artifact['items']) == 1
        assert artifact['report']['evidence_review'][0]['reason']


def test_long_inline_document_is_not_duplicated_in_planner_or_analysis_context(tmp_path):
    model = RecoveringBatchModel()
    model.fail = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        text = '\n\n'.join(f'Rule {i}: ' + 'Authentication accepts valid credentials. ' * 30 for i in range(20))
        run = until(client, start(client, chat, experience='agent', intent='review_requirement', content=text, as_requirement=True, confirm_strategy=False))
        assert run['status'] == 'completed', run
        for task, context in model.calls:
            if task in ('agent_plan', 'agent_analyze'):
                assert len(context['request']['content']) < 2500
                assert all(len(m['content']) < 1000 for m in context['conversation'])
                assert len(json.dumps(context, ensure_ascii=False)) < 22000


def test_large_design_batches_generation_and_preserves_tail_requirement_coverage(tmp_path):
    class FullDesignModel(RecoveringBatchModel):
        async def generate(self, task, context):
            if task in ('agent_scenarios', 'agent_cases'):
                self.calls.append((task, copy.deepcopy(context)))
                if task == 'agent_scenarios':
                    items = [{'id': 'S_' + r['id'], 'title': r['title'], 'description': r['description'], 'priority': 'P1', 'refs': r['refs'],
                        'requirement_ids': [r['id']], 'branch_ids': [e['id'] for e in context['business_model']['edges'] if set(e['refs']) & set(r['refs'])]} for r in context['analysis']]
                else:
                    items = [{'id': 'C_' + s['id'], 'title': s['title'], 'scenario_id': s['id'], 'type': 'Business', 'priority': 'P1', 'preconditions': '',
                        'steps': [{'action': 'Submit credentials', 'expected': 'Enforce the supplied rule'}], 'refs': s['refs'],
                        'requirement_ids': s['requirement_ids'], 'branch_ids': s['branch_ids']} for s in context['scenarios']]
                return {'items': items, 'has_more': False}
            result = await super().generate(task, context)
            if task == 'agent_analyze':
                result['items'] = [{'id': 'R' + str(i), 'title': e['text'][:40], 'description': e['text'], 'refs': [e['id']]} for i, e in enumerate(context['evidence'])]
            return result
    model = FullDesignModel()
    model.fail = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        text = '\n\n'.join(f'Rule {i}: ' + 'Authentication accepts valid credentials. ' * 40 for i in range(36))
        source = client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Large rules', 'role': 'primary', 'text': text}).json()
        run = until(client, start(client, chat, experience='agent', intent='generate_case', confirm_strategy=False))
        assert run['status'] == 'completed', run
        generated = [(t, c) for t, c in model.calls if t in ('agent_scenarios', 'agent_cases')]
        assert all(len(json.dumps(c, ensure_ascii=False)) < 40000 for t, c in generated)
        assert len([c for t, c in generated if t == 'agent_scenarios']) > 1
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        tail = client.get('/api/sources/' + source['id']).json()['chunks'][-1]['id']
        assert tail in {r for i in artifact['items'] for r in i['refs']}
        assert not artifact['report']['coverage']['gaps']
