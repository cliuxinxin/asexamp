"""Pipeline regressions exercised through the real API with controlled replies.

These fixtures assert preservation, bounded context and publication invariants;
their canned business judgments are not a model-quality evaluation.
"""

import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from tcg.main import create_app
from test_backend_api import setup_chat, start, until
from test_incremental_agent import WorkModel


def artifact_for(client, run):
    response = client.get('/api/artifacts/' + run['artifact_ids'][-1])
    response.raise_for_status()
    return response.json()


async def record_and_block(model, task, context):
    model.calls.append((task, copy.deepcopy(context)))
    if task == model.block_task and not model.release.is_set():
        model.entered.set()
        while not model.release.is_set():
            await asyncio.sleep(.01)


def instruct_and_release(client, model, run, content):
    try:
        assert model.entered.wait(4), 'The expected in-flight provider call did not start'
        client.post('/api/runs/' + run['id'] + '/instructions',
                    json={'content': content}).raise_for_status()
    finally:
        model.release.set()


def test_new_scope_after_review_started_survives_empty_impact_selection(tmp_path):
    class NewScope(WorkModel):
        async def generate(self, task, context):
            if task == 'work_impact':
                self.calls.append((task, copy.deepcopy(context)))
                return {'unit_ids': [], 'global_change': False,
                        'summary': '新增注册规则不改变现有登录规则。'}
            return await super().generate(task, context)

    model = NewScope()
    model.block_task = 'work_review'
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录密码必须至少六位。')
        run = start(client, chat, experience='agent', confirm_strategy=False)
        instruct_and_release(client, model, run, '另外增加注册模块：注册必须验证邮箱。登录规则不变。')
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        artifact = artifact_for(client, run)
        descriptions = [item['description'] for item in artifact['report']['requirements']]
        assert '登录密码必须至少六位。' in descriptions
        assert any('注册必须验证邮箱' in text for text in descriptions)
        assert any('注册' in item['title'] for item in artifact['items'])
        assert not artifact['report']['coverage']['gaps']
        assert sum(task == 'work_impact' for task, _ in model.calls) == 1
        assert sum(task == 'work_analyze' and context['evidence'][0].get('text') == '登录密码必须至少六位。'
                   for task, context in model.calls) == 1


def test_modify_repairs_the_invalid_operation_leaf_and_preserves_other_items(tmp_path):
    class BrokenOperation(WorkModel):
        async def generate(self, task, context):
            if task == 'work_modify':
                self.calls.append((task, copy.deepcopy(context)))
                return {'kind': 'patch', 'operations': [{
                    'op': 'update', 'id': context['items'][0]['id'],
                    'item': {'preconditions': ['wrong wrapper']}}],
                    'summary': '修改选定用例的前置条件。'}
            if task == 'agent_repair':
                self.calls.append((task, copy.deepcopy(context)))
                return {'path': context['repair']['path'], 'value': '已准备有效登录账户。'}
            return await super().generate(task, context)

    model = BrokenOperation()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='密码至少六位。\n\n登录成功进入首页。')
        first = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert first['status'] == 'completed', first.get('error', first)
        original = artifact_for(client, first)
        assert len(original['items']) == 2
        run = until(client, start(client, chat, experience='agent', intent='modify',
                                  artifact_id=original['id'], selected_ids=[original['items'][0]['id']],
                                  content='前置条件改成：已准备有效登录账户。'))
        assert run['status'] == 'completed', run.get('error', run)
        repairs = [context for task, context in model.calls if task == 'agent_repair']
        assert len(repairs) == 1
        assert repairs[0]['repair']['path'] == 'operations[0].item.preconditions'
        assert repairs[0]['repair']['value'] == ['wrong wrapper']
        assert not {'items', 'operations', 'evidence'}.intersection(repairs[0])
        updated = artifact_for(client, run)
        expected = copy.deepcopy(original['items'])
        expected[0]['preconditions'] = '已准备有效登录账户。'
        assert updated['items'] == expected
        assert updated['revision'] == original['revision'] + 1


