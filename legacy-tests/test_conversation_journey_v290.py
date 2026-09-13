"""Continuous v2.9 acceptance over real HTTP, SQLite and production LangGraph.

Only model routing and domain responses are controlled. Composer requests carry
natural language and visible UI context, never a command or intent selector.
No source, run, artifact, gate or revision is manufactured in the database.
"""
import copy
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from tcg.main import create_app
from test_backend_api import until
from test_change_journey_v280 import ChangeJourneyModel, state
from test_manual_journey_v271 import CONFIRMED, Journey, part, upload


AUDIT_RULE = '有效凭证登录成功时，记录包含账号和设备标识的审计记录。'
UNUSED_RULE = '导出文件的审计记录保留九十天。'
SCENE_TITLE = '使用注册账号的有效凭证完成登录'


class ConversationJourneyModel(ChangeJourneyModel):
    needs_clarification = True

    async def generate(self, task, context):
        if task == 'artifact_modify' and context['artifact']['type'] == 'scenarios':
            self.calls.append((task, copy.deepcopy(context)))
            selected = set(context['selected_ids'])
            return {'operations': [{'op': 'update', 'id': row['id'],
                'item': {'title': SCENE_TITLE}} for row in context['artifact']['items']
                if row['id'] in selected], 'summary': '只明确选中场景标题，不改变业务规则。'}
        if task == 'artifact_modify' and context['artifact']['type'] == 'cases':
            self.calls.append((task, copy.deepcopy(context)))
            evidence = context['evidence']
            assert any(AUDIT_RULE in entry['text'] for entry in evidence)
            assert not any(UNUSED_RULE in entry['text'] for entry in evidence)
            refs = [entry['id'] for entry in evidence if AUDIT_RULE in entry['text']]
            selected = set(context['selected_ids'])
            operations = []
            for row in context['artifact']['items']:
                if row['id'] in selected:
                    operations.append({'op': 'update', 'id': row['id'], 'item': {
                        'steps': row['steps'] + [{'action': '登录成功后查看本次登录的审计记录',
                            'expected': '记录包含本次登录账号和设备标识'}],
                        'refs': list(dict.fromkeys(row['refs'] + refs))}})
            return {'operations': operations, 'summary': '仅为所选用例增加新资料要求的审计检查。'}
        if task == 'artifact_explain':
            self.calls.append((task, copy.deepcopy(context)))
            rows = context['artifact']['items']
            refs = [entry['id'] for entry in context.get('evidence', []) if entry.get('role') != 'example']
            return {'answer': '当前用例设计尚未执行测试：' + '；'.join(
                row['id'] + '：' + row['title'] + '，' + '；'.join(
                    step['expected'] for step in row.get('steps', [])) for row in rows), 'refs': refs}
        return await super().generate(task, context)


@pytest.fixture
def conversation_journey(tmp_path):
    model = ConversationJourneyModel()
    app = create_app(tmp_path, model)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with httpx.Client(base_url='http://127.0.0.1:' + str(sock.getsockname()[1]), timeout=30) as client:
            yield Journey((client, app, model))
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def add_text(journey, name, text):
    response = journey.client.post('/api/chats/' + journey.chat['id'] + '/sources/text',
        json={'name': name, 'text': text, 'role': 'change'})
    assert response.status_code == 200, response.text
    return response.json()


