"""Large read results must not trap the durable work session above its budget."""
import copy
import json
import pytest

from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.schemas import DomainError
from test_backend_api import setup_chat, start, until
from test_incremental_agent import WorkModel


class ManyReads(WorkModel):
    def __init__(self, offline=False):
        super().__init__()
        self.read_round = 0
        self.offline = offline

    async def generate(self, task, context):
        assert len(json.dumps(context, ensure_ascii=False)) <= 16000
        if task == 'work_analyze' and self.read_round < 2:
            self.calls.append((task, copy.deepcopy(context)))
            source = context['evidence'][0]['source_id']
            if self.read_round == 0:
                requests = [{'tool': 'read_evidence', 'refs': [f'{source}#P{i}' for i in range(a, a + 7)]}
                            for a in (10, 20)]
            else:
                requests = [{'tool': 'search_evidence', 'query': q}
                            for q in ('Agency access', 'whitelist override', 'Roleplay permissions')]
            self.read_round += 1
            return {'kind': 'need_context', 'requests': requests, 'summary': '读取当前规则的相关细节'}
        if task == 'work_analyze' and self.offline:
            error = DomainError('Controlled outage after context assembly')
            error.retryable = False
            raise error
        value = await super().generate(task, context)
        if task == 'work_analyze':
            # Keep this provider fixture's derived rules small; source bodies stay intact.
            for item in value['items']:
                item['description'] = 'Agency access follows the explicitly supplied whitelist rules.'
        return value


def long_source():
    sentence = 'Agency access whitelist override Roleplay permissions require explicit role validation. '
    return '\n\n'.join(f'Section {i}: ' + sentence * 5 for i in range(1, 29))


def test_read_then_three_searches_stays_bounded_and_publishes(tmp_path):
    model = ManyReads()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text=long_source())
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        assert run['artifact_ids']
        assert any(c.get('context_window') for t, c in model.calls if t == 'work_analyze')
        assert any('预算' in i['summary'] for i in run['agent']['insights'])


def test_retry_after_restart_reassembles_saved_large_read_results(tmp_path):
    model = ManyReads(offline=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text=long_source())
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed'
    model.offline = False
    with TestClient(create_app(tmp_path, model)) as client:
        client.post('/api/runs/' + run['id'] + '/retry').raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error')
        assert model.read_round == 2


def test_paged_reads_preserve_job_inputs_and_can_retrieve_all_omitted_rows():
    from tcg.incremental_context import fit_context, result_page
    base = {'goal': '核查当前要求', 'profile': {'additional_rules': '保留每项业务约束'},
            'evidence': [{'id': 'S#P1', 'text': '原始规则' * 200, 'role': 'primary'}],
            'analysis': [{'id': 'R1', 'description': '不得改写已确认规则'}]}
    result = {'matches': [{'id': f'S#P{i}', 'text': '定位片段' * 80} for i in range(2, 30)],
              'query': 'Agency access', 'total_matches': 28}
    extras = [{'id': 'S#P1', 'text': '段落末尾', 'role': 'primary',
               'excerpt': {'start': 800, 'end': 804, 'total': 804}},
              {'id': 'S#P2', 'text': '完整补充规则' * 300, 'role': 'primary'}]
    session = {'evidence': extras, 'observations': [{'tool': 'search_evidence', 'result': result}]}
    before = copy.deepcopy(base)
    context = fit_context(base, session, 4300)
    assert len(json.dumps(context, ensure_ascii=False)) <= 4300
    assert base == before
    assert context['analysis'] == base['analysis']
    assert context['profile'] == base['profile']
    assert context['evidence'][0] == base['evidence'][0]
    page = context['observations'][0]['result']
    received = page['matches'][:]
    while page['page']['next_cursor'] is not None:
        page = result_page(session, page['page']['result_id'], page['page']['next_cursor'], budget=2000)
        received.extend(page['matches'])
    assert received == result['matches']
    assert session['evidence'] == extras
    with pytest.raises(DomainError):
        result_page(session, 'result-from-another-work-item')