def test_dependency_edges_wait_for_the_analysis_revision_the_same_reply_invalidates(tmp_path):
    old_label = '旧状态：失败次数未定义'
    new_label = '现行状态：失败5次锁定'

    class SimultaneousDependency(WorkModel):
        async def generate(self, task, context):
            if task == 'work_links':
                self.calls.append((task, copy.deepcopy(context)))
                login = next(u for u in context['units'] if u['title'] == 'Requirements')
                security = next(u for u in context['units'] if u['title'] == '安全策略')
                return {'kind': 'patch', 'summary': '后读安全策略限制登录状态。', 'questions': [],
                        'updates': [{'unit_id': login['id'], 'from_units': [security['id']],
                                     'evidence_refs': security['requirements'][0]['refs'],
                                     'summary': '登录状态必须纳入失败次数限制。'}],
                        'edges': [{'id': 'cross', 'from': login['nodes'][-1]['id'],
                                   'to': security['nodes'][0]['id'], 'label': '登录受安全策略限制',
                                   'refs': login['requirements'][0]['refs'] + security['requirements'][0]['refs']}]}
            reply = await super().generate(task, context)
            if task == 'work_analyze' and '登录' in context['evidence'][0]['text']:
                if len(context['evidence']) == 1:
                    reply['nodes'][-1]['label'] = old_label
                else:
                    reply['nodes'][-1].update(id='locked', label=new_label)
                    reply['edges'][0]['to'] = 'locked'
            return reply

    model = SimultaneousDependency()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录允许输入密码，失败次数未定义。')
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={
            'name': '安全策略', 'role': 'supplement', 'text': '所有入口连续失败5次后锁定。'}).raise_for_status()
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error', run)
        artifact = artifact_for(client, run)
        graph = artifact['report']['business_model']
        assert old_label not in {node['label'] for node in graph['nodes']}
        current = next(node for node in graph['nodes'] if node['label'] == new_label)
        cross = [edge for edge in graph['edges'] if edge['label'] == '登录受安全策略限制']
        assert len(cross) == 1 and cross[0]['from'] == current['id']
        link_contexts = [context for task, context in model.calls if task == 'work_links']
        assert len(link_contexts) == 2
        assert old_label in {node['label'] for unit in link_contexts[0]['units'] for node in unit['nodes']}
        assert new_label in {node['label'] for unit in link_contexts[1]['units'] for node in unit['nodes']}
        assert artifact['report']['source_processing'] == {'completed_units': 3, 'total_units': 3}
        assert not artifact['report']['coverage']['gaps']


def test_review_of_published_artifact_cannot_delete_its_only_case(tmp_path):
    class Deleting(WorkModel):
        delete = False

        async def generate(self, task, context):
            reply = await super().generate(task, context)
            if task == 'work_review' and self.delete:
                reply['operations'] = [{'op': 'delete', 'id': context['items'][0]['id']}]
            return reply

    model = Deleting()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录密码必须至少六位。')
        first = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert first['status'] == 'completed', first.get('error', first)
        original = artifact_for(client, first)
        assert len(original['items']) == 1
        revisions = client.get('/api/artifacts/' + original['id'] + '/revisions').json()
        model.delete = True
        run = until(client, start(client, chat, experience='agent', intent='review_case',
                                  artifact_id=original['id'], content='检查并优化现有用例。'))
        assert run['status'] == 'failed', run
        assert not run['artifact_ids']
        assert '覆盖' in run['error'] or '适用' in run['error']
        unchanged = client.get('/api/artifacts/' + original['id']).json()
        assert unchanged['revision'] == original['revision']
        assert unchanged['items'] == original['items']
        assert client.get('/api/artifacts/' + original['id'] + '/revisions').json() == revisions


