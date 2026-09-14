"""Conversation-only acceptance over real HTTP and the native Pipeline graph.

Only model inference is controlled. The chat model emits real AIMessage tool
calls, never a JSON command plan; the pipeline generates typed business data.
"""
import copy
import socket
import threading
import time
from collections import Counter
from typing import Any

import httpx
import pytest
import uvicorn
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tcg.main import create_app


SUPPLEMENT = '有效凭证登录成功后，必须立即注销同账号的其他活动会话。'
SHARED_RULE = '已确认：账号连续五次失败时锁定10分钟，期满自动解锁。'


class NativeJourneyChatModel(BaseChatModel):
    gateway: Any = Field(exclude=True)
    tool_names: frozenset[str] = frozenset()

    @property
    def _llm_type(self):
        return 'native-journey-model'

    def bind_tools(self, tools, **kwargs):
        names = {tool.name if hasattr(tool, 'name') else tool['function']['name']
                 for tool in tools}
        self.gateway.bound_tools.append(names)
        return self.model_copy(update={"tool_names": frozenset(names)})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if 'submit_execution_plan' in self.tool_names:
            self.gateway.planner_inputs.append(copy.deepcopy(messages))
            assert self.gateway.next_call is not None
            name, arguments = self.gateway.next_call
            capabilities = {
                'start_pipeline_tool': 'pipeline_start', 'control_pipeline_tool': 'pipeline_control',
                'resume_pipeline_tool': 'pipeline_control', 'modify_artifact_tool': 'artifact_edit',
                'update_from_sources_tool': 'artifact_edit', 'modify_case_columns_tool': 'case_columns',
                'estimate_workload_tool': 'estimate', 'analyze_artifact_tool': 'answer',
                'read_artifact_tool': 'answer', 'list_context_tool': 'answer',
                'add_knowledge_tool': 'knowledge', 'answer_clarification_tool': 'clarification',
                'modify_profile_tool': 'profile_edit', 'learn_template_tool': 'template_learn',
                'revise_review_tool': 'review_edit', 'save_samples_tool': 'samples',
                'complete_template_fields_tool': 'template_fill', 'export_artifact_tool': 'export',
                'apply_profile_tool': 'current_control', 'discard_template_tool': 'current_control'}
            steps = [{'capability': capabilities[name],
                'instruction': '按用户本次原话处理当前请求，不扩大范围。'}]
            if name == 'modify_case_columns_tool' and arguments.get('export_after_approval'):
                steps.append({'capability': 'export', 'instruction': '导出刚才修改并确认后的用例 Excel。'})
                self.gateway.deferred_exports.append({'artifact_id': arguments['artifact_id']})
            plan = {'title': '处理本次请求', 'steps': steps}
            message = AIMessage(content='', tool_calls=[{'name': 'submit_execution_plan', 'args': plan,
                'id': 'planner-' + str(len(self.gateway.planner_inputs)), 'type': 'tool_call'}])
            return ChatResult(generations=[ChatGeneration(message=message)])
        self.gateway.chat_inputs.append(copy.deepcopy(messages))
        if isinstance(messages[-1], ToolMessage):
            self.gateway.tool_results.append(copy.deepcopy(messages[-1]))
            message = AIMessage(content='已处理当前请求，请查看当前成果和下一步提示。')
        else:
            if 'export_artifact_tool' in self.tool_names and self.gateway.deferred_exports:
                name, arguments = 'export_artifact_tool', self.gateway.deferred_exports.pop(0)
            else:
                assert self.gateway.next_call is not None, 'Unexpected model decision without a scripted user turn.'
                name, arguments = self.gateway.next_call
                self.gateway.next_call = None
            assert name in self.tool_names, (name, self.tool_names)
            self.gateway.tool_calls.append((name, copy.deepcopy(arguments)))
            message = AIMessage(content='', tool_calls=[{'name': name, 'args': arguments,
                'id': 'native-call-' + str(len(self.gateway.tool_calls)), 'type': 'tool_call'}])
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


