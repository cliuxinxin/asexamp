"""Unified artifact workspace projections and revision-bound saves.

The browser owns an uncommitted draft. AI review resolutions still publish
through the native review gate; manual and revision-proposal saves publish one
optimistically guarded artifact revision directly.
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
    profile_id: str | None = Field(default=None, min_length=1)
    profile_revision: int | None = Field(default=None, ge=1)
    revision: int | None = Field(default=None, ge=1)
    layout: Literal['case', 'step'] = 'case'
    run_id: str | None = None
    proposal_id: str | None = None
    prompt_id: str | None = None
    report: dict[str, Any] | None = None
    column_changes: dict[str, Any] | None = None


class TableSave(TableDraft):
    client_request_id: str = Field(min_length=1, max_length=200)
    proposal_decision: Literal['resolve', 'reject'] = 'resolve'


def _artifact(store, artifact_id, revision=None):
    value = store.get('artifact', artifact_id)
    if not value.get('_visible') or value.get('type') not in ('analysis', 'scenarios', 'cases'):
        raise DomainError('请选择可查看的业务成果', 404)
    return store.revision(artifact_id, revision) if revision else value


def _profile(store, artifact, profile_id=None, profile_revision=None):
    profiles = store.list('profile', project_id=artifact['project_id'])
    chat = store.get('chat', artifact['chat_id'])
    selected = profile_id
    if selected is None and artifact['type'] in ('scenarios', 'cases'):
        selected = chat.get('profile_id') or (profiles[0]['id'] if profiles else None)
    value = store.get('profile', selected) if selected else None
    if value and value['project_id'] != artifact['project_id']:
        raise DomainError('Profile 不属于当前项目', 404)
    if profile_revision is not None and (not value or value['version'] != profile_revision):
        raise DomainError('Profile 已更新，请重新打开成果工作区', 409)
    return value


def _proposal(store, artifact, proposal_id):
    if not proposal_id:
        return None, None
    try:
        value = store.get('artifact_proposal', proposal_id)
    except DomainError as exc:
        if exc.status == 404:
            raise DomainError('这份修改建议不存在', 404) from None
        raise
    if value.get('artifact_id') != artifact['id'] or any(
            value.get(key) != artifact.get(key) for key in ('chat_id', 'project_id')):
        raise DomainError('修改建议不属于当前成果', 404)
    proposal_type = value.get('proposal_type')
    if proposal_type not in ('review', 'revision'):
        raise DomainError('修改建议类型无效', 409)
    return value, proposal_type


def _issues(report):
    reports = report.get('review_reports') or []
    latest = next((entry for entry in reversed(reports) if isinstance(entry, dict)), report)
    values = [copy.deepcopy(value) if isinstance(value, dict) else {'title': str(value)}
              for value in latest.get('issues', [])]
    values += [{'title': '待确认问题', 'detail': str(value)} for value in latest.get('questions', [])]
    notes = latest.get('notes') or []
    values += [{'title': '评审说明', 'detail': str(value)}
               for value in ([notes] if isinstance(notes, str) else notes)]
    return values


def _effective(artifact, report, selected):
    return {**artifact, 'report': report,
            '_profile': artifact_profile({**artifact, 'report': report},
                                         selected['config'] if selected else None)}


async def review_view(store, pipeline, artifact_id, *, run_id=None, proposal_id=None,
                      revision=None, profile_id=None, layout=None):
    from .table_projection import artifact_table_projection
    artifact = _artifact(store, artifact_id, revision)
    chat = store.get('chat', artifact['chat_id'])
    prompt = await current_prompt(store, pipeline, chat)
    if run_id:
        run = store.run(run_id)
        if any(run.get(key) != artifact.get(key) for key in ('chat_id', 'project_id')):
            raise DomainError('任务不属于当前成果所在对话', 404)
    if not proposal_id and revision is None and prompt and prompt.get('artifact_id') == artifact_id:
        proposal_id = prompt.get('proposal_id')
    proposal, proposal_type = _proposal(store, artifact, proposal_id)
    if proposal:
        if run_id and proposal_type == 'review' and proposal.get('run_id') != run_id:
            raise DomainError('评审建议不属于当前任务', 404)
        base_revision = proposal['artifact_revision']
        if revision is not None and revision != base_revision:
            raise DomainError('修改建议与所查看的历史版本不一致', 409)
        artifact = store.revision(artifact_id, base_revision)
    selected = (None if revision is not None and profile_id is None
                else _profile(store, artifact, profile_id))
    display_profile = selected['config'] if selected else artifact.get('_profile', {})
    layout = layout or (display_profile.get('excel_layout', 'case')
                        if artifact['type'] == 'cases' else 'case')
    report = copy.deepcopy((proposal or artifact).get('report', {}))
    effective = _effective(artifact, report, selected)
    proposed = copy.deepcopy((proposal or artifact)['items'])
    original = artifact_table_projection({**effective, 'items': artifact['items']}, layout=layout)
    proposed_grid = artifact_table_projection({**effective, 'items': proposed}, layout=layout)
    current = store.get('artifact', artifact_id)
    active = bool(proposal and proposal.get('status') == 'pending' and prompt
                  and prompt.get('proposal_id') == proposal_id)
    stale = current['revision'] != artifact['revision'] or bool(proposal and not active)
    read_only = bool(revision is not None or stale or
                     store.runs(chat_id=artifact['chat_id'], statuses=('queued', 'running')))
    mode = 'read_only' if read_only else 'ai_proposal' if active else 'manual'
    return {'artifact_id': artifact_id, 'artifact_type': artifact['type'], 'title': artifact['title'],
        'artifact_revision': artifact['revision'], 'mode': mode,
        'profile_id': selected['id'] if selected else None,
        'profile_revision': selected['version'] if selected else None,
        'layout': layout, 'columns': original['columns'],
        'original_items': copy.deepcopy(artifact['items']), 'proposed_items': proposed,
        'original_rows': original['rows'], 'proposed_rows': proposed_grid['rows'],
        'report': report, 'issues': _issues(report), 'read_only': read_only, 'stale': stale,
        'run_id': ((proposal or {}).get('run_id') or (prompt or {}).get('run_id')) if active else run_id,
        'proposal_id': proposal_id, 'prompt_id': prompt['id'] if active else None,
        'proposal_kind': proposal_type,
        'message': '此版本仅供查看，请重新打开当前成果或待确认建议后修改。' if read_only else ''}


def _bound(store, artifact_id, body):
    artifact = _artifact(store, artifact_id, body.revision)
    if artifact['revision'] != body.expected_revision:
        raise DomainError('成果已更新，请重新打开成果工作区', 409)
    if body.revision is None and store.get('artifact', artifact_id)['revision'] != body.expected_revision:
        raise DomainError('成果已更新，请重新打开成果工作区', 409)
    frozen_history = (body.revision is not None and body.profile_id is None
                      and body.profile_revision is None)
    selected = None if frozen_history else _profile(
        store, artifact, body.profile_id, body.profile_revision)
    proposal, proposal_type = _proposal(store, artifact, body.proposal_id)
    if proposal and proposal['artifact_revision'] != body.expected_revision:
        raise DomainError('修改建议所对应的成果版本已改变', 409)
    if proposal and body.run_id and proposal_type == 'review' and proposal.get('run_id') != body.run_id:
        raise DomainError('评审建议不属于当前任务', 404)
    report = copy.deepcopy(body.report if body.report is not None else (proposal or artifact).get('report', {}))
    return artifact, selected, proposal, proposal_type, _effective(artifact, report, selected)


def _draft_shape(store, business, artifact, proposal, rows):
    """Validate editable fields and real refs without publishing new evidence."""
    if not isinstance(rows, list):
        raise DomainError('成果条目必须为数组')
    ids = set()
    source_ids, _, evidence = business._evidence(artifact)
    for source_id in (proposal or {}).get('_source_ids', []):
        if source_id not in source_ids:
            try:
                evidence += store.evidence([source_id])
            except DomainError as exc:
                if exc.status != 404:
                    raise
    allowed = {entry['id'] for entry in evidence}
    allowed.update(entry['id'] for entry in ((proposal or {}).get('dialogue') or {}).get('evidence', []))
    for row in rows:
        if not isinstance(row, dict):
            raise DomainError('成果条目必须为对象')
        for field in ('id', 'title'):
            if not isinstance(row.get(field), str):
                raise DomainError('成果字段 ' + field + ' 必须为文本')
        if not row['id'].strip() or row['id'] in ids:
            raise DomainError('成果编号不能为空或重复')
        ids.add(row['id'])
        refs = row.get('refs')
        if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise DomainError('成果包含不属于当前资料的依据引用')
        if artifact['type'] in ('analysis', 'scenarios') and not isinstance(row.get('description'), str):
            raise DomainError('成果字段 description 必须为文本')
        if artifact['type'] == 'scenarios':
            if not isinstance(row.get('priority'), str):
                raise DomainError('场景字段 priority 必须为文本')
            if not isinstance(row.get('requirement_ids'), list) or any(
                    not isinstance(value, str) for value in row['requirement_ids']):
                raise DomainError('场景 requirement_ids 必须为文本数组')
        if artifact['type'] == 'cases':
            for field in ('scenario_id', 'type', 'priority', 'preconditions'):
                if not isinstance(row.get(field), str):
                    raise DomainError('用例字段 ' + field + ' 必须为文本')
            if not isinstance(row.get('requirement_ids', []), list) or any(
                    not isinstance(value, str) for value in row.get('requirement_ids', [])):
                raise DomainError('用例 requirement_ids 必须为文本数组')
            steps = row.get('steps')
            if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) or
                    not isinstance(step.get('action'), str) or
                    not isinstance(step.get('expected'), str) for step in steps):
                raise DomainError('每个步骤必须同时包含操作和预期结果文本')


def _column_plan(store, artifact, body):
    if not body.column_changes:
        return None
    if artifact['type'] != 'cases':
        raise DomainError('只有测试用例支持修改导出列')
    from .case_columns import manual_column_plan
    return manual_column_plan(store, artifact, body.column_changes, body.profile_id)


def project_draft(store, business, artifact_id, body, *, export=False):
    from .table_projection import artifact_table_projection
    artifact, selected, proposal, _, effective = _bound(store, artifact_id, body)
    if body.revision is not None:
        if (body.items != (proposal or artifact)['items'] or body.column_changes is not None
                or body.report not in (None, (proposal or artifact).get('report', {}))):
            raise DomainError('历史版本只能查看和导出，不能修改', 409)
    else:
        _draft_shape(store, business, artifact, proposal, body.items)
    plan = _column_plan(store, artifact, body)
    if plan:
        report = copy.deepcopy(effective.get('report', {}))
        report['table_columns'] = copy.deepcopy(plan['config']['excel_columns'])
        effective = _effective({**artifact, 'report': report}, report, selected)
    draft = {**effective, 'items': copy.deepcopy(body.items)}
    if export:
        if artifact['type'] == 'analysis':
            import io
            from openpyxl import Workbook
            projection = artifact_table_projection(draft, layout=body.layout)
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = 'Requirements'
            sheet.freeze_panes = 'A2'
            sheet.append([column['header'] for column in projection['columns']])
            for row in projection['rows']:
                sheet.append(row['cells'])
            output = io.BytesIO()
            workbook.save(output)
            return output.getvalue()
        from .documents import export_artifact
        return export_artifact(draft, body.layout)
    return artifact_table_projection(draft, layout=body.layout)


def _plain(row):
    return {key: value for key, value in row.items() if not key.startswith('_')}


def _resolved_report(artifact, proposal, supplied, column_plan, row_count):
    report = copy.deepcopy(supplied if supplied is not None else (proposal or artifact).get('report', {}))
    report.pop('_native_input_digest', None)
    original_lineage = artifact.get('report', {}).get('lineage')
    report.pop('lineage', None)
    if original_lineage:
        report['lineage'] = copy.deepcopy(original_lineage)
    if column_plan:
        report['table_columns'] = copy.deepcopy(column_plan['config']['excel_columns'])
    report['human_review'] = {'completed_at': now(), 'resolved_count': row_count}
    return report


def _prepare_resolution(store, business, artifact, proposal, items, selected,
                        *, report=None, column_plan=None):
    """Capture human edits, validate lineage, and freeze one target revision."""
    originals = {row['id']: row for row in artifact['items']}
    suggestions = {row['id']: row for row in (proposal or artifact)['items']}
    rows = copy.deepcopy(items)
    for row in rows:
        old = originals.get(row.get('id'), {})
        trusted = old if _plain(row) == _plain(old) else suggestions.get(row.get('id'), old)
        row.update({key: copy.deepcopy(value) for key, value in trusted.items() if key.startswith('_')})
        for key in list(row):
            if key.startswith('_') and key not in trusted:
                row.pop(key)
    sources, roles, _ = business._evidence(artifact)
    if proposal:
        sources = list(dict.fromkeys(sources + proposal.get('_source_ids', [])))
        roles.update(proposal.get('_source_roles', {}))
        dialogue = proposal.get('dialogue')
        if dialogue:
            draft = dialogue['source']
            if dialogue.get('parent_changes'):
                raise DomainError('旧预览包含上游更改，请重新准备当前成果修改', 409)
            store.add_source(draft['chat_id'], draft['name'], 'supplement', draft['content'],
                draft['chunks'], source_id=draft['id'])
            sources = list(dict.fromkeys(sources + [draft['id']]))
            roles[draft['id']] = 'supplement'
    authored = [row for row in rows if _plain(row) != _plain(suggestions.get(row['id'], {}))
                and _plain(row) != _plain(originals.get(row['id'], {}))]
    source = None
    if authored:
        content = '用户在成果工作区中确认的手动修改：\n' + json.dumps(
            [{key: value for key, value in _plain(row).items() if key != 'refs'} for row in authored],
            ensure_ascii=False, indent=2)
        source = store.add_source(artifact['chat_id'], '成果表格修改 · ' + artifact['title'], 'change',
            content, [{'text': content, 'locator': '成果工作区人工输入'}])
        sources.append(source['id'])
        roles[source['id']] = 'change'
        refs = [entry['id'] for entry in store.evidence([source['id']])]
        for row in authored:
            row['refs'] = list(dict.fromkeys(row.get('refs', []) + refs))
    from .dialogue_lineage import normalize_independent_rows
    dialogue_source_id = ((proposal or {}).get('dialogue') or {}).get('source', {}).get('id')
    rows = normalize_independent_rows(artifact['type'], rows, artifact['items'],
        '用户在评审表格中明确设置为 N/A',
        source_id=source['id'] if source else dialogue_source_id)
    resolved_report = _resolved_report(artifact, proposal, report, column_plan, len(rows))
    parents = business._parents(artifact)
    business._validate(artifact['type'], rows, store.evidence(sources, roles), parents,
        artifact_profile({**artifact, 'report': resolved_report}, selected['config'] if selected else None))
    guard = deps.manifest(store, source_ids=sources,
        artifact_ids=[{'id': value['id'], 'revision': value['revision']} for value in parents + [artifact]],
        profile_ids=[selected['id']] if selected else [])
    return {'items': rows, 'report': resolved_report, '_source_ids': sources,
            '_source_roles': roles, '_dependencies': guard,
            'artifact_id': artifact['id'], 'artifact_revision': artifact['revision']}


def _receipt(store, chat, prompt_id, artifact, pending=None, *, status='succeeded'):
    value = {'id': 'approval:' + prompt_id, 'chat_id': chat['id'], 'project_id': chat['project_id'],
             'status': status, 'parts': ([] if status == 'cancelled' else [
                 {'type': 'artifact', 'artifact_id': artifact['id'], 'revision': artifact['revision']}]),
             **({'pending': pending} if pending else {})}
    store.put('native_approval_receipt', value)
    return value


def _record_manual_column_followup(store, artifact, followup):
    if not followup:
        return
    message_id = uid('manual_columns_')
    store.put('message', {'id': message_id, 'project_id': artifact['project_id'],
        'chat_id': artifact['chat_id'], 'role': 'assistant', 'created_at': now(),
        'content': followup['message'], 'metadata': {'turn_response': {
            'id': message_id, 'status': followup.get('status', 'needs_confirmation'),
            'message': followup['message'], 'parts': followup.get('parts', []),
            'pending': followup.get('pending', []), 'actions': []}}})


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
                    action = saved.get('resume_action',
                                       'rejected' if saved.get('result', {}).get('rejected') else 'approved')
                    await pipeline.resume(run['id'], action=action, expected_prompt_id=body.prompt_id)
            return {**saved['result'], 'artifact': public(store.get('artifact', artifact_id))}
        await pipeline.ensure_editable(artifact['chat_id'])
        artifact, selected, proposal, proposal_type, _ = _bound(store, artifact_id, body)
        if body.revision is not None:
            raise DomainError('历史版本不能保存，请回到当前版本', 409)
        explicit_reject = body.proposal_decision == 'reject'
        if explicit_reject and not proposal:
            raise DomainError('没有可拒绝的待确认建议', 409)
        if not explicit_reject:
            _draft_shape(store, business, artifact, proposal, body.items)
        chat = store.get('chat', artifact['chat_id'])
        prompt = await current_prompt(store, pipeline, chat)
        if proposal:
            if (not prompt or prompt.get('id') != body.prompt_id
                    or prompt.get('proposal_id') != body.proposal_id
                    or (explicit_reject and proposal.get('status') != 'pending')):
                raise DomainError('待确认建议已更新，请重新打开当前评审', 409)
        elif body.prompt_id or ((prompt or {}).get('artifact_id') == artifact_id
                               and (prompt or {}).get('proposal_id')):
            raise DomainError('此成果有待确认修改，请重新打开修改预览', 409)
        column_plan = None if explicit_reject else _column_plan(store, artifact, body)
        rejected = explicit_reject or bool(
            proposal and proposal.get('changes') and body.items == artifact['items']
            and body.column_changes is None
            and body.report in (None, artifact.get('report', {})))
        if proposal_type == 'review':
            from .review_proposals import require_current_review
            run_id = proposal['run_id']
            if rejected:
                result = {'message': '已拒绝这份评审建议，原成果保持不变。',
                          'run_id': run_id, 'rejected': True}

                def prepare_rejection():
                    with store.transaction():
                        require_current_review(store, run_id, proposal['id'])
                        store.put('table_review_save', {'id': key, 'request_hash': request_hash,
                            'run_id': run_id, 'resume_action': 'rejected', 'result': result})
                run = await pipeline.resume(run_id, action='rejected', expected_prompt_id=body.prompt_id,
                                            prepare_resume=prepare_rejection)
                return {**result, 'run': run}
            result = {'message': '已保存你的审阅选择，正在完成评审。', 'run_id': run_id}

            def prepare():
                with store.transaction():
                    current, chosen, current_proposal, _, _ = _bound(store, artifact_id, body)
                    require_current_review(store, run_id, current_proposal['id'])
                    resolution = _prepare_resolution(store, business, current, current_proposal,
                        body.items, chosen, report=body.report, column_plan=column_plan)
                    resolution.update(id='resolution_' + deps.digest([key, request_hash])[:32],
                        prompt_id=body.prompt_id, chat_id=chat['id'], project_id=chat['project_id'],
                        created_at=now())
                    store.put('table_review_resolution', resolution)
                    store.put('artifact_proposal', {**current_proposal, '_resolution_id': resolution['id']})
                    store.put('table_review_save', {'id': key, 'request_hash': request_hash,
                        'run_id': run_id, 'resume_action': 'approved', 'result': result})
            run = await pipeline.resume(run_id, expected_prompt_id=body.prompt_id, prepare_resume=prepare)
            return {**result, 'run': run}
        with store.transaction(), native_writes():
            if rejected:
                store.put('artifact_proposal', {**proposal, 'status': 'rejected', 'rejected_at': now()})
                store.put('chat', {**store.get('chat', chat['id']), '_native_artifact_prompt': None})
                _receipt(store, chat, body.prompt_id, artifact, status='cancelled')
                result = {'message': '已拒绝这份修改建议，原成果保持不变。',
                          'artifact': public(artifact), 'parts': [], 'pending': [], 'rejected': True}
                store.put('table_review_save', {'id': key, 'request_hash': request_hash, 'result': result})
                return result
            if proposal:
                deps.assert_manifest(store, proposal['_dependencies'])
                if proposal.get('column_change'):
                    expected = proposal['column_change']
                    _profile(store, artifact, expected['profile_id'], expected['profile_version'])
            resolution = _prepare_resolution(store, business, artifact, proposal, body.items, selected,
                report=body.report, column_plan=column_plan)
            reason = 'native_workspace_proposal' if proposal else 'native_manual_edit'
            updated = store.revise_artifact(artifact_id, body.expected_revision, resolution['items'],
                reason=reason, report=resolution['report'], source_ids=resolution['_source_ids'],
                source_roles=resolution['_source_roles'], dependencies=resolution['_dependencies'],
                provenance=resolution['_dependencies'])
            followup = None
            if proposal:
                store.put('artifact_proposal', {**proposal, 'status': 'applied',
                    'applied_revision': updated['revision'], '_applied': True,
                    '_applied_artifact_id': artifact_id, '_applied_revision': updated['revision']})
                store.put('chat', {**store.get('chat', chat['id']), '_native_artifact_prompt': None})
                if proposal.get('column_change'):
                    from .case_columns import stage_column_profile
                    followup = stage_column_profile(store, updated, proposal['column_change'])
                _receipt(store, chat, body.prompt_id, updated, (followup or {}).get('pending'))
            elif column_plan:
                from .case_columns import stage_column_profile
                followup = stage_column_profile(store, updated, column_plan)
                _record_manual_column_followup(store, artifact, followup)
            result = {'message': '已保存成果工作区中的内容。', 'artifact': public(updated),
                'parts': [{'type': 'artifact', 'artifact_id': artifact_id,
                           'revision': updated['revision']}] + (followup or {}).get('parts', []),
                'pending': (followup or {}).get('pending', [])}
            store.put('table_review_save', {'id': key, 'request_hash': request_hash, 'result': result})
        await pipeline.on_artifact_changed(updated)
        return result


def register_table_review_routes(app):
    base = '/api/artifacts/{artifact_id}/workspace-grid'

    @app.get(base)
    async def view(artifact_id: str, run_id: str | None = None, proposal_id: str | None = None,
                   revision: int | None = None, profile_id: str | None = None,
                   layout: Literal['case', 'step'] | None = None):
        return await review_view(app.state.store, app.state.engine, artifact_id, run_id=run_id,
            proposal_id=proposal_id, revision=revision, profile_id=profile_id, layout=layout)

    @app.post(base + '/project')
    def project(artifact_id: str, body: TableDraft):
        return project_draft(app.state.store, app.state.business, artifact_id, body)

    @app.post(base + '/export')
    def export(artifact_id: str, body: TableDraft):
        artifact = _artifact(app.state.store, artifact_id, body.revision)
        data = project_draft(app.state.store, app.state.business, artifact_id, body, export=True)
        filename = '测试用例-成果工作区.xlsx' if artifact['type'] == 'cases' else '测试场景-成果工作区.xlsx'
        return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': "attachment; filename=tcg-workspace.xlsx; filename*=UTF-8''" + quote(filename)})

    @app.post(base + '/save')
    async def save(artifact_id: str, body: TableSave):
        artifact = _artifact(app.state.store, artifact_id)
        async with app.state.conversation._chat_locks.setdefault(artifact['chat_id'], asyncio.Lock()):
            result = await save_review(app.state.store, app.state.business, app.state.engine, artifact_id, body)
            if body.proposal_id:
                try:
                    receipt = app.state.store.get('native_approval_receipt', 'approval:' + body.prompt_id)
                except DomainError as exc:
                    if exc.status != 404:
                        raise
                else:
                    turn = {'id': uid('planreply_'), 'client_message_id': '',
                        'project_id': artifact['project_id'], 'chat_id': artifact['chat_id'],
                        'created_at': now(), 'status': 'succeeded', 'message': result['message'],
                        'parts': copy.deepcopy(result.get('parts', [])),
                        'pending': copy.deepcopy(result.get('pending', [])), 'actions': [], '_runtime': 'native'}
                    continued = await app.state.conversation.supervisor.after_control(
                        artifact['chat_id'], {'id': body.prompt_id}, turn,
                        rejected=receipt['status'] == 'cancelled', applied=receipt)
                    result = {**result, 'status': continued.get('status', 'succeeded'),
                        'message': continued.get('message') or result['message'],
                        'parts': continued.get('parts', result.get('parts', [])),
                        'pending': continued.get('pending', result.get('pending', []))}
            return result
