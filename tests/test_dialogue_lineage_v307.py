"""Dialogue evidence and organizational lineage commit together after approval."""
import copy

import pytest

from tcg import dependencies as deps
from tcg.dialogue_lineage import preview_changes
from tcg.operations import _save, native_writes
from tcg.schemas import DomainError
from tcg.tool_registry import build_tools
from test_native_business_v300 import setup, generated
from test_native_tools_v300 import Pipeline


def adding_model(service, model):
    previous = model.generate_native

    async def respond(task, context, schema, instruction):
        if task != 'revise_artifact' or not context.get('add_only'):
            return await previous(task, context, schema, instruction)
        model.calls.append((task, copy.deepcopy(context)))
        rows = copy.deepcopy(context['items'])
        if rows:
            rows[0]['title'] = 'must never alter the original during an add'
        kind = context['artifact_type']
        new = {'id': 'NEW-' + kind, 'title': '并发登录', 'refs': context['dialogue_evidence_ids']}
        if kind == 'cases':
            new.update(scenario_id=context['addition_parent_id'], type='Business', priority='P1',
                preconditions='登录功能可用', purpose='并发登录验证',
                steps=[{'action': '并发登录', 'expected': '预期行为待确认'}])
        else:
            new.update(description='按用户要求验证并发登录')
            if kind == 'scenarios':
                new.update(priority='P1', requirement_ids=[context['addition_parent_id']])
        return {'items': rows + [new], 'report': {'summary': '新增预览'}}

    model.generate_native = respond


