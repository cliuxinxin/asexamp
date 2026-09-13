import asyncio
import importlib
import json

import httpx
import pytest
from tcg.model import LangChainGateway, Settings, SYSTEM, TASK_INSTRUCTIONS
from tcg.schemas import DomainError


def module(name):
    try:
        return importlib.import_module('tcg.' + name)
    except ModuleNotFoundError:
        pytest.fail('Shared framework module is missing: ' + name)


def test_zero_window_is_conservative_and_final_contract_is_counted(tmp_path, monkeypatch):
    budget = module('context_budget')
    monkeypatch.setenv('TCG_MODEL_CONTEXT_TOKENS', '0')
    result = budget.request_budget(tmp_path, 'query', {'text': '中' * 16000})
    assert result['window'] == 32768
    assert result['output_tokens'] == 8192
    assert not result['fits']
    assert result['input_tokens'] > 32000
    assert result['count_method'] == 'conservative_estimate'


def test_settings_validate_server_limit_and_gateway_admits_before_transport(tmp_path):
    settings = Settings(tmp_path)
    with pytest.raises(DomainError):
        settings.save({'provider': 'openai', 'base_url': 'http://model', 'model': 'custom',
                       'timeout_seconds': 30, 'output_limit_mode': 'server'})
    settings.save({'provider': 'openai', 'base_url': 'http://model', 'model': 'custom',
                   'timeout_seconds': 30, 'context_window': 16384, 'output_tokens': 8192})
    requests, records = [], []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"ok","refs":[]}'}}]})
    gateway = LangChainGateway(settings, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    gateway.request_recorder = records.append
    with pytest.raises(DomainError):
        asyncio.run(gateway.generate('query', {'text': '中' * 10000}))
    assert requests == []
    asyncio.run(gateway.generate('query', {}))
    assert json.loads(requests[0].content)['max_tokens'] == 8192
    assert requests[0].url.path == '/api/v1/chat/completions'
    assert records[-1]['budget']['fits']
    assert len(records[-1]['request_digest']) == 64


def test_rule_projection_discards_unrelated_report_and_keeps_provenance():
    service = module('context_service')
    report = {'diagrams': ['BULK' * 10000], 'requirement_map': {'rules': [
        {'id': 'rule-a', 'text': 'Amount must be positive', 'refs': ['e1']},
        {'id': 'rule-b', 'text': 'Unrelated rule', 'refs': ['e2']}]}}
    projected = service.rule_projection(report, [{'id': 'R1', 'refs': ['e1']}])
    assert [r['id'] for r in projected['rules']] == ['rule-a']
    assert projected['rules'][0]['refs'] == ['e1']
    assert 'BULK' not in json.dumps(projected)
    assert projected['coverage']['omitted_rule_ids'] == ['rule-b']


def test_context_uses_historical_ancestors_and_discloses_selected_source_coverage(tmp_path):
    service = module('context_service')
    from tcg.storage import Store, dump, now
    store = Store(tmp_path)
    scope = {'chat_id': 'chat', 'project_id': 'project', '_source_ids': [], '_profile': {'case_level': 'deep'}}
    analysis = {**scope, 'id': 'a', 'type': 'analysis', 'revision': 1, 'items': [
        {'id': 'R1', 'description': 'Historical requirement', 'refs': ['e1']},
        {'id': 'R2', 'description': 'Unrelated', 'refs': ['e2']}],
        'report': {'diagrams': ['BULKY' * 10000], 'requirement_map': {'rules': [{'id': 'rule1', 'text': 'Positive', 'refs': ['e1']}]}}}
    scenario = {**scope, 'id': 's', 'type': 'scenarios', 'revision': 1,
        'items': [{'id': 'S1', 'requirement_ids': ['R1'], 'description': 'Historical scenario', 'refs': ['e1']}],
        'report': {'lineage': {'analysis_artifact_id': 'a', 'analysis_revision': 1}}}
    case = {**scope, 'id': 'c', 'type': 'cases', 'revision': 1,
        'items': [{'id': 'C1', 'scenario_id': 'S1', 'refs': ['e1']}],
        'report': {'lineage': {'scenario_artifact_id': 's', 'scenario_revision': 1}}}
    for artifact in (analysis, scenario, case):
        store.put('artifact', artifact)
        store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', (artifact['id'], 1, dump(artifact), now(), 'generated', '{}'))
    current_scenario = {**scenario, 'revision': 2, 'items': [{'id': 'S1', 'description': 'Changed current scenario'}]}
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', ('s', 2, dump(current_scenario), now(), 'edit', '{}'))
    store.put('artifact', current_scenario)
    evidence = [{'id': 'e1', 'source_id': 'source1', 'role': 'requirement', 'text': 'Amount positive'},
                {'id': 'e2', 'source_id': 'new', 'role': 'requirement', 'text': 'Completely unrelated'},
                {'id': 'e3', 'source_id': 'new', 'role': 'requirement', 'text': 'Another unrelated fact'}]
    # Persist source bodies for consumed-source digesting without needing a Run.
    for sid in ('source1', 'new'):
        store.put('source', {'id': sid, 'chat_id': 'chat', 'project_id': 'project', '_text': sid, 'role': 'requirement'})
    pack = service.artifact_context(store, 'artifact_explain', case, case['items'], evidence, explicit_source_ids=['new'])
    assert pack['scenarios'][0]['description'] == 'Historical scenario'
    assert [r['id'] for r in pack['analysis']] == ['R1']
    assert pack['global_rules']['rules'][0]['id'] == 'rule1'
    assert [e['id'] for e in pack['evidence']] == ['e1']
    assert pack['coverage']['omitted_evidence_ids'] == ['e2', 'e3']
    assert pack['coverage']['partial'] is True
    assert 'BULKY' not in json.dumps(pack)
    case['items'][0]['id'] = 'CHANGED'
    assert pack['artifact']['items'][0]['id'] == 'C1'
    draft = service.artifact_context(store, 'artifact_sync', store.get('artifact', 'c'),
        store.get('artifact', 'c')['items'], evidence,
        extra={'scenarios': [{'id': 'S1', 'description': 'Proposed new scenario', 'refs': ['e2']}],
               'deletion_basis': [{'id': 'removed', 'refs': ['e3']}]})
    assert draft['scenarios'][0]['description'] == 'Proposed new scenario'
    assert {e['id'] for e in draft['evidence']} == {'e1', 'e2', 'e3'}
    assert draft['historical_ancestors']['scenarios'][0]['description'] == 'Historical scenario'
    estimate = service.artifact_context(store, 'artifact_estimate', scenario, scenario['items'], evidence)
    assert 'evidence' not in estimate and 'analysis' not in estimate
    assert estimate['scenarios'] == scenario['items']
    assert estimate['profile']['case_level'] == 'deep'
    historical = store.get('artifact', 'c')
    historical['_source_ids'] = ['source1']
    historical['_source_roles'] = {'source1': 'clarification'}
    clarification = [{'id': 'override', 'source_id': 'source1', 'role': 'requirement', 'text': '30改60'}]
    clarified = service.artifact_context(store, 'artifact_explain', historical, historical['items'], evidence + clarification)
    assert next(e for e in clarified['evidence'] if e['id'] == 'override')['role'] == 'clarification'


def test_analysis_signature_includes_source_content_roles_and_semantic_scope(tmp_path):
    service = module('context_service')
    from tcg.storage import Store
    store = Store(tmp_path)
    store.put('source', {'id': 's', '_text': 'old'})
    first = service.analysis_signature(store, ['s'], {'s': 'requirement'}, {'scope': 'A', 'additional_rules': 'strict'})
    assert first != service.analysis_signature(store, ['s'], {'s': 'change'}, {'scope': 'A', 'additional_rules': 'strict'})
    assert first != service.analysis_signature(store, ['s'], {'s': 'requirement'}, {'scope': 'B', 'additional_rules': 'strict'})
    store.put('source', {'id': 's', '_text': 'new'})
    assert first != service.analysis_signature(store, ['s'], {'s': 'requirement'}, {'scope': 'A', 'additional_rules': 'strict'})


def test_cross_partition_rules_expand_relevant_requirement_dependencies():
    service = module('context_service')
    report = {'relationships': [{'id': 'dep', 'type': 'dependency', 'text': 'R1 needs R2', 'requirement_ids': ['R1', 'R2'], 'refs': ['e2']}],
              'global_rules': [{'id': 'global', 'text': 'Every change audited', 'refs': ['e3']}],
              'conflicts': [{'id': 'conflict', 'text': 'Conflicting limits', 'requirement_ids': ['R1'], 'refs': ['e1'], 'status': 'unresolved'}]}
    projection = service.rule_projection(report, [{'id': 'R1', 'refs': ['e1']}])
    assert {r['id'] for r in projection['rules']} == {'dep', 'global', 'conflict'}


def test_applicable_rule_is_never_silently_dropped_for_projection_size():
    service = module('context_service')
    rules = [{'id': str(i), 'text': 'Required rule', 'refs': ['different']} for i in range(40)]
    projection = service.rule_projection({'requirement_map': {'global_rules': rules}}, [{'id': 'R1', 'refs': ['e']}])
    assert len(projection['rules']) == 40
    assert not projection['coverage']['partial']


def test_server_output_limit_is_accounted_without_request_parameter(tmp_path):
    budget = module('context_budget')
    settings = Settings(tmp_path)
    settings.save({'provider': 'openai', 'base_url': 'http://custom/api/v1', 'model': 'local', 'timeout_seconds': 30,
                   'output_limit_mode': 'server', 'server_output_tokens': 2048})
    result = budget.request_budget(tmp_path, 'query', {}, settings)
    assert result['output_tokens'] == 2048
    captured = []
    def transport(request):
        captured.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"ok"}'}}]})
    gateway = LangChainGateway(settings, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    asyncio.run(gateway.generate('query', {}))
    assert set(json.loads(captured[0].content)) == {'model', 'messages'}
    assert captured[0].url.path == '/api/v1/chat/completions'


def test_optional_chinese_retrieval_matches_related_phrases():
    service = module('context_service')
    assert service._terms('订单支付失败处理') & service._terms('支付失败后保留订单')


def test_per_item_parent_versions_are_preserved_in_pack_and_manifest(tmp_path):
    service = module('context_service')
    from tcg.storage import Store, dump, now
    store = Store(tmp_path)
    scope = {'chat_id': 'chat', 'project_id': 'project', '_source_ids': [], '_profile': {}}
    for revision, title in ((1, 'old'), (2, 'new')):
        scenario = {**scope, 'id': 's', 'type': 'scenarios', 'revision': revision,
            'items': [{'id': 'S1', 'title': title}, {'id': 'S2', 'title': title}], 'report': {}}
        store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', ('s', revision, dump(scenario), now(), 'edit', '{}'))
        store.put('artifact', scenario)
    case = {**scope, 'id': 'c', 'type': 'cases', 'revision': 1,
        'items': [{'id': 'C1', 'scenario_id': 'S1'}, {'id': 'C2', 'scenario_id': 'S2'}],
        'report': {'lineage': {'scenario_artifact_id': 's', 'scenario_revision': 2, 'scenario_revisions': {'S1': 1, 'S2': 2}}}}
    store.put('artifact', case)
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)', ('c', 1, dump(case), now(), 'generated', '{}'))
    pack = service.artifact_context(store, 'artifact_explain', case, case['items'], [])
    assert {r['id']: r['title'] for r in pack['scenarios']} == {'S1': 'old', 'S2': 'new'}
    assert {(a['id'], a['revision']) for a in pack['dependency_manifest']['artifacts']} == {('c', 1), ('s', 1), ('s', 2)}


