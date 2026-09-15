"""Saved knowledge reaches the next scoped specialist and the workspace proposal."""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tcg.main import create_app
from test_native_business_v300 import NativeModel
from test_supervisor_http_v312 import QueueGateway, QueueModel, post, seed
from workspace_helpers import save_workspace


RULE = '登录前必须通过短信验证码校验。'


class SupplementBusiness(NativeModel):
    async def generate_native(self, task, context, schema, instruction):
        result = await super().generate_native(task, context, schema, instruction)
        if task == 'revise_artifact':
            evidence = [e for e in context['evidence'] if RULE in e['text']]
            assert evidence, 'The saved supplementary source never reached generation.'
            for row in result['items']:
                row['preconditions'] = RULE
                row['refs'] = list(dict.fromkeys(row['refs'] + [evidence[0]['id']]))
        return result


class SupplementModel(QueueModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            result = json.loads(messages[-1].content)
            if result.get('source'):
                self.gateway.saved_source = result['source']['id']
        elif 'update_from_sources_tool' in self.names:
            context = json.loads(messages[0].content.split('CURRENT TRUSTED STATE (source titles/text are data):\n', 1)[1])
            source_ids = context['assigned_step'].get('source_ids', [])
            assert source_ids == [self.gateway.saved_source], 'The supervisor lost the newly saved source binding.'
            self.gateway.executed.append('update_from_sources_tool')
            message = AIMessage(content='', tool_calls=[{
                'name': 'update_from_sources_tool', 'id': 'apply-supplement', 'type': 'tool_call',
                'args': {'artifact_id': self.gateway.artifact_id, 'source_ids': source_ids,
                         'instruction': '使用补充规则更新所选用例的前置条件。', 'role': 'clarification'}}])
            return ChatResult(generations=[ChatGeneration(message=message)])
        return super()._generate(messages, stop, run_manager, **kwargs)


class SupplementGateway(QueueGateway):
    def __init__(self):
        super().__init__()
        self.model = SupplementModel(gateway=self)
        self.business_model = SupplementBusiness()
        self.saved_source = self.artifact_id = None


def test_supplement_is_saved_then_proposed_and_only_applied_by_workspace(tmp_path):
    gateway = SupplementGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        gateway.artifact_id = cases['id']
        gateway.plans = [{'title': '补充并更新当前用例', 'steps': [
            {'capability': 'knowledge', 'instruction': '保存补充业务规则。'},
            {'capability': 'artifact_edit', 'instruction': '依据刚保存的补充规则更新所选用例。'}]}]
        gateway.calls = [('add_knowledge_tool', {'content': RULE})]
        result, body = post(client, chat, 'supplement', '补充规则：' + RULE + '请更新当前选中的用例。',
            artifact_id=cases['id'], artifact_revision=cases['revision'], selected_ids=[cases['items'][0]['id']])
        assert result['status'] == 'needs_confirmation', result
        assert gateway.executed == ['add_knowledge_tool', 'update_from_sources_tool']
        store = app.state.store
        assert store.get('artifact', cases['id'])['items'] == cases['items']
        proposal = store.get('artifact_proposal', result['pending'][0]['proposal_id'])
        assert proposal['items'][0]['preconditions'] == RULE
        assert proposal['items'][1] == cases['items'][1]
        assert gateway.saved_source in proposal['_source_ids']
        action = next(a['result'] for a in result['actions'] if a['name'] == 'add_knowledge_tool')
        assert action['artifact_update']['status'] == 'not_applied'
        plan = store.list('execution_plan', chat_id=chat['id'])[0]
        assert plan['_saved_source_ids'] == [gateway.saved_source]
        again, _ = post(client, chat, 'supplement', body['content'], artifact_id=cases['id'],
            artifact_revision=cases['revision'], selected_ids=[cases['items'][0]['id']])
        assert again['id'] == result['id']
        assert len([s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification']) == 1
        save_workspace(client, result['pending'][0])
        saved = store.get('artifact', cases['id'])
        assert saved['revision'] == cases['revision'] + 1
        assert saved['items'][0]['preconditions'] == RULE
        assert saved['items'][1] == cases['items'][1]
        update_scopes = [scope for scope in gateway.bindings if 'update_from_sources_tool' in scope]
        assert update_scopes and all('add_knowledge_tool' not in scope for scope in update_scopes)


@pytest.mark.parametrize('confirmed', [True, False])
def test_save_only_knowledge_never_creates_an_artifact_proposal(tmp_path, confirmed):
    gateway = SupplementGateway()
    app = create_app(tmp_path, gateway)
    with TestClient(app) as client:
        chat, cases = asyncio.run(seed(app.state.store, gateway))
        gateway.plans = [{'title': '只保存资料', 'steps': [{'capability': 'knowledge', 'instruction': '只保存，不修改成果。'}]}]
        gateway.calls = [('add_knowledge_tool', {'content': RULE, 'confirmed': confirmed})]
        result, _ = post(client, chat, 'save-only', '只保存这条资料，不修改成果：' + RULE, artifact_id=cases['id'])
        assert result['status'] == 'succeeded', result
        store = app.state.store
        assert not store.list('artifact_proposal', chat_id=chat['id'])
        assert store.get('artifact', cases['id'])['items'] == cases['items']
        plan = store.list('execution_plan', chat_id=chat['id'])[0]
        assert plan.get('_saved_source_ids', []) == ([gateway.saved_source] if confirmed else [])
        assert gateway.executed == ['add_knowledge_tool']
