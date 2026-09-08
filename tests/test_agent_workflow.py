import asyncio
import copy
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import setup_chat, start, until
from test_backend_api import Model as LegacyModel


class AgentModel:
    def __init__(self, invalid=None, questions=False, missing=False, block=None):
        self.calls = []
        self.invalid, self.questions, self.missing = invalid, questions, missing
        self.block = block
        self.entered, self.release = threading.Event(), threading.Event()

    async def generate(self, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        if task == self.block and not self.release.is_set():
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(.01)
        refs = [e['id'] for e in context.get('evidence', []) if e['role'] != 'example']
        if task == 'agent_feedback':
            return {'proceed': False, 'has_changes': True}
        if task == 'agent_plan':
            actions = context['available_actions']
            action = actions[0]
            if self.invalid == 'planner':
                action = 'finish'
            return {'depth': 'standard', 'rationale': 'Authentication has success and rejection paths.',
                    'plan': [{'id': 'analyze', 'title': 'Analyze authentication'}, {'id': 'scenarios', 'title': 'Design both paths'}, {'id': 'cases', 'title': 'Specify observable cases'}, {'id': 'check', 'title': 'Check design coverage'}],
                    'next_action': action, 'insight': {'summary': 'Design both authentication branches using the supplied requirement.', 'refs': refs}}
        if task == 'agent_analyze':
            badrefs = ['fabricated'] if self.invalid == 'evidence' else refs
            model = {'nodes': [{'id': 'entry', 'label': 'Credentials', 'refs': badrefs}, {'id': 'result', 'label': 'Result', 'refs': refs}],
                     'edges': [{'id': 'accepted', 'from': 'entry', 'to': 'missing' if self.invalid == 'graph' else 'result', 'label': 'Valid', 'refs': refs}, {'id': 'rejected', 'from': 'entry', 'to': 'result', 'label': 'Invalid', 'refs': refs}]}
            return {'items': [{'id': 'R1', 'title': 'Authenticate', 'description': 'Accept valid credentials and reject invalid credentials.', 'refs': refs}],
                    'report': {'summary': 'Authentication has accepted and rejected outcomes.', 'questions': ['What is the lockout threshold?'] if self.questions and not context.get('instructions') else [], 'assumptions': ['No lockout policy supplied'], 'business_model': model,
                               'strategy': {'depth': context['depth'], 'rationale': 'Two outcomes require positive and negative tests.', 'techniques': ['equivalence partitions', 'boundaries'], 'scope': ['Authentication']}}}
        if task == 'agent_scenarios':
            existing = context.get('previous_items', [])
            return {'items': [] if existing else [{'id': 'S1', 'title': 'Authentication paths', 'description': 'Exercise both credential outcomes', 'priority': 'P1', 'refs': refs, 'requirement_ids': ['R1'], 'branch_ids': ['accepted', 'rejected']}], 'has_more': False}
        if task == 'agent_cases':
            return {'items': [] if self.missing or context.get('previous_items') else [{'id': 'C1', 'title': 'Credential outcomes', 'scenario_id': 'S1', 'type': 'Business', 'priority': 'P1', 'preconditions': 'Account exists', 'steps': [{'action': 'Submit valid then invalid credentials', 'expected': 'Success then rejection'}], 'refs': refs, 'requirement_ids': ['R1'], 'branch_ids': ['accepted', 'rejected']}], 'has_more': False}
        if task == 'agent_summary':
            return {'summary': 'Designed one credential case covering both confirmed branches. This is design coverage; no tests were executed.', 'refs': refs}
        raise AssertionError(task)


def test_agent_real_graph_strategy_approval_coverage_memory_and_events(tmp_path):
    model = AgentModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        assert run.get('experience') == 'agent'
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'strategy_review'
        assert run['agent']['plan'] and run['graph_version'] == 2
        analysis = client.get('/api/artifacts/' + run['interrupt']['artifact_id']).json()
        assert analysis['report']['business_model']['edges']
        assert analysis['report']['diagrams'][0]['mermaid'].startswith('flowchart TD')
        assert client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert run['agent']['coverage']['branches_covered'] == 2
        assert 'no tests were executed' in run['agent']['summary']
        assert client.get('/api/chats/' + chat['id']).json()['memory']['decisions']
        assert any(event['kind'] == 'agent' for event in client.app.state.store.events(run['id']))
        assert all(c['depth_guidance'] for t, c in model.calls if t == 'agent_cases')


@pytest.mark.parametrize('invalid', ['graph', 'evidence', 'planner'])
def test_invalid_agent_outputs_never_complete(tmp_path, invalid):
    with TestClient(create_app(tmp_path, AgentModel(invalid=invalid))) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run
        assert run.get('recovery', {}).get('suggestions')


def test_missing_coverage_is_visible_incomplete(tmp_path):
    with TestClient(create_app(tmp_path, AgentModel(missing=True))) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run
        assert run['agent']['coverage']['gaps']
        assert run['recovery']['category'] == 'incomplete_coverage'


def test_blocking_questions_pause_auto_and_feedback_survives_restart(tmp_path):
    model = AgentModel(questions=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'clarification'
    with TestClient(create_app(tmp_path, model)) as client:
        response = client.post('/api/runs/' + run['id'] + '/resume', json={'answer': 'Lock out after five failures.'})
        assert response.status_code == 200, response.text
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert any('five failures' in json.dumps(c.get('instructions')) for t, c in model.calls if t == 'agent_cases')


class PersistentQuestionsModel(AgentModel):
    """An external model that keeps asking even after the user has replied."""
    async def generate(self, task, context):
        result = await super().generate(task, context)
        if task == 'agent_analyze':
            result['report']['questions'] = ['What is the lockout threshold?']
        if task == 'agent_feedback':
            replies = {
                '现有信息已经够用了，请往后设计测试用例。': {'proceed': True, 'has_changes': False},
                '锁定阈值是4次，其他问题先保留，直接生成。': {'proceed': True, 'has_changes': True},
            }
            result = replies.get(context['feedback'], result)
        return result


@pytest.mark.parametrize('endpoint,payload', [
    ('resume', {'proceed': True}),
    ('resume', {'answer': '不要对了，就这样吧'}),
    ('instructions', {'content': '按照一般的系统进行假设'}),
    ('instructions', {'content': '现有信息已经够用了，请往后设计测试用例。'}),
    ('resume', {'proceed': True, 'answer': '现有信息已经够用了，请往后设计测试用例。'}),
])
def test_continue_exits_persistent_clarification_without_reanalyzing(tmp_path, endpoint, payload):
    model = PersistentQuestionsModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        assert run['interrupt']['type'] == 'clarification'
        source_ids = [s['id'] for s in client.get('/api/chats/' + chat['id']).json()['sources']]
    # Existing waiting checkpoints must be usable after an upgrade/restart.
    with TestClient(create_app(tmp_path, model)) as client:
        response = client.post('/api/runs/' + run['id'] + '/' + endpoint, json=payload)
        assert response.status_code == 200, response.text
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert len([t for t, _ in model.calls if t == 'agent_analyze']) == 1
        snapshot = client.get('/api/chats/' + chat['id']).json()
        assert [s['id'] for s in snapshot['sources']] == source_ids
        assert snapshot['memory']['open_questions'] == ['What is the lockout threshold?']
        assert snapshot['memory']['clarification_decision']['mode'] == 'proceed'
        final = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert final['report']['deferred_questions'] == ['What is the lockout threshold?']
        assert final['report']['assumptions'] == ['No lockout policy supplied']
        assert 'What is the lockout threshold?' in run['agent']['summary']
        case_context = next(c for t, c in model.calls if t == 'agent_cases')
        assert case_context['deferred_questions'] == ['What is the lockout threshold?']
        assert all('analyze' not in c['available_actions'] for t, c in model.calls if t == 'agent_plan' and c.get('clarification_decision'))


def test_continue_with_business_changes_applies_them_before_generation(tmp_path):
    model = PersistentQuestionsModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': '锁定阈值是4次，其他问题先保留，直接生成。'})
        assert response.status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert len([t for t, _ in model.calls if t == 'agent_analyze']) == 2
        context = next(c for t, c in model.calls if t == 'agent_cases')
        assert '锁定阈值是4次' in context['instructions'][-1]['content']


def test_reply_that_requests_more_review_does_not_bypass_questions(tmp_path):
    model = PersistentQuestionsModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': '不要继续，先核对需求。'})
        assert response.status_code == 200
        run = until(client, run)
        assert run['status'] == 'waiting', run
        assert not any(t == 'agent_cases' for t, _ in model.calls)


def test_proceed_does_not_bypass_missing_requirement_intake(tmp_path):
    class MissingModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_intake':
                return {'classification': 'instruction', 'question': '请提供业务规则。'}
            return await super().generate(task, context)
    with TestClient(create_app(tmp_path, MissingModel())) as client:
        project = client.get('/api/projects').json()[0]
        chat = client.post('/api/projects/' + project['id'] + '/chats', json={'title': 'Empty'}).json()
        run = until(client, start(client, chat, experience='agent', content='Generate cases'))
        response = client.post('/api/runs/' + run['id'] + '/resume', json={'proceed': True})
        assert response.status_code == 400
        assert client.get('/api/runs/' + run['id']).json()['status'] == 'waiting'


def test_overlapping_feedback_is_rejected_without_losing_the_accepted_reply(tmp_path):
    model = PersistentQuestionsModel(block='agent_feedback')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        first = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': '锁定阈值是4次，其他问题先保留，直接生成。'})
        assert first.status_code == 200
        assert model.entered.wait(4)
        second = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Only administrators are in scope.'})
        assert second.status_code == 409
        model.release.set()
        run = until(client, run)
        assert run['status'] == 'completed', run
        context = next(c for t, c in model.calls if t == 'agent_cases')
        assert '锁定阈值是4次' in context['instructions'][-1]['content']
        assert 'administrators' not in json.dumps(context['instructions'])


def test_continue_during_analysis_is_control_and_survives_restart(tmp_path):
    model = PersistentQuestionsModel(block='agent_analyze')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat, experience='agent')
        assert model.entered.wait(4)
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': '不要对了，就这样吧'})
        assert response.status_code == 200
        assert len(client.get('/api/chats/' + chat['id']).json()['sources']) == 1
    model.release.set()
    with TestClient(create_app(tmp_path, model)) as client:
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert 'What is the lockout threshold?' in run['agent']['summary']


def test_new_business_instruction_after_proceed_reopens_strategy_review(tmp_path):
    model = PersistentQuestionsModel(block='agent_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        client.post('/api/runs/' + run['id'] + '/resume', json={'proceed': True}).raise_for_status()
        assert model.entered.wait(4)
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Only administrator login is in scope.'})
        assert response.status_code == 200, response.text
        model.release.set()
        run = until(client, run)
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'clarification'
        assert client.get('/api/chats/' + chat['id']).json()['memory']['clarification_decision'] is None


def test_continue_at_interrupt_boundary_does_not_leave_task_waiting(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    with TestClient(create_app(tmp_path, PersistentQuestionsModel())) as client:
        graph = client.app.state.engine.agent.graph
        original = graph.aget_state
        async def delayed_snapshot(*args, **kwargs):
            snapshot = await original(*args, **kwargs)
            if snapshot.interrupts and not release.is_set():
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(.01)
            return snapshot
        monkeypatch.setattr(graph, 'aget_state', delayed_snapshot)
        _, chat, _ = setup_chat(client)
        run = start(client, chat, experience='agent')
        assert entered.wait(4)
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': '不要对了，就这样吧'})
        assert response.status_code == 200
        release.set()
        run = until(client, run)
        assert run['status'] == 'completed', run


def test_instruction_during_call_discards_obsolete_result(tmp_path):
    model = AgentModel(block='agent_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = start(client, chat, experience='agent', confirm_strategy=False)
        assert model.entered.wait(4)
        response = client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Only administrator credentials are in scope.'})
        assert response.status_code == 200, response.text
        assert response.json()['run']['agent']['pending_instructions'] == 1
        model.release.set()
        run = until(client, run)
        assert run['status'] == 'completed', run
        contexts = [c for t, c in model.calls if t == 'agent_cases']
        assert len(contexts) == 2
        assert 'administrator' in json.dumps(contexts[-1]['instructions'])
        assert run['agent']['pending_instructions'] == 0


def test_running_instruction_survives_shutdown_and_requires_updated_strategy_approval(tmp_path):
    model = AgentModel(block='agent_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).raise_for_status()
        assert model.entered.wait(4)
        client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Limit scope to administrators.'}).raise_for_status()
    model.release.set()
    with TestClient(create_app(tmp_path, model)) as client:
        run = until(client, run)
        assert run['status'] == 'waiting', run
        assert run['interrupt']['type'] == 'strategy_review'
        client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Include operators too.'}).raise_for_status()
        run = until(client, run)
        assert run['status'] == 'waiting', run
        client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).raise_for_status()
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert 'operators' in json.dumps([c for t, c in model.calls if t == 'agent_cases'][-1]['instructions'])


def test_new_evidence_visibly_supersedes_old_confirmed_decision(tmp_path):
    class ConflictModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_analyze' and context.get('instructions'):
                old = next(d for d in context['memory']['decisions'] if d['id'].startswith('run_'))
                result['report']['conflicts'] = [{'decision_id': old['id'], 'summary': 'Administrator scope supersedes the earlier general scope.', 'refs': context['instructions'][-1]['refs']}]
            return result
    model = ConflictModel(block='agent_cases')
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent'))
        client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True}).raise_for_status()
        assert model.entered.wait(4)
        client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Only administrators.'}).raise_for_status()
        model.release.set()
        run = until(client, run)
        memory = client.get('/api/chats/' + chat['id']).json()['memory']
        assert any(d['status'] == 'superseded' for d in memory['decisions'])
        assert any('supersedes' in i['summary'] for i in run['agent']['insights'])


def test_clear_inline_requirement_classified_and_ambiguous_request_asks(tmp_path):
    class IntakeModel(AgentModel):
        async def generate(self, task, context):
            if task == 'agent_intake':
                return {'classification': 'requirement' if 'must' in context['request']['content'] else 'instruction', 'question': '请提供要测试的业务规则。'}
            return await super().generate(task, context)
    with TestClient(create_app(tmp_path, IntakeModel())) as client:
        project = client.get('/api/projects').json()[0]
        chat = client.post('/api/projects/' + project['id'] + '/chats', json={'title': 'Inline'}).json()
        run = until(client, start(client, chat, experience='agent', content='Users must authenticate with credentials.'))
        assert run['interrupt']['type'] == 'strategy_review', run
        client.post('/api/runs/' + run['id'] + '/cancel')
        other = client.post('/api/projects/' + project['id'] + '/chats', json={'title': 'No requirements'}).json()
        run = until(client, start(client, other, experience='agent', content='Generate cases'))
        assert run['interrupt']['type'] == 'clarification'
        assert client.get('/api/chats/' + other['id']).json()['sources'] == []


class GoalModel(AgentModel):
    def __init__(self, goal='query'):
        super().__init__()
        self.goal = goal

    async def generate(self, task, context):
        if task == 'route':
            self.calls.append((task, copy.deepcopy(context)))
            return {'intent': self.goal}
        if task in ('query', 'modify', 'review_cases', 'learn_template', 'import_cases'):
            return await LegacyModel.generate(self, task, context)
        return await super().generate(task, context)


@pytest.mark.parametrize('intent', ['query', 'auto'])
def test_agent_honors_query_goal_and_auto_routing(tmp_path, intent):
    model = GoalModel()
    # Legacy scripted behavior uses these options.
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent=intent, content='What happens for valid credentials?'))
        assert run['status'] == 'completed', run
        artifacts = [client.get('/api/artifacts/' + aid).json() for aid in run['artifact_ids']]
        assert [a['type'] for a in artifacts] == ['answer']
        assert not any(t == 'agent_cases' for t, _ in model.calls)
        assert any(t == 'route' for t, _ in model.calls) == (intent == 'auto')