def test_historical_evidence_pack_never_uses_current_text(tmp_path):
    from tcg.storage import Store
    from tcg.context_service import artifact_context
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '历史证据')
    source = store.add_source(chat['id'], '需求', 'primary', '旧规则', [{'text': '旧规则', 'location': 'P1'}])
    _, run = store.create_run(chat['id'], {'intent': 'auto', 'mode': 'auto', 'content': '生成'})
    artifact = store.artifact(run['id'], 'analysis', 'analysis', '需求', [
        {'id': 'R1', 'title': '规则', 'description': '旧规则', 'refs': [source['id'] + '#P1']}])
    old_source = artifact['_dependencies']['sources'][0]
    chunk = store.get('chunk', source['id'] + '#P1')
    store.put('source', {**store.get('source', source['id']), '_text': '新规则'})
    store.put('chunk', {**chunk, 'text': '新规则'})
    pack = artifact_context(store, 'artifact_explain', artifact, artifact['items'], store.evidence([source['id']]))
    assert [e['text'] for e in pack['evidence']] == ['旧规则']
    assert pack['evidence'][0]['source_version'] == old_source['version']
    assert pack['dependency_manifest']['sources'] == [old_source]
    import copy
    artifact = copy.deepcopy(artifact)
    artifact['_dependencies']['sources'][0]['version'] = 999
    missing = artifact_context(store, 'artifact_explain', artifact, artifact['items'], store.evidence([source['id']]))
    assert missing['evidence'] == []
    assert missing['coverage']['missing_historical_source_versions'][0]['version'] == 999
    assert missing['coverage']['missing_referenced_evidence_ids'] == [source['id'] + '#P1']