def test_later_review_group_uses_accepted_type_changes_and_cannot_remove_last_business_case(tmp_path):
    class ChangingTypes(WorkModel):
        async def generate(self, task, context):
            reply = await super().generate(task, context)
            if task == 'work_cases':
                # Each complete case fits one review group; their sum does not.
                for item in reply['items']:
                    item['preconditions'] = '已准备输入数据。' * 300
            if task == 'work_review':
                reply['operations'] = [{'op': 'update', 'id': item['id'], 'item': {'type': 'Negative'}}
                                       for item in context['items']]
                reply['report']['type_assessment'] = [
                    {'type': case_type, 'applicable': case_type in ('Business', 'Negative'),
                     'reason': '本测试要求同时保留业务流程和失败输入用例。'}
                    for case_type in context['case_types']]
            return reply

    model = ChangingTypes()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='正确密码允许登录。\n\n错误密码必须拒绝登录。')
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'failed', run
        reviews = [context for task, context in model.calls if task == 'work_review']
        assert len(reviews) == 2
        assert all(len(context['items']) == 1 for context in reviews)
        assert reviews[0]['type_coverage']['Business'] == 2
        assert reviews[1]['type_coverage']['Business'] == 1
        assert reviews[1]['type_coverage']['Negative'] == 1
        assert 'Business' in run['error']
        work = client.get('/api/runs/' + run['id'] + '/work').json()['items']
        assert sum(item['kind'] == 'work_review' and item['status'] == 'completed' for item in work) == 1
        assert sum(item['kind'] == 'work_review' and item['status'] == 'failed' for item in work) == 1
        assert not run['artifact_ids']


def test_presentation_instruction_after_cross_path_generation_does_not_duplicate_path(tmp_path):
    instruction = '仅调整展示：用例标题使用简短中文，业务规则保持不变。'
    path_title = '审批通过才允许退款'

    class Linked(WorkModel):
        async def generate(self, task, context):
            if task == 'work_links':
                self.calls.append((task, copy.deepcopy(context)))
                early = next(unit for unit in context['units'] if unit['title'] == 'Requirements')
                late = next(unit for unit in context['units'] if unit['title'] == '退款')
                return {'kind': 'patch', 'summary': '审批后才能退款。', 'questions': [],
                        'edges': [{'id': 'cross', 'from': early['nodes'][-1]['id'],
                                   'to': late['nodes'][0]['id'], 'label': path_title,
                                   'refs': early['requirements'][0]['refs'] + late['requirements'][0]['refs']}]}
            if task == 'work_impact':
                self.calls.append((task, copy.deepcopy(context)))
                return {'unit_ids': [], 'global_change': False, 'summary': '仅调整展示，不改变已有规则。'}
            reply = await super().generate(task, context)
            if task == 'work_analyze' and all(item.get('text') == instruction for item in context['evidence']):
                reply.update(items=[], nodes=[], edges=[], evidence_review=[
                    {'ref': item['id'], 'classification': 'non_requirement', 'reason': '仅为展示指令。'}
                    for item in context['evidence']])
            return reply

    model = Linked()
    model.block_task = 'work_review'
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='审批单通过后进入退款环节。')
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={
            'name': '退款', 'role': 'primary', 'text': '退款需要已通过的审批单。'}).raise_for_status()
        run = start(client, chat, experience='agent', confirm_strategy=False)
        instruct_and_release(client, model, run, instruction)
        assert any(task == 'work_cases' and any(item['title'] == path_title for item in context['scenarios'])
                   for task, context in model.calls)
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        artifact = artifact_for(client, run)
        assert sum(item['title'] == path_title for item in artifact['items']) == 1
        assert sum(item['title'] == path_title for item in artifact['report']['requirements']) == 1
        assert sum(edge['label'] == path_title for edge in artifact['report']['business_model']['edges']) == 1
        assert not artifact['report']['coverage']['gaps']


