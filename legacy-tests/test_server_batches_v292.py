"""Real graphs and durable store; the controlled model rejects oversized inputs."""
import copy

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from tcg.server_capacity import ContextCapacityError
from test_backend_api import setup_chat, start, until
from test_workflow_v25 import FlowModel


class ServerModel(FlowModel):
    def __init__(self, overflow_tasks=(), paginate=False):
        super().__init__()
        self.overflow_tasks = set(overflow_tasks)
        self.paginate = paginate
        self.block_last = False

    async def generate(self, task, context):
        field = {'analyze_requirement': 'evidence', 'generate_scenarios': 'analysis',
                 'generate_cases': 'scenarios', 'review_cases': 'cases',
                 'complete_case_fields': 'cases', 'reconcile_requirements': 'requirements'}.get(task)
        if field is None:
            return await super().generate(task, context)
        self.calls.append((task, copy.deepcopy(context)))
        rows = context[field]
        if task in self.overflow_tasks and len(rows) > 1:
            raise ContextCapacityError(32768)
        if task == 'generate_cases' and self.block_last and any(row['title'] == 'Rule 3' for row in rows):
            raise ContextCapacityError(32768)
        if task == 'analyze_requirement':
            return {'items': [{'id': 'REQ-' + str(i), 'title': row['text'], 'description': row['text'],
                              'refs': [row['id']]} for i, row in enumerate(rows)],
                    'report': {'summary': 'Rules', 'questions': [], 'assumptions': [],
                               'diagrams': [{'mermaid': 'flowchart TD\n A[Input] --> B[Check]'}]}}
        if task == 'generate_scenarios':
            return {'items': [{'id': 'SC-' + row['id'], 'title': row['title'], 'description': row['description'],
                              'priority': 'P1', 'refs': row['refs'], 'requirement_ids': [row['id']]} for row in rows],
                    'has_more': False}
        if task == 'generate_cases':
            if self.paginate and len(rows) > 2 and context.get('cursor'):
                raise ContextCapacityError(32768)
            old = {row['id'] for row in context.get('previous_items', [])}
            items = [{'id': 'TC-' + row['id'], 'title': row['title'], 'scenario_id': row['id'],
                      'type': 'Business', 'priority': 'P1', 'preconditions': 'Feature available',
                      'steps': [{'action': 'Check rule', 'expected': row['title']}], 'refs': row['refs']}
                     for row in rows if 'TC-' + row['id'] not in old]
            more = self.paginate and len(rows) > 2 and not context.get('cursor')
            return {'items': items[:1] if more else items, 'has_more': more, 'next_cursor': 'next' if more else None}
        if task == 'review_cases':
            return {'operations': [], 'report': {'summary': 'Checked'}}
        if task == 'complete_case_fields':
            return {'items': [{'id': row['id'], 'fields': {field: 'Verify ' + row['title']
                        for field in context['missing_fields'][row['id']]}, 'unresolved': {}} for row in rows]}
        if task == 'reconcile_requirements':
            return {'relationships': [], 'global_rules': [], 'conflicts': []}
        raise AssertionError(task)


def setup_four(client):
    return setup_chat(client, '\n\n'.join('Rule ' + str(i) for i in range(4)))


def finish_human(client, run):
    gates = []
    while run['status'] == 'waiting':
        gates.append(run['interrupt']['type'])
        response = client.post('/api/runs/' + run['id'] + '/resume', json={'approved': True})
        assert response.status_code == 200, response.text
        run = until(client, run)
    return run, gates