class NativeJourneyGateway:
    def __init__(self):
        self.next_call = None
        self.bound_tools = []
        self.tool_calls = []
        self.tool_results = []
        self.planner_inputs = []
        self.direct_controls = []
        self.deferred_exports = []
        self.chat_inputs = []
        self.generations = []
        self.model = NativeJourneyChatModel(gateway=self)

    def chat_model(self, *args, **kwargs):
        return self.model

    async def generate_native(self, task, context, schema, instruction):
        self.generations.append((task, copy.deepcopy(context)))
        refs = [entry['id'] for entry in context.get('evidence', [])
                if entry.get('role') != 'example']
        report = {'summary': '根据本项目登录需求完成当前阶段。', 'questions': []}
        if task == 'understand_requirements':
            shared = SHARED_RULE if any(SHARED_RULE in entry['text']
                for entry in context.get('evidence', [])) else ''
            return {'items': [{'id': 'REQ-1', 'title': '有效凭证登录',
                'description': '注册用户输入有效凭证后登录成功。' + shared, 'refs': refs}], 'report': report}
        if task == 'generate_scenarios':
            return {'items': [{'id': 'SC-1', 'title': '有效凭证登录成功',
                'description': context['analysis'][0]['description'], 'priority': 'P1',
                'requirement_ids': [context['analysis'][0]['id']], 'refs': refs}], 'report': report}
        if task == 'generate_cases':
            return {'items': [{'id': 'TC-1', 'title': context['scenarios'][0]['title'],
                'description': '验证正确账号密码可以登录。',
                'scenario_id': context['scenarios'][0]['id'], 'type': 'Business',
                'priority': 'P1', 'preconditions': '注册账号存在。',
                'steps': [{'action': '输入有效账号密码并登录', 'expected': SUPPLEMENT
                    if SUPPLEMENT in context['scenarios'][0]['description'] else '登录成功'}],
                'refs': refs}], 'report': report}
        if task == 'review_cases':
            rows = copy.deepcopy(context['cases'])
            rows[0]['title'] = '已评审：' + rows[0]['title']
            return {'items': rows, 'report': {'summary': '已核对步骤与预期结果。', 'issues': []}}
        if task == 'estimate_workload':
            return {'summary': '只估算当前场景，不生成用例。', 'scenarios': [{
                'scenario_id': row['id'], 'min_count': 1, 'max_count': 2,
                'rationale': '覆盖一次成功登录，可增加凭证组合。', 'assumptions': []}
                for row in context['scenarios']]}
        if task == 'explain_artifact':
            return {'answer': '当前内容验证有效凭证登录，步骤和预期一一对应。' +
                str(context['items'][0].get('steps', [])), 'refs': refs}
        if task == 'revise_artifact':
            assert context['artifact_type'] == 'analysis'
            assert any(SUPPLEMENT in entry['text'] for entry in context['evidence'])
            rows = copy.deepcopy(context['items'])
            rows[0]['description'] = SUPPLEMENT
            rows[0]['refs'] = list(dict.fromkeys(rows[0]['refs'] + refs))
            return {'items': rows, 'report': report}
        raise AssertionError('Unexpected native generation task: ' + task)