def test_conversation_from_shared_clarification_to_targeted_case_review(conversation_journey):
    j = conversation_journey
    source = upload(j, 'login-requirements.md')
    started = j.turn('根据这份需求生成场景和用例，每个阶段都等我人工确认。',
        'workflow.start', {'stop_after': 'complete'})
    run, analysis = j.gate(started['actions'][0]['result']['run'], 'clarification')
    rid = run['id']
    original_analysis = copy.deepcopy(analysis)
    draft = j.client.get('/api/runs/' + rid + '/clarification-draft').json()
    assert draft['questions'] and not draft['submitted']
    assert j.counts() == {'analyze_requirement': 1}

    # A current answer is adopted, shared and applied in one conversation turn.
    adopted = j.turn('我确认锁定十分钟，期满自动解锁。采用这个答案，先不要生成场景。',
        'clarification.adopt', {'run_id': rid, 'expected_revision': draft['revision'],
            'answers': {draft['questions'][0]['id']: CONFIRMED},
            'submit': True, 'save_to_project': True})
    submitted = part(adopted, 'clarification_draft')['draft']
    assert submitted['submitted'] and submitted['shared']
    fact = j.store.get('source', submitted['source_id'])
    assert fact['status'] == 'confirmed' and fact['_project_shared']
    assert CONFIRMED in fact['_text']
    run, analysis = j.gate(run, 'strategy_review')
    assert analysis['revision'] == original_analysis['revision'] + 1
    assert analysis['items'][0] == original_analysis['items'][0]
    assert analysis['items'][1]['description'] == CONFIRMED
    assert fact['id'] + '#P1' in analysis['items'][1]['refs']
    assert analysis['report']['questions'] == []
    assert run['interrupt'].get('questions', []) == []
    assert j.counts() == {'analyze_requirement': 1}

    # Another project chat retrieves the same fact; the original gate is intact.
    second = j.client.post('/api/projects/' + j.project['id'] + '/chats',
        json={'title': 'Same project: reuse confirmed rules'}).json()
    before = j.snapshot(rid)
    listed = j.turn('列出本项目已确认的锁定规则和来源。', 'project.list_facts', chat=second)
    assert {row['source_id'] for row in listed['actions'][0]['result']['facts']} == {fact['id']}
    reused = j.turn('本项目已经确认连续失败五次后锁定多久？', 'artifact.analyze', chat=second)
    assert '10 分钟' in part(reused, 'answer')['text']
    assert part(reused, 'answer')['refs'] == [fact['id'] + '#P1']
    j.unchanged(rid, before)

    run, scenarios = j.gate(j.continue_gate(run, '确认更新后的理解，生成场景后等我。'), 'scenario_review')
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1}
    before = j.snapshot(rid)
    estimated = j.turn('这些场景大概要多少 case？只估算，不生成、不继续。', 'artifact.estimate',
        artifact_id=scenarios['id'], artifact_revision=scenarios['revision'])
    estimate = part(estimated, 'estimate')['data']
    assert (estimate['min_count'], estimate['max_count']) == (4, 8)
    assert not [a for a in j.store.list('artifact', chat_id=j.chat['id']) if a['type'] == 'cases']
    j.unchanged(rid, before)

    original_scenarios = copy.deepcopy(scenarios)
    j.turn('把选中场景标题明确为“使用注册账号的有效凭证完成登录”，其余不改，先别继续。',
        'artifact.revise', artifact_id=scenarios['id'], artifact_revision=scenarios['revision'],
        selected_ids=[scenarios['items'][0]['id']])
    run, scenarios = j.gate(run, 'scenario_review')
    assert scenarios['revision'] == original_scenarios['revision'] + 1
    assert scenarios['items'][0]['title'] == SCENE_TITLE
    assert scenarios['items'][0]['description'] == original_scenarios['items'][0]['description']
    assert scenarios['items'][1] == original_scenarios['items'][1]
    assert j.artifact(analysis['id']) == analysis
    assert j.counts() == before[2]

    run, cases = j.gate(j.continue_gate(run, '确认当前场景，生成用例草稿后等我确认。'), 'case_draft_review')
    assert cases['items'][0]['title'] == SCENE_TITLE
    assert cases['report']['lineage']['scenario_revision'] == scenarios['revision']
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1}
    original_cases = copy.deepcopy(cases)

    # Merely uploading evidence cannot adopt it or block the current draft gate.
    adopted_source = add_text(j, '登录审计补充', AUDIT_RULE)
    unused_source = add_text(j, '未采用的导出审计补充', UNUSED_RULE)
    pending = state(j, cases)
    assert pending['next_action']['kind'] == 'confirm', pending
    assert {adopted_source['id'], unused_source['id']} <= set(pending['impact']['source_ids'])
    assert j.artifact(cases['id']) == original_cases
    assert adopted_source['id'] not in j.store.run(rid)['_source_ids']

    j.turn('只采用登录审计补充，为选中用例增加账号和设备标识检查；只修改这条 case，先不评审。',
        'project.update_from_sources', {'artifact_id': cases['id'], 'expected_revision': cases['revision'],
            'selected_ids': [cases['items'][0]['id']], 'source_ids': [adopted_source['id']],
            'sync_targets': ['cases'], 'preview': False},
        artifact_id=cases['id'], artifact_revision=cases['revision'], selected_ids=[cases['items'][0]['id']])
    run, cases = j.gate(run, 'case_draft_review')
    assert cases['revision'] == original_cases['revision'] + 1
    assert cases['items'][0]['steps'][:-1] == original_cases['items'][0]['steps']
    assert cases['items'][0]['steps'][-1]['expected'] == '记录包含本次登录账号和设备标识'
    assert cases['items'][1] == original_cases['items'][1]
    assert adopted_source['id'] + '#P1' in cases['items'][0]['refs']
    assert unused_source['id'] not in j.store.get('artifact', cases['id'])['_source_ids']
    assert j.artifact(analysis['id']) == analysis
    assert j.artifact(scenarios['id']) == scenarios
    relation = j.turn('展开当前用例的场景、需求关系和未同步提示。', 'artifact.coverage',
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    context = part(relation, 'coverage')['data']
    discrepancies = context['upstream_discrepancies']
    assert len(discrepancies) == 1
    assert discrepancies[0]['item_ids'] == [cases['items'][0]['id']]
    assert discrepancies[0]['status'] == 'pending'
    assert {parent['artifact_id'] for parent in discrepancies[0]['parents']} == {analysis['id'], scenarios['id']}
    assert context['lineage_rows'][0]['scenario']['revision'] == scenarios['revision']

    details = j.turn('查看草稿的完整步骤与预期结果，不改动。', 'artifact.read',
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    assert part(details, 'case_details')['items'][0]['steps'] == cases['items'][0]['steps']
    run, reviewed = j.gate(j.continue_gate(run, '确认当前用例草稿，开始评审；上游差异保留提示。'), 'case_result_review')
    assert reviewed['items'][0]['steps'] == cases['items'][0]['steps']
    assert j.artifact(analysis['id']) == analysis
    assert j.artifact(scenarios['id']) == scenarios
    assert reviewed['report']['upstream_discrepancies'][0]['status'] == 'pending'
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
        'generate_cases': 1, 'review_cases': 1}
    completed = until(j.client, j.continue_gate(run, '确认评审结果，完成任务。'))
    assert completed['status'] == 'completed' and completed['id'] == rid

    before = j.snapshot(rid)
    explained = j.turn('解释并总结已经生成的 case，说明审计检查，不修改也不重新运行。', 'artifact.analyze',
        artifact_id=reviewed['id'], artifact_revision=reviewed['revision'])
    assert reviewed['items'][0]['id'] in part(explained, 'answer')['text']
    assert '账号和设备标识' in part(explained, 'answer')['text']
    j.unchanged(rid, before)
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
        'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1
    assert source['id'] in j.store.run(rid)['_source_ids']
    assert all(not {'command', 'intent', 'intent_hint'} & body.keys() for body in j.requests)


def test_explicit_project_fact_is_reused_without_starting_a_workflow(conversation_journey):
    j = conversation_journey
    confirmed = j.turn('确认本项目登录模块 v2 的审计记录必须包含设备标识。', 'project.confirm_fact', {
        'content': '登录审计记录必须包含设备标识。', 'fact_key': '登录审计字段',
        'scope': {'module': '登录', 'version': 'v2'}})
    fact = confirmed['actions'][0]['result']['fact']
    assert fact['status'] == 'confirmed'
    assert fact['provenance']['message_id'].startswith('input:')
    second = j.client.post('/api/projects/' + j.project['id'] + '/chats',
        json={'title': 'Shared project fact reuse'}).json()
    listed = j.turn('查看登录模块 v2 已经确认的业务规则。', 'project.list_facts',
        {'scope': {'module': '登录', 'version': 'v2'}}, chat=second)
    assert [row['source_id'] for row in listed['actions'][0]['result']['facts']] == [fact['id']]
    assert '设备标识' in part(listed, 'answer')['text']
    assert j.store.runs(chat_id=j.chat['id']) == []
    assert j.store.runs(chat_id=second['id']) == []
    assert j.counts() == {}
