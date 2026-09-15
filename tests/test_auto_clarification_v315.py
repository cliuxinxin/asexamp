"""Automatic clarification uses the same durable application path as human answers."""
import asyncio

import pytest

from tcg.native_business import NativeBusiness
from tcg.pipeline import PipelineRuntime
from test_native_business_v300 import NativeModel
from test_native_pipeline_v300 import agree, setup, settled


@pytest.mark.asyncio
async def test_auto_adopts_all_suggestion_shapes_and_finishes_without_interrupt(tmp_path):
    store, chat, business, runtime = setup(tmp_path, questions=True)
    original = business.understand

    async def understand(run):
        artifact = await original(run)
        return store.revise_artifact(artifact['id'], artifact['revision'], artifact['items'], report={
            'questions': [
                {'id': 'Q1', 'question': '失败怎么办？', 'suggestion': '提示错误。'},
                {'id': 'Q2', 'question': '输入保留吗？', 'suggested_answer': '保留输入。'},
                {'id': 'Q3', 'question': '多久解锁？'},
                {'id': 'Q4', 'question': '有额外规则吗？'}],
            'question_suggestions': [{'question': '多久解锁？', 'answer': '5 分钟。'}]})

    business.understand = understand
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto'}))['id'])
        assert run['status'] == 'completed', run
        assert [name for name, _ in business.calls] == ['understand', 'clarify', 'scenarios', 'cases', 'review']
        answers = business.calls[1][1]
        assert answers == {'失败怎么办？': '提示错误。', '输入保留吗？': '保留输入。',
                           '多久解锁？': '5 分钟。', '有额外规则吗？': '暂按当前需求已描述的范围执行，未说明的条件标记为待确认。'}
        assert store.run(run['id'])['clarification_save_to_project'] is True
        checkpoint = await runtime.graph.aget_state(runtime._config(run['id']))
        assert not checkpoint.next
    finally:
        await runtime.stop()
        store.close()


@pytest.mark.asyncio
async def test_auto_clarification_records_automatic_origin_and_reuses_source_on_retry(tmp_path):
    store, chat, _, _ = setup(tmp_path)
    class FailOnce(NativeModel):
        fail = True

        async def generate_native(self, task, context, schema, instruction):
            if task == 'revise_artifact' and self.fail:
                self.fail = False
                raise RuntimeError('Connection lost after clarification source was saved')
            return await super().generate_native(task, context, schema, instruction)

    business = NativeBusiness(store, FailOnce())
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await settled(runtime, (await runtime.start_run(chat['id'], {'mode': 'auto', 'stop_after': 'analysis'}))['id'])
        assert run['status'] == 'failed' and run['failed_node'] == 'apply_clarification', run
        source = next(s for s in store.list('source', chat_id=chat['id']) if s['role'] == 'clarification')
        assert source['_project_shared'] is True
        assert source['provenance']['origin'] == 'auto_clarification'
        assert source['provenance']['confirmed_by'] == 'Auto 模式自动采纳'
        count = len(store.list('source', chat_id=chat['id']))
        await runtime.retry(run['id'])
        run = await settled(runtime, run['id'])
        assert run['status'] == 'completed', run
        analysis = next(a for a in store.list('artifact', chat_id=chat['id']) if a['type'] == 'analysis')
        assert source['id'] in store.run(run['id'])['_source_ids']
        assert not analysis['report']['questions']
        assert len(store.list('source', chat_id=chat['id'])) == count
    finally:
        await runtime.stop()
        store.close()


@pytest.mark.asyncio
async def test_auto_with_explicit_pause_still_waits_at_clarification(tmp_path):
    store, chat, business, runtime = setup(tmp_path, questions=True)
    business.release = asyncio.Event()
    await runtime.start()
    try:
        run = await runtime.start_run(chat['id'], {'mode': 'auto'})
        await business.started.wait()
        await runtime.request_pause(run['id'])
        business.release.set()
        run = await settled(runtime, run['id'])
        assert run['status'] == 'waiting' and run['interrupt']['type'] == 'clarification'
        assert [name for name, _ in business.calls] == ['understand']
        run = await agree(runtime, run)
        assert run['status'] == 'completed'
    finally:
        await runtime.stop()
        store.close()
