import asyncio

import pytest

from tcg.graph import Engine
from tcg.model import Settings
from tcg.schemas import DomainError
from test_framework_storage_v270 import domain


def test_generation_checks_source_again_after_model_returns(domain):
    store, run, source, scenes, cases = domain
    class Model:
        async def generate(self, task, context):
            store.put('source', {**store.get('source', source['id']), '_text': '需求已改变'})
            return {'items': scenes['items']}
    engine = Engine(store, Model(), Settings(store.directory))
    with pytest.raises(DomainError, match='依赖已改变'):
        asyncio.run(engine.invoke_model('generate_scenarios', {'evidence': store.evidence([source['id']])}, run['id']))
    engine.diagnostics.close()


def test_saved_batch_and_final_commit_reject_changed_source(domain):
    store, run, source, scenes, cases = domain
    calls = []
    class Model:
        async def generate(self, task, context):
            calls.append(task)
            return {'items': scenes['items']}
    engine = Engine(store, Model(), Settings(store.directory))
    context = {'evidence': store.evidence([source['id']])}
    asyncio.run(engine.invoke_model('generate_scenarios', context, run['id']))
    store.put('source', {**store.get('source', source['id']), '_text': '第二批之前需求已改变'})
    with pytest.raises(DomainError, match='依赖已改变'):
        asyncio.run(engine.invoke_model('generate_scenarios', context, run['id']))
    assert calls == ['generate_scenarios']
    with pytest.raises(DomainError, match='依赖已改变'):
        store.artifact(run['id'], 'late-scenarios', 'scenarios', '过期场景', scenes['items'])
    assert store.cache_get(run['id'], 'late-scenarios') is None
    engine.diagnostics.close()


def test_template_completion_keeps_original_generation_guard(domain):
    store, run, source, scenes, cases = domain
    calls = []
    class Model:
        async def generate(self, task, context):
            calls.append(task)
            return {'items': cases['items']}
    engine = Engine(store, Model(), Settings(store.directory))
    context = {'evidence': store.evidence([source['id']])}
    asyncio.run(engine.invoke_model('generate_cases', context, run['id']))
    store.put('source', {**store.get('source', source['id']), '_text': '生成后需求发生变化'})
    with pytest.raises(DomainError, match='依赖已改变'):
        asyncio.run(engine.invoke_model('complete_case_fields', context, run['id']))
    assert calls == ['generate_cases']
    engine.diagnostics.close()
