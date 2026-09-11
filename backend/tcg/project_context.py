"""Project reuse and explicit restarts, with original evidence identifiers preserved."""
import copy
import json

from .schemas import DomainError
from .storage import now, public

MAX_SAMPLES = 5
MAX_SAMPLE_CHARACTERS = 12000
MAX_SHARED_SOURCES = 100
MAX_SHARED_CHARACTERS = 200000


def validate_sample_cases(value):
    if not isinstance(value, list) or len(value) > MAX_SAMPLES:
        raise DomainError('固定格式样例最多 5 条')
    if len(json.dumps(value, ensure_ascii=False)) > MAX_SAMPLE_CHARACTERS:
        raise DomainError('固定格式样例超过 12000 字符，请减少所选用例')
    forbidden = {'refs', 'source_ids', 'requirement_ids', 'scenario_id', 'project_id', 'chat_id'}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get('title'), str) or not row['title'].strip():
            raise DomainError('格式样例需要非空标题')
        if any(key.startswith('_') or key in forbidden for key in row):
            raise DomainError('格式样例不能保存业务引用或内部记录')
        steps = row.get('steps')
        if not isinstance(steps, list) or not steps or any(not isinstance(step, dict)
                or not isinstance(step.get('action'), str) or not isinstance(step.get('expected'), str)
                for step in steps):
            raise DomainError('格式样例需要 action / expected 步骤数组')
    return value


def shared_sources(store, project_id):
    return [source for source in store.list('source', project_id=project_id)
            if source.get('_project_shared') and source.get('_active')
            and source['role'] == 'clarification']


def share_clarification(store, source_id, project_id):
    """Called only after an explicit clarification submission; safe on retries."""
    with store.transaction():
        source = store.get('source', source_id)
        if source['project_id'] != project_id or source['role'] != 'clarification':
            raise DomainError('只有本项目的已提交澄清可以共享')
        if source.get('_project_shared'):
            return source
        others = shared_sources(store, project_id)
        if len(others) >= MAX_SHARED_SOURCES or sum(s['characters'] for s in others) + source['characters'] > MAX_SHARED_CHARACTERS:
            raise DomainError('项目共享澄清超过 100 条或 20 万字符；请先移除过期共享内容，或取消保存到项目')
        source = store.put('source', {**source, '_project_shared': True, '_shared_at': now()})
        store.audit(source_id, 'project_clarification_shared', {'project_id': project_id})
        return source


def shared_context(store, project_id):
    store.get('project', project_id)
    return {'clarifications': [
        {'id': s['id'], 'name': s['name'], 'text': s['_text'], 'created_at': s.get('_shared_at', s['created_at']),
         'active': True, 'chat_id': s['chat_id']}
        for s in shared_sources(store, project_id)],
        'samples': [{'profile_id': p['id'], 'profile_name': p['name'], 'version': p['version'],
                     'count': len(p['config'].get('sample_cases', []))}
                    for p in store.list('profile', project_id=project_id)]}


def unshare_clarification(store, project_id, source_id):
    with store.transaction():
        source = store.get('source', source_id)
        if source['project_id'] != project_id or not source.get('_project_shared'):
            raise DomainError('未找到本项目的共享澄清', 404)
        store.put('source', {**source, '_project_shared': False})
        store.audit(source_id, 'project_clarification_unshared', {'project_id': project_id})
    return {'ok': True}


def pin_samples(store, artifact_id, profile_id, expected_version, selected_ids):
    with store.transaction():
        artifact = store.get('artifact', artifact_id)
        profile = store.get('profile', profile_id)
        if artifact['type'] != 'cases' or not artifact.get('_visible'):
            raise DomainError('请选择已发布的测试用例')
        if profile['project_id'] != artifact['project_id']:
            raise DomainError('Profile 与用例必须属于同一项目')
        if not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= MAX_SAMPLES or len(set(selected_ids)) != len(selected_ids):
            raise DomainError('请选择 1 至 5 条不重复的样例用例')
        rows = {row['id']: row for row in artifact['items']}
        if not set(selected_ids) <= rows.keys():
            raise DomainError('所选用例不属于当前结果')
        dropped = {'refs', 'source_ids', 'requirement_ids', 'scenario_id', 'project_id', 'chat_id', 'id'}
        samples = [{key: copy.deepcopy(value) for key, value in rows[item_id].items()
                    if not key.startswith('_') and key not in dropped} for item_id in selected_ids]
        validate_sample_cases(samples)
        return store.update_profile(profile_id, profile['name'],
            {**profile['config'], 'sample_cases': samples}, expected_version)