@pytest.mark.parametrize('intent,expected', [('review_requirement', 'analysis'), ('generate_scenario', 'scenarios')])
def test_agent_stops_at_explicit_analysis_or_scenario_goal(tmp_path, intent, expected):
    model = AgentModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', intent=intent, confirm_strategy=False))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert artifact['type'] == expected
        assert not any(t == 'agent_cases' for t, _ in model.calls)


def test_agent_selected_modification_preserves_other_cases(tmp_path):
    model = GoalModel()
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        artifact = client.get('/api/artifacts/' + initial['artifact_ids'][-1]).json()
        second = {**artifact['items'][0], 'id': 'C2', 'title': 'Unselected unchanged'}
        client.put('/api/artifacts/' + artifact['id'], json={'expected_revision': 1, 'items': artifact['items'] + [second]}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='modify', artifact_id=artifact['id'], selected_ids=['C1'], content='Rename selected case'))
        assert run['status'] == 'completed', run
        result = client.get('/api/artifacts/' + artifact['id']).json()
        assert result['revision'] == 3
        assert result['items'][0]['title'] == 'Modified through chat'
        assert result['items'][1] == second


def test_planner_can_reassess_and_no_progress_is_bounded(tmp_path):
    class ReassessModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_plan' and context.get('analysis'):
                assert 'analyze' in context['available_actions']
                result['next_action'] = 'analyze'
            return result
    model = ReassessModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run
        assert run['recovery']['category'] == 'incomplete_coverage'
        assert len([t for t, _ in model.calls if t == 'agent_analyze']) <= 4