@pytest.mark.parametrize('intent', ['query', 'modify'])
def test_next_single_task_call_sees_instruction_received_while_previous_call_blocked(tmp_path, intent):
    instruction = '最新要求：最终内容必须使用“已经采用新指令”，不要沿用旧内容。'
    old_text, new_text = '旧内容尚未更新', '已经采用新指令'

    class LatestInstruction(WorkModel):
        async def generate(self, task, context):
            if task == 'work_' + intent:
                await record_and_block(self, task, context)
                updated = instruction in json.dumps(context, ensure_ascii=False)
                text = new_text if updated else old_text
                if intent == 'query':
                    return {'kind': 'answer', 'answer': text, 'refs': []}
                return {'kind': 'patch', 'operations': [{'op': 'update', 'id': context['items'][0]['id'],
                                                       'item': {'title': text}}], 'summary': text}
            return await super().generate(task, context)

    model = LatestInstruction()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录密码必须至少六位。')
        kwargs = {}
        original = None
        if intent == 'modify':
            first = until(client, start(client, chat, experience='agent', confirm_strategy=False))
            assert first['status'] == 'completed', first.get('error', first)
            original = artifact_for(client, first)
            kwargs['artifact_id'] = original['id']
        model.block_task = 'work_' + intent
        run = start(client, chat, intent=intent, experience='agent', confirm_strategy=False,
                    content='使用旧内容完成当前请求。', **kwargs)
        instruct_and_release(client, model, run, instruction)
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        calls = [context for task, context in model.calls if task == 'work_' + intent]
        assert len(calls) == 2
        assert instruction not in json.dumps(calls[0], ensure_ascii=False)
        assert instruction in json.dumps(calls[1], ensure_ascii=False)
        assert len(run['artifact_ids']) == 1
        artifact = artifact_for(client, run)
        assert old_text not in json.dumps(artifact['items'], ensure_ascii=False)
        if intent == 'query':
            assert artifact['items'][0]['description'] == new_text
        else:
            assert artifact['items'][0]['title'] == new_text
            assert artifact['revision'] == original['revision'] + 1


def test_dependency_update_selects_bounded_units_of_one_oversized_original_paragraph(tmp_path):
    marker = '所有登录入口失败五次后必须锁定。'
    body = '安全规则背景。' * 2700 + marker
    assert len(body) > 18000

    class BoundedDependency(WorkModel):
        selected_unit = None
        selected_excerpt = None
        update_sent = False

        async def generate(self, task, context):
            if task == 'agent_repair':
                self.calls.append((task, copy.deepcopy(context)))
                assert context['repair']['path'] == 'updates[0].from_units'
                supplied = context['constraints']['source_units']
                selected = next(unit for unit in supplied if unit['id'] == self.selected_unit)
                assert selected['excerpts'][0]['excerpt'] == self.selected_excerpt
                return {'path': context['repair']['path'], 'value': [self.selected_unit]}
            if task == 'work_links':
                self.calls.append((task, copy.deepcopy(context)))
                early = next((unit for unit in context['units'] if unit['title'] == 'Requirements'), None)
                late = next((unit for unit in context['units']
                             if any(marker in requirement['title'] for requirement in unit['requirements'])), None)
                reply = {'kind': 'patch', 'edges': [], 'summary': '核查晚读安全规则对登录的限制。', 'questions': []}
                if early and late and not self.update_sent:
                    self.update_sent = True
                    self.selected_unit = late['id']
                    self.selected_excerpt = copy.deepcopy(late['excerpts'][0]['excerpt'])
                    # A paragraph ref alone is ambiguous: repair must ask for
                    # the exact supplied source-unit selection, not its body.
                    reply['updates'] = [{'unit_id': early['id'], 'evidence_refs': late['requirements'][0]['refs'],
                                         'summary': '登录必须采用晚读安全规则的失败次数限制。'}]
                return reply
            reply = await super().generate(task, context)
            if task == 'work_analyze':
                evidence = context['evidence']
                # Keep derived facts compact; the test stresses source slicing,
                # not a provider that gratuitously repeats the source body.
                reply['items'] = [{'id': 'combined',
                                   'title': marker if any(marker in item['text'] for item in evidence) else '登录与安全规则',
                                   'description': '登录失败五次必须锁定。' if len(evidence) > 1 else '当前证据中的业务规则。',
                                   'refs': list(dict.fromkeys(item['id'] for item in evidence))}]
            return reply

    model = BoundedDependency()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, early = setup_chat(client, text='登录允许输入密码；失败次数尚未定义。')
        late = client.app.state.store.add_source(chat['id'], '安全策略', 'supplement', body,
                                                [{'text': body, 'location': 'P1'}])
        stored = client.get('/api/sources/' + late['id']).json()
        assert len(stored['chunks']) == 1
        assert stored['chunks'][0]['text'] == body
        run = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert run['status'] == 'completed', run.get('error', run)
        assert model.update_sent, 'The fixture must actually request a dependency update'
        repairs = [context for task, context in model.calls if task == 'agent_repair']
        assert len(repairs) == 1
        analyses = [context for task, context in model.calls if task == 'work_analyze']
        initial_late = [item for context in analyses for item in context['evidence']
                        if item['source_id'] == late['id'] and len(context['evidence']) == 1]
        assert len(initial_late) > 1
        assert ''.join(item['text'] for item in initial_late) == body
        updated = [context for context in analyses if any(item['source_id'] == early['id'] for item in context['evidence'])
                   and any(item['source_id'] == late['id'] for item in context['evidence'])]
        assert len(updated) == 1
        applied = [item for item in updated[0]['evidence'] if item['source_id'] == late['id']]
        assert len(applied) == 1
        assert applied[0]['excerpt'] == model.selected_excerpt
        assert applied[0]['text'] == body[model.selected_excerpt['start']:model.selected_excerpt['end']]
        assert marker in applied[0]['text']
        for task, context in model.calls:
            assert len(json.dumps(context, ensure_ascii=False)) <= 16000, task
            assert body not in json.dumps(context, ensure_ascii=False), task
        artifact = artifact_for(client, run)
        assert any(item['description'] == '登录失败五次必须锁定。' for item in artifact['report']['requirements'])
        assert not artifact['report']['coverage']['gaps']


