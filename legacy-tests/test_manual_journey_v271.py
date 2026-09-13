"""One continuous user journey over a live HTTP socket and production LangGraph.

Only model responses are controlled. No command/intent selector is sent by the
composer, and no artifact, gate, revision, cache or run is manufactured in SQLite.
The second test is the same composer contract's short Auto control.
"""
import copy
import io
import socket
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import pytest
import uvicorn
from openpyxl import load_workbook

from tcg.main import create_app
from test_backend_api import until
from test_framework_http_v270 import FrameworkHTTPModel


ROOT = Path(__file__).resolve().parents[1]
QUESTION = '连续失败五次后锁定多久？'
CONFIRMED = '连续失败五次后锁定 10 分钟，期满自动解锁。'
CHANGE = '有效凭证登录成功后，必须立即注销同账号的其他活动会话。'
GENERATION = {'analyze_requirement', 'generate_scenarios', 'generate_cases', 'review_cases', 'summarize'}


class ManualJourneyModel(FrameworkHTTPModel):
    """Produce complete, grounded stage responses, including unaffected controls."""

    async def generate(self, task, context):
        refs = [e['id'] for e in context.get('evidence', []) if e.get('role') != 'example']
        if task == 'analyze_requirement':
            self.calls.append((task, copy.deepcopy(context)))
            confirmed = any('10 分钟' in e.get('text', '') for e in context.get('evidence', []))
            return {'items': [
                {'id': 'REQ-1', 'title': '有效凭证登录', 'description': '有效凭证允许注册用户登录。', 'refs': refs},
                {'id': 'REQ-2', 'title': '错误凭证与账号锁定', 'description': '错误提示不泄露账号；连续失败五次锁定账号。', 'refs': refs},
            ], 'report': {'summary': '登录规则与待确认的锁定时长。', 'questions': [] if confirmed else [QUESTION],
                'question_suggestions': [] if confirmed else [{'question': QUESTION, 'answer': '建议锁定 5 分钟，期满自动解锁。',
                    'basis': '原文没有时长；这是可修改、尚未确认的假设。', 'refs': [], 'confidence': 'assumption'}],
                'assumptions': [], 'requirement_map': {'rules': ['有效凭证登录', '错误凭证与账号锁定']},
                'strategy': {'depth': 'standard', 'rationale': '覆盖有效凭证和错误凭证两条路径'},
                'diagrams': [{'title': '登录流程', 'mermaid': 'flowchart TD\n A[输入凭证] --> B{凭证有效}\n B --> C[登录成功]\n B --> D[通用错误提示]'}]}}
        if task == 'generate_scenarios':
            self.calls.append((task, copy.deepcopy(context)))
            return {'items': [{'id': 'SC-' + str(index), 'title': row['title'], 'description': row['description'],
                'priority': 'P1', 'requirement_ids': [row['id']], 'refs': row['refs']}
                for index, row in enumerate(context['analysis'], 1)], 'has_more': False}
        if task == 'generate_cases':
            self.calls.append((task, copy.deepcopy(context)))
            return {'items': [{'id': 'TC-' + row['id'].split('-')[-1], 'title': row['title'],
                'scenario_id': row['id'], 'type': 'Business', 'priority': 'P1',
                'description': row['description'], 'preconditions': '注册账号存在；另一设备已有活动会话。',
                'steps': [{'action': '输入有效凭证并登录' if row['id'].endswith('SC-1') else '连续输入五次错误密码',
                    'expected': ('登录成功，其他活动会话立即被注销' if '其他活动会话' in row['description'] else '登录成功')
                        if row['id'].endswith('SC-1') else ('显示通用错误提示；账号锁定 10 分钟'
                            if any('10 分钟' in e.get('text', '') for e in context.get('evidence', []))
                            else '显示通用错误提示并锁定账号；时长等待产品确认')}],
                'refs': row['refs']} for row in context['scenarios']], 'has_more': False}
        if task == 'artifact_analyze_sources':
            self.calls.append((task, copy.deepcopy(context)))
            assert any('10 分钟' in e['text'] for e in context['evidence'])
            return {'answer': '本项目已确认：' + CONFIRMED, 'refs': refs}
        if task in ('artifact_modify', 'artifact_sync_scenarios', 'artifact_sync'):
            self.calls.append((task, copy.deepcopy(context)))
            artifact = context['artifact']
            operations = []
            selected = set(context.get('selected_ids') or [row['id'] for row in artifact['items']])
            for row in artifact['items']:
                if row['id'] not in selected:
                    continue
                item = copy.deepcopy(row)
                if artifact['type'] == 'analysis':
                    item['description'] = CHANGE
                elif artifact['type'] == 'scenarios':
                    item['title'] = '有效登录后立即注销其他活动会话'
                    item['description'] = '验证有效凭证登录成功，同账号其他活动会话立即被注销。'
                    if '微调' in context.get('instruction', ''):
                        item['title'] = '跨设备验证登录后立即注销其他活动会话'
                else:
                    item['title'] = '跨设备验证登录后立即注销其他活动会话'
                    item['steps'] = [{'action': '设备 A 已登录；在设备 B 输入有效凭证并登录',
                                      'expected': '设备 B 登录成功；设备 A 的活动会话立即失效'}]
                item['refs'] = list(dict.fromkeys(item.get('refs', []) + refs))
                operations.append({'op': 'update', 'id': row['id'], 'item': item})
            return {'operations': operations, 'summary': '只修改指定规则及其关联条目。'}
        if task == 'review_cases':
            self.calls.append((task, copy.deepcopy(context)))
            item = copy.deepcopy(context['cases'][0])
            item['title'] = 'AI 评审：' + item['title']
            return {'operations': [{'op': 'update', 'id': item['id'], 'item': item}],
                'report': {'summary': '已评审设计并明确标题；尚未执行测试。'}}
        return await super().generate(task, context)