class NativeJourney:
    def __init__(self, client, app, gateway):
        self.client, self.app, self.gateway = client, app, gateway
        self.sequence = 0
        self.requests = []
        self.project = client.get('/api/projects').json()[0]
        created = client.post('/api/projects/' + self.project['id'] + '/chats',
                              json={'title': 'Native conversation acceptance'})
        assert created.status_code == 200, created.text
        self.chat = created.json()
        uploaded = client.post('/api/chats/' + self.chat['id'] + '/sources',
            files={'file': ('login.md', '注册用户输入有效账号密码后登录成功。'.encode(), 'text/markdown')},
            data={'role': 'primary'})
        assert uploaded.status_code == 200, uploaded.text
        self.source = uploaded.json()

    def snapshot(self):
        response = self.client.get('/api/chats/' + self.chat['id'])
        assert response.status_code == 200, response.text
        return response.json()

    def turn(self, content, tool_name, arguments=None, *, reply=None, mode='hitp', status='succeeded'):
        if reply and reply.get('proposal_id') and tool_name in ('workspace_save', 'resume_pipeline_tool') and status != 'needs_input':
            return self.workspace(reply, reject=(arguments or {}).get('action') == 'rejected')
        self.sequence += 1
        self.gateway.next_call = (tool_name, arguments or {})
        body = {'client_message_id': 'native-journey-' + str(self.sequence),
                'content': content, 'mode': mode}
        if reply:
            body['reply_to'] = reply['id']
        self.requests.append(copy.deepcopy(body))
        response = self.client.post('/api/chats/' + self.chat['id'] + '/turns', json=body)
        assert response.status_code == 200, response.text
        turn = response.json()
        assert turn['status'] == status, turn
        assert isinstance(turn['id'], str) and turn['client_message_id'] == body['client_message_id']
        assert isinstance(turn['message'], str)
        assert all(isinstance(turn[key], list) for key in ('parts', 'pending', 'actions'))
        if self.gateway.next_call is not None:
            invoked = any(action['name'] == tool_name and action['status'] == status
                          for action in turn['actions'])
            stale_control = status == 'needs_input' and reply and any(
                action['name'] == 'current_control' and action['status'] == 'needs_input'
                and action['result'].get('error_status') == 409 for action in turn['actions'])
            assert invoked or stale_control, turn
            self.gateway.direct_controls.append(self.gateway.next_call)
            self.gateway.next_call = None
        return turn

    def workspace(self, prompt, *, reject=False):
        from workspace_helpers import save_workspace
        self.sequence += 1
        result, _ = save_workspace(self.client, prompt, request_id='journey-workspace-' + str(self.sequence), reject=reject)
        return result

    def gate(self, kind):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            assert len(snapshot['runs']) == 1, snapshot
            run = snapshot['runs'][0]
            assert run['status'] != 'failed', run
            if run['status'] == 'waiting':
                assert run['interrupt']['type'] == kind, run
                current = snapshot['conversation_prompt']
                assert current and current['kind'] == kind and current['id'], current
                response = self.client.get('/api/artifacts/' + run['interrupt']['artifact_id'])
                assert response.status_code == 200, response.text
                artifact = response.json()
                assert run['interrupt']['artifact_revision'] == artifact['revision']
                assert run['draft_artifact_id'] == artifact['id']
                return run, artifact, current
            time.sleep(.02)
        raise AssertionError(snapshot)

    def counts(self):
        return Counter(task for task, _ in self.gateway.generations)

    def artifact(self, artifact_id):
        response = self.client.get('/api/artifacts/' + artifact_id)
        assert response.status_code == 200, response.text
        return response.json()

    def completed(self):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            assert len(snapshot['runs']) == 1, snapshot
            run = snapshot['runs'][0]
            assert run['status'] != 'failed', run
            if run['status'] == 'completed':
                assert snapshot['conversation_prompt'] is None
                visible = [m for m in snapshot['messages'] if m.get('metadata', {}).get('artifact_ids')]
                assert visible, 'The unchanged React UI discovers completed outputs from message metadata.'
                assert visible[-1]['metadata']['artifact_ids'][-1] == run['current_artifact_id']
                latest = self.artifact(run['current_artifact_id'])
                assert visible[-1]['metadata']['artifact_revisions'][latest['id']] == latest['revision']
                return run
            time.sleep(.02)
        raise AssertionError(snapshot)


