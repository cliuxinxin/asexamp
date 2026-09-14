"""Native tools preserve user scope, checkpoint binding and actual exported bytes."""
import base64
import copy
import io
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from tcg.documents import parse_text
from tcg.schemas import DomainError
from tcg.storage import Store
from tcg.tool_registry import build_tools


class Pipeline:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.token = 'pipeline:gate:1'
        self.editing = False

    @asynccontextmanager
    async def edit_session(self, chat_id):
        if self.busy:
            raise DomainError('任务正在运行，请等待当前步骤完成', 409)
        self.editing = True
        try:
            yield
        finally:
            self.editing = False

    async def on_artifact_changed(self, artifact):
        assert self.editing
        self.calls.append(('changed', artifact['id']))
        self.token = 'pipeline:gate:2'

    async def resume(self, rid, action='approved', expected_prompt_id=None, payload=None):
        if expected_prompt_id != self.token:
            raise DomainError('当前提示已改变', 409)
        self.calls.append(('resume', rid, action, payload))
        self.token = 'pipeline:next:1'
        return {'id': rid, 'status': 'queued'}

    async def start_run(self, cid, request):
        self.calls.append(('start', cid, request))
        return {'id': 'new-run', 'status': 'queued'}

    def request_pause(self, rid):
        return {'id': rid}

    cancel = retry = request_pause


class Business:
    def __init__(self, store):
        self.store, self.calls = store, []
        self.gateway = self

    async def revise(self, artifact, ids=None, **kwargs):
        self.calls.append(('revise', artifact['id'], ids, kwargs))
        result = copy.deepcopy(artifact)
        for row in result['items']:
            if ids is None or row['id'] in ids:
                row.update(kwargs.get('new_values') or {'title': kwargs['instruction']})
        if kwargs.get('preview'):
            from tcg.review_proposals import revision_proposal
            from tcg.dependencies import manifest
            return revision_proposal(artifact, result['items'], result.get('report', {}),
                manifest(self.store, artifact_ids=[artifact['id']]), [], {})
        result['revision'] += 1
        return result

    def apply_revision_preview(self, proposal):
        self.calls.append(('apply', proposal['artifact_id']))
        return {**self.store.get('artifact', proposal['artifact_id']),
            'revision': proposal['artifact_revision'] + 1, 'items': proposal['items']}

    async def estimate(self, artifact, ids=None):
        self.calls.append(('estimate', artifact['id'], ids))
        return {'min_count': 1, 'max_count': 2, 'scenarios': [], 'summary': '估算已完成'}

    async def analyze(self, artifact, instruction, ids=None):
        self.calls.append(('analyze', artifact['id'], ids))
        return {'answer': '已保存用例验证登录。', 'refs': []}

    async def generate_native(self, task, context, schema, instruction):
        self.calls.append(('template', task, context))
        return {'template_kinds': ['scenarios'], 'summary': '学到场景列', 'config': {
            'scenario_excel_columns': [{'field': 'id', 'header': '场景编号'},
                                       {'field': 'title', 'header': '场景标题'}]}}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    project = store.list('project')[0]
    chat = store.create_chat(project['id'], '原生工具')
    profile = store.list('profile', project_id=project['id'])[0]
    analysis = store.put('artifact', {'id': 'analysis', 'chat_id': chat['id'], 'project_id': project['id'],
        'type': 'analysis', 'title': '需求理解', 'revision': 1, '_visible': True, '_profile': profile['config'],
        'items': [{'id': 'R1', 'title': '登录需求'}], 'report': {}})
    scenarios = store.put('artifact', {'id': 'scenarios', 'chat_id': chat['id'], 'project_id': project['id'],
        'type': 'scenarios', 'title': '登录场景', 'revision': 1, '_visible': True, '_profile': profile['config'],
        'items': [{'id': 'S1', 'title': '登录成功', 'requirement_ids': ['R1']},
                  {'id': 'S2', 'title': '登录失败', 'requirement_ids': ['R1']}], 'report': {}})
    cases = store.put('artifact', {'id': 'cases', 'chat_id': chat['id'], 'project_id': project['id'],
        'type': 'cases', 'title': '登录用例', 'revision': 1, '_visible': True, '_profile': profile['config'],
        'items': [{'id': 'C1', 'title': '登录成功', 'scenario_id': 'S1', 'type': 'Business', 'priority': 'P1',
                   'preconditions': '已注册账号', 'steps': [{'action': '输入正确账号', 'expected': '登录成功'}],
                   'refs': ['source#P1'], 'actual_result': '人工执行结果'}], 'report': {}})
    _, run = store.create_run(chat['id'], {'content': '生成用例', 'intent': 'generate_case', 'mode': 'hitp'})
    run = store.update_run(run['id'], status='waiting')
    pipeline, business = Pipeline(), Business(store)
    prompt = {'id': pipeline.token, 'kind': 'scenario_review', 'run_id': run['id'],
              'artifact_id': scenarios['id'], 'artifact_revision': 1}
    def make(body=None, current_prompt=None):
        return {t.name: t for t in build_tools(store, business, pipeline, chat,
            body or {'reply_to': prompt['id']}, current_prompt or prompt)}
    yield SimpleNamespace(store=store, project=project, chat=chat, profile=profile, run=run,
                          pipeline=pipeline, business=business, prompt=prompt, make=make)
    store.close()