def test_cached_tool_result_pages_can_be_read_through_the_graph(tmp_path, monkeypatch):
    from tcg.incremental_agent import IncrementalAgent
    monkeypatch.setattr(IncrementalAgent, 'CONTEXT_BUDGET', 8000)

    class PageQuery(WorkModel):
        async def generate(self, task, context):
            self.calls.append((task, copy.deepcopy(context)))
            assert len(json.dumps(context, ensure_ascii=False)) <= 8000
            observations = context.get('observations', [])
            if not observations:
                requests = [{'tool': 'search_evidence', 'query': q}
                            for q in ('Agency access', 'whitelist override', 'Roleplay permissions')]
            elif len(observations) == 3:
                page = next(o['result']['page'] for o in observations if o['result']['page']['next_cursor'] is not None)
                requests = [{'tool': 'read_tool_result', 'result_id': page['result_id'], 'cursor': page['next_cursor']}]
            elif observations[0]['tool'] == 'search_evidence':
                match = observations[0]['result']['matches'][0]
                requests = [{'tool': 'read_evidence', 'refs': [match['id']]}]
            else:
                return {'kind': 'answer', 'answer': '需要明确校验角色权限。', 'refs': [context['evidence'][0]['id']]}
            return {'kind': 'need_context', 'requests': requests, 'summary': '读取定位结果的下一页'}

    model = PageQuery()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text=long_source())
        run = until(client, start(client, chat, intent='query', experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        assert len(model.calls) == 4


def test_repacking_a_continuation_keeps_the_original_result_cursor():
    from tcg.incremental_context import archive_result, fit_context, result_page
    matches = [{'id': f'R{i}', 'text': '完整搜索片段' * 60} for i in range(20)]
    session = {'evidence': [], 'observations': []}
    result_id = archive_result(session, 'search_evidence', {'matches': matches})
    page = result_page(session, result_id, cursor=3, budget=3500)
    session['observations'] = [{'tool': 'search_evidence', 'result': page}]
    context = fit_context({'goal': 'g' * 2000, 'evidence': []}, session, 4200)
    current = context['observations'][0]['result']
    assert current['page']['result_id'] == result_id
    assert current['page']['cursor'] == 3
    received = current['matches'][:]
    while current['page']['next_cursor'] is not None:
        current = result_page(session, result_id, current['page']['next_cursor'], budget=1800)
        received.extend(current['matches'])
    assert received == matches[3:]


def test_evidence_page_receipt_keeps_its_retrieval_pointer_when_repacked():
    from tcg.incremental_context import archive_result, fit_context, result_page
    evidence = [{'id': f'S#P{i}', 'role': 'primary', 'text': '完整原文' * 150} for i in range(12)]
    session = {'evidence': evidence, 'observations': []}
    result_id = archive_result(session, 'read_evidence', {'evidence': evidence})
    page = result_page(session, result_id, 2, budget=3500)['page']
    session['observations'] = [{'tool': 'read_evidence', 'result': {'page': page, 'read_refs': ['S#P2']}}]
    context = fit_context({'goal': 'g' * 2000, 'evidence': []}, session, 4200)
    receipt = context['observations'][0]['result']
    assert receipt['page'] == page
    remainder = result_page(session, receipt['page']['result_id'], receipt['page']['next_cursor'], budget=4000)
    assert remainder['evidence'][0] == evidence[page['next_cursor']]


def test_metadata_group_can_finish_without_chasing_the_rest_of_the_document(tmp_path):
    class Metadata(WorkModel):
        async def generate(self, task, context):
            if task == 'work_analyze' and all('Document version' in e.get('text', '') for e in context['evidence']):
                self.calls.append((task, copy.deepcopy(context)))
                return {'kind': 'patch', 'items': [], 'nodes': [], 'edges': [],
                        'summary': '封面版本信息，不构成业务规则，继续处理后续分组。',
                        'evidence_review': [{'ref': e['id'], 'classification': 'context', 'reason': '文档版本'}
                                            for e in context['evidence']]}
            return await super().generate(task, context)

    model = Metadata()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, cover = setup_chat(client, text='Document version 1.0, owner and publication date.')
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={
            'name': '正文', 'role': 'primary', 'text': '用户名必须填写。'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error')
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert all(not ref.startswith(cover['id']) for item in artifact['items'] for ref in item['refs'])