@pytest.mark.parametrize('intent', ['modify', 'review_case'])
def test_late_instruction_preserves_accepted_unaffected_artifact_batches(tmp_path, intent):
    instruction = '只调整第二组退款用例：标题改成“退款已经采用新指令”；其余组保留已完成修改。'
    first_title, stale_title, latest_title = '登录首批修改已保留', '退款旧回复不得发布', '退款已经采用新指令'
    final_title = '退出末批修改已完成'
    editing_task = 'work_modify' if intent == 'modify' else 'work_review'

    class ScopedBatches(WorkModel):
        editing = False
        target_id = None
        titles = None
        selected_impact_units = None

        async def generate(self, task, context):
            if task == 'work_impact':
                self.calls.append((task, copy.deepcopy(context)))
                selected = [unit['id'] for unit in context['units']
                            if any(item['id'] == self.target_id for item in unit['requirements'])]
                self.selected_impact_units = selected
                return {'unit_ids': selected, 'global_change': False,
                        'summary': '只修改第二组退款用例，保留其他组已接受结果。'}
            if self.editing and task == editing_task:
                if any(item['id'] == self.target_id for item in context['items']):
                    await record_and_block(self, task, context)
                else:
                    self.calls.append((task, copy.deepcopy(context)))
                latest = instruction in json.dumps(context, ensure_ascii=False)
                operations = [{
                    'op': 'update', 'id': item['id'],
                    'item': {'title': latest_title if latest and item['id'] == self.target_id else self.titles[item['id']]}}
                    for item in context['items']]
                return {'kind': 'patch', 'operations': operations, 'summary': '已保存当前组的标题修改。',
                        'report': {'summary': '已检查当前组并保留业务覆盖。', 'issues': [], 'score': 85,
                                   'type_assessment': [
                                       {'type': case_type, 'applicable': case_type == 'Business',
                                        'reason': '本测试只修改标题，业务用例类型保持不变。'}
                                       for case_type in context.get('case_types', [])]}}
            reply = await super().generate(task, context)
            if task == 'work_cases':
                for item in reply['items']:
                    item['preconditions'] = '已准备输入数据。' * 370
            return reply

    model = ScopedBatches()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录必须校验密码。\n\n退款必须验证订单。\n\n退出必须销毁会话。')
        first = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert first['status'] == 'completed', first.get('error', first)
        original = artifact_for(client, first)
        assert len(original['items']) == 3
        first_id, second_id, third_id = [item['id'] for item in original['items']]
        model.target_id = second_id
        model.titles = {first_id: first_title, second_id: stale_title, third_id: final_title}
        model.editing = True
        model.block_task = editing_task
        call_start = len(model.calls)
        run = start(client, chat, experience='agent', intent=intent, artifact_id=original['id'],
                    content='依次检查并修改三组用例标题。', confirm_strategy=False)
        try:
            assert model.entered.wait(4), 'The second artifact batch did not reach the provider'
            before = client.get('/api/runs/' + run['id'] + '/work').json()['items']
            accepted = [item for item in before if item['kind'] == editing_task and item['status'] == 'completed']
            assert len(accepted) == 1
            preview = client.get('/api/artifacts/' + accepted[0]['artifact_id']).json()
            assert preview['items'][0]['id'] == first_id
            assert preview['items'][0]['title'] == first_title
            client.post('/api/runs/' + run['id'] + '/instructions', json={'content': instruction}).raise_for_status()
        finally:
            model.release.set()
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        calls = [context for task, context in model.calls[call_start:] if task == editing_task]
        assert all(len(context['items']) == 1 for context in calls)
        assert sum(context['items'][0]['id'] == first_id for context in calls) == 1
        assert sum(context['items'][0]['id'] == third_id for context in calls) == 1
        replaced_calls = [context for context in calls if context['items'][0]['id'] == second_id]
        assert len(replaced_calls) == 2
        assert instruction not in json.dumps(replaced_calls[0], ensure_ascii=False)
        assert instruction in json.dumps(replaced_calls[1], ensure_ascii=False)
        assert model.selected_impact_units is not None and len(model.selected_impact_units) == 1
        assert len(run['artifact_ids']) == 1
        updated = artifact_for(client, run)
        expected = copy.deepcopy(original['items'])
        for item, title in zip(expected, [first_title, latest_title, final_title]):
            item['title'] = title
        assert updated['items'] == expected
        assert updated['revision'] == original['revision'] + 1
        assert stale_title not in json.dumps(updated['items'], ensure_ascii=False)
        assert all(len(json.dumps(context, ensure_ascii=False)) <= 16000 for context in calls)


