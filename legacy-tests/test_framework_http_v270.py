"""Real HTTP + LangGraph acceptance path for the v2.7 conversation framework.

Model semantics are deterministic; HTTP routing, workflow execution, SQLite
checkpoints, artifact revisions, lineage synchronization and XLSX exports are
the production implementations.
"""
import copy
import io

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tcg.main import create_app
from test_backend_api import setup_chat, until
from test_workflow_v25 import FlowModel


class FrameworkHTTPModel(FlowModel):
    def __init__(self):
        super().__init__()
        self.turn_decision = None

    async def generate(self, task, context):
        if task == 'conversation_turn':
            self.calls.append((task, copy.deepcopy(context)))
            assert self.turn_decision is not None
            return copy.deepcopy(self.turn_decision)
        if task == 'artifact_estimate':
            self.calls.append((task, copy.deepcopy(context)))
            return {'scenarios': [{'scenario_id': row['id'], 'min_count': 2, 'max_count': 4,
                'rationale': '覆盖正常、异常和边界路径', 'assumptions': ['仅为设计估算，未生成或执行用例']}
                for row in context['scenarios']]}
        if task == 'artifact_explain':
            self.calls.append((task, copy.deepcopy(context)))
            rows = context['artifact']['items']
            refs = [e['id'] for e in context.get('evidence', []) if e.get('role') != 'example']
            return {'answer': '当前成果摘要：' + '、'.join(row['id'] for row in rows), 'refs': refs}
        if task == 'artifact_review_readonly':
            self.calls.append((task, copy.deepcopy(context)))
            case = context['cases'][0]
            refs = [e['id'] for e in context.get('evidence', []) if e.get('role') != 'example']
            return {'report': {'summary': '只读评审：步骤与预期结果清晰，尚未执行测试。',
                'issues': [{'id': case['id'], 'description': '执行时需记录实际环境。', 'refs': refs}],
                'coverage': ['已覆盖登录主路径'], 'refs': refs}}
        if task == 'project_source_impact':
            self.calls.append((task, copy.deepcopy(context)))
            refs = [e['id'] for e in context['new_evidence']]
            return {'requirement_ids': [context['requirements'][0]['id']],
                'summary': '新增资料要求登录后注销其他活动会话。', 'global_impact': False,
                'uncertain': False, 'refs': refs}
        if task in ('artifact_modify', 'artifact_sync_scenarios', 'artifact_sync'):
            self.calls.append((task, copy.deepcopy(context)))
            artifact = context['artifact']
            evidence_refs = [e['id'] for e in context.get('evidence', []) if e.get('role') != 'example']
            operations = []
            for row in artifact['items']:
                item = copy.deepcopy(row)
                if artifact['type'] == 'analysis':
                    item['description'] = '有效凭证登录后，必须注销该账号的其他活动会话。'
                    item['refs'] = list(dict.fromkeys(item.get('refs', []) + evidence_refs))
                elif artifact['type'] == 'scenarios':
                    item['title'] = '登录并同步会话状态'
                    item['description'] = '验证登录成功和其他活动会话注销。'
                    item['refs'] = list(dict.fromkeys(item.get('refs', []) + evidence_refs))
                else:
                    item['title'] = '登录后注销其他活动会话'
                    item['steps'] = [{'action': '使用有效凭证登录',
                                      'expected': '登录成功且该账号其他活动会话被注销'}]
                    item['refs'] = list(dict.fromkeys(item.get('refs', []) + evidence_refs))
                operations.append({'op': 'update', 'id': row['id'], 'item': item})
            return {'operations': operations, 'summary': '已按指定范围生成版本化修改。'}
        return await super().generate(task, context)


def _turn(client, model, chat_id, sequence, content, actions, **body):
    model.turn_decision = {'actions': actions}
    response = client.post(f'/api/chats/{chat_id}/turns', json={
        'client_message_id': f'framework-http-{sequence}', 'content': content, **body})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] not in ('failed', 'cancelled'), result
    return result


def _part(turn, kind):
    return next(part for part in turn['parts'] if part['type'] == kind)


