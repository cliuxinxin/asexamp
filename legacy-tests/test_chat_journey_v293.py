"""A conversation-only Human journey over real HTTP, SQLite and LangGraph.

The model supplies domain output and interprets open-ended messages. Assents
carry only visible prompt identity and prose; no command, action or artifact
selection is smuggled into the HTTP request.
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
from test_change_journey_v280 import NEW_RULE, RULE_CHANGE
from test_conversation_journey_v290 import ConversationJourneyModel, add_text
from test_manual_journey_v271 import Journey, part, upload


SUGGESTED_RULE = '连续失败五次后锁定 5 分钟，期满自动解锁。'


class ChatJourneyModel(ConversationJourneyModel):
    async def generate(self, task, context):
        if task == 'project_source_impact' and any(RULE_CHANGE in entry['text']
                                                  for entry in context['new_evidence']):
            self.calls.append((task, copy.deepcopy(context)))
            row = next(row for row in context['requirements'] if row['id'].endswith('REQ-1'))
            return {'requirement_ids': [row['id']], 'summary': '只影响登录成功后的会话处理。',
                    'global_impact': False, 'uncertain': False,
                    'refs': [entry['id'] for entry in context['new_evidence']]}
        if task == 'artifact_modify' and context['artifact']['type'] == 'analysis':
            evidence = context.get('evidence', [])
            if any(RULE_CHANGE in entry['text'] for entry in evidence):
                self.calls.append((task, copy.deepcopy(context)))
                refs = [entry['id'] for entry in evidence if RULE_CHANGE in entry['text']]
                row = next(row for row in context['artifact']['items'] if row['id'].endswith('REQ-1'))
                return {'operations': [{'op': 'update', 'id': row['id'], 'item': {
                    'description': RULE_CHANGE, 'refs': list(dict.fromkeys(row['refs'] + refs))}}],
                    'summary': '只增加登录成功后的活动会话注销规则。'}
            if not any(NEW_RULE in entry['text'] for entry in evidence):
                self.calls.append((task, copy.deepcopy(context)))
                refs = [entry['id'] for entry in evidence
                        if entry.get('role') == 'clarification' and '5 分钟' in entry['text']]
                assert refs, 'The adopted suggestion must be supplied as saved clarification evidence.'
                row = next(row for row in context['artifact']['items'] if row['id'].endswith('REQ-2'))
                return {'operations': [{'op': 'update', 'id': row['id'], 'item': {
                    'description': SUGGESTED_RULE,
                    'refs': list(dict.fromkeys(row['refs'] + refs))}}],
                    'summary': '采用用户同意的五分钟锁定建议。'}
        result = await super().generate(task, context)
        if task == 'generate_cases' and any('5 分钟' in entry['text']
                                            for entry in context.get('evidence', [])):
            for row in result['items']:
                if row['scenario_id'].endswith('SC-2'):
                    row['steps'][0]['expected'] = '显示通用错误提示；账号锁定 5 分钟，期满自动解锁'
                elif row['scenario_id'].endswith('SC-3'):
                    row['preconditions'] = '管理员已登录；目标用户存在活动会话。'
                    row['steps'] = [{'action': '管理员撤销指定用户的所有活动会话',
                                     'expected': '该用户的所有活动会话失效'}]
        return result


@pytest.fixture
def chat_journey(tmp_path):
    model = ChatJourneyModel()
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


def prompt(j, kind):
    response = j.client.get('/api/chats/' + j.chat['id'])
    assert response.status_code == 200, response.text
    value = response.json().get('conversation_prompt')
    assert value and value['kind'] == kind, value
    assert value['id'] and value['title'] and value['message']
    return value


def assent(j, current):
    j.sequence += 1
    # The prompt reply must be resolved from the current server state. Calling
    # the open-ended interpreter here fails rather than accidentally passing.
    j.model.turn_decision = None
    body = {'client_message_id': 'chat-assent-' + str(j.sequence),
            'content': '同意', 'reply_to': current['id'], 'mode': 'hitp'}
    j.requests.append(copy.deepcopy(body))
    response = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'succeeded', result
    return result


def current_run(j, rid):
    response = j.client.get('/api/runs/' + rid)
    assert response.status_code == 200, response.text
    return response.json()


def test_plain_assents_and_upstream_changes_return_to_the_correct_human_gate(chat_journey):
    j = chat_journey
    upload(j, 'login-requirements.md')
    started = j.turn('请根据上传资料生成测试场景和用例，每一步都等我在对话里确认。',
                     'workflow.start', {'stop_after': 'complete'})
    run, analysis = j.gate(started['actions'][0]['result']['run'], 'clarification')
    rid = run['id']
    clarification = prompt(j, 'clarification')
    assert '5 分钟' in str(clarification['questions'])

    assent(j, clarification)
    run, analysis = j.gate(current_run(j, rid), 'strategy_review')
    understanding = prompt(j, 'strategy_review')
    assert understanding['id'] != clarification['id']
    assert analysis['items'][1]['description'] == SUGGESTED_RULE
    assert j.counts() == {'analyze_requirement': 1}
    draft = j.client.get('/api/runs/' + rid + '/clarification-draft').json()
    assert draft['submitted'] and draft['shared']
    shared = j.store.get('source', draft['source_id'])
    assert shared['_project_shared'] and '5 分钟' in shared['_text']

    assent(j, understanding)
    run, scenarios = j.gate(current_run(j, rid), 'scenario_review')
    scenario_prompt = prompt(j, 'scenario_review')
    assert scenario_prompt['id'] != understanding['id']
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1}

    # Read-only side questions use the same composer and leave the live prompt
    # usable; neither changes a revision nor spends a confirmation.
    before = j.snapshot(rid)
    estimated = j.turn('这些场景大概要多少条 case？只估算，不生成。',
                       'artifact.estimate', {'artifact_id': scenarios['id']},
                       reply_to=scenario_prompt['id'])
    assert (part(estimated, 'estimate')['data']['min_count'],
            part(estimated, 'estimate')['data']['max_count']) == (4, 8)
    j.unchanged(rid, before)
    assert prompt(j, 'scenario_review')['id'] == scenario_prompt['id']
    explained = j.turn('解释一下当前场景，先不用继续。',
                       'artifact.analyze', {'artifact_id': scenarios['id']},
                       reply_to=scenario_prompt['id'])
    assert part(explained, 'answer')['text']
    j.unchanged(rid, before)
    assert prompt(j, 'scenario_review')['id'] == scenario_prompt['id']

    # A newly uploaded requirement is explicitly adopted in a normal message.
    # Save the upstream understanding first; downstream work waits for assent.
    source = add_text(j, '管理员撤销会话补充需求', NEW_RULE)
    old_analysis, old_scenarios = copy.deepcopy(analysis), copy.deepcopy(scenarios)
    j.turn('这份新文件是补充需求。先更新需求理解，给我确认后再往下走。',
           'project.update_from_sources', {'artifact_id': analysis['id'],
               'source_ids': [source['id']], 'targets': ['analysis'], 'preview': False},
           reply_to=scenario_prompt['id'])
    run, analysis = j.gate(current_run(j, rid), 'strategy_review')
    updated_understanding = prompt(j, 'strategy_review')
    assert updated_understanding['id'] != scenario_prompt['id']
    assert run['id'] == rid
    assert analysis['id'] == old_analysis['id']
    assert analysis['revision'] == old_analysis['revision'] + 1
    assert analysis['items'][:2] == old_analysis['items']
    new_rule = next(row for row in analysis['items'] if row['description'] == NEW_RULE)
    assert source['id'] + '#P1' in new_rule['refs']
    assert j.artifact(scenarios['id'])['items'] == old_scenarios['items']
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1}

    assent(j, updated_understanding)
    run, scenarios = j.gate(current_run(j, rid), 'scenario_review')
    updated_scenarios = prompt(j, 'scenario_review')
    assert updated_scenarios['id'] != updated_understanding['id']
    assert any(new_rule['id'] in row['requirement_ids'] and NEW_RULE in row['description']
               for row in scenarios['items'])
    assert not j.counts().get('generate_cases')
    assert not [row for row in j.store.list('artifact', chat_id=j.chat['id']) if row['type'] == 'cases']

    assent(j, updated_scenarios)
    run, cases = j.gate(current_run(j, rid), 'case_draft_review')
    case_prompt = prompt(j, 'case_draft_review')
    latest_input = next(context for task, context in reversed(j.model.calls) if task == 'generate_cases')
    assert latest_input['scenarios'] == scenarios['items']
    assert {row['scenario_id'] for row in cases['items']} == {row['id'] for row in scenarios['items']}
    assert j.counts()['generate_cases'] == 1 and not j.counts().get('review_cases')

    assent(j, case_prompt)
    run, reviewed = j.gate(current_run(j, rid), 'case_result_review')
    review_prompt = prompt(j, 'case_result_review')
    assert review_prompt['id'] != case_prompt['id']
    assert j.counts()['review_cases'] == 1 and not j.counts().get('summarize')
    assert len(reviewed['items']) == len(cases['items'])

    assent(j, review_prompt)
    completed = until(j.client, current_run(j, rid))
    assert completed['id'] == rid and completed['status'] == 'completed'
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1
    assert j.counts()['analyze_requirement'] == 1
    assert j.counts()['generate_cases'] == j.counts()['review_cases'] == j.counts()['summarize'] == 1
    assert all(not {'command', 'intent', 'intent_hint', 'artifact_id', 'selected_ids'} & body.keys()
               for body in j.requests)


def test_case_draft_detour_confirms_understanding_then_scenarios_before_updating_cases(chat_journey):
    j = chat_journey
    upload(j, 'login-requirements.md')
    started = j.turn('生成测试场景和用例，每个环节等我在对话里同意。',
                     'workflow.start', {'stop_after': 'complete'})
    run, _ = j.gate(started['actions'][0]['result']['run'], 'clarification')
    rid = run['id']
    assent(j, prompt(j, 'clarification'))
    run, analysis = j.gate(current_run(j, rid), 'strategy_review')
    assent(j, prompt(j, 'strategy_review'))
    run, scenarios = j.gate(current_run(j, rid), 'scenario_review')
    assent(j, prompt(j, 'scenario_review'))
    run, cases = j.gate(current_run(j, rid), 'case_draft_review')
    draft_prompt = prompt(j, 'case_draft_review')
    old_analysis, old_scenarios, old_cases = map(copy.deepcopy, (analysis, scenarios, cases))
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1}

    source = add_text(j, '登录成功后的会话注销规则', RULE_CHANGE)
    j.turn('这份补充改变了登录成功规则。先修改需求理解，后续每步仍然等我同意。',
           'project.update_from_sources', {'artifact_id': analysis['id'],
               'source_ids': [source['id']], 'targets': ['analysis'], 'preview': False},
           reply_to=draft_prompt['id'])
    run, analysis = j.gate(current_run(j, rid), 'strategy_review')
    assert analysis['items'][0]['description'] == RULE_CHANGE
    assert analysis['items'][1] == old_analysis['items'][1]
    assert j.artifact(scenarios['id'])['items'] == old_scenarios['items']
    assert j.artifact(cases['id'])['items'] == old_cases['items']

    assent(j, prompt(j, 'strategy_review'))
    run, scenarios = j.gate(current_run(j, rid), 'scenario_review')
    assert scenarios['items'][0]['description'] == RULE_CHANGE
    assert scenarios['items'][1] == old_scenarios['items'][1]
    assert j.artifact(cases['id'])['items'] == old_cases['items']
    assert j.counts()['generate_cases'] == 1 and not j.counts().get('review_cases')

    assent(j, prompt(j, 'scenario_review'))
    run, cases = j.gate(current_run(j, rid), 'case_draft_review')
    assert run['id'] == rid and cases['id'] == old_cases['id']
    assert cases['revision'] == old_cases['revision'] + 1
    assert cases['items'][0]['steps'][0]['expected'] == RULE_CHANGE
    assert source['id'] + '#P1' in cases['items'][0]['refs']
    assert cases['items'][1] == old_cases['items'][1]
    assert j.counts()['generate_cases'] == 1 and not j.counts().get('review_cases')

    assent(j, prompt(j, 'case_draft_review'))
    run, reviewed = j.gate(current_run(j, rid), 'case_result_review')
    assert reviewed['items'][0]['steps'] == cases['items'][0]['steps']
    assert j.counts()['review_cases'] == 1
    assent(j, prompt(j, 'case_result_review'))
    completed = until(j.client, current_run(j, rid))
    assert completed['id'] == rid and completed['status'] == 'completed'
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
                          'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    assert all(not {'command', 'intent', 'intent_hint', 'artifact_id', 'selected_ids'} & body.keys()
               for body in j.requests)
