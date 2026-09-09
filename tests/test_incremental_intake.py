import copy

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import start, until
from test_incremental_agent import WorkModel


class IntakeModel(WorkModel):
    classification = 'requirement'

    async def generate(self, task, context):
        if task in ('work_route', 'work_intake'):
            self.calls.append((task, copy.deepcopy(context)))
            return {'intent': 'generate_case', 'depth': 'standard',
                    'classification': self.classification}
        return await super().generate(task, context)


def empty_chat(client):
    project = client.get('/api/projects').json()[0]
    return client.post('/api/projects/' + project['id'] + '/chats', json={'title': '直接聊天'}).json()


@pytest.mark.parametrize('intent', ['auto', 'generate_case'])
def test_typed_business_requirement_works_without_upload_or_checkbox(tmp_path, intent):
    model = IntakeModel()
    text = '用户使用用户名和密码登录，连续失败五次锁定三十分钟。'
    with TestClient(create_app(tmp_path, model)) as client:
        chat = empty_chat(client)
        run = until(client, start(client, chat, experience='agent', intent=intent, content=text))
        assert run['status'] == 'completed', run.get('error')
        sources = client.get('/api/chats/' + chat['id']).json()['sources']
        assert len(sources) == 1
        assert client.get('/api/sources/' + sources[0]['id']).json()['text'] == text
        assert sum(task in ('work_route', 'work_intake') for task, _ in model.calls) == 1
        assert run['agent']['work']['completed'] == run['agent']['work']['total']


def test_bare_generation_request_waits_for_business_input_then_continues(tmp_path):
    model = IntakeModel()
    model.classification = 'instruction'
    with TestClient(create_app(tmp_path, model)) as client:
        chat = empty_chat(client)
        run = until(client, start(client, chat, experience='agent', intent='auto', content='生成用例'))
        assert run['status'] == 'waiting'
        assert run['interrupt']['type'] == 'clarification'
        assert not client.get('/api/chats/' + chat['id']).json()['sources']
        client.post('/api/runs/' + run['id'] + '/resume',
                    json={'answer': '用户通过用户名和密码登录，密码至少六位。'}).raise_for_status()
        run = until(client, run, statuses=('completed', 'failed'))
        assert run['status'] == 'completed', run.get('error')
        assert sum(task == 'work_analyze' for task, _ in model.calls) == 1