def test_natural_language_turns_drive_real_mainflow_artifacts_and_exports(tmp_path):
    model = FrameworkHTTPModel()
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        project, chat, _ = setup_chat(client)

        started = _turn(client, model, chat['id'], 1,
            '请根据当前登录需求完整生成测试场景和测试用例。',
            [{'name': 'workflow.start', 'arguments': {'mode': 'auto', 'stop_after': 'complete'}}])
        run = until(client, started['actions'][0]['result']['run'])
        assert run['status'] == 'completed', run
        saved = [a for a in app.state.store.list('artifact', chat_id=chat['id']) if a.get('_visible')]
        analysis = next(a for a in saved if a['type'] == 'analysis')
        scenarios = next(a for a in saved if a['type'] == 'scenarios')
        cases = next(a for a in saved if a['type'] == 'cases')
        initial_revisions = (analysis['revision'], scenarios['revision'], cases['revision'])
        assert all(revision >= 1 for revision in initial_revisions)
        assert cases['items'][0]['steps'][0]['expected'] == 'User is authenticated'
        assert sum(task == 'review_cases' for task, _ in model.calls) == 1

        before_run = copy.deepcopy(app.state.store.run(run['id']))
        before_scenarios = copy.deepcopy(scenarios)
        estimate = _turn(client, model, chat['id'], 2,
            '先估算当前场景需要多少条用例，不要生成、不要推进主任务。',
            [{'name': 'artifact.estimate', 'arguments': {'artifact_id': scenarios['id']}}],
            artifact_id=scenarios['id'], artifact_revision=scenarios['revision'])
        estimate_data = _part(estimate, 'estimate')['data']
        assert (estimate_data['min_count'], estimate_data['max_count']) == (2, 4)
        assert app.state.store.get('artifact', scenarios['id']) == before_scenarios
        assert app.state.store.run(run['id']) == before_run

        explained = _turn(client, model, chat['id'], 3,
            '用产品经理能理解的话总结当前用例及其编号，不要修改。',
            [{'name': 'artifact.analyze', 'arguments': {'artifact_id': cases['id']}}],
            artifact_id=cases['id'], artifact_revision=cases['revision'])
        assert cases['items'][0]['id'] in _part(explained, 'answer')['text']
        assert app.state.store.get('artifact', cases['id']) == cases

        reviewed = _turn(client, model, chat['id'], 4,
            '只读评审当前用例，并展示实际步骤和预期结果，不要修改。',
            [{'name': 'artifact.review_cases', 'arguments': {'artifact_id': cases['id']}}],
            artifact_id=cases['id'], artifact_revision=cases['revision'])
        review = _part(reviewed, 'answer')
        details = _part(reviewed, 'case_details')
        assert details['items'][0]['steps'][0] == cases['items'][0]['steps'][0]
        assert '尚未执行测试' in review['report']['summary']
        assert app.state.store.get('artifact', cases['id']) == cases

        preview = _turn(client, model, chat['id'], 5,
            '把当前场景改成登录后同步会话状态，并同步关联用例；先预览，不要推进主任务。',
            [{'name': 'artifact.preview', 'arguments': {'artifact_id': scenarios['id'],
                'sync_related': True, 'related_artifact_ids': [cases['id']]}}],
            artifact_id=scenarios['id'], artifact_revision=scenarios['revision'])
        diff = _part(preview, 'diff')
        assert {change['artifact_id'] for change in diff['changes']} == {scenarios['id'], cases['id']}
        assert app.state.store.get('artifact', scenarios['id']) == scenarios
        assert app.state.store.get('artifact', cases['id']) == cases
        applied = _turn(client, model, chat['id'], 6, '应用刚才的场景和关联用例修改。',
            [{'name': 'artifact.apply', 'arguments': {'proposal_id': diff['proposal_id']}}])
        assert applied['status'] == 'succeeded'
        scenarios = app.state.store.get('artifact', scenarios['id'])
        cases = app.state.store.get('artifact', cases['id'])
        assert scenarios['revision'] == initial_revisions[1] + 1
        assert cases['revision'] == initial_revisions[2] + 1
        assert '同步会话状态' in scenarios['items'][0]['title']
        assert '其他活动会话' in cases['items'][0]['steps'][0]['expected']
        assert app.state.store.run(run['id']) == before_run

        updated = _turn(client, model, chat['id'], 7,
            '新增规则：登录成功后必须注销该账号的其他活动会话。请分析影响并更新需求、场景和用例。',
            [{'name': 'project.update_from_sources', 'arguments': {'artifact_id': analysis['id'],
                'content': '登录成功后必须注销该账号的其他活动会话。',
                'targets': ['analysis', 'scenarios', 'cases']}}],
            artifact_id=analysis['id'], artifact_revision=analysis['revision'])
        assert updated['status'] == 'succeeded', updated
        analysis = app.state.store.get('artifact', analysis['id'])
        scenarios = app.state.store.get('artifact', scenarios['id'])
        cases = app.state.store.get('artifact', cases['id'])
        assert (analysis['revision'], scenarios['revision'], cases['revision']) == (
            initial_revisions[0] + 1, initial_revisions[1] + 2, initial_revisions[2] + 2)
        assert '其他活动会话' in analysis['items'][0]['description']
        assert '其他活动会话' in cases['items'][0]['steps'][0]['expected']

        coverage = _turn(client, model, chat['id'], 8,
            '展示当前需求、场景和用例的结构覆盖及是否过期，不要修改。',
            [{'name': 'artifact.coverage', 'arguments': {'artifact_id': cases['id']}}],
            artifact_id=cases['id'], artifact_revision=cases['revision'])
        coverage_data = _part(coverage, 'coverage')['data']
        assert {key: coverage_data['coverage']['totals'][key]
                for key in ('requirements', 'scenarios', 'cases')} == {
                    'requirements': 1, 'scenarios': 1, 'cases': 1}
        assert not coverage_data['stale']['analysis']
        assert not coverage_data['stale']['scenarios']

        exported = _turn(client, model, chat['id'], 9,
            '把最新测试场景和测试用例分别导出为 Excel。',
            [{'name': 'artifact.export', 'arguments': {
                'artifact_ids': [scenarios['id'], cases['id']]}}])
        files = _part(exported, 'files')['files']
        assert len(files) == 2
        rows = {}
        for file in files:
            response = client.get(file['url'])
            assert response.status_code == 200
            sheet = load_workbook(io.BytesIO(response.content)).active
            rows[file['artifact_id']] = list(sheet.values)
        assert rows[scenarios['id']][1][1] == '登录并同步会话状态'
        assert rows[cases['id']][1][1] == '登录后注销其他活动会话'
        assert '其他活动会话被注销' in ' '.join(str(cell) for cell in rows[cases['id']][1] if cell)

        # Each request body carried user prose and optional UI artifact context,
        # never a command object or legacy intent selector.
        turns = app.state.store.list('conversation_turn', chat_id=chat['id'])
        assert len(turns) == 9
        assert all('command' not in turn['_body'] and 'intent' not in turn['_body'] for turn in turns)
        assert all(turn['_graph_version'] == 'turn-v270' for turn in turns)
        context_response = client.get('/api/chats/' + chat['id'] + '/turns/' + estimate['id'] + '/contexts')
        assert context_response.status_code == 200, context_response.text
        contexts = context_response.json()['items']
        assert {'conversation_turn', 'artifact_estimate'} <= {item['task'] for item in contexts}
        estimate_context = next(item for item in contexts if item['task'] == 'artifact_estimate')
        assert estimate_context['status'] == 'succeeded'
        assert estimate_context['request_budget']['fits']
        assert estimate_context['evidence_ids'] == []
        assert estimate_context['context_manifest']['artifacts'][0]['revision'] == initial_revisions[1]
        assert all(app.state.store.run(row['id']).get('graph_version') == 7
                   for row in app.state.store.runs(chat_id=chat['id']))
        assert project['id'] == app.state.store.get('chat', chat['id'])['project_id']
