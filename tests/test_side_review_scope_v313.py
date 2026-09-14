"""Selected side-chat feedback revises a frozen pending proposal, not the whole run."""
import asyncio
import copy

import pytest

from tcg.native_business import NativeBusiness
from tcg.native_views import current_prompt
from tcg.pipeline import PipelineRuntime
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.tool_registry import build_tools
from test_native_business_v300 import NativeModel
from test_native_pipeline_v300 import agree, settled


class SelectedReviewModel(NativeModel):
    async def generate_native(self, task, context, schema, instruction):
        result = await super().generate_native(task, context, schema, instruction)
        if task == 'understand_requirements':
            result['report']['questions'] = []
        if task == 'review_cases':
            for row in result['items']:
                if context.get('review_feedback'):
                    row['title'] = next(r['title'] for r in context['cases'] if r['id'] == row['id'])
                    row['preconditions'] = '已登录'
            result['report']['issues'] = [{'title': '仅本条需修订' if context.get('review_feedback') else '已有评审意见',
                'detail': row['id'] + ' 保留原步骤和可观察结果', 'case_ids': [row['id']]} for row in result['items']]
        return result


@pytest.fixture
def configured(tmp_path):
    store = Store(tmp_path)
    project = store.create_project('Selected review')
    chat = store.create_chat(project['id'], '局部评审反馈')
    store.add_source(chat['id'], '需求', 'primary', '登录显示首页。\n退出后禁止访问。',
        [{'text': '登录显示首页。'}, {'text': '退出后禁止访问。'}])
    model = SelectedReviewModel()
    business = NativeBusiness(store, model)
    yield store, chat, model, business
    store.close()


async def pending_review(runtime, chat):
    run = await runtime.start_run(chat['id'], {'mode': 'hitp', 'content': '生成完整用例并评审'})
    run = await settled(runtime, run['id'])
    assert run['interrupt']['type'] == 'strategy_review'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'scenario_review'
    run = await agree(runtime, run)
    assert run['interrupt']['type'] == 'case_result_review'
    return run


@pytest.mark.asyncio
async def test_selected_feedback_preserves_unselected_pending_rows_and_issues(configured):
    store, chat, model, business = configured
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await pending_review(runtime, chat)
        prompt = await current_prompt(store, runtime, store.get('chat', chat['id']))
        original = store.get('artifact', prompt['artifact_id'])
        proposal = store.get('artifact_proposal', prompt['proposal_id'])
        first, second = proposal['items']
        assert second['title'] != original['items'][1]['title']
        old_request = copy.deepcopy(store.run(run['id'])['_request'])
        tools = build_tools(store, business, runtime, chat, {'content': '只把这一条的前置条件改为已登录',
            'artifact_id': original['id'], 'artifact_revision': original['revision'],
            'selected_ids': [first['id']], 'reply_to': prompt['id']}, prompt)
        tool = next(t for t in tools if t.name == 'revise_review_tool')
        result = await tool.ainvoke({'feedback': '只把选中用例前置条件改为已登录'})
        assert result['status'] == 'succeeded', result
        updated = await settled(runtime, run['id'])
        assert updated['interrupt']['type'] == 'case_result_review', updated
        current = store.get('artifact_proposal', updated['interrupt']['proposal_id'])
        assert current['id'] != proposal['id']
        assert store.get('artifact_proposal', proposal['id'])['status'] == 'superseded'
        by_id = {row['id']: row for row in current['items']}
        assert by_id[first['id']]['preconditions'] == '已登录'
        assert by_id[second['id']] == second
        previous_issue = next(issue for issue in proposal['report']['review_reports'][-1]['issues']
                              if issue['case_ids'] == [second['id']])
        assert previous_issue in current['report']['review_reports'][-1]['issues']
        feedback_context = [context for task, context in model.calls if task == 'review_cases'][-1]
        assert [row['id'] for row in feedback_context['cases']] == [first['id']]
        assert feedback_context['cases'][0] == first
        assert all(second['id'] not in issue.get('case_ids', [])
                   for issue in feedback_context['previous_review']['report']['issues'])
        assert store.get('artifact', original['id']) == original
        assert store.run(run['id'])['_request'] == old_request
        done = await agree(runtime, updated)
        assert done['status'] == 'completed'
        applied = store.get('artifact', original['id'])
        assert applied['revision'] == original['revision'] + 1
        assert next(row for row in applied['items'] if row['id'] == second['id']) == second
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_scope', ['foreign_row', 'expanded_tool_scope', 'stale_revision', 'stale_prompt'])
async def test_invalid_feedback_scope_returns_409_without_advancing_gate(configured, bad_scope):
    store, chat, model, business = configured
    runtime = PipelineRuntime(store, business)
    await runtime.start()
    try:
        run = await pending_review(runtime, chat)
        prompt = await current_prompt(store, runtime, store.get('chat', chat['id']))
        artifact = store.get('artifact', prompt['artifact_id'])
        first, second = artifact['items']
        body = {'content': '修改选中条目评审意见', 'artifact_id': artifact['id'],
            'artifact_revision': artifact['revision'], 'selected_ids': [first['id']], 'reply_to': prompt['id']}
        args = {'feedback': '修订前置条件'}
        if bad_scope == 'foreign_row':
            body['selected_ids'] = ['NOT-IN-THIS-PROPOSAL']
        elif bad_scope == 'expanded_tool_scope':
            args['item_ids'] = [first['id'], second['id']]
        elif bad_scope == 'stale_revision':
            body['artifact_revision'] += 1
        else:
            body['reply_to'] = 'old-prompt'
        tools = build_tools(store, business, runtime, chat, body, prompt)
        before_calls = len(model.calls)
        result = await next(t for t in tools if t.name == 'revise_review_tool').ainvoke(args)
        assert result['status'] == 'needs_input' and result['error_status'] == 409, result
        assert len(model.calls) == before_calls
        snapshot = await runtime.snapshot(run['id'])
        assert snapshot['status'] == 'waiting'
        assert snapshot['interrupt']['proposal_id'] == prompt['proposal_id']
        assert store.get('artifact', artifact['id']) == artifact
    finally:
        await runtime.stop()