def test_analysis_cannot_silently_omit_a_supplied_requirement_chunk(tmp_path):
    class OmittingModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_analyze':
                result['items'][0]['refs'] = result['items'][0]['refs'][:1]
            return result
    with TestClient(create_app(tmp_path, OmittingModel())) as client:
        _, chat, _ = setup_chat(client)
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Additional rule', 'text': 'Suspend accounts after five unsuccessful attempts.', 'role': 'supplement'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run


def test_pending_modification_is_not_published_before_final_epoch_guard(tmp_path):
    model = GoalModel()
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        artifact = client.get('/api/artifacts/' + initial['artifact_ids'][-1]).json()
        model.block = 'agent_summary'
        run = start(client, chat, experience='agent', intent='modify', artifact_id=artifact['id'], selected_ids=['C1'], content='Rename selected case')
        assert model.entered.wait(4)
        assert client.get('/api/artifacts/' + artifact['id']).json()['revision'] == artifact['revision']
        client.post('/api/runs/' + run['id'] + '/instructions', json={'content': 'Use a concise title.'}).raise_for_status()
        model.release.set()
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert client.get('/api/artifacts/' + artifact['id']).json()['revision'] == artifact['revision'] + 1


def test_instruction_arriving_at_interrupt_boundary_is_applied_without_extra_user_action(tmp_path):
    model = AgentModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        engine = client.app.state.engine
        original = engine.agent.graph.aget_state
        injected = False
        async def at_boundary(config):
            nonlocal injected
            snapshot = await original(config)
            if snapshot.interrupts and not injected:
                injected = True
                engine.agent.add_instruction(config['configurable']['thread_id'], 'Use administrator scope.')
            return snapshot
        engine.agent.graph.aget_state = at_boundary
        run = until(client, start(client, chat, experience='agent'))
        assert run['status'] == 'waiting', run
        assert run['agent']['pending_instructions'] == 0
        contexts = [c for t, c in model.calls if t == 'agent_analyze']
        assert len(contexts) == 2
        assert 'administrator' in json.dumps(contexts[-1]['instructions'])