def supplement_run(store, run_id, source_ids, content=''):
    """Cancel and replace a paused run in one transaction; keep all saved artifacts."""
    from .documents import parse_text
    with store.transaction():
        run = store.run(run_id)
        if run['status'] != 'waiting':
            raise DomainError('请在等待确认时补充资料；运行结束后可在新任务中使用新增资料', 409)
        if run.get('_edit_token'):
            raise DomainError('正在修改当前结果，请等待保存后再补充资料', 409)
        if run['intent'] not in ('review_requirement', 'generate_scenario', 'generate_case'):
            raise DomainError('当前操作请先结束，再基于新增资料发起评审或其他任务', 409)
        if not isinstance(source_ids, list) or len(source_ids) > 100 or len(set(source_ids)) != len(source_ids):
            raise DomainError('新增来源需要不重复的 ID 数组，最多 100 项')
        if not isinstance(content, str) or len(content) > 100000:
            raise DomainError('补充正文最多 10 万字符')
        previous_ids = list(run['_source_ids'])
        added = []
        for source_id in source_ids:
            source = store.get('source', source_id)
            if source['project_id'] != run['project_id'] or not source.get('_active'):
                raise DomainError('新增来源不属于当前项目或已停用')
            if source_id not in previous_ids:
                added.append(source_id)
        if content.strip():
            text, chunks = parse_text(content)
            source = store.add_source(run['chat_id'], '本轮补充资料', 'supplement', text, chunks)
            added.append(source['id'])
        if not added:
            raise DomainError('请上传新资料或填写补充正文')
        request = copy.deepcopy(run['_request'])
        request.update(intent=run['intent'], mode=run['mode'], content='结合新增资料重新理解需求，并完成原目标：\n' + run['_request']['content'],
                       source_ids=list(dict.fromkeys(previous_ids + added)), profile_id=run['_profile_id'], profile_override=run['_profile'],
                       as_requirement=False, artifact_id=None, selected_ids=None)
        # Resuming from an old artifact would silently skip understanding the new sources.
        request['_fresh_after_supplement'] = True
        for key in ('complete_fields_only', 'complete_descriptions_only'):
            request.pop(key, None)
        old = {**run, 'status': 'cancelled', 'stage': 'supplemented', '_resume': None, '_edit_token': None}
        old.pop('interrupt', None)
        store.save_run(old)
        _, successor = store.create_run(run['chat_id'], request)
        roles = {sid: run.get('_source_roles', {}).get(sid, store.get('source', sid)['role'])
                 for sid in successor['_source_ids']}
        successor = store.update_run(successor['id'], _source_roles=roles, previous_run_id=run_id)
        store.save_run({**old, 'successor_run_id': successor['id']})
        store.audit(run_id, 'supplement_restart', {'successor_run_id': successor['id'], 'source_ids': added})
    return {'run': public(successor), 'previous_run_id': run_id,
            'message': '已保留原有结果，并创建后续任务；将结合新旧资料重新理解需求和生成。'}


def register_project_routes(app):
    # Local imports keep the core operations testable without a server runtime.
    from pydantic import BaseModel, Field

    class PinSamplesInput(BaseModel):
        profile_id: str
        expected_version: int = Field(ge=1)
        selected_ids: list[str] = Field(min_length=1, max_length=5)

    class SupplementInput(BaseModel):
        source_ids: list[str] = Field(default_factory=list, max_length=100)
        content: str = Field(default='', max_length=100000)

    @app.get('/api/projects/{project_id}/shared-context')
    def get_shared(project_id: str):
        return shared_context(app.state.store, project_id)

    @app.delete('/api/projects/{project_id}/shared-context/{source_id}')
    def remove_shared(project_id: str, source_id: str):
        return unshare_clarification(app.state.store, project_id, source_id)

    @app.post('/api/artifacts/{artifact_id}/pin-samples')
    def save_samples(artifact_id: str, body: PinSamplesInput):
        return pin_samples(app.state.store, artifact_id, body.profile_id, body.expected_version, body.selected_ids)

    @app.post('/api/runs/{run_id}/supplement')
    async def add_supplement(run_id: str, body: SupplementInput):
        engine = app.state.engine
        result = supplement_run(app.state.store, run_id, body.source_ids, body.content)
        engine.trace('run.supplemented', run_id, successor_run_id=result['run']['id'])
        engine.schedule(result['run']['id'])
        return result