def test_requirement_closure_reaches_transitive_rules_and_uses_real_budget(tmp_path):
    from tcg.storage import Store
    from tcg.context_service import artifact_context
    store = Store(tmp_path)
    chat = store.create_chat(store.list('project')[0]['id'], '依赖闭包')
    source = store.add_source(chat['id'], '需求', 'primary', '规则', [
        {'text': f'规则{i}', 'location': f'P{i}'} for i in range(1, 5)])
    refs = [source['id'] + f'#P{i}' for i in range(1, 5)]
    _, run = store.create_run(chat['id'], {'intent': 'auto', 'mode': 'auto', 'content': '生成'})
    requirements = [{'id': f'R{i}', 'title': f'规则{i}', 'description': f'规则{i}', 'refs': [refs[i-1]]} for i in (1, 2, 3)]
    report = {'requirement_map': {
        'relationships': [{'id': 'link12', 'requirement_ids': ['R1', 'R2'], 'refs': [refs[0]]},
                          {'id': 'link23', 'requirement_ids': ['R2', 'R3'], 'refs': [refs[1]]}],
        'rules': [{'id': 'only3', 'requirement_ids': ['R3'], 'refs': [refs[2]], 'text': '最终约束'}],
        'global_rules': [{'id': f'g{i}', 'refs': [refs[3]], 'text': '所有场景需要审计'} for i in range(30)]}}
    analysis = store.artifact(run['id'], 'analysis', 'analysis', '需求', requirements, report)
    store.cache_set(run['id'], 'analysis_artifact', {'id': analysis['id'], 'revision': 1})
    scenario = store.artifact(run['id'], 'scenarios', 'scenarios', '场景', [
        {'id': 'S1', 'title': '场景', 'description': '验证', 'priority': 'P1', 'requirement_ids': ['R1'], 'refs': [refs[0]]}])
    pack = artifact_context(store, 'artifact_explain', scenario, scenario['items'], store.evidence([source['id']]))
    assert {r['id'] for r in pack['analysis']} == {'R1', 'R2', 'R3'}
    assert len(pack['global_rules']['rules']) == 33
    assert {e['id'] for e in pack['evidence']} == set(refs)
    huge_report = {'global_rules': [{'id': 'huge', 'text': '约束' * 20000, 'refs': [refs[0]]}]}
    large = store.annotate_artifact(scenario['id'], 1, {'report': huge_report})
    with pytest.raises(DomainError, match='上下文容量'):
        artifact_context(store, 'artifact_explain', large, large['items'], store.evidence([source['id']]))