@pytest.mark.asyncio
async def test_scenario_add_stages_exact_message_and_applies_requirement_in_one_revision(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    adding_model(service, model)
    message = '新增并发登录场景。\n具体并发处理结果尚未定义，不要假设。'
    before_sources = store.list('source')
    proposal = await service.revise(scenarios, instruction='增加并发登录场景',
        dialogue_content=message, add=True)
    assert store.list('source') == before_sources
    assert store.get('artifact', analysis['id'])['revision'] == 1
    assert proposal['dialogue']['parent_changes'][0]['value']['items'][-1]['description'] == message
    assert proposal['items'][:-1] == scenarios['items']
    applied = service.apply_revision_preview(proposal)
    requirement = store.get('artifact', analysis['id'])
    assert requirement['revision'] == 2
    assert requirement['items'][:-1] == analysis['items']
    assert applied['items'][-1]['requirement_ids'] == [requirement['items'][-1]['id']]
    source = store.get('source', proposal['dialogue']['source']['id'])
    assert source['_text'] == message
    assert source['role'] == 'supplement'
    assert store.revision(scenarios['id'], 1)['items'] == scenarios['items']
    # Continuing reads the new parent and its evidence without repeating understanding.
    store.update_run(run['id'], _source_ids=applied['_source_ids'], _source_roles=applied['_source_roles'])
    updated_cases = await service.cases(run, requirement, applied)
    assert applied['items'][-1]['id'] in {r['scenario_id'] for r in updated_cases['items']}


@pytest.mark.asyncio
async def test_case_add_reuses_explicit_parent_and_preserves_selected_and_other_rows(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    adding_model(service, model)
    chosen = cases['items'][0]['id']
    proposal = await service.revise(cases, ids=[chosen], instruction='补充用例',
        dialogue_content='在第一个场景下补一个并发登录用例', add=True,
        parent_id=scenarios['items'][0]['id'])
    assert proposal['dialogue']['parent_changes'] == []
    assert proposal['items'][:-1] == cases['items']
    updated = service.apply_revision_preview(proposal)
    assert updated['items'][-1]['scenario_id'] == scenarios['items'][0]['id']
    assert store.get('artifact', scenarios['id'])['revision'] == 1
    assert store.get('artifact', analysis['id'])['revision'] == 1


@pytest.mark.asyncio
async def test_orphan_import_gets_visible_real_parents_only_after_apply(setup):
    store, run, service, model = setup
    # Imported cases may have no linked upstream artifacts at all.
    source = store.get('source', run['_source_ids'][0])
    case = {'id': 'C-import', 'title': '登录成功', 'type': 'Business', 'priority': 'P1',
        'scenario_id': '', 'preconditions': '账号可用', 'purpose': '登录成功',
        'steps': [{'action': '登录', 'expected': '显示首页'}], 'refs': [source['id'] + '#P1']}
    artifact = {'id': 'art_import', 'chat_id': run['chat_id'], 'project_id': run['project_id'],
        'type': 'cases', 'title': '导入用例', 'revision': 1, 'items': [case], 'report': {},
        '_visible': True, '_source_ids': run['_source_ids'], '_source_roles': {}, '_profile': run['_profile']}
    with native_writes():
        _save(store, artifact, 'imported', {}, None, None)
    adding_model(service, model)
    # Existing unlinked imported rows are legal; only newly requested additions get new parents.
    proposal = await service.revise(artifact, instruction='新增用例', dialogue_content='直接增加并发登录用例', add=True)
    diffs = preview_changes(proposal, artifact)
    assert [d['expected_revision'] for d in diffs] == [0, 0, 1]
    assert all(d['before_items'] == [] for d in diffs[:2])
    updated = service.apply_revision_preview(proposal)
    parents = [store.get('artifact', c['artifact_id']) for c in proposal['dialogue']['parent_changes']]
    assert [p['type'] for p in parents] == ['analysis', 'scenarios']
    assert all(p['_visible'] and p['revision'] == 1 for p in parents)
    assert parents[1]['items'][0]['requirement_ids'] == [parents[0]['items'][0]['id']]
    assert updated['items'][-1]['scenario_id'] == parents[1]['items'][0]['id']
    assert updated['items'][0] == case
    reviewed = await service.review(run, updated)
    assert reviewed['items'][0]['scenario_id'] == ''
    evidence = store.evidence(updated['_source_ids'], updated['_source_roles'])
    forged = {**case, 'id': 'NEW-UNLINKED'}
    with pytest.raises(DomainError, match='新增或重新关联'):
        service._validate('cases', [forged], evidence, service._parents(reviewed), reviewed['_profile'])


@pytest.mark.asyncio
async def test_stale_parent_and_validation_failure_leave_no_source_or_partial_parent(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    adding_model(service, model)
    proposal = await service.revise(scenarios, instruction='新增场景', dialogue_content='增加并发登录', add=True)
    before_sources = store.list('source')
    broken = copy.deepcopy(proposal)
    broken['items'][-1]['refs'] = ['invented#P1']
    with pytest.raises(DomainError):
        service.apply_revision_preview(broken)
    assert store.list('source') == before_sources
    assert store.get('artifact', analysis['id'])['revision'] == 1
    assert store.get('artifact', scenarios['id'])['revision'] == 1
    await service.revise(analysis, ids=[analysis['items'][0]['id']], new_values={'title': '已经修改'})
    with pytest.raises(DomainError, match='依赖已改变'):
        service.apply_revision_preview(proposal)
    assert store.list('source') == before_sources
    assert store.get('artifact', scenarios['id'])['revision'] == 1


@pytest.mark.asyncio
async def test_tool_uses_exact_body_and_apply_preserves_separate_pipeline_confirmation(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    adding_model(service, model)
    pipeline = Pipeline()
    chat = store.get('chat', run['chat_id'])
    text = '在这个场景下新增并发登录用例，预期结果待确认。'
    body = {'content': text, 'artifact_id': scenarios['id'], 'selected_ids': [scenarios['items'][0]['id']]}
    tools = {t.name: t for t in build_tools(store, service, pipeline, chat, body)}
    result = await tools['modify_artifact_tool'].ainvoke({'artifact_id': cases['id'],
        'instruction': '模型转换的简短指令', 'add': True})
    assert result['status'] == 'needs_confirmation'
    proposal = store.get('native_revision_proposal', result['pending'][0]['proposal_id'])
    assert proposal['dialogue']['source']['content'] == text
    assert proposal['dialogue']['parent_changes'] == []
    assert not pipeline.calls
    pending = result['pending'][0]
    apply_tools = {t.name: t for t in build_tools(store, service, pipeline, chat,
        {'content': '同意', 'reply_to': pending['id']}, pending)}
    accepted = await apply_tools['apply_artifact_preview_tool'].ainvoke({})
    assert accepted['status'] == 'succeeded'
    assert pipeline.calls == [('changed', cases['id'])]
    assert store.get('chat', chat['id'])['_native_artifact_prompt'] is None
    repeated = await apply_tools['apply_artifact_preview_tool'].ainvoke({})
    assert repeated['status'] == 'needs_input'


@pytest.mark.asyncio
async def test_cross_chat_parent_is_rejected_without_evidence_writes(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    other = store.create_chat(run['project_id'], '另外一个会话')
    foreign = {**analysis, 'id': 'foreign-analysis', 'chat_id': other['id']}
    with native_writes():
        _save(store, foreign, 'fixture', {}, None, None)
    tampered = {**scenarios, 'id': 'tampered-scenarios',
        'report': {'lineage': {'analysis_artifact_id': foreign['id']}}}
    with native_writes():
        _save(store, tampered, 'fixture', {}, None, None)
    before_sources = store.list('source')
    with pytest.raises(DomainError, match='不属于当前对话'):
        await service.revise(tampered, instruction='新增场景', dialogue_content='并发登录', add=True)
    assert store.list('source') == before_sources


@pytest.mark.asyncio
async def test_add_to_newer_parent_includes_its_evidence_and_keeps_older_cases_stale(setup):
    store, run, service, model = setup
    analysis, scenarios, cases = await generated(setup)
    sid = scenarios['items'][0]['id']
    proposal = await service.revise(scenarios, ids=[sid],
        new_values={'description': '按刚确认的业务规则，登录失败保留输入'},
        dialogue_content='补充：登录失败时必须保留输入。')
    changed = service.apply_revision_preview(proposal)
    new_ref = proposal['dialogue']['evidence'][0]['id']
    assert cases['items'][0]['id'] in deps.artifact_status(store, cases)['affected_item_ids']
    adding_model(service, model)
    addition = await service.revise(cases, instruction='补充用例',
        dialogue_content='在登录场景下再增加并发验证', add=True, parent_id=sid)
    context = [c for task, c in model.calls if task == 'revise_artifact'][-1]
    assert new_ref in {e['id'] for e in context['evidence']}
    assert context['items'] == context['previous_items'] == context['existing_items'] == []
    assert [r['id'] for r in context['scenarios']] == [sid]
    updated = service.apply_revision_preview(addition)
    status = deps.artifact_status(store, updated)
    assert cases['items'][0]['id'] in status['affected_item_ids']
    assert updated['items'][-1]['id'] not in status['affected_item_ids']
    assert sid in service._affected(updated, changed)