def test_dependency_can_read_a_source_units_current_instruction_override(tmp_path):
    instruction = '将安全策略更新为：所有登录入口连续失败5次锁定。'

    class OverrideDependency(WorkModel):
        update_sent = False
        selected_unit = None
        selected_refs = None

        async def generate(self, task, context):
            if task == 'work_impact':
                self.calls.append((task, copy.deepcopy(context)))
                return {'unit_ids': [u['id'] for u in context['units'] if u['title'] == '安全策略'],
                        'global_change': False, 'adds_new_scope': False, 'summary': '更新安全策略组。'}
            if task == 'work_links':
                self.calls.append((task, copy.deepcopy(context)))
                login = next(u for u in context['units'] if u['title'] == 'Requirements')
                security = next(u for u in context['units'] if u['title'] == '安全策略')
                refs = {r for item in security['requirements'] for r in item['refs']}
                changed = [e['id'] for e in context['evidence']
                           if e['role'] == 'clarification' and e['id'] in refs]
                updates = []
                if changed and not self.update_sent:
                    self.update_sent = True
                    self.selected_unit, self.selected_refs = security, changed
                    updates = [{'unit_id': login['id'], 'from_units': [security['id']],
                                'evidence_refs': changed, 'summary': '更新后的安全规则也限制登录。'}]
                return {'kind': 'patch', 'edges': [], 'updates': updates,
                        'summary': '检查安全策略与登录的依赖。', 'questions': []}
            return await super().generate(task, context)

    model = OverrideDependency()
    model.block_task = 'work_review'
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, login = setup_chat(client, text='登录允许输入密码。')
        client.post('/api/chats/' + chat['id'] + '/sources/text', json={
            'name': '安全策略', 'role': 'supplement', 'text': '统一安全策略，失败次数未定义。'}).raise_for_status()
        run = start(client, chat, experience='agent', confirm_strategy=False)
        instruct_and_release(client, model, run, instruction)
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        assert model.update_sent
        assert set(model.selected_refs) <= {e['id'] for e in model.selected_unit['excerpts']}
        assert not any(task == 'agent_repair' for task, _ in model.calls)
        analyses = [context for task, context in model.calls if task == 'work_analyze'
                    and any(e['source_id'] == login['id'] for e in context['evidence'])]
        assert len(analyses) == 2
        assert any(e['role'] == 'clarification' and e['text'] == instruction
                   for e in analyses[-1]['evidence'])
        assert not artifact_for(client, run)['report']['coverage']['gaps']