@pytest.mark.parametrize('mode', ['auto', 'hitp'])
def test_server_rejections_split_all_stages_and_keep_human_gates(tmp_path, mode):
    model = ServerModel({'analyze_requirement', 'generate_scenarios', 'generate_cases',
                         'review_cases', 'complete_case_fields', 'reconcile_requirements'})
    with TestClient(create_app(tmp_path, model)) as client:
        project, chat, _ = setup_four(client)
        profile = client.post('/api/projects/' + project['id'] + '/profiles', json={'name': 'Fields',
            'config': {'excel_columns': [{'field': 'title', 'header': 'Title'},
                {'field': 'purpose', 'header': 'Purpose', 'value_source': 'ai', 'required': True}]}}).json()
        run = until(client, start(client, chat, experience='reliable', mode=mode, profile_id=profile['id']))
        run, gates = finish_human(client, run)
        assert run['status'] == 'completed', run
        if mode == 'hitp':
            assert gates == ['strategy_review', 'scenario_review', 'case_draft_review', 'case_result_review']
        else:
            assert gates == []
        artifact = next(value for value in (client.get('/api/artifacts/' + aid).json() for aid in run['artifact_ids']) if value['type'] == 'cases')
        assert len(artifact['items']) == 4
        assert len({row['id'] for row in artifact['items']}) == 4
        assert all(row['purpose'] for row in artifact['items'])
        for task, field in [('analyze_requirement', 'evidence'), ('generate_scenarios', 'analysis'),
                            ('generate_cases', 'scenarios'), ('review_cases', 'cases')]:
            calls = [context for t, context in model.calls if t == task]
            assert len(calls[0][field]) == 4, (task, calls)
            assert len(calls) == 7, (task, calls)


def test_retry_after_restart_reuses_completed_subgroups_and_prior_stages(tmp_path):
    model = ServerModel({'generate_cases'})
    model.block_last = True
    app = create_app(tmp_path, model)
    with TestClient(app) as client:
        _, chat, _ = setup_four(client)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'failed' and '单个业务条目' in run['error'], run
        successful = [tuple(row['id'] for row in context['scenarios']) for task, context in model.calls
                      if task == 'generate_cases' and len(context['scenarios']) == 1 and context['scenarios'][0]['title'] != 'Rule 3']
        assert len(successful) == 3
        prior_count = len(model.calls)
    model.block_last = False
    with TestClient(create_app(tmp_path, model)) as client:
        response = client.post('/api/runs/' + run['id'] + '/retry', json={})
        assert response.status_code == 200, response.text
        run = until(client, run)
        assert run['status'] == 'completed', run
        later = model.calls[prior_count:]
        assert not any(task in ('analyze_requirement', 'generate_scenarios') for task, _ in later)
        cases_calls = [context for task, context in later if task == 'generate_cases']
        assert len(cases_calls) == 1 and cases_calls[0]['scenarios'][0]['title'] == 'Rule 3'


def test_later_page_overflow_keeps_accepted_rows_and_does_not_recall_page(tmp_path):
    model = ServerModel(paginate=True)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_four(client)
        run = until(client, start(client, chat, experience='reliable'))
        assert run['status'] == 'completed', run
        artifact = next(value for value in (client.get('/api/artifacts/' + aid).json() for aid in run['artifact_ids']) if value['type'] == 'cases')
        assert len(artifact['items']) == 4
        assert sorted(row['title'] for row in artifact['items']) == ['Rule 0', 'Rule 1', 'Rule 2', 'Rule 3']
        calls = [context for task, context in model.calls if task == 'generate_cases']
        assert [len(context['scenarios']) for context in calls] == [4, 4, 2, 2]
        assert len(calls[2]['previous_items']) == 1
        assert artifact['items'][0]['id'].startswith('C1-')


def test_field_completion_splits_without_regenerating_cases(tmp_path):
    model = ServerModel({'complete_case_fields'})
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_four(client)
        run = until(client, start(client, chat, experience='reliable', profile_override={
            'excel_columns': [{'field': 'purpose', 'header': 'Purpose', 'value_source': 'ai', 'required': True}]}))
        assert run['status'] == 'completed', run
        assert sum(task == 'generate_cases' for task, _ in model.calls) == 1
        calls = [context for task, context in model.calls if task == 'complete_case_fields']
        assert [len(context['cases']) for context in calls] == [4, 2, 1, 1, 2, 1, 1]
        artifact = next(value for value in (client.get('/api/artifacts/' + aid).json()
                        for aid in run['artifact_ids']) if value['type'] == 'cases')
        assert all(row['purpose'] for row in artifact['items'])