@pytest.fixture
def native_journey(tmp_path):
    gateway = NativeJourneyGateway()
    app = create_app(tmp_path, gateway)
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
            yield NativeJourney(client, app, gateway)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_native_chat_tools_drive_each_human_gate_and_resume_reads_current_artifact(native_journey):
    j = native_journey
    j.turn('根据上传需求生成场景和用例，每一步都等我确认。', 'start_pipeline_tool',
           {'mode': 'hitp', 'stop_after': 'review'})
    run, analysis, understanding = j.gate('strategy_review')
    rid = run['id']
    assert j.counts() == {'understand_requirements': 1}

    j.turn('可以，继续吧', 'resume_pipeline_tool', {'run_id': rid}, reply=understanding)
    run, scenarios, scenario_prompt = j.gate('scenario_review')
    assert scenario_prompt['id'] != understanding['id']
    assert scenarios['items'][0]['requirement_ids'] == [analysis['items'][0]['id']]
    assert j.counts() == {'understand_requirements': 1, 'generate_scenarios': 1}

    j.turn('这些场景大概需要多少条用例？只估算，不生成。', 'estimate_workload_tool',
           {'artifact_id': scenarios['id']}, reply=scenario_prompt)
    after_run, after_scenarios, after_prompt = j.gate('scenario_review')
    assert after_run['id'] == rid and after_prompt['id'] == scenario_prompt['id']
    assert after_scenarios == scenarios
    assert not j.counts().get('generate_cases')

    j.turn('把这个场景标题改成“验证注册用户凭证登录”，先不要继续。', 'modify_artifact_tool',
           {'artifact_id': scenarios['id'], 'item_id': scenarios['items'][0]['id'],
            'new_values': {'title': '验证注册用户凭证登录'}}, reply=scenario_prompt, status='needs_confirmation')
    preview = j.snapshot()['conversation_prompt']
    assert preview['kind'] == 'artifact_proposal'
    j.turn('同意保存场景修改', 'workspace_save', reply=preview)
    run, modified, changed_prompt = j.gate('scenario_review')
    assert modified['id'] == scenarios['id'] and modified['revision'] == scenarios['revision'] + 1
    assert modified['items'][0]['title'] == '验证注册用户凭证登录'
    assert modified['items'][0]['requirement_ids'] == scenarios['items'][0]['requirement_ids']
    assert changed_prompt['id'] != scenario_prompt['id']
    assert not j.counts().get('generate_cases')

    j.turn('同意，继续', 'resume_pipeline_tool', {'run_id': rid}, reply=changed_prompt)
    run, cases, review_prompt = j.gate('case_result_review')
    generation = next(context for task, context in j.gateway.generations if task == 'generate_cases')
    assert generation['scenarios'] == modified['items']
    assert cases['items'][0]['scenario_id'] == modified['items'][0]['id']
    assert cases['items'][0]['title'] == modified['items'][0]['title']
    assert j.counts()['review_cases'] == 1
    assert review_prompt['changes'][0]['after']['title'].startswith('已评审：')
    j.turn('评审结果可以，完成吧', 'resume_pipeline_tool', {'run_id': rid}, reply=review_prompt)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = j.snapshot()
        assert len(snapshot['runs']) == 1
        completed = snapshot['runs'][0]
        if completed['status'] in ('completed', 'failed'):
            break
        time.sleep(.02)
    assert completed['status'] == 'completed' and completed['id'] == rid, completed
    assert snapshot['conversation_prompt'] is None
    assert j.counts() == {'understand_requirements': 1, 'generate_scenarios': 1,
                          'generate_cases': 1, 'review_cases': 1, 'estimate_workload': 1}
    assert len(j.gateway.tool_calls) + len(j.gateway.direct_controls) == len(j.requests)
    assert len(j.gateway.tool_results) + len(j.gateway.direct_controls) == len(j.requests)
    assert all(not {'command', 'intent', 'intent_hint', 'artifact_id', 'selected_ids'} & body.keys()
               for body in j.requests)


def test_native_chat_can_start_auto_pipeline_without_a_confirmation_turn(native_journey):
    j = native_journey
    j.turn('根据上传需求自动完成场景、用例和评审。', 'start_pipeline_tool',
           {'mode': 'auto', 'stop_after': 'review'}, mode='auto')
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        snapshot = j.snapshot()
        assert len(snapshot['runs']) == 1, snapshot
        completed = snapshot['runs'][0]
        assert completed['status'] != 'waiting', completed
        if completed['status'] in ('completed', 'failed'):
            break
        time.sleep(.02)
    assert completed['status'] == 'completed', completed
    assert snapshot['conversation_prompt'] is None
    assert j.counts() == {'understand_requirements': 1, 'generate_scenarios': 1,
                          'generate_cases': 1, 'review_cases': 1}
    assert len(j.gateway.tool_calls) == len(j.gateway.tool_results) == 1


