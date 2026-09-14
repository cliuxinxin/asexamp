"""Full-screen case review projections and revision-bound human resolutions.

The browser owns an uncommitted draft. Saving a review stores one resolution;
only the existing native review gate publishes it as an artifact revision.
"""
import asyncio
import copy
import json
from typing import Any, Literal
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from . import dependencies as deps
from .native_business import artifact_profile
from .native_views import current_prompt
from .operations import native_writes
from .schemas import DomainError
from .storage import now, public, uid


class TableDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[dict[str, Any]] = Field(max_length=20000)
    expected_revision: int = Field(ge=1)
    profile_id: str = Field(min_length=1)
    profile_revision: int = Field(ge=1)
    revision: int | None = Field(default=None, ge=1)
    layout: Literal['case', 'step'] = 'case'
    run_id: str | None = None
    proposal_id: str | None = None
    prompt_id: str | None = None


class TableSave(TableDraft):
    client_request_id: str = Field(min_length=1, max_length=200)


def _artifact(store, artifact_id, revision=None):
    value = store.get('artifact', artifact_id)
    if not value.get('_visible') or value['type'] != 'cases':
        raise DomainError('请选择可查看的测试用例成果', 404)
    return store.revision(artifact_id, revision) if revision else value


def _profile(store, artifact, profile_id=None, profile_revision=None):
    profiles = store.list('profile', project_id=artifact['project_id'])
    chat = store.get('chat', artifact['chat_id'])
    selected = profile_id or chat.get('profile_id') or (profiles[0]['id'] if profiles else None)
    value = store.get('profile', selected) if selected else None
    if value and value['project_id'] != artifact['project_id']:
        raise DomainError('Profile 不属于当前项目', 404)
    if profile_revision is not None and (not value or value['version'] != profile_revision):
        raise DomainError('Profile 已更新，请重新打开评审表格', 409)
    return value


def _proposal(store, artifact, proposal_id):
    if not proposal_id:
        return None, None
    for kind in ('review_proposal', 'native_revision_proposal'):
        try:
            value = store.get(kind, proposal_id)
            break
        except DomainError as exc:
            if exc.status != 404:
                raise
    else:
        raise DomainError('这份修改建议不存在', 404)
    if value.get('artifact_id') != artifact['id'] or any(
            value.get(k) != artifact.get(k) for k in ('chat_id', 'project_id')):
        raise DomainError('修改建议不属于当前成果', 404)
    return value, kind


def _issues(report):
    reports = report.get('review_reports') or []
    latest = next((r for r in reversed(reports) if isinstance(r, dict)), report)
    values = [copy.deepcopy(v) if isinstance(v, dict) else {'title': str(v)}
              for v in latest.get('issues', [])]
    values += [{'title': '待确认问题', 'detail': str(v)} for v in latest.get('questions', [])]
    notes = latest.get('notes') or []
    values += [{'title': '评审说明', 'detail': str(v)} for v in ([notes] if isinstance(notes, str) else notes)]
    return values


async def review_view(store, pipeline, artifact_id, *, run_id=None, proposal_id=None,
                      revision=None, profile_id=None, layout=None):
    from .table_projection import case_table_projection
    artifact = _artifact(store, artifact_id, revision)
    chat = store.get('chat', artifact['chat_id'])
    prompt = await current_prompt(store, pipeline, chat)
    if run_id:
        run = store.run(run_id)
        if any(run.get(k) != artifact.get(k) for k in ('chat_id', 'project_id')):
            raise DomainError('任务不属于当前成果所在对话', 404)
    if not proposal_id and revision is None and prompt and prompt.get('artifact_id') == artifact_id:
        proposal_id = prompt.get('proposal_id')
    proposal, kind = _proposal(store, artifact, proposal_id)
    if proposal:
        if run_id and kind == 'review_proposal' and proposal['run_id'] != run_id:
            raise DomainError('评审建议不属于当前任务', 404)
        base_revision = proposal.get('artifact_revision', proposal.get('base_revision'))
        if revision is not None and revision != base_revision:
            raise DomainError('修改建议与所查看的历史版本不一致', 409)
        artifact = store.revision(artifact_id, base_revision)
    selected = _profile(store, artifact, profile_id)
    layout = layout or (selected or {}).get('config', {}).get('excel_layout', 'case')
    report = copy.deepcopy((proposal or artifact).get('report', {}))
    effective = {**artifact, 'report': report,
                 '_profile': artifact_profile({**artifact, 'report': report}, selected['config'] if selected else None)}
    proposed = copy.deepcopy((proposal or artifact)['items'])
    original = case_table_projection({**effective, 'items': artifact['items']}, layout=layout)
    proposed_grid = case_table_projection({**effective, 'items': proposed}, layout=layout)
    current = store.get('artifact', artifact_id)
    active = bool(proposal and prompt and prompt.get('proposal_id') == proposal_id)
    stale = current['revision'] != artifact['revision'] or bool(proposal and not active)
    read_only = bool(revision is not None or stale or store.runs(chat_id=artifact['chat_id'], statuses=('queued', 'running')))
    return {'artifact_id': artifact_id, 'title': artifact['title'], 'artifact_revision': artifact['revision'],
        'profile_id': selected['id'] if selected else None, 'profile_revision': selected['version'] if selected else None,
        'layout': layout, 'columns': original['columns'], 'original_items': copy.deepcopy(artifact['items']),
        'proposed_items': proposed, 'original_rows': original['rows'], 'proposed_rows': proposed_grid['rows'],
        'issues': _issues(report), 'read_only': read_only, 'stale': stale,
        'run_id': (proposal or {}).get('run_id') or (prompt or {}).get('run_id') if active else run_id,
        'proposal_id': proposal_id, 'prompt_id': prompt['id'] if active else None,
        'proposal_kind': kind, 'message': '此版本仅供查看，请重新打开当前评审建议后修改。' if read_only else ''}