def test_candidate_context_allows_artifact_and_workflow_capacity_grouping(tmp_path):
    from tcg.artifact_actions import bounded_groups
    from tcg.context_budget import request_budget
    from tcg.context_service import artifact_context
    from tcg.storage import Store
    from tcg.workflow import WorkflowEngine
    store = Store(tmp_path)
    chat = store.create_chat(store.list('project')[0]['id'], '容量分组')
    source = store.add_source(chat['id'], '需求', 'primary', '需求原文', [
        {'text': '甲' * 5000, 'location': 'P1'}, {'text': '乙' * 5000, 'location': 'P2'}])
    _, run = store.create_run(chat['id'], {'intent': 'generate_case', 'mode': 'auto', 'content': '生成用例', 'experience': 'reliable'})
    rows = [{'id': 'S' + str(i), 'title': '场景', 'description': '中' * 1000, 'priority': 'P1',
             'refs': [source['id'] + '#P' + str(i)]} for i in (1, 2)]
    artifact = store.artifact(run['id'], 'capacity-scenarios', 'scenarios', '场景', rows)
    store.cache_set(run['id'], 'scenarios_artifact', {'id': artifact['id'], 'revision': artifact['revision']})
    evidence = store.evidence([source['id']])
    from types import SimpleNamespace
    engine = WorkflowEngine(store, SimpleNamespace(), Settings(tmp_path))
    def build(selected):
        return artifact_context(store, 'artifact_explain', artifact, selected, evidence, admit=False)
    single = build(rows[:1])
    combined = build(rows)
    assert request_budget(tmp_path, 'artifact_explain', single)['fits']
    assert not request_budget(tmp_path, 'artifact_explain', combined)['fits']
    assert {e['id'] for e in combined['evidence']} == {e['id'] for e in evidence}
    assert [len(group) for group in bounded_groups(engine, 'artifact_explain', rows, build)] == [1, 1]
    # A mandatory singleton must still fail at admitting construction or grouping.
    huge_rows = [{**rows[0], 'description': '大' * 20000}]
    huge = store.artifact(run['id'], 'oversized-scenarios', 'scenarios', '超大场景', huge_rows)
    def huge_candidate(selected):
        return artifact_context(store, 'artifact_explain', huge, selected, evidence, admit=False)
    assert huge_candidate(huge_rows)['artifact']['items'][0]['description'] == '大' * 20000
    with pytest.raises(DomainError, match='上下文容量'):
        artifact_context(store, 'artifact_explain', huge, huge_rows, evidence)
    with pytest.raises(DomainError, match='模型容量'):
        bounded_groups(engine, 'artifact_explain', huge_rows, huge_candidate)
    with pytest.raises(DomainError, match='模型容量'):
        engine.groups(run['id'], huge_rows, 'scenarios', context_builder=huge_candidate)
    assert [len(group) for group in engine.groups(run['id'], rows, 'scenarios')] == [1, 1]