@pytest.mark.parametrize('goal', ['review_case', 'learn_template'])
def test_agent_preserves_review_and_template_goals(tmp_path, goal):
    model = GoalModel()
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        run = until(client, start(client, chat, experience='agent', intent=goal, artifact_id=initial['artifact_ids'][-1], content='Review existing cases' if goal == 'review_case' else 'Learn formatting'))
        assert run['status'] == 'completed', run
        output = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert output['type'] == ('cases' if goal == 'review_case' else 'proposal')
        if goal == 'review_case':
            assert output['items'][0]['title'] == 'Reviewed login case'
        else:
            assert output['report']['config']['additional_rules'] == 'Use concise titles'


def test_targeted_coverage_repair_retains_existing_case_and_closes_gap(tmp_path):
    class RepairModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_cases':
                if not context.get('previous_items'):
                    result['items'][0]['branch_ids'] = ['accepted']
                else:
                    original = copy.deepcopy(context['previous_items'][0])
                    result['items'] = [{**original, 'id': 'C2', 'title': 'Invalid credential rejection', 'branch_ids': ['rejected']}]
            return result
    with TestClient(create_app(tmp_path, RepairModel())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run
        assert run['agent']['coverage']['branches_covered'] == 2
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()
        assert [i['id'] for i in artifact['items']] == ['C1', 'C2']
        version = client.get('/api/artifacts/' + artifact['id'] + '/revisions/1').json()
        assert version['report']['coverage'] == run['agent']['coverage']


@pytest.mark.parametrize('depth', ['quick', 'standard', 'deep'])
def test_explicit_depth_changes_generation_guidance(tmp_path, depth):
    class DepthModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_plan':
                result['depth'] = depth
            return result
    model = DepthModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', depth=depth, confirm_strategy=False))
        assert run['status'] == 'completed', run
        context = next(c for t, c in model.calls if t == 'agent_cases')
        assert context['depth'] == context['strategy']['depth'] == depth
        assert {'quick': 'highest-risk', 'standard': 'decision tables', 'deep': 'transition sequences'}[depth] in context['depth_guidance']


def test_depth_cannot_drift_after_strategy_without_reassessment(tmp_path):
    class DriftingModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_plan' and context.get('analysis'):
                result['depth'] = 'deep'
            return result
    with TestClient(create_app(tmp_path, DriftingModel())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run


def test_open_questions_are_durable_and_assumptions_are_not_confirmed(tmp_path):
    model = AgentModel(questions=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'waiting'
        memory = client.get('/api/chats/' + chat['id']).json()['memory']
        assert memory['open_questions'] == ['What is the lockout threshold?']
        assert not memory['decisions']
        assert memory['scope_confirmed'] is False
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.get('/api/chats/' + chat['id']).json()['memory']['open_questions'] == memory['open_questions']


def test_schema_repair_cannot_drop_a_valid_case_to_appear_complete(tmp_path):
    class DroppingModel(AgentModel):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'agent_cases' and not context.get('validation_repair'):
                valid = {**copy.deepcopy(result['items'][0]), 'id': 'C2', 'title': 'Another valid credential partition'}
                result['items'][0]['steps'][0].pop('expected')
                result['items'].append(valid)
            return result
    with TestClient(create_app(tmp_path, DroppingModel())) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run
        assert not any(client.get('/api/artifacts/' + aid).json()['type'] == 'cases' for aid in run['artifact_ids'])


def test_confirmed_memory_reaches_models_beyond_twelve_messages(tmp_path):
    model = GoalModel()
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent'))
        client.post('/api/runs/' + initial['id'] + '/resume', json={'approved': True}).raise_for_status()
        assert until(client, initial)['status'] == 'completed'
        decision = client.get('/api/chats/' + chat['id']).json()['memory']['decisions'][0]
        for index in range(7):
            run = until(client, start(client, chat, experience='agent', intent='query', content=f'Question {index}: when is authentication allowed?'))
            assert run['status'] == 'completed', run
        context = [c for t, c in model.calls if t == 'query'][-1]
        assert len(context['conversation']) == 12
        assert decision in context['memory']['decisions']


@pytest.mark.parametrize('manual', [True, False])
def test_case_deletion_updates_coverage_and_traceability_without_changing_history(tmp_path, manual):
    class DeleteModel(GoalModel):
        async def generate(self, task, context):
            if task == 'modify':
                return {'operations': [{'op': 'delete', 'id': 'C1'}], 'summary': 'Removed requested case'}
            return await super().generate(task, context)
    model = DeleteModel()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        initial = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        aid = initial['artifact_ids'][-1]
        if manual:
            client.put('/api/artifacts/' + aid, json={'expected_revision': 1, 'items': []}).raise_for_status()
        else:
            run = until(client, start(client, chat, experience='agent', intent='modify', artifact_id=aid, selected_ids=['C1'], content='Delete selected case'))
            assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + aid).json()
        assert artifact['items'] == []
        assert artifact['report']['coverage']['requirements_covered'] == 0
        assert artifact['report']['coverage']['branches_covered'] == 0
        assert artifact['report']['coverage']['gaps']
        assert artifact['report']['traceability'] == []
        assert client.get('/api/artifacts/' + aid + '/revisions/1').json()['report']['coverage']['branches_covered'] == 2


def test_template_learning_accepts_example_only_sources(tmp_path):
    model = GoalModel()
    model.delay_task = model.fail_task = None
    model.bad_refs = False
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, source = setup_chat(client)
        client.delete('/api/sources/' + source['id'])
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={'name': 'Format sample', 'text': 'ID, Title, Step, Expected', 'role': 'example'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', intent='learn_template', content='Learn this format'))
        assert run['status'] == 'completed', run
        assert client.get('/api/artifacts/' + run['artifact_ids'][-1]).json()['type'] == 'proposal'
