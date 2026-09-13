"""Server admission and bounded routing recovery keep user control intact."""
import copy

import pytest

from tcg.graph import Engine
from tcg.model import Settings
from tcg.storage import Store
from tcg.conversation_context import build_context
from tcg.schemas import DomainError


class AcceptingModel:
    def __init__(self):
        self.calls = []

    async def generate(self, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        return {'ok': True}


@pytest.mark.asyncio
async def test_engine_sends_context_beyond_retired_character_gate(tmp_path):
    store = Store(tmp_path)
    model = AcceptingModel()
    engine = Engine(store, model, Settings(tmp_path))
    context = {'evidence': [{'id': 'source#P1', 'text': '业务原文' * 130000}]}
    try:
        assert await engine._invoke_model('connection_test', context) == {'ok': True}
        assert model.calls[0][1] == context
    finally:
        store.close()


def test_conversation_metadata_is_not_rejected_or_trimmed_by_local_capacity(tmp_path):
    store = Store(tmp_path)
    chat = store.create_chat(store.list('project')[0]['id'], 'large context')
    class SmallEstimate:
        def fits(self, *args):
            return False
    try:
        registry = {'test.read': {'description': 'A long capability definition ' * 5000, 'effect': 'read'}}
        context = build_context(store, SmallEstimate(), chat, {'content': '只解释，不要继续'}, registry)
        assert context['content'] == '只解释，不要继续'
        assert context['capabilities'][0]['description'] == registry['test.read']['description']
    finally:
        store.close()


@pytest.mark.asyncio
async def test_routing_compacts_only_after_server_rejection_and_keeps_controls():
    from tcg.server_capacity import ContextCapacityError
    from tcg.server_routing import invoke_routing
    original = {'content': '只解释所选场景，不要生成或继续', 'view': {'artifact_id': 'A', 'selected_ids': ['S2'], 'view_order': ['S2', 'S1']},
        'runs': [{'id': 'R', 'mode': 'hitp', 'interrupt': {'type': 'scenario_review', 'artifact_id': 'A'}, 'control_version': 3}],
        'capabilities': [{'name': 'test.read', 'effect': 'read', 'parameters': {'type': 'object'}}],
        'recent_messages': [{'role': 'user', 'text': str(i) * 500} for i in range(6)],
        'artifacts': [{'id': 'A', 'items': [{'id': f'S{i}', 'title': 'scenario'} for i in range(30)]}],
        'pending': [{'id': 'P', 'kind': 'proposal'}], 'requested_settings': {'mode': 'hitp'},
        'sources': [{'id': 'SRC', 'role': 'supplement', 'name': 'new rules'}]}
    calls = []
    class Model:
        async def invoke_model(self, task, context, run_id=None):
            calls.append(copy.deepcopy(context))
            if len(calls) == 1:
                raise ContextCapacityError()
            return {'actions': [], 'message': '解释'}
    result = await invoke_routing(Model(), 'conversation_turn', original)
    assert result['message'] == '解释' and len(calls) == 2
    assert calls[0] == original
    for key in ('content', 'view', 'runs', 'capabilities', 'pending', 'requested_settings', 'sources'):
        assert calls[1][key] == original[key]
    assert len(calls[1]['recent_messages']) < len(original['recent_messages'])
    assert calls[1]['context_recovery']['partial'] is True
    assert original['artifacts'][0]['items'] == calls[0]['artifacts'][0]['items']


@pytest.mark.asyncio
async def test_routing_does_not_retry_authentication_or_loop_on_indivisible_input():
    from tcg.server_capacity import ContextCapacityError
    from tcg.server_routing import invoke_routing
    for failure, expected_calls in ((DomainError('authentication failed'), 1), (ContextCapacityError(), 1)):
        calls = []
        class Model:
            async def invoke_model(self, task, context, run_id=None):
                calls.append(context)
                raise failure
        with pytest.raises(type(failure)):
            await invoke_routing(Model(), 'conversation_turn', {'content': 'Long indivisible instruction'})
        assert len(calls) == expected_calls
