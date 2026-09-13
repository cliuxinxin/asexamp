"""Continuous v2.8 HTTP journeys with real LangGraph, storage and revisions.

Only the external model is controlled. Every source, edit, preview, application
and confirmation uses public HTTP; no run or artifact is seeded in storage.
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
from test_framework_http_v270 import _turn
from test_manual_journey_v271 import Journey, ManualJourneyModel, CONFIRMED, part, upload


SCENE_CHANGE = '设备 B 登录成功后，设备 A 的活动会话必须立即失效。'
RULE_CHANGE = '有效凭证登录成功后，必须立即注销同账号的其他活动会话。'
NEW_RULE = '管理员可以主动撤销指定用户的所有活动会话。'


class ChangeJourneyModel(ManualJourneyModel):
    needs_clarification = False

    async def generate(self, task, context):
        if task == 'analyze_requirement':
            value = await super().generate(task, context)
            if not self.needs_clarification:
                value['report']['questions'] = []
                value['report']['question_suggestions'] = []
            return value
        if task == 'project_source_impact':
            self.calls.append((task, copy.deepcopy(context)))
            refs = [e['id'] for e in context['new_evidence']]
            return {'requirement_ids': [], 'summary': '管理员撤销会话是一条独立的新需求。',
                'global_impact': False, 'uncertain': False, 'refs': refs,
                'new_requirements': [{'title': '管理员撤销会话', 'description': NEW_RULE, 'refs': refs}]}
        if task == 'artifact_modify' and context['artifact']['type'] == 'analysis':
            self.calls.append((task, copy.deepcopy(context)))
            evidence = [e for e in context['evidence'] if e.get('role') in ('change', 'clarification')]
            refs = [e['id'] for e in evidence]
            if any(NEW_RULE in e['text'] for e in evidence):
                return {'operations': [{'op': 'add', 'item': {'id': 'REQ-3',
                    'title': '管理员撤销会话', 'description': NEW_RULE, 'refs': refs}}],
                    'summary': '新增独立规则，保留已有需求。'}
            row = next(row for row in context['artifact']['items'] if row['id'].endswith('REQ-2'))
            return {'operations': [{'op': 'update', 'id': row['id'],
                'item': {'description': CONFIRMED, 'refs': list(dict.fromkeys(row['refs'] + refs))}}],
                'summary': '采用已确认的锁定时长。',
                'report_patch': {'summary': CONFIRMED,
                    'diagrams': [{'title': '已确认锁定规则', 'mermaid': 'flowchart TD\nA[五次失败] --> B[锁定十分钟]'}]}}
        if task in ('artifact_sync_scenarios', 'artifact_sync'):
            self.calls.append((task, copy.deepcopy(context)))
            operations = []
            rows = context['artifact']['items']
            if task == 'artifact_sync_scenarios':
                for requirement in context['analysis']:
                    matches = [row for row in rows if requirement['id'] in row.get('requirement_ids', [])]
                    if matches:
                        operations += [{'op': 'update', 'id': row['id'], 'item': {
                            'description': requirement['description'], 'refs': requirement['refs']}}
                            for row in matches]
                    else:
                        operations.append({'op': 'add', 'item': {'id': 'SC-3', 'title': requirement['title'],
                            'description': requirement['description'], 'priority': 'P1',
                            'requirement_ids': [requirement['id']], 'refs': requirement['refs']}})
            else:
                for scenario in context['scenarios']:
                    operations += [{'op': 'update', 'id': row['id'], 'item': {
                        'steps': [{'action': '设备 A 已登录，再从设备 B 登录', 'expected': scenario['description']}],
                        'refs': scenario['refs']}}
                        for row in rows if row['scenario_id'] == scenario['id']]
            return {'operations': operations, 'summary': '仅同步已保存的上游变化。'}
        return await super().generate(task, context)


@pytest.fixture
def change_journey(tmp_path):
    model = ChangeJourneyModel()
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


def state(j, artifact):
    response = j.client.get('/api/chats/' + j.chat['id'] + '/workspace-state',
        params={'artifact_id': artifact['id']})
    assert response.status_code == 200, response.text
    return response.json()


def local_edit(j, artifact, index, **fields):
    items = copy.deepcopy(artifact['items'])
    items[index].update(fields)
    response = j.client.put('/api/artifacts/' + artifact['id'], json={
        'expected_revision': artifact['revision'], 'items': items})
    assert response.status_code == 200, response.text
    return response.json()


def blocked(j, run):
    current = j.client.get('/api/runs/' + run['id']).json()
    response = j.client.post('/api/runs/' + run['id'] + '/resume', json={
        'approved': True, 'interrupt_id': current['interrupt_id'],
        'expected_control_version': current['control_version'],
        'expected_revision': current['interrupt']['artifact_revision']})
    assert response.status_code == 409, response.text
    assert j.client.get('/api/runs/' + run['id']).json()['status'] == 'waiting'


def reconcile(j, artifact):
    current = state(j, artifact)
    assert current['next_action']['kind'] == 'reconcile', current
    j.sequence += 1
    result = _turn(j.client, j.model, j.chat['id'], 'v280-' + str(j.sequence),
        '预览同步已保存的变更，保持当前确认节点。',
        [{'name': 'workspace.reconcile', 'arguments': current['next_action']['arguments']}],
        mode='hitp', artifact_id=artifact['id'], artifact_revision=artifact['revision'])
    assert result['status'] == 'needs_confirmation', result
    proposal = part(result, 'diff')
    pending = state(j, artifact)
    assert pending['next_action']['kind'] == 'apply'
    assert pending['pending_proposal']['id'] == proposal['proposal_id']
    return proposal


def apply(j, proposal):
    return j.turn('应用这份同步预览，保留当前确认节点。', 'artifact.apply',
        {'artifact_id': proposal['artifact_id'], 'proposal_id': proposal['proposal_id']})


def begin(j):
    upload(j, 'login-requirements.md')
    started = j.turn('基于上传需求生成场景和用例，每个关键节点等我确认。',
        'workflow.start', {'stop_after': 'complete'})
    return j.gate(started['actions'][0]['result']['run'],
        'clarification' if j.model.needs_clarification else 'strategy_review')


def test_saved_scene_change_blocks_case_gate_until_preview_is_applied(change_journey):
    j = change_journey
    run, analysis = begin(j)
    run, scenarios = j.gate(j.continue_gate(run, '确认理解，生成场景。'), 'scenario_review')
    run, cases = j.gate(j.continue_gate(run, '确认场景，生成用例草稿。'), 'case_draft_review')
    original_scenarios, original_cases = copy.deepcopy(scenarios), copy.deepcopy(cases)
    assert state(j, cases)['impact']['status'] == 'current'

    scenarios = local_edit(j, scenarios, 0, description=SCENE_CHANGE)
    pending = state(j, scenarios)
    assert pending['impact']['status'] == 'pending'
    affected = next(row for row in pending['impact']['affected'] if row['artifact_id'] == cases['id'])
    assert affected['item_ids'] == [cases['items'][0]['id']]
    assert j.artifact(cases['id']) == original_cases
    blocked(j, run)
    assert j.counts().get('review_cases', 0) == 0

    preview = reconcile(j, scenarios)
    assert j.artifact(scenarios['id']) == scenarios
    assert j.artifact(cases['id']) == original_cases
    blocked(j, run)  # Merely preparing the preview cannot make old cases current.
    apply(j, preview)
    cases = j.artifact(cases['id'])
    assert j.artifact(scenarios['id']) == scenarios  # Saved edits are never replayed.
    assert cases['revision'] == original_cases['revision'] + 1
    assert cases['items'][0]['steps'][0]['expected'] == SCENE_CHANGE
    assert cases['items'][1] == original_cases['items'][1]
    assert scenarios['items'][1] == original_scenarios['items'][1]
    assert j.store.revision(cases['id'], original_cases['revision'])['items'] == original_cases['items']
    run, latest = j.gate(run, 'case_draft_review')
    assert latest['revision'] == cases['revision']
    assert state(j, cases)['impact']['status'] == 'current'
    assert state(j, cases)['next_action']['kind'] == 'confirm'

    run, reviewed = j.gate(j.continue_gate(run, '确认最新草稿，开始评审。'), 'case_result_review')
    assert reviewed['items'][0]['steps'][0]['expected'] == SCENE_CHANGE
    completed = until(j.client, j.continue_gate(run, '确认评审结果，完成任务。'))
    assert completed['status'] == 'completed'
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
        'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1


def test_understanding_edit_and_new_rule_refresh_existing_scene_branch(change_journey):
    j = change_journey
    run, analysis = begin(j)
    run, scenarios = j.gate(j.continue_gate(run, '确认理解，生成场景。'), 'scenario_review')
    original_analysis, original_scenarios = copy.deepcopy(analysis), copy.deepcopy(scenarios)

    analysis = local_edit(j, analysis, 0, description=RULE_CHANGE)
    assert state(j, analysis)['impact']['status'] == 'pending'
    blocked(j, run)
    preview = reconcile(j, analysis)
    assert j.artifact(scenarios['id']) == original_scenarios
    apply(j, preview)
    scenarios = j.artifact(scenarios['id'])
    assert scenarios['revision'] == original_scenarios['revision'] + 1
    assert scenarios['items'][0]['description'] == RULE_CHANGE
    assert scenarios['items'][1] == original_scenarios['items'][1]
    assert j.artifact(analysis['id']) == analysis
    assert analysis['items'][1] == original_analysis['items'][1]
    run, _ = j.gate(run, 'scenario_review')
    assert state(j, analysis)['impact']['status'] == 'current'

    response = j.client.post('/api/chats/' + j.chat['id'] + '/sources/text', json={
        'name': '新增管理员撤销规则', 'text': NEW_RULE, 'role': 'change'})
    assert response.status_code == 200, response.text
    source = response.json()
    before_analysis, before_scenarios = copy.deepcopy(analysis), copy.deepcopy(scenarios)
    assert state(j, analysis)['impact']['status'] == 'pending'
    blocked(j, run)
    preview = reconcile(j, analysis)
    assert j.artifact(analysis['id']) == before_analysis
    assert j.artifact(scenarios['id']) == before_scenarios
    analysis_change = next(change for change in preview['changes'] if change['artifact_id'] == analysis['id'])
    assert len(analysis_change['diff']['added']) == 1
    assert analysis_change['diff']['updated'] == []
    apply(j, preview)

    analysis, scenarios = j.artifact(analysis['id']), j.artifact(scenarios['id'])
    assert analysis['items'][:2] == before_analysis['items']
    assert scenarios['items'][:2] == before_scenarios['items']
    assert len(analysis['items']) == len(scenarios['items']) == 3
    new_requirement, new_scenario = analysis['items'][-1], scenarios['items'][-1]
    assert new_requirement['description'] == NEW_RULE
    assert new_requirement['id'] not in {row['id'] for row in before_analysis['items']}
    assert source['id'] + '#P1' in new_requirement['refs']
    assert new_scenario['requirement_ids'] == [new_requirement['id']]
    assert new_scenario['description'] == NEW_RULE
    assert state(j, analysis)['impact']['status'] == 'current'
    run, latest = j.gate(run, 'scenario_review')
    assert latest['revision'] == scenarios['revision']
    run, cases = j.gate(j.continue_gate(run, '确认新增规则及最新场景，生成用例草稿。'), 'case_draft_review')
    assert len(cases['items']) == 3
    added_case = next(row for row in cases['items'] if row['scenario_id'] == new_scenario['id'])
    assert added_case['description'] == NEW_RULE
    assert source['id'] + '#P1' in added_case['refs']
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1}
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1


def test_confirmed_clarification_changes_understanding_before_scene_generation(change_journey):
    j = change_journey
    j.model.needs_clarification = True
    run, analysis = begin(j)
    original = copy.deepcopy(analysis)
    adopted = j.turn('锁定时长确认为十分钟，先保存答案草稿。', 'clarification.adopt', {'answer': CONFIRMED})
    draft = part(adopted, 'clarification_draft')['draft']
    saved = j.turn('提交保存这份澄清答案，先不要生成场景。', 'clarification.save',
        {'expected_revision': draft['revision']})
    draft = part(saved, 'clarification_draft')['draft']
    assert draft['submitted']
    assert j.artifact(analysis['id']) == original
    run, analysis = j.gate(j.continue_gate(run, '采用已确认澄清，继续到需求理解确认。'), 'strategy_review')
    assert analysis['items'][1]['description'] == CONFIRMED
    assert analysis['items'][0] == original['items'][0]
    assert analysis['revision'] > original['revision']
    assert draft['source_id'] + '#P1' in analysis['items'][1]['refs']
    assert analysis['report']['summary'] == CONFIRMED
    assert analysis['report']['questions'] == []
    assert analysis['report']['diagrams'] != original['report']['diagrams']
    assert j.store.revision(analysis['id'], original['revision'])['items'] == original['items']
    assert state(j, analysis)['impact']['status'] == 'current'
    run, scenarios = j.gate(j.continue_gate(run, '确认已澄清的需求理解，生成场景。'), 'scenario_review')
    assert scenarios['items'][1]['description'] == CONFIRMED
    run, cases = j.gate(j.continue_gate(run, '确认场景，生成用例草稿。'), 'case_draft_review')
    assert '10 分钟' in cases['items'][1]['steps'][0]['expected']
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1}


def test_inline_rule_previews_analysis_from_scene_focus_without_duplicate_source(change_journey):
    j = change_journey
    run, analysis = begin(j)
    run, scenarios = j.gate(j.continue_gate(run, '确认理解，生成场景。'), 'scenario_review')
    original_analysis, original_scenarios = copy.deepcopy(analysis), copy.deepcopy(scenarios)
    before_sources = j.client.get('/api/chats/' + j.chat['id']).json()['sources']
    j.sequence += 1
    body = {'client_message_id': 'inline-rule-v280', 'content': NEW_RULE + '请预览关联需求和场景的更新，保留当前确认节点。',
        'mode': 'hitp', 'artifact_id': scenarios['id'], 'artifact_revision': scenarios['revision']}
    assert not {'command', 'intent', 'intent_hint'} & body.keys()
    j.model.turn_decision = {'actions': [{'name': 'workspace.reconcile', 'arguments': {
        'content': NEW_RULE, 'artifact_id': scenarios['id'], 'expected_revision': scenarios['revision']}}]}
    response = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'needs_confirmation', result
    proposal = part(result, 'diff')
    assert proposal['artifact_id'] == analysis['id']
    assert {change['artifact_id'] for change in proposal['changes']} == {analysis['id'], scenarios['id']}
    assert j.artifact(analysis['id']) == original_analysis
    assert j.artifact(scenarios['id']) == original_scenarios
    sources = j.client.get('/api/chats/' + j.chat['id']).json()['sources']
    assert len(sources) == len(before_sources) + 1
    source = next(source for source in sources if source['id'] not in {old['id'] for old in before_sources})
    assert source['role'] == 'change'
    assert j.store.get('source', source['id'])['_text'] == NEW_RULE
    pending = state(j, scenarios)
    assert pending['next_action']['kind'] == 'apply'
    assert pending['pending_proposal']['id'] == proposal['proposal_id']
    assert pending['pending_proposal']['artifact_id'] == analysis['id']

    # A transport retry returns the same durable preview and does no model work.
    before_calls = len(j.model.calls)
    repeated = j.client.post('/api/chats/' + j.chat['id'] + '/turns', json=body)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()['id'] == result['id']
    assert part(repeated.json(), 'diff')['proposal_id'] == proposal['proposal_id']
    assert j.client.get('/api/chats/' + j.chat['id']).json()['sources'] == sources
    assert len(j.model.calls) == before_calls
    assert j.artifact(analysis['id']) == original_analysis
    assert j.artifact(scenarios['id']) == original_scenarios
    assert state(j, scenarios)['pending_proposal']['id'] == proposal['proposal_id']
    blocked(j, run)

    apply(j, proposal)
    analysis, scenarios = j.artifact(analysis['id']), j.artifact(scenarios['id'])
    assert analysis['items'][:2] == original_analysis['items']
    assert scenarios['items'][:2] == original_scenarios['items']
    assert len(analysis['items']) == len(scenarios['items']) == 3
    assert analysis['items'][-1]['description'] == NEW_RULE
    assert source['id'] + '#P1' in analysis['items'][-1]['refs']
    assert scenarios['items'][-1]['requirement_ids'] == [analysis['items'][-1]['id']]
    current_run, latest = j.gate(run, 'scenario_review')
    assert current_run['id'] == run['id']
    assert latest['revision'] == scenarios['revision']
    assert state(j, scenarios)['pending_proposal'] is None
    assert state(j, scenarios)['impact']['status'] == 'current'
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1}
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1


def test_auto_still_completes_all_original_stages(change_journey):
    j = change_journey
    j.model.needs_clarification = True
    upload(j, 'login-requirements.md')
    started = j.turn('基于需求自动完成场景、用例和评审。', 'workflow.start', mode='auto')
    run = until(j.client, started['actions'][0]['result']['run'])
    assert run['mode'] == 'auto' and run['status'] == 'completed', run
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
        'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    stages = [message['metadata']['stage'] for message in j.client.get('/api/chats/' + j.chat['id']).json()['messages']
        if message.get('metadata', {}).get('stage_artifact_id')]
    assert stages == ['understand', 'scenarios', 'cases', 'review']
    cases = next(a for a in j.store.list('artifact', chat_id=j.chat['id']) if a['type'] == 'cases')
    assert state(j, cases)['impact']['status'] == 'current'


def test_new_auto_run_pauses_before_consuming_stale_scenarios(change_journey):
    j = change_journey
    upload(j, 'login-requirements.md')
    started = j.turn('基于需求自动完成场景、用例和评审。', 'workflow.start', mode='auto')
    original_run = until(j.client, started['actions'][0]['result']['run'])
    assert original_run['status'] == 'completed'
    artifacts = j.store.list('artifact', chat_id=j.chat['id'])
    analysis = j.artifact(next(a['id'] for a in artifacts if a['type'] == 'analysis'))
    scenarios = j.artifact(next(a['id'] for a in artifacts if a['type'] == 'scenarios'))
    analysis = local_edit(j, analysis, 0, description=RULE_CHANGE)
    assert state(j, scenarios)['impact']['status'] == 'pending'
    before = j.counts()

    started = j.turn('使用当前场景自动生成新一轮测试用例。', 'workflow.start',
        {'intent': 'generate_case', 'artifact_id': scenarios['id'], 'as_requirement': False,
            'stop_after': 'complete'}, mode='auto',
        artifact_id=scenarios['id'], artifact_revision=scenarios['revision'])
    paused = until(j.client, started['actions'][0]['result']['run'])
    assert paused['status'] == 'waiting', paused
    assert paused['mode'] == 'auto'
    assert paused['interrupt']['type'] == 'workflow_paused'
    assert paused['interrupt']['reason'] == 'reconcile'
    assert paused['id'] != original_run['id']
    assert j.counts() == before  # The stale scenario never reaches case generation.
    blocked(j, paused)

    proposal = reconcile(j, analysis)
    assert j.artifact(scenarios['id']) == scenarios
    apply(j, proposal)
    scenarios = j.artifact(scenarios['id'])
    assert scenarios['items'][0]['description'] == RULE_CHANGE
    current = j.client.get('/api/runs/' + paused['id']).json()
    assert current['status'] == 'waiting'
    assert current['interrupt']['type'] == 'workflow_paused'
    assert j.counts() == before
    resumed = j.turn('已检查并应用同步，继续本轮自动生成。', 'workflow.continue',
        {'run_id': paused['id'], 'approved': True}, mode='auto')
    completed = until(j.client, resumed['actions'][0]['result']['run'])
    assert completed['id'] == paused['id'] and completed['status'] == 'completed', completed
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1,
        'generate_cases': 2, 'review_cases': 2, 'summarize': 2}
    fresh_cases = j.artifact(completed['artifact_ids'][0])
    assert fresh_cases['report']['lineage']['scenario_artifact_id'] == scenarios['id']
    assert fresh_cases['report']['lineage']['scenario_revision'] == scenarios['revision']
    assert fresh_cases['items'][0]['description'] == RULE_CHANGE
    assert len(j.store.runs(chat_id=j.chat['id'])) == 2