def test_completed_pipeline_supplement_preview_and_shared_clarification_close_the_chat_loop(native_journey):
    j = native_journey
    j.turn('生成场景和用例，每步等我确认。', 'start_pipeline_tool',
           {'mode': 'hitp', 'stop_after': 'review'})
    initial = {}
    for kind in ('strategy_review', 'scenario_review', 'case_result_review'):
        run, artifact, pending = j.gate(kind)
        initial[artifact['type']] = artifact
        j.turn('同意，继续', 'resume_pipeline_tool', {'run_id': run['id']}, reply=pending)
    completed = j.completed()
    rid = completed['id']
    old_analysis, old_scenarios, old_cases = (j.artifact(initial[k]['id']) for k in ('analysis', 'scenarios', 'cases'))

    # Uploading a file does not approve changes or reopen the completed graph.
    uploaded = j.client.post('/api/chats/' + j.chat['id'] + '/sources',
        files={'file': ('session-supplement.md', SUPPLEMENT.encode(), 'text/markdown')},
        data={'role': 'supplement'})
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()
    assert j.snapshot()['runs'][0]['status'] == 'completed'
    assert j.artifact(old_analysis['id'])['revision'] == old_analysis['revision']

    j.turn('新文件是补充需求，先修改需求理解，后续每一步仍然等我确认。',
           'update_from_sources_tool', {'artifact_id': old_analysis['id'],
            'source_ids': [source['id']], 'instruction': '采用补充需求修改登录成功后的会话处理。'}, status='needs_confirmation')
    j.workspace(j.snapshot()['conversation_prompt'])
    run, analysis, analysis_prompt = j.gate('strategy_review')
    assert run['id'] == rid and analysis['id'] == old_analysis['id']
    assert analysis['revision'] == old_analysis['revision'] + 1
    assert analysis['items'][0]['description'] == SUPPLEMENT
    source_view = j.client.get('/api/sources/' + source['id']).json()
    supplement_refs = {chunk['id'] for chunk in source_view['chunks']}
    assert supplement_refs <= set(analysis['items'][0]['refs'])
    assert j.artifact(old_scenarios['id']) == old_scenarios
    assert j.artifact(old_cases['id']) == old_cases

    j.turn('需求理解同意，继续', 'resume_pipeline_tool', {'run_id': rid}, reply=analysis_prompt)
    run, scenarios, scenario_prompt = j.gate('scenario_review')
    assert run['id'] == rid and scenarios['id'] == old_scenarios['id']
    assert scenarios['revision'] == old_scenarios['revision'] + 1
    assert SUPPLEMENT in scenarios['items'][0]['description']
    assert supplement_refs <= set(scenarios['items'][0]['refs'])
    assert j.artifact(old_cases['id']) == old_cases
    generation_counts = j.counts()

    estimated = j.turn('按更新后的场景估算用例数量，先别生成。', 'estimate_workload_tool',
                       {'artifact_id': scenarios['id']}, reply=scenario_prompt)
    estimate = next(p for p in estimated['parts'] if p['type'] == 'estimate')['data']
    assert (estimate['min_count'], estimate['max_count']) == (1, 2)
    explained = j.turn('解释已有用例的步骤和预期，暂时不要继续。', 'analyze_artifact_tool',
        {'artifact_id': old_cases['id'], 'instruction': '解释已有用例的步骤和预期。'}, reply=scenario_prompt)
    answer = next(p for p in explained['parts'] if p['type'] == 'answer')
    assert '登录成功' in answer['text']
    assert j.gate('scenario_review')[2]['id'] == scenario_prompt['id']
    assert j.artifact(old_cases['id']) == old_cases
    assert j.counts()['generate_cases'] == generation_counts['generate_cases']

    # Preview is a separate confirmation object. Applying it must not also
    # approve the scenario gate, and its old token cannot apply the edit twice.
    j.turn('场景标题改成“登录成功并注销其他会话”，先给我预览。', 'modify_artifact_tool',
        {'artifact_id': scenarios['id'], 'item_id': scenarios['items'][0]['id'],
         'new_values': {'title': '登录成功并注销其他会话'}, 'preview': True},
        reply=scenario_prompt, status='needs_confirmation')
    proposal = j.snapshot()['conversation_prompt']
    assert proposal['kind'] == 'artifact_proposal'
    assert j.artifact(scenarios['id']) == scenarios
    j.turn('同意这个修改', 'workspace_save', reply=proposal)
    run, modified, modified_prompt = j.gate('scenario_review')
    assert modified['revision'] == scenarios['revision'] + 1
    assert modified['items'][0]['title'] == '登录成功并注销其他会话'
    assert j.artifact(old_cases['id']) == old_cases
    stale = j.client.post('/api/artifacts/' + modified['id'] + '/workspace-grid/save', json={
        'expected_revision': proposal['artifact_revision'], 'proposal_id': proposal['proposal_id'],
        'prompt_id': proposal['id'], 'items': modified['items'], 'client_request_id': 'stale-second-save'})
    assert stale.status_code == 409
    assert j.artifact(modified['id']) == modified
    assert j.gate('scenario_review')[2]['id'] == modified_prompt['id']

    j.turn('场景同意，生成关联用例', 'resume_pipeline_tool', {'run_id': rid}, reply=modified_prompt)
    run, cases, review_prompt = j.gate('case_result_review')
    assert cases['id'] == old_cases['id']
    assert cases['items'][0]['steps'][0]['expected'] == SUPPLEMENT
    assert supplement_refs <= set(cases['items'][0]['refs'])
    assert j.counts()['generate_cases'] == j.counts()['review_cases'] == 2
    j.turn('评审同意，完成吧', 'resume_pipeline_tool', {'run_id': rid}, reply=review_prompt)
    assert j.completed()['id'] == rid
    assert j.counts()['understand_requirements'] == 1
    assert j.counts()['generate_scenarios'] == j.counts()['generate_cases'] == j.counts()['review_cases'] == 2

    saved = j.turn('项目规则确认如下，请保存供同项目同事复用：' + SHARED_RULE,
        'add_knowledge_tool', {'content': SHARED_RULE, 'role': 'clarification', 'share': True, 'confirmed': True})
    shared_id = saved['actions'][0]['result']['source']['id']
    shared_view = j.client.get('/api/sources/' + shared_id).json()
    shared_refs = {chunk['id'] for chunk in shared_view['chunks']}
    assert j.snapshot()['runs'][0]['status'] == 'completed'

    # A distinct chat in the same project starts with its own uploaded file.
    # Shared knowledge must be selected by the server, not passed as source_ids.
    colleague = NativeJourney(j.client, j.app, j.gateway)
    assert colleague.chat['id'] != j.chat['id'] and colleague.project['id'] == j.project['id']
    first_colleague_call = len(j.gateway.chat_inputs)
    colleague.turn('根据我上传的需求生成场景和用例，先让我确认理解。',
        'start_pipeline_tool', {'mode': 'hitp', 'stop_after': 'review'})
    _, colleague_analysis, _ = colleague.gate('strategy_review')
    context = next(context for task, context in reversed(j.gateway.generations)
                   if task == 'understand_requirements')
    assert any(entry['source_id'] == shared_id and SHARED_RULE in entry['text'] for entry in context['evidence'])
    assert SHARED_RULE in colleague_analysis['items'][0]['description']
    assert shared_refs <= set(colleague_analysis['items'][0]['refs'])
    assert any('CURRENT TRUSTED STATE' in str(message.content) and shared_id in str(message.content)
               for message in j.gateway.chat_inputs[first_colleague_call])
    assert all(not {'command', 'intent', 'intent_hint', 'artifact_id', 'selected_ids', 'source_ids'} & body.keys()
               for body in j.requests + colleague.requests)