@pytest.mark.asyncio
async def test_tools_use_typed_native_schema_and_real_case_steps(setup):
    tools = setup.make()
    assert tools['modify_artifact_tool'].args_schema.model_json_schema()['properties']['new_values']
    result = await tools['read_artifact_tool'].ainvoke({'artifact_id': 'cases'})
    assert result['parts'][0]['type'] == 'case_details'
    assert result['items'][0]['steps'][0] == {'action': '输入正确账号', 'expected': '登录成功'}
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_estimate_does_not_generate_or_resume_and_preserves_selection(setup):
    tools = setup.make({'reply_to': setup.prompt['id'], 'artifact_id': 'scenarios', 'selected_ids': ['S2']})
    result = await tools['estimate_workload_tool'].ainvoke({})
    assert result['parts'][0]['type'] == 'estimate'
    assert setup.business.calls == [('estimate', 'scenarios', ['S2'])]
    invalid = await tools['estimate_workload_tool'].ainvoke({'scenario_ids': ['S1']})
    assert invalid['status'] == 'needs_input'
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_one_turn_cannot_approve_two_gates_or_changed_result(setup):
    tools = setup.make()
    accepted = await tools['resume_pipeline_tool'].ainvoke({})
    repeat = await tools['resume_pipeline_tool'].ainvoke({})
    assert accepted['status'] == 'succeeded'
    assert repeat['status'] == 'needs_input'
    assert len(setup.pipeline.calls) == 1
    setup.pipeline.token = 'pipeline:gate:1'
    tools = setup.make()
    edited = await tools['modify_artifact_tool'].ainvoke({'artifact_id': 'scenarios', 'item_id': 'S2',
                                                          'new_values': {'title': '新的场景标题'}})
    assert edited['status'] == 'needs_confirmation'
    assert not [call for call in setup.pipeline.calls if call[0] == 'changed']
    stale = await tools['resume_pipeline_tool'].ainvoke({})
    assert stale['status'] == 'needs_input'
    assert len([c for c in setup.pipeline.calls if c[0] == 'resume']) == 1