@pytest.mark.parametrize('prior_operation', ['update', 'replace'])
def test_instruction_rebases_affected_accepted_batch_without_losing_prior_changes(tmp_path, prior_operation):
    instruction = '第一组前置条件改为用户新前提，保留其他已完成修改。'

    class AcceptedBatch(WorkModel):
        first_id = None
        second_id = None

        async def generate(self, task, context):
            if task == 'work_impact':
                self.calls.append((task, copy.deepcopy(context)))
                return {'unit_ids': [u['id'] for u in context['units']
                                     if any(i['id'] == self.first_id for i in u['requirements'])],
                        'global_change': False, 'adds_new_scope': False,
                        'summary': '仅调整已保存第一组的前提条件。'}
            if task == 'work_modify':
                item = context['items'][0]
                if item['id'] == self.second_id:
                    await record_and_block(self, task, context)
                else:
                    self.calls.append((task, copy.deepcopy(context)))
                if instruction in context['goal'] and item['id'] != self.second_id:
                    operations = [{'op': 'update', 'id': item['id'], 'item': {'preconditions': '用户新前提'}}]
                elif prior_operation == 'replace' and item['id'] == self.first_id:
                    replacement = {**copy.deepcopy(item), 'id': 'replacement', 'title': '优化后第一组标题'}
                    operations = [{'op': 'delete', 'id': item['id']}, {'op': 'add', 'item': replacement}]
                else:
                    operations = [{'op': 'update', 'id': item['id'],
                                   'item': {'title': '优化后第一组标题' if item['id'] == self.first_id else '优化后第二组标题'}}]
                return {'kind': 'patch', 'operations': operations, 'summary': '保存局部修改。'}
            reply = await super().generate(task, context)
            if task == 'work_cases':
                for item in reply['items']:
                    item['preconditions'] = '账户准备。' * 500
            return reply

    model = AcceptedBatch()
    with TestClient(create_app(tmp_path, model)) as client:
        _, chat, _ = setup_chat(client, text='登录必须校验密码。\n\n退款必须验证订单。')
        generated = until(client, start(client, chat, experience='agent', confirm_strategy=False))
        assert generated['status'] == 'completed', generated.get('error', generated)
        original = artifact_for(client, generated)
        model.first_id, model.second_id = [i['id'] for i in original['items']]
        model.block_task = 'work_modify'
        run = start(client, chat, experience='agent', intent='modify', artifact_id=original['id'],
                    content='优化两组用例标题。')
        instruct_and_release(client, model, run, instruction)
        run = until(client, run)
        assert run['status'] == 'completed', run.get('error', run)
        updated = artifact_for(client, run)
        assert len(updated['items']) == 2
        first = next(i for i in updated['items'] if i['title'] == '优化后第一组标题')
        assert first['preconditions'] == '用户新前提'
        assert (first['id'] == model.first_id) == (prior_operation == 'update')
        if prior_operation == 'replace':
            assert model.first_id not in {i['id'] for i in updated['items']}
        revised_context = next(context for task, context in model.calls if task == 'work_modify'
                               and instruction in context['goal'] and context['items'][0]['id'] != model.second_id)
        assert revised_context['items'][0]['title'] == '优化后第一组标题'
        assert updated['revision'] == original['revision'] + 1
