import json
from copy import deepcopy

import pytest

from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import Model, setup_chat, start, until


class MissingAction(Model):
    def __init__(self, repaired=True):
        super().__init__()
        self.repaired = repaired

    async def generate(self, task, context):
        result = await super().generate(task, context)
        if task == 'generate_cases':
            result['items'][0]['steps'].append({'action':'Log out', 'expected':'Session ends'})
            if not context.get('validation_repair') or not self.repaired:
                result['items'][0]['steps'][1] = {'expected':'PRIVATE-EXPECTED-RESULT'}
        return result


def test_generation_repairs_missing_second_step_action_before_review(tmp_path):
    model = MissingAction()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert artifact['items'][0]['steps'][1] == {'action':'Log out', 'expected':'Session ends'}
        contexts = [c for t, c in model.calls if t == 'generate_cases']
        assert len(contexts) == 2
        issue = contexts[1]['validation_repair']['validation_error']
        assert issue == {'code':'type_mismatch','path':'items[0].steps[1].action','expected':'string','actual':'missing'}
        assert len([t for t, _ in model.calls if t == 'review_cases']) == 1
        rows = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        assert any(e['event'] == 'cases.repair_complete' for e in rows)
        assert 'PRIVATE-' not in json.dumps(rows)


def test_bad_generation_repair_remains_failed_then_resumes_same_page(tmp_path):
    model = MissingAction(repaired=False)
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed'
        assert len([t for t, _ in model.calls if t == 'generate_cases']) == 2
        assert not any(t == 'review_cases' for t, _ in model.calls)
        assert client.app.state.store.cache_get(run['id'], 'cases_valid:0') is None
        assert 'steps[1].action' in run['error']
    model = MissingAction()
    with TestClient(create_app(tmp_path, model)) as client:
        assert client.post('/api/runs/' + run['id'] + '/retry').status_code == 200
        run = until(client, run)
        assert run['status'] == 'completed', run
        assert [t for t, _ in model.calls] == ['generate_cases', 'review_cases']


@pytest.mark.parametrize('change', ['drop', 'identity', 'continuation', 'valid_sibling'])
def test_format_repair_cannot_drop_cases_or_change_valid_page_content(tmp_path, change):
    class LossyRepair(MissingAction):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task == 'generate_cases':
                sibling = deepcopy(result['items'][0])
                sibling.update(id='TC-2', title='Another valid case', steps=[{'action':'Check access', 'expected':'Access follows requirement'}])
                result['items'].append(sibling)
                result.update(has_more=True, next_cursor='page-two')
                if context.get('validation_repair'):
                    if change == 'drop':
                        result['items'].pop()
                    elif change == 'identity':
                        result['items'][0]['id'] = 'TC-OTHER'
                    elif change == 'continuation':
                        result.update(has_more=False, next_cursor=None)
                    else:
                        result['items'][1]['title'] = 'Unrequested change'
            return result
    model = LossyRepair()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'failed', run
        assert client.app.state.store.cache_get(run['id'], 'cases_valid:0') is None
        assert not any(t == 'review_cases' for t, _ in model.calls)
        rows = client.get('/api/runs/' + run['id'] + '/diagnostics').json()['events']
        failure = next(e for e in rows if e['event'] == 'node.error')
        assert failure['validation_error']['code'] == 'repair_changed_page'


@pytest.mark.parametrize('invalid_metadata', [None, 'duplicate_id', 'cursor'])
def test_repaired_page_keeps_valid_sibling_and_continues_all_pages(tmp_path, invalid_metadata):
    class Paged(Model):
        async def generate(self, task, context):
            result = await super().generate(task, context)
            if task != 'generate_cases':
                return result
            if context['cursor']:
                result['items'][0].update(id='TC-3', title='Final page case')
                return result
            sibling = deepcopy(result['items'][0])
            sibling.update(id='TC-2', title='Valid sibling')
            result['items'].append(sibling)
            result.update(has_more=True, next_cursor='page-two')
            if not context.get('validation_repair'):
                result['items'][0]['steps'][0].pop('action')
                if invalid_metadata == 'duplicate_id':
                    sibling['id'] = 'TC-1'
                elif invalid_metadata == 'cursor':
                    result['next_cursor'] = ''
            return result
    model = Paged()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client)
        run = until(client, start(client, chat))
        assert run['status'] == 'completed', run
        artifact = client.get('/api/artifacts/' + run['artifact_ids'][0]).json()
        assert [i['id'] for i in artifact['items']] == ['TC-1', 'TC-2', 'TC-3']
        assert artifact['items'][1]['title'] == 'Valid sibling'
        assert len([t for t, _ in model.calls if t == 'generate_cases']) == 3