def _bound(store, artifact_id, body):
    artifact = _artifact(store, artifact_id, body.revision)
    if artifact['revision'] != body.expected_revision:
        raise DomainError('成果已更新，请重新打开评审表格', 409)
    if body.revision is None and store.get('artifact', artifact_id)['revision'] != body.expected_revision:
        raise DomainError('成果已更新，请重新打开评审表格', 409)
    selected = _profile(store, artifact, body.profile_id, body.profile_revision)
    proposal, kind = _proposal(store, artifact, body.proposal_id)
    if proposal and proposal.get('artifact_revision', proposal.get('base_revision')) != body.expected_revision:
        raise DomainError('修改建议所对应的成果版本已改变', 409)
    if proposal and body.run_id and kind == 'review_proposal' and proposal['run_id'] != body.run_id:
        raise DomainError('评审建议不属于当前任务', 404)
    report = copy.deepcopy((proposal or artifact).get('report', {}))
    return artifact, selected, proposal, kind, {**artifact, 'report': report,
        '_profile': artifact_profile({**artifact, 'report': report}, selected['config'] if selected else None)}


def _draft_shape(store, business, artifact, proposal, rows):
    """Validate draft shape and real references without publishing user evidence."""
    ids = set()
    source_ids, roles, evidence = business._evidence(artifact)
    for sid in (proposal or {}).get('_source_ids', (proposal or {}).get('source_ids', [])):
        if sid not in source_ids:
            try:
                evidence += store.evidence([sid])
            except DomainError as exc:
                if exc.status != 404:
                    raise
    allowed = {e['id'] for e in evidence}
    allowed.update(e['id'] for e in ((proposal or {}).get('dialogue') or {}).get('evidence', []))
    for row in rows:
        for field in ('id', 'title', 'scenario_id', 'type', 'priority', 'preconditions'):
            if not isinstance(row.get(field), str):
                raise DomainError('用例字段 ' + field + ' 必须为文本')
        if not row['id'].strip() or row['id'] in ids:
            raise DomainError('用例编号不能为空或重复')
        ids.add(row['id'])
        steps = row.get('steps')
        if not isinstance(steps, list) or not steps or any(not isinstance(s, dict) or
                not isinstance(s.get('action'), str) or not isinstance(s.get('expected'), str) for s in steps):
            raise DomainError('每个步骤必须同时包含操作和预期结果文本')
        refs = row.get('refs')
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise DomainError('用例包含不属于当前资料的依据引用')


def project_draft(store, business, artifact_id, body, *, export=False):
    from .table_projection import case_table_projection
    artifact, selected, proposal, kind, effective = _bound(store, artifact_id, body)
    if body.revision is not None:
        # Immutable history remains readable when its original source is retired.
        # Exact snapshot equality prevents this read-only path from exporting edits.
        if body.items != (proposal or artifact)['items']:
            raise DomainError('历史版本只能查看和导出，不能修改', 409)
    else:
        _draft_shape(store, business, artifact, proposal, body.items)
    draft = {**effective, 'items': copy.deepcopy(body.items)}
    if export:
        from .documents import export_cases
        return export_cases(draft, body.layout)
    return case_table_projection(draft, layout=body.layout)


def _plain(row):
    return {k: v for k, v in row.items() if not k.startswith('_')}