@pytest.mark.asyncio
async def test_edits_cannot_cross_chat_or_run_while_busy(setup):
    other = setup.store.create_chat(setup.project['id'], '其他会话')
    setup.store.put('artifact', {'id': 'foreign', 'chat_id': other['id'], 'project_id': setup.project['id'],
        'type': 'scenarios', 'revision': 1, 'items': [{'id': 'S1'}]})
    tools = setup.make()
    denied = await tools['modify_artifact_tool'].ainvoke({'artifact_id': 'foreign', 'instruction': '改标题'})
    assert denied['error_status'] == 404
    setup.pipeline.busy = True
    busy = await tools['modify_artifact_tool'].ainvoke({'artifact_id': 'scenarios', 'instruction': '改标题'})
    assert busy['error_status'] == 409
    assert not setup.business.calls
    assert (await tools['read_artifact_tool'].ainvoke({'artifact_id': 'cases'}))['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_confirmed_project_facts_shared_but_assumptions_not_shared(setup):
    tools = setup.make()
    fact = await tools['add_knowledge_tool'].ainvoke({'content': '锁定后 5 分钟自动解锁。'})
    assumption = await tools['add_knowledge_tool'].ainvoke({'content': '暂假设密码有效期为 90 天', 'confirmed': False})
    shared = setup.store.get('source', fact['source']['id'])
    local = setup.store.get('source', assumption['source']['id'])
    assert shared['_project_shared'] and shared['status'] == 'confirmed'
    assert local['status'] == 'provisional' and not local.get('_project_shared')
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_clarification_adoption_is_a_distinct_native_resume(setup):
    p = {**setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': '锁定多久？', 'suggestion': '5 分钟'}]}
    result = await setup.make(current_prompt=p)['answer_clarification_tool'].ainvoke({'adopt_suggestions': True})
    assert result['status'] == 'succeeded'
    assert setup.pipeline.calls == [('resume', setup.run['id'], 'clarify',
                                    {'answers': {'Q1': '5 分钟'}, 'save_to_project': True})]


@pytest.mark.asyncio
async def test_template_learning_keeps_case_columns_then_exports_actual_scenario_cells(setup):
    text, chunks = parse_text('场景编号\t场景标题')
    src = setup.store.add_source(setup.chat['id'], '场景模板', 'example', text, chunks)
    tools = setup.make()
    learned = await tools['learn_template_tool'].ainvoke({'source_ids': [src['id']], 'kind': 'scenarios'})
    assert learned['status'] == 'needs_confirmation'
    p = setup.store.get('chat', setup.chat['id'])['_native_template_prompt']
    tools = setup.make({'reply_to': p['id']}, p)
    applied = await tools['apply_profile_tool'].ainvoke({})
    assert applied['status'] == 'succeeded'
    updated = setup.store.get('profile', setup.profile['id'])
    assert updated['config']['excel_columns'] == setup.profile['config']['excel_columns']
    result = await tools['export_artifact_tool'].ainvoke({'artifact_ids': ['scenarios'],
                                                         'profile_id': setup.profile['id']})
    exp = setup.store.get('frozen_export', result['parts'][0]['files'][0]['url'].split('/')[-1])
    workbook = load_workbook(io.BytesIO(base64.b64decode(exp['_bytes'])))
    assert list(workbook.active.values)[:2] == [('场景编号', '场景标题'), ('S1', '登录成功')]
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_profile_samples_strip_business_references_and_manual_results(setup):
    result = await setup.make()['save_samples_tool'].ainvoke({'artifact_id': 'cases', 'case_ids': ['C1']})
    assert result['status'] == 'succeeded'
    samples = result['profile']['config']['sample_cases']
    assert len(samples) == 1
    assert not {'id', 'scenario_id', 'refs', 'actual_result'} & samples[0].keys()
    assert samples[0]['steps'][0]['expected'] == '登录成功'


@pytest.mark.asyncio
async def test_preview_stays_pending_and_model_has_no_apply_or_discard_tool(setup):
    tools = setup.make()
    preview = await tools['modify_artifact_tool'].ainvoke({'artifact_id': 'scenarios', 'item_id': 'S1',
        'new_values': {'title': '预览标题'}, 'preview': True})
    assert preview['status'] == 'needs_confirmation'
    assert setup.store.get('artifact', 'scenarios')['items'][0]['title'] == '登录成功'
    assert not setup.pipeline.calls
    assert 'apply_artifact_preview_tool' not in tools
    assert 'discard_artifact_preview_tool' not in tools
    p = setup.store.get('chat', setup.chat['id'])['_native_artifact_prompt']
    assert 'apply_artifact_preview_tool' not in setup.make({'reply_to': p['id'], 'reply_kind': 'confirm'}, p)
    proposal = setup.store.get('artifact_proposal', p['proposal_id'])
    assert proposal['proposal_type'] == 'revision' and proposal['status'] == 'pending'
    assert not setup.pipeline.calls



@pytest.mark.asyncio
async def test_start_preserves_composer_mode_profile_and_narrow_user_goal(setup):
    body = {'content': '只生成场景', 'mode': 'hitp', 'profile_id': setup.profile['id'], '_turn_id': 'turn-native'}
    result = await setup.make(body)['start_pipeline_tool'].ainvoke({
        'requirements': '只生成场景', 'intent': 'generate_scenario', 'mode': 'auto', 'stop_after': 'review'})
    assert result['status'] == 'succeeded'
    request = setup.pipeline.calls[0][2]
    assert request['mode'] == 'hitp' and request['stop_after'] == 'scenarios'
    assert request['profile_id'] == setup.profile['id']
    assert request['_conversation_turn_id'] == 'turn-native'


@pytest.mark.asyncio
async def test_parallel_approval_tools_reserve_one_confirmation_before_waiting(setup):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = []

    async def permissive_resume(rid, action='approved', expected_prompt_id=None, payload=None):
        accepted.append(action)
        entered.set()
        await release.wait()
        return {'id': rid, 'status': 'queued'}

    setup.pipeline.resume = permissive_resume
    p = {**setup.prompt, 'kind': 'clarification', 'questions': [
        {'id': 'Q1', 'question': '锁定多久？', 'suggestion': '5 分钟'}]}
    tools = setup.make(current_prompt=p)
    first = asyncio.create_task(tools['answer_clarification_tool'].ainvoke({'adopt_suggestions': True}))
    await entered.wait()
    second = await tools['answer_clarification_tool'].ainvoke({'adopt_suggestions': True})
    assert second['status'] == 'needs_input'
    assert accepted == ['clarify'], 'The registry must guard parallel calls even before runtime checks.'
    release.set()
    assert (await first)['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_explicit_template_ids_cannot_bypass_the_presented_pipeline_confirmation(setup):
    text, chunks = parse_text('场景编号\t场景标题')
    source = setup.store.add_source(setup.chat['id'], '场景模板', 'example', text, chunks)
    learned = await setup.make()['learn_template_tool'].ainvoke({'source_ids': [source['id']], 'kind': 'scenarios'})
    ids = learned['pending'][0]['template_ids']
    tools = setup.make()
    rejected = await tools['apply_profile_tool'].ainvoke({'template_ids': ids, 'profile_id': setup.profile['id']})
    assert rejected['status'] == 'needs_input'
    assert setup.store.get('profile', setup.profile['id'])['version'] == 1
    assert (await tools['resume_pipeline_tool'].ainvoke({}))['status'] == 'needs_input'
    assert not setup.pipeline.calls


@pytest.mark.asyncio
async def test_start_uses_explicit_existing_artifact_and_ignores_viewed_card_for_fresh_generation(setup):
    tools = setup.make({'content': '使用新的需求重新生成', 'artifact_id': 'cases', 'mode': 'hitp'})
    result = await tools['start_pipeline_tool'].ainvoke({'requirements': '新的独立需求'})
    assert result['status'] == 'succeeded'
    assert 'artifact_id' not in setup.pipeline.calls[-1][2]
    continued = await tools['start_pipeline_tool'].ainvoke({'artifact_id': 'scenarios', 'intent': 'generate_case'})
    assert continued['status'] == 'succeeded'
    assert setup.pipeline.calls[-1][2]['artifact_id'] == 'scenarios'


@pytest.mark.asyncio
async def test_start_never_discards_selected_case_scope_or_uses_a_different_start(setup):
    tools = setup.make({'content': '评审选中用例', 'artifact_id': 'cases', 'selected_ids': ['C1']})
    fresh = await tools['start_pipeline_tool'].ainvoke({'intent': 'review_case'})
    other = await tools['start_pipeline_tool'].ainvoke({'intent': 'generate_case', 'artifact_id': 'scenarios'})
    assert fresh['status'] == other['status'] == 'needs_input'
    assert not setup.pipeline.calls
    reviewed = await tools['start_pipeline_tool'].ainvoke({'intent': 'review_case', 'artifact_id': 'cases'})
    assert reviewed['status'] == 'succeeded'
    assert setup.pipeline.calls[-1][2]['selected_ids'] == ['C1']