@pytest.fixture
def live_journey(tmp_path):
    model = ManualJourneyModel()
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
            yield client, app, model
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


class Journey:
    def __init__(self, runtime):
        self.client, self.app, self.model = runtime
        self.store = self.app.state.store
        self.sequence = 0
        self.requests = []
        self.project = self.client.get('/api/projects').json()[0]
        self.chat = self.client.post('/api/projects/' + self.project['id'] + '/chats',
            json={'title': 'HITP continuous acceptance'}).json()

    def turn(self, text, name, arguments=None, *, chat=None, mode='hitp', resolve_view=True, **context):
        self.sequence += 1
        # The controlled planner resolves the visible artifact from the same UI
        # context a real planner sees; only prose/settings travel over HTTP.
        planned = dict(arguments or {})
        if resolve_view and context.get('artifact_id') and name not in ('artifact.apply', 'artifact.discard'):
            planned.setdefault('artifact_id', context['artifact_id'])
        self.model.turn_decision = {'actions': [{'name': name, 'arguments': planned}]}
        body = {'client_message_id': 'manual-journey-' + str(self.sequence), 'content': text, 'mode': mode, **context}
        assert not {'command', 'intent', 'intent_hint'} & body.keys()
        self.requests.append(copy.deepcopy(body))
        response = self.client.post('/api/chats/' + (chat or self.chat)['id'] + '/turns', json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == ('needs_confirmation' if name == 'artifact.preview' else 'succeeded'), result
        return result

    def artifact(self, aid):
        response = self.client.get('/api/artifacts/' + aid)
        assert response.status_code == 200, response.text
        return response.json()

    def gate(self, run, kind):
        run = until(self.client, run)
        assert run['status'] == 'waiting' and run['interrupt']['type'] == kind, run
        assert run['mode'] == 'hitp'
        artifact = self.artifact(run['interrupt']['artifact_id'])
        assert run['interrupt']['artifact_revision'] == artifact['revision']
        assert self.store.revision(artifact['id'], artifact['revision'])['items'] == artifact['items']
        return run, artifact

    def counts(self):
        return Counter(task for task, _ in self.model.calls if task in GENERATION)

    def snapshot(self, rid):
        return (copy.deepcopy(self.store.run(rid)),
            {a['id']: {'current': copy.deepcopy(a), 'history': copy.deepcopy(self.store.revisions(a['id']))}
             for a in self.store.list('artifact', chat_id=self.chat['id'])}, self.counts())

    def unchanged(self, rid, snapshot, *, preview=False):
        current = self.snapshot(rid)
        if preview:
            # A proposal briefly acquires/releases an edit lease. That touches
            # updated_at, while every gate/version/input remains identical.
            current[0]['updated_at'] = snapshot[0]['updated_at']
        assert current == snapshot, {'run_changes': {key: (snapshot[0].get(key), current[0].get(key))
            for key in snapshot[0].keys() | current[0].keys() if snapshot[0].get(key) != current[0].get(key)},
            'artifact_changes': snapshot[1] != current[1], 'calls': (snapshot[2], current[2])}

    def continue_gate(self, run, text):
        result = self.turn(text, 'workflow.continue', {'run_id': run['id'], 'approved': True})
        return result['actions'][0]['result']['run']


def part(turn, kind):
    return next(value for value in turn['parts'] if value['type'] == kind)


def upload(journey, name):
    path = ROOT / 'examples/manual-v271' / name
    response = journey.client.post('/api/chats/' + journey.chat['id'] + '/sources',
        files={'file': (name, path.read_bytes(), 'text/markdown')}, data={'role': 'change' if 'change' in name else 'primary'})
    assert response.status_code == 200, response.text
    return response.json()


def excel(journey, file):
    response = journey.client.get(file['url'])
    assert response.status_code == 200
    assert 'spreadsheetml' in response.headers['content-type']
    return list(load_workbook(io.BytesIO(response.content)).active.values)


def test_one_continuous_manual_http_journey(live_journey):
    j = Journey(live_journey)
    source = upload(j, 'login-requirements.md')
    started = j.turn('基于上传需求生成场景和用例，缺失规则先澄清，关键节点等我确认。',
        'workflow.start', {'mode': 'auto', 'stop_after': 'complete'})
    run, analysis = j.gate(started['actions'][0]['result']['run'], 'clarification')
    rid = run['id']
    assert j.store.run(rid)['pause_contract'] == 2
    assert j.counts() == {'analyze_requirement': 1}

    # Adopt -> editable persistent draft -> save/share; no stage continues.
    before = j.snapshot(rid)
    adopted = j.turn('采用全部澄清建议到草稿，先不提交。', 'clarification.adopt', {'adopt_all': True})
    draft = part(adopted, 'clarification_draft')['draft']
    assert all(q['adopted'] for q in draft['questions'])
    assert not [q for q in draft['questions'] if not q['adopted']]
    assert '5 分钟' in draft['answer'] and not draft['submitted'] and not draft['shared']
    edited = j.turn('把锁定答案改为十分钟，仍只保存草稿。', 'clarification.adopt',
        {'expected_revision': draft['revision'], 'answer': QUESTION + '\n' + CONFIRMED})
    draft = part(edited, 'clarification_draft')['draft']
    assert j.client.get('/api/runs/' + rid + '/clarification-draft').json() == draft
    assert '10 分钟' in draft['answer'] and '5 分钟' not in draft['answer']
    j.unchanged(rid, before)
    saved = j.turn('提交保存这份澄清答案，并共享到项目，先不继续任务。', 'clarification.save',
        {'expected_revision': draft['revision'], 'share': True})
    draft = part(saved, 'clarification_draft')['draft']
    assert draft['submitted'] and draft['shared']
    confirmed_source = j.store.get('source', draft['source_id'])
    assert confirmed_source['_project_shared'] and '10 分钟' in confirmed_source['_text']
    assert j.store.run(rid)['interrupt'] == before[0]['interrupt']
    assert j.counts() == before[2]

    # Another chat reads the exact shared fact and cites its saved source.
    second = j.client.post('/api/projects/' + j.project['id'] + '/chats', json={'title': 'Reuse shared clarification'}).json()
    before = j.snapshot(rid)
    reused = j.turn('本项目已经确认连续失败五次后锁定多久？只回答已确认事实。', 'artifact.analyze', chat=second)
    assert '10 分钟' in part(reused, 'answer')['text']
    assert part(reused, 'answer')['refs'] == [draft['source_id'] + '#P1']
    reuse_context = next(context for task, context in reversed(j.model.calls) if task == 'artifact_analyze_sources')
    assert {e['source_id'] for e in reuse_context['evidence']} == {draft['source_id']}
    j.unchanged(rid, before)

    run, analysis = j.gate(j.continue_gate(run, '用已保存的澄清继续到需求理解确认。'), 'strategy_review')
    before = j.snapshot(rid)
    explained = j.turn('解释当前需求理解和测试方案，先不要生成场景。', 'artifact.analyze',
        artifact_id=analysis['id'], artifact_revision=analysis['revision'])
    assert 'REQ-1' in part(explained, 'answer')['text']
    j.unchanged(rid, before)
    run, scenarios = j.gate(j.continue_gate(run, '确认这版需求理解和方案，生成场景后等我。'), 'scenario_review')
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1}
    before = j.snapshot(rid)
    estimate = j.turn('当前场景需要多少条用例？逐场景估算，不生成、不继续。', 'artifact.estimate',
        artifact_id=scenarios['id'], artifact_revision=scenarios['revision'])
    assert (part(estimate, 'estimate')['data']['min_count'], part(estimate, 'estimate')['data']['max_count']) == (4, 8)
    exported = j.turn('把当前场景导出 Excel，仍停在场景确认。', 'artifact.export', {'artifact_ids': [scenarios['id']]})
    initial_file = part(exported, 'files')['files'][0]
    assert initial_file['revision'] == scenarios['revision']
    initial_rows = excel(j, initial_file)
    initial_bytes = j.client.get(initial_file['url']).content
    assert len(initial_rows) == 3
    j.unchanged(rid, before)

    # A real new attachment is read without writes, then only its affected rule is updated.
    change = upload(j, 'forced-logout-change.md')
    before = j.snapshot(rid)
    sources_before = copy.deepcopy(j.store.list('source', chat_id=j.chat['id']))
    impact = j.turn('只读分析新附件对已有需求和场景的影响，不修改、不继续。', 'project.source_impact',
        {'artifact_id': analysis['id'], 'source_ids': [change['id']]},
        artifact_id=analysis['id'], artifact_revision=analysis['revision'])
    report = part(impact, 'source_impact')['data']
    assert report['requirement_ids'] == [analysis['items'][0]['id']]
    assert report['coverage']['checked_pairs'] == report['coverage']['expected_pairs'] == 2
    assert report['refs'] == [change['id'] + '#P1']
    assert report['downstream_candidates'][0]['candidate_item_ids'] == [scenarios['items'][0]['id']]
    assert report['input_versions']['sources'][0]['id'] == change['id']
    assert report['input_versions']['sources'][0]['version'] == j.store.get('source', change['id'])['version']
    impact_context = next(context for task, context in reversed(j.model.calls) if task == 'project_source_impact')
    assert [(row['id'], row['text']) for row in impact_context['new_evidence']] == [(change['id'] + '#P1', CHANGE)]
    assert sources_before == j.store.list('source', chat_id=j.chat['id'])
    j.unchanged(rid, before)
    old_run, old_analysis, old_scenarios = copy.deepcopy(run), copy.deepcopy(analysis), copy.deepcopy(scenarios)
    j.turn('按新附件只更新受影响的需求理解及关联场景，无关条目不动，保留当前等待。',
        'project.update_from_sources', {'artifact_id': analysis['id'], 'source_ids': [change['id']],
            'targets': ['analysis', 'scenarios']}, artifact_id=analysis['id'], artifact_revision=analysis['revision'])
    analysis, scenarios = j.artifact(analysis['id']), j.artifact(scenarios['id'])
    assert analysis['revision'] == old_analysis['revision'] + 1
    assert scenarios['revision'] == old_scenarios['revision'] + 1
    assert analysis['items'][1] == old_analysis['items'][1]
    assert scenarios['items'][1] == old_scenarios['items'][1]
    assert change['id'] + '#P1' in analysis['items'][0]['refs']
    assert change['id'] + '#P1' in scenarios['items'][0]['refs']
    assert '其他活动会话' in analysis['items'][0]['description']
    run, _ = j.gate(run, 'scenario_review')
    assert run['id'] == rid and run['stop_after'] == 'complete'
    assert run['interrupt_id'] != old_run['interrupt_id'] or run['control_version'] > old_run['control_version']
    stale = j.client.post('/api/runs/' + rid + '/resume', json={'approved': True,
        'interrupt_id': old_run['interrupt_id'], 'expected_control_version': old_run['control_version'],
        'expected_revision': old_scenarios['revision']})
    assert stale.status_code == 409
    assert j.counts() == before[2]

    run, cases = j.gate(j.continue_gate(run, '确认最新场景，生成用例草稿后等我，不要自动评审。'), 'case_draft_review')
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1}
    generated = next(context for task, context in reversed(j.model.calls) if task == 'generate_cases')
    assert generated['scenarios'][0]['description'] == scenarios['items'][0]['description']
    assert change['id'] + '#P1' in {e['id'] for e in generated['evidence']}
    assert cases['report']['lineage']['scenario_artifact_id'] == scenarios['id']
    assert cases['report']['lineage']['scenario_revision'] == scenarios['revision']
    assert '立即被注销' in cases['items'][0]['steps'][0]['expected']

    # A linked micro-edit is previewed and applied while the draft gate stays put.
    before = j.snapshot(rid)
    old_scenarios, old_cases = copy.deepcopy(scenarios), copy.deepcopy(cases)
    preview = j.turn('微调选中场景为跨设备验证，并同步关联用例；只预览，其他条目不动。', 'artifact.preview',
        {'sync_related': True, 'related_artifact_ids': [cases['id']]},
        artifact_id=scenarios['id'], artifact_revision=scenarios['revision'], selected_ids=[scenarios['items'][0]['id']],
        resolve_view=False)
    diff = part(preview, 'diff')
    assert {value['artifact_id'] for value in diff['changes']} == {scenarios['id'], cases['id']}
    j.unchanged(rid, before, preview=True)
    j.turn('应用刚才的场景及关联用例微调，仍不要启动 AI 评审。', 'artifact.apply', {'proposal_id': diff['proposal_id']})
    scenarios, cases = j.artifact(scenarios['id']), j.artifact(cases['id'])
    assert scenarios['revision'] == old_scenarios['revision'] + 1
    assert cases['revision'] == old_cases['revision'] + 1
    assert scenarios['items'][1] == old_scenarios['items'][1]
    assert cases['items'][1] == old_cases['items'][1]
    assert '跨设备' in scenarios['items'][0]['title']
    assert cases['items'][0]['steps'][0]['expected'] == '设备 B 登录成功；设备 A 的活动会话立即失效'
    run, _ = j.gate(run, 'case_draft_review')
    assert j.counts() == before[2]

    before = j.snapshot(rid)
    read = j.turn('展开当前草稿的真实操作步骤和预期结果。', 'artifact.read',
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    assert part(read, 'case_details')['items'][0]['steps'] == cases['items'][0]['steps']
    reviewed = j.turn('只读评审草稿的覆盖、步骤和预期，给出意见，不优化、不保存、不继续。', 'artifact.review_cases',
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    assert part(reviewed, 'case_details')['items'][0]['steps'] == cases['items'][0]['steps']
    assert '尚未执行测试' in part(reviewed, 'answer')['report']['summary']
    coverage = j.turn('查看需求到场景到用例的结构覆盖及过期状态。', 'artifact.coverage',
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    data = part(coverage, 'coverage')['data']
    assert {key: data['coverage']['totals'][key] for key in ('requirements', 'scenarios', 'cases')} == {
        'requirements': 2, 'scenarios': 2, 'cases': 2}
    assert not data['stale']['analysis'] and not data['stale']['scenarios']
    profile = j.client.get('/api/projects/' + j.project['id'] + '/profiles').json()[0]
    j.turn('把第一条用例写法保存为当前项目 Profile 的样例。', 'project.pin_samples',
        {'profile_id': profile['id'], 'selected_ids': [cases['items'][0]['id']], 'expected_version': profile['version']},
        artifact_id=cases['id'], artifact_revision=cases['revision'])
    updated_profile = j.store.get('profile', profile['id'])
    sample = updated_profile['config']['sample_cases'][0]
    assert updated_profile['version'] == profile['version'] + 1
    assert sample['steps'] == cases['items'][0]['steps']
    assert not {'refs', 'scenario_id'} & sample.keys()
    j.unchanged(rid, before)

    # Only explicit continuation from the draft gate calls the mutating AI review.
    run, reviewed_cases = j.gate(j.continue_gate(run, '确认草稿，开始 AI 评审并保存优化结果，评审结果等我最终确认。'), 'case_result_review')
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1, 'review_cases': 1}
    review_context = next(context for task, context in reversed(j.model.calls) if task == 'review_cases')
    assert review_context['cases'][0]['steps'] == cases['items'][0]['steps']
    assert reviewed_cases['revision'] > cases['revision']
    assert reviewed_cases['items'][0]['title'].startswith('AI 评审：')
    before = j.snapshot(rid)
    explained = j.turn('解释评审结果并总结这次设计，列出真实编号，尚未执行测试。', 'artifact.analyze',
        artifact_id=reviewed_cases['id'], artifact_revision=reviewed_cases['revision'])
    assert 'TC-1' in part(explained, 'answer')['text']
    exported = j.turn('分别导出最新场景和当前评审结果 Excel，保留最终确认门。', 'artifact.export',
        {'artifact_ids': [scenarios['id'], reviewed_cases['id']]})
    files = part(exported, 'files')['files']
    assert len(files) == 2
    rows = {file['artifact_id']: excel(j, file) for file in files}
    assert '跨设备' in str(rows[scenarios['id']])
    assert '设备 A 的活动会话立即失效' in str(rows[reviewed_cases['id']])
    assert excel(j, initial_file) == initial_rows
    assert j.client.get(initial_file['url']).content == initial_bytes
    j.unchanged(rid, before)
    completed = until(j.client, j.continue_gate(run, '最终确认当前评审结果，完成本次任务。'))
    assert completed['status'] == 'completed' and completed['id'] == rid
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    assert len(j.store.runs(chat_id=j.chat['id'])) == 1
    turns = j.store.list('conversation_turn', project_id=j.project['id'])
    assert len(turns) == j.sequence
    assert all(body['mode'] == 'hitp' and not {'command', 'intent', 'intent_hint'} & body.keys() for body in j.requests)
    # TurnInput stores its default intent_hint=auto even when absent on the wire.
    assert all(turn['_body']['mode'] == 'hitp' and not {'command', 'intent'} & turn['_body'].keys() for turn in turns)
    assert all(turn['_graph_version'] == 'turn-v270' for turn in turns)
    assert source['id'] in j.store.run(rid)['_source_ids']


def test_auto_composer_completes_the_same_real_stages_without_manual_gates(live_journey):
    j = Journey(live_journey)
    upload(j, 'login-requirements.md')
    result = j.turn('基于上传需求自动生成完整测试场景和用例。', 'workflow.start', {'mode': 'hitp'}, mode='auto')
    run = until(j.client, result['actions'][0]['result']['run'])
    assert run['mode'] == 'auto' and run['status'] == 'completed', run
    assert j.counts() == {'analyze_requirement': 1, 'generate_scenarios': 1, 'generate_cases': 1, 'review_cases': 1, 'summarize': 1}
    stages = [message['metadata']['stage'] for message in j.client.get('/api/chats/' + j.chat['id']).json()['messages']
              if message.get('metadata', {}).get('stage_artifact_id')]
    assert stages == ['understand', 'scenarios', 'cases', 'review']