def _prepare_resolution(store, business, artifact, proposal, items, selected):
    """Capture actual human edits, validate and freeze a single target revision."""
    originals = {r['id']: r for r in artifact['items']}
    suggestions = {r['id']: r for r in (proposal or artifact)['items']}
    rows = copy.deepcopy(items)
    for row in rows:
        old = originals.get(row.get('id'), {})
        trusted = old if _plain(row) == _plain(old) else suggestions.get(row.get('id'), old)
        row.update({k: copy.deepcopy(v) for k, v in trusted.items() if k.startswith('_')})
        for key in list(row):
            if key.startswith('_') and key not in trusted:
                row.pop(key)
    sources, roles, _ = business._evidence(artifact)
    if proposal:
        sources = list(dict.fromkeys(sources + proposal.get('_source_ids', proposal.get('source_ids', []))))
        roles.update(proposal.get('_source_roles', proposal.get('source_roles', {})))
        dialogue = proposal.get('dialogue')
        if dialogue:
            draft = dialogue['source']
            if dialogue.get('parent_changes'):
                raise DomainError('旧预览包含上游更改，请重新准备当前用例修改', 409)
            store.add_source(draft['chat_id'], draft['name'], 'supplement', draft['content'],
                draft['chunks'], source_id=draft['id'])
            sources = list(dict.fromkeys(sources + [draft['id']]))
            roles[draft['id']] = 'supplement'
    authored = [row for row in rows if _plain(row) != _plain(suggestions.get(row['id'], {}))
                and _plain(row) != _plain(originals.get(row['id'], {}))]
    source = None
    if authored:
        content = '用户在全屏评审中确认的手动修改：\n' + json.dumps(
            [{k: v for k, v in _plain(r).items() if k != 'refs'} for r in authored], ensure_ascii=False)
        source = store.add_source(artifact['chat_id'], '评审表格修改 · ' + artifact['title'], 'change',
            content, [{'text': content, 'locator': '全屏表格人工输入'}])
        sources.append(source['id'])
        roles[source['id']] = 'change'
        refs = [e['id'] for e in store.evidence([source['id']])]
        for row in authored:
            row['refs'] = list(dict.fromkeys(row['refs'] + refs))
    from .dialogue_lineage import normalize_independent_rows
    rows = normalize_independent_rows('cases', rows, artifact['items'], '用户在评审表格中明确设置为 N/A',
        source_id=source['id'] if source else None)
    report = copy.deepcopy((proposal or artifact).get('report', {}))
    report.pop('_native_input_digest', None)
    report['human_review'] = {'completed_at': now(), 'resolved_count': len(rows)}
    parents = business._parents(artifact)
    business._validate('cases', rows, store.evidence(sources, roles), parents, artifact_profile(artifact))
    guard = deps.manifest(store, source_ids=sources,
        artifact_ids=[{'id': a['id'], 'revision': a['revision']} for a in parents + [artifact]],
        profile_ids=[selected['id']] if selected else [])
    return {'items': rows, 'report': report, '_source_ids': sources, '_source_roles': roles,
            '_dependencies': guard, 'artifact_id': artifact['id'], 'artifact_revision': artifact['revision']}


def _receipt(store, chat, prompt_id, artifact, pending=None):
    value = {'id': 'approval:' + prompt_id, 'chat_id': chat['id'], 'project_id': chat['project_id'],
             'status': 'succeeded', 'parts': [{'type': 'artifact', 'artifact_id': artifact['id'], 'revision': artifact['revision']}],
             **({'pending': pending} if pending else {})}
    store.put('native_approval_receipt', value)
    return value


