import asyncio
import copy
import importlib

import pytest
from tcg.context_budget import request_budget
from tcg.graph import Engine
from tcg.model import Settings
from tcg.storage import Store, dump, now


class FakeModel:
    def __init__(self):
        self.calls = []
        self.bad_refs = False

    async def generate(self, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        assert task == 'dialogue'
        refs = [e['id'] for e in context.get('evidence', [])]
        if context.get('dialogue_phase') == 'summary':
            refs = list(dict.fromkeys(r for answer in context['answers'] for r in answer['refs']))
        return {'answer': '根据提供的内容解释；未执行测试。', 'refs': ['invented'] if self.bad_refs else refs}


def helper():
    try:
        return importlib.import_module('tcg.dialogue_context').answer_dialogue
    except ModuleNotFoundError:
        pytest.fail('Bounded dialogue helper is missing')


def setup(tmp_path, chunks):
    store = Store(tmp_path)
    project = store.create_project('Dialogue')
    chat = store.create_chat(project['id'], 'Read only')
    source = store.add_source(chat['id'], 'requirements', 'primary', '\n'.join(chunks),
        [{'text': text, 'location': str(index)} for index, text in enumerate(chunks)])
    settings = Settings(tmp_path)
    settings.save({'provider': 'openai', 'base_url': 'http://local', 'model': 'fake', 'timeout_seconds': 30,
                   'context_window': 8192, 'output_tokens': 1024})
    model = FakeModel()
    engine = Engine(store, model, settings)
    _, run = store.create_run(chat['id'], {'content': '总结所有需求', 'intent': 'query', 'mode': 'hitp', 'source_ids': [source['id']]})
    store.update_run(run['id'], status='waiting', stage='query', stop_after='scenarios',
                     interrupt={'type': 'scenario_review', 'questions': ['LARGE' * 20000], 'message': '请确认'})
    return store, engine, model, store.run(run['id']), source


def test_source_dialogue_batches_and_summary_has_no_documents_or_artifacts(tmp_path):
    answer_dialogue = helper()
    store, engine, model, run, source = setup(tmp_path, ['订单支付规则 ' + '中' * 1700 for _ in range(5)])
    before = store.run(run['id'])
    result = asyncio.run(answer_dialogue(engine, run['id'], pending=run['interrupt']))
    batches = [c for _, c in model.calls if c.get('dialogue_phase') != 'summary']
    summaries = [c for _, c in model.calls if c.get('dialogue_phase') == 'summary']
    assert len(batches) > 1 and summaries
    assert sum(len(c['evidence']) for c in batches) == 5
    assert all(c['request'] == {'content': '总结所有需求', 'intent': 'query', 'mode': 'hitp'} for _, c in model.calls)
    assert all(request_budget(tmp_path, 'dialogue', c, engine.settings)['fits'] for _, c in model.calls)
    assert all(not c.get('evidence') and not c.get('artifact') and '中' * 100 not in str(c) for c in summaries)
    assert 'LARGE' not in str(model.calls)
    assert result['coverage']['included_evidence_count'] == 5
    assert not result['coverage']['partial']
    after = store.run(run['id'])
    assert (after['status'], after['stop_after'], after['interrupt']) == (before['status'], before['stop_after'], before['interrupt'])
    assert store.list('artifact', chat_id=run['chat_id']) == []


def test_selected_artifact_dialogue_preserves_historical_rows_and_refs(tmp_path):
    answer_dialogue = helper()
    store, engine, model, run, source = setup(tmp_path, ['订单应当支付成功', '无关的通知规则'])
    refs = [e['id'] for e in store.evidence([source['id']])]
    scope = {'project_id': run['project_id'], 'chat_id': run['chat_id'], '_source_ids': [source['id']], '_profile': run['_profile']}
    scenario = {**scope, 'id': 's', 'revision': 1, 'type': 'scenarios', 'items': [{'id': 'S1', 'description': '历史支付场景', 'refs': refs[:1]}]}
    artifact = {**scope, 'id': 'c', 'revision': 1, 'type': 'cases', 'items': [
        {'id': 'C1', 'scenario_id': 'S1', 'title': '支付', 'refs': refs[:1]},
        {'id': 'C2', 'title': 'UNRELATED' * 20000, 'refs': refs[1:]}],
        'report': {'diagrams': ['BULKY' * 20000], 'lineage': {'scenario_artifact_id': 's', 'scenario_revision': 1}}}
    for value in (scenario, artifact):
        store.put('artifact', value)
        store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (value['id'], 1, dump(value), now(), 'generated', '{}'))
    latest = {**scenario, 'revision': 2, 'items': [{'id': 'S1', 'description': '最新场景'}]}
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', ('s', 2, dump(latest), now(), 'edit', '{}'))
    store.put('artifact', latest)
    store.update_run(run['id'], _artifact_snapshot=artifact, _request={**run['_request'], 'selected_ids': ['C1']})
    result = asyncio.run(answer_dialogue(engine, run['id'], content='解释选中的支付用例', pending={'artifact_id': 'c', 'type': 'case_review'}))
    context = model.calls[0][1]
    assert context['artifact']['items'][0]['id'] == 'C1'
    assert context['scenarios'][0]['description'] == '历史支付场景'
    assert [e['id'] for e in context['evidence']] == refs[:1]
    assert 'UNRELATED' not in str(model.calls) and 'BULKY' not in str(model.calls)
    assert result['refs'] == refs[:1]


def test_artifact_dialogue_batches_capacity_errors_from_shared_context_builder(tmp_path):
    answer_dialogue = helper()
    store, engine, model, run, source = setup(tmp_path, ['业务规则' + '中' * 1600 for _ in range(4)])
    evidence = store.evidence([source['id']])
    artifact = {'id': 'analysis', 'type': 'analysis', 'revision': 1, 'chat_id': run['chat_id'], 'project_id': run['project_id'],
        '_source_ids': [source['id']], '_profile': run['_profile'],
        'items': [{'id': 'R' + str(i), 'description': '业务规则', 'refs': [e['id']]} for i, e in enumerate(evidence)], 'report': {}}
    store.put('artifact', artifact)
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (artifact['id'], 1, dump(artifact), now(), 'generated', '{}'))
    store.update_run(run['id'], _artifact_snapshot=artifact)
    result = asyncio.run(answer_dialogue(engine, run['id']))
    assert result['coverage']['batch_count'] > 1
    assert result['coverage']['included_evidence_count'] == 4
    assert all(request_budget(tmp_path, 'dialogue', c, engine.settings)['fits'] for _, c in model.calls)


def test_irrelevant_sources_are_disclosed_and_invalid_refs_rejected(tmp_path):
    from tcg.schemas import DomainError
    answer_dialogue = helper()
    store, engine, model, run, source = setup(tmp_path, ['订单支付需要有效账户'])
    result = asyncio.run(answer_dialogue(engine, run['id'], content='怎么保存成果？'))
    assert model.calls[0][1]['evidence'] == []
    assert result['coverage']['partial']
    assert result['coverage']['included_evidence_count'] == 0
    model.bad_refs = True
    with pytest.raises(DomainError):
        asyncio.run(answer_dialogue(engine, run['id'], content='怎么保存成果？'))
    assert store.run(run['id'])['status'] == 'waiting'