async def save_review(store, business, pipeline, artifact_id, body):
    artifact = _artifact(store, artifact_id)
    key = 'table-save:' + deps.digest([artifact_id, body.client_request_id])
    request_hash = deps.digest(body.model_dump())
    async with pipeline._mutation_session(artifact['chat_id']):
        try:
            saved = store.get('table_review_save', key)
        except DomainError as exc:
            if exc.status != 404:
                raise
            saved = None
        if saved:
            if saved['request_hash'] != request_hash:
                raise DomainError('该保存请求已用于其他修改，请重新提交', 409)
            if saved.get('run_id'):
                run = await pipeline.snapshot(saved['run_id'])
                if run['status'] == 'waiting' and (run.get('interrupt') or {}).get('prompt_id') == body.prompt_id:
                    await pipeline.resume(run['id'], expected_prompt_id=body.prompt_id)
            return {**saved['result'], 'artifact': public(store.get('artifact', artifact_id))}
        await pipeline.ensure_editable(artifact['chat_id'])
        artifact, selected, proposal, kind, effective = _bound(store, artifact_id, body)
        if body.revision is not None:
            raise DomainError('历史版本不能保存，请回到当前版本', 409)
        _draft_shape(store, business, artifact, proposal, body.items)
        chat = store.get('chat', artifact['chat_id'])
        prompt = await current_prompt(store, pipeline, chat)
        if proposal:
            if not prompt or prompt.get('id') != body.prompt_id or prompt.get('proposal_id') != body.proposal_id:
                raise DomainError('待确认建议已更新，请重新打开当前评审', 409)
        elif body.prompt_id or (prompt or {}).get('artifact_id') == artifact_id and (prompt or {}).get('proposal_id'):
            raise DomainError('此成果有待确认修改，请重新打开修改预览', 409)
        if kind == 'review_proposal':
            from .review_proposals import require_current_review
            run_id = proposal['run_id']
            result = {'message': '已保存你的审阅选择，正在完成评审。', 'run_id': run_id}
            def prepare():
                with store.transaction():
                    current, chosen, current_proposal, _, _ = _bound(store, artifact_id, body)
                    require_current_review(store, run_id, current_proposal['id'])
                    resolution = _prepare_resolution(store, business, current, current_proposal, body.items, chosen)
                    resolution.update(id='resolution_' + deps.digest([key, request_hash])[:32],
                        prompt_id=body.prompt_id, chat_id=chat['id'], project_id=chat['project_id'], created_at=now())
                    store.put('table_review_resolution', resolution)
                    store.put('review_proposal', {**current_proposal, '_resolution_id': resolution['id']})
                    store.put('table_review_save', {'id': key, 'request_hash': request_hash, 'run_id': run_id, 'result': result})
            run = await pipeline.resume(run_id, expected_prompt_id=body.prompt_id, prepare_resume=prepare)
            return {**result, 'run': run}
        with store.transaction(), native_writes():
            if proposal:
                deps.assert_manifest(store, proposal['dependencies'])
                if proposal.get('column_change'):
                    expected = proposal['column_change']
                    _profile(store, artifact, expected['profile_id'], expected['profile_version'])
            resolution = _prepare_resolution(store, business, artifact, proposal, body.items, selected)
            updated = store.revise_artifact(artifact_id, body.expected_revision, resolution['items'],
                reason='native_table_review', report=resolution['report'], source_ids=resolution['_source_ids'],
                source_roles=resolution['_source_roles'], dependencies=resolution['_dependencies'], provenance=resolution['_dependencies'])
            followup = None
            if proposal:
                store.put('native_revision_proposal', {**proposal, '_applied': True,
                    '_applied_artifact_id': artifact_id, '_applied_revision': updated['revision']})
                store.put('chat', {**store.get('chat', chat['id']), '_native_artifact_prompt': None})
                if proposal.get('column_change'):
                    from .case_columns import stage_column_profile
                    followup = stage_column_profile(store, updated, proposal['column_change'])
                _receipt(store, chat, body.prompt_id, updated, (followup or {}).get('pending'))
            result = {'message': '已保存表格中的审阅内容。', 'artifact': public(updated),
                'parts': [{'type': 'artifact', 'artifact_id': artifact_id, 'revision': updated['revision']}] + (followup or {}).get('parts', [])}
            store.put('table_review_save', {'id': key, 'request_hash': request_hash, 'result': result})
        await pipeline.on_artifact_changed(updated)
        return result


def register_table_review_routes(app):
    @app.get('/api/artifacts/{artifact_id}/table-review')
    async def view(artifact_id: str, run_id: str | None = None, proposal_id: str | None = None,
                   revision: int | None = None, profile_id: str | None = None, layout: Literal['case', 'step'] | None = None):
        return await review_view(app.state.store, app.state.engine, artifact_id, run_id=run_id,
            proposal_id=proposal_id, revision=revision, profile_id=profile_id, layout=layout)

    @app.post('/api/artifacts/{artifact_id}/table-review/project')
    def project(artifact_id: str, body: TableDraft):
        return project_draft(app.state.store, app.state.business, artifact_id, body)

    @app.post('/api/artifacts/{artifact_id}/table-review/export')
    def export(artifact_id: str, body: TableDraft):
        data = project_draft(app.state.store, app.state.business, artifact_id, body, export=True)
        return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': "attachment; filename=tcg-review.xlsx; filename*=UTF-8''" + quote('测试用例-表格评审.xlsx')})

    @app.post('/api/artifacts/{artifact_id}/table-review/save')
    async def save(artifact_id: str, body: TableSave):
        artifact = _artifact(app.state.store, artifact_id)
        async with app.state.conversation._chat_locks.setdefault(artifact['chat_id'], asyncio.Lock()):
            return await save_review(app.state.store, app.state.business, app.state.engine, artifact_id, body)
