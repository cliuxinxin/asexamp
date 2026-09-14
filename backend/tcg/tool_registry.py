"""Native, request-scoped tools; business services own validation and persistence.

The model selects capabilities. A tool cannot expand a browser selection, cross
projects, or manufacture the checkpoint token needed to approve a later gate.
"""
import base64
import copy
import functools
import hashlib
import inspect
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.tools import tool

from .documents import export_artifact, parse_text
from .project_context import merge_template_config, pin_samples, share_clarification
from .profile_changes import apply_profile_change, config_changes, change_summary
from .profile_edits import ProfileColumnEdit, propose_profile_edit, read_profile
from .schemas import DomainError, ROLES
from .storage import now, public, uid
from .model_diagnostics import failure_part


def _result(message, parts=(), status='succeeded', **extra):
    return {'status': status, 'message': message, 'parts': list(parts), **extra}


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _control_receipt(action, before, after):
    """Describe the acknowledged control operation using runtime state only."""
    replacement = after.get('id', before['id']) != before['id']
    stage = (after.get('stage') or 'understand') if replacement else (
        before.get('failed_node') if action == 'retry' else None) or before.get('stage') or after.get('stage')
    labels = {'understand': '理解需求', 'apply_clarification': '更新需求理解',
        'scenarios': '生成场景', 'cases': '生成用例', 'review': '评审用例',
        'clarification': '需求澄清', 'strategy_review': '确认需求理解',
        'scenario_review': '确认场景', 'case_draft_review': '确认用例草稿',
        'case_result_review': '确认评审结果', 'intake': '理解需求'}
    label = labels.get(stage, '当前步骤')
    status = after.get('status', before.get('status'))
    if action == 'retry':
        progress = {'queued': '等待执行', 'running': '正在执行',
            'waiting': '等待当前节点的回复', 'failed': '当前步骤仍未完成',
            'cancelled': '任务已取消', 'completed': '任务已完成'}.get(status, '请求已提交')
        message = (f'已请求重新理解需求，{progress}。' if replacement else
            f'已请求重新处理“{label}”，{progress}。') + '已有成果已保留。'
    elif action == 'pause':
        message = '当前任务已暂停，等待你的回复。' if status == 'waiting' else '已请求在当前步骤完成后暂停。'
    else:
        message = '任务已经完成，已有成果保留。' if status == 'completed' else '已取消任务，已有成果保留。'
    return {'action': action, 'run_id': after.get('id', before['id']), 'stage': stage,
        'stage_label': label, 'status': status, 'message': message}


def build_tools(store, business, pipeline, chat, body, prompt=None, on_result=None):
    """Build tools for one turn with an immutable user target and prompt binding."""
    chat, body, prompt = copy.deepcopy(chat), copy.deepcopy(body), copy.deepcopy(prompt or {})
    reply_token = body.get('reply_to') if 'reply_to' in body else prompt.get('id')
    confirmation_consumed = False

    def emit(fn):
        @functools.wraps(fn)
        async def invoke(*args, **kwargs):
            try:
                result = await fn(*args, **kwargs)
            except DomainError as exc:
                if getattr(exc, 'call_id', None):
                    result = _result(exc.message, [failure_part(exc)], status='failed', error_status=exc.status)
                else:
                    result = _result(exc.message, status='needs_input', error_status=exc.status)
            result['tool_name'] = fn.__name__
            if on_result:
                await _await(on_result(copy.deepcopy(result)))
                return copy.deepcopy(result)
            return result
        return invoke

    def artifacts(kind=None):
        return [a for a in store.list('artifact', chat_id=chat['id'])
                if a.get('_visible') and a['project_id'] == chat['project_id']
                and (not kind or a['type'] == kind)]

    def target(artifact_id=None, kind=None):
        chosen = artifact_id or body.get('artifact_id') or prompt.get('artifact_id')
        if chosen:
            value = store.get('artifact', chosen)
            if value.get('chat_id') != chat['id'] or value.get('project_id') != chat['project_id']:
                raise DomainError('成果不属于当前对话', 404)
            if body.get('_scope_artifact_id') and chosen != body['_scope_artifact_id']:
                raise DomainError('本计划只能操作原请求选中的成果', 409)
            expected = body.get('_expected_revisions', {}).get(chosen)
            if expected is not None and value['revision'] != expected:
                raise DomainError('计划引用的成果版本已改变，请查看当前版本后重新提出要求', 409)
            if not kind or value['type'] == kind:
                return value
            if artifact_id:
                raise DomainError('请选择 ' + kind + ' 成果')
        candidates = artifacts(kind)
        if len(candidates) != 1:
            raise DomainError('请指定要操作的成果 ID；可先查看当前成果目录')
        return candidates[0]

    def selection(artifact, ids=None):
        selected = body.get('selected_ids') if body.get('artifact_id') == artifact['id'] else None
        result = copy.deepcopy(ids if ids is not None else selected)
        if result is not None:
            if not result or len(set(result)) != len(result):
                raise DomainError('请提供不重复且非空的条目 ID')
            actual = {row['id'] for row in artifact['items']}
            if not set(result) <= actual:
                raise DomainError('所选条目不属于当前成果')
            if selected and not set(result) <= set(selected):
                raise DomainError('本轮只能操作输入框中已选中的条目')
        return result

    def own_run(run_id=None):
        if run_id:
            value = store.run(run_id)
            if value['chat_id'] != chat['id'] or value['project_id'] != chat['project_id']:
                raise DomainError('任务不属于当前对话', 404)
            return value
        bound = prompt.get('run_id')
        if bound:
            return own_run(bound)
        rows = store.runs(chat_id=chat['id'], statuses=('queued', 'running', 'waiting', 'failed'))
        if len(rows) != 1:
            raise DomainError('请指定需要操作的任务 ID')
        return rows[0]

    from .conversation_facts import sources_allowed, ensure_artifact_allowed

    def sources(source_ids):
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise DomainError('请选择不重复的资料 ID')
        source_ids = sources_allowed(store, chat['id'], source_ids, strict=True)
        values = [store.get('source', sid) for sid in source_ids]
        for source in values:
            shared = source.get('_project_shared') and source.get('status') == 'confirmed'
            if source['project_id'] != chat['project_id'] or (source['chat_id'] != chat['id'] and not shared):
                raise DomainError('资料不属于当前对话或已共享项目资料', 404)
            if not source.get('_active'):
                raise DomainError('资料已停用')
        return values

    def profile(profile_id=None):
        pid = profile_id or body.get('profile_id') or store.get('chat', chat['id']).get('profile_id')
        value = store.get('profile', pid) if pid else store.list('profile', project_id=chat['project_id'])[0]
        if value['project_id'] != chat['project_id']:
            raise DomainError('Profile 不属于当前项目', 404)
        return value

    @asynccontextmanager
    async def edit_session():
        async with pipeline.edit_session(chat['id']):
            yield

    @asynccontextmanager
    async def approval(kinds, **objects):
        """Reserve the turn's single frozen confirmation before any asynchronous work."""
        nonlocal confirmation_consumed
        if confirmation_consumed:
            raise DomainError('本轮已处理一次确认；请查看新的提示后再回复继续', 409)
        if prompt.get('kind') not in kinds or not reply_token or reply_token != prompt.get('id'):
            raise DomainError('本轮回复未绑定这项确认，请查看当前提示后再回复', 409)
        for key, expected in objects.items():
            actual = prompt.get(key)
            if isinstance(expected, list):
                matches = isinstance(actual, list) and sorted(actual) == sorted(expected)
            else:
                matches = actual == expected
            if not matches:
                raise DomainError('本轮只能确认当前提示中的对象，请先查看所需结果', 409)
        # No await separates the check and reservation, including parallel ToolNode calls.
        confirmation_consumed = True
        try:
            yield
        except BaseException:
            confirmation_consumed = False
            raise

    def artifact_result(artifact, message='修改已保存，请查看更新后的成果。'):
        return _result(message, [{'type': 'artifact', 'artifact_id': artifact['id'],
                                 'revision': artifact['revision']}])

    @tool
    @emit
    async def list_context_tool() -> dict:
        """List current artifact and attachment IDs without sending document bodies."""
        from .native_views import model_prompt
        return _result('当前对话的成果与资料目录。', artifacts=[
            {k: a.get(k) for k in ('id', 'type', 'title', 'revision')} for a in artifacts()],
            sources=[{k: s.get(k) for k in ('id', 'name', 'role', 'characters')}
                     for s in store.list('source', chat_id=chat['id']) if s.get('_active')
                     and sources_allowed(store, chat['id'], [s['id']])],
            prompt=model_prompt(prompt))

    @tool
    @emit
    async def list_artifacts_tool(kind: str | None = None) -> dict:
        """List all current chat artifact IDs, types, titles and revisions without large row bodies."""
        if kind and kind not in ('analysis', 'scenarios', 'cases'):
            raise DomainError('成果类型需为 analysis、scenarios 或 cases')
        return _result('当前成果目录。', artifacts=[
            {k: a.get(k) for k in ('id', 'type', 'title', 'revision')} for a in artifacts(kind)])

    @tool
    @emit
    async def list_sources_tool() -> dict:
        """List current attachments and confirmed shared project sources, with IDs and source roles."""
        from .project_context import shared_sources
        values = {s['id']: s for s in store.list('source', chat_id=chat['id']) if s.get('_active')}
        values.update({s['id']: s for s in shared_sources(store, chat['project_id'])})
        allowed = set(sources_allowed(store, chat['id'], list(values)))
        values = {sid: source for sid, source in values.items() if sid in allowed}
        return _result('当前资料目录。', sources=[{k: s.get(k) for k in
            ('id', 'name', 'role', 'characters', 'status')} for s in values.values()])

    @tool
    @emit
    async def read_artifact_tool(artifact_id: str | None = None, item_ids: list[str] | None = None,
                                 offset: int = 0, limit: int = 30) -> dict:
        """Read saved requirement, scenario or case rows, including steps and expected results.

        Pages are explicit: use next_offset to read remaining rows. No generation or writes.
        """
        value = target(artifact_id)
        ids = selection(value, item_ids)
        if offset < 0 or not 1 <= limit <= 100:
            raise DomainError('offset 需非负，limit 需为 1 至 100')
        rows = [r for r in value['items'] if ids is None or r['id'] in ids]
        page = rows[offset:offset + limit]
        part = {'type': 'case_details', 'artifact_id': value['id'], 'revision': value['revision'],
                'title': value['title'], 'items': page} if value['type'] == 'cases' else {
                    'type': 'artifact', 'artifact_id': value['id'], 'revision': value['revision']}
        return _result('已读取保存内容。', [part], artifact_id=value['id'], revision=value['revision'],
                       items=page, report=value.get('report', {}), total=len(rows),
                       next_offset=offset + len(page) if offset + len(page) < len(rows) else None)

    @tool
    @emit
    async def read_review_proposal_tool(proposal_id: str | None = None, run_id: str | None = None,
                                       item_ids: list[str] | None = None, offset: int = 0,
                                       issue_offset: int = 0, limit: int = 25) -> dict:
        """Read pending review findings and proposed case changes without applying them.

        Saved cases are unchanged until approval. Use this tool to explain a particular review
        suggestion. Changes and issues are paginated independently using their next offsets.
        """
        from .review_proposals import read_review_proposal
        from .native_views import review_opinions
        run = own_run(run_id)
        selected_id = proposal_id or prompt.get('review_proposal_id') or run.get('review_proposal_id')
        if not selected_id:
            raise DomainError('当前没有可查看的评审建议')
        if offset < 0 or issue_offset < 0 or not 1 <= limit <= 50:
            raise DomainError('offset 需非负，limit 需为 1 至 50')
        proposal = read_review_proposal(store, run['id'], selected_id)
        ids = copy.deepcopy(item_ids)
        if ids is not None:
            known = {row['id'] for row in proposal['items'] + proposal['before_items']}
            if not ids or len(set(ids)) != len(ids) or not set(ids) <= known:
                raise DomainError('请提供这份评审建议中不重复的用例编号')
            bound = body.get('selected_ids') if body.get('artifact_id') == proposal['artifact_id'] else None
            if bound and not set(ids) <= set(bound):
                raise DomainError('本轮只能读取输入框中已选中的用例')
        changes = [row for row in proposal['changes'] if ids is None or row['id'] in ids]
        page = changes[offset:offset + limit]
        review = review_opinions(proposal)
        issues = review['issues'][issue_offset:issue_offset + limit]
        return _result('已读取评审建议；尚未应用的建议不会改变已保存用例。',
            proposal_id=proposal['id'], run_id=run['id'], artifact_id=proposal['artifact_id'],
            artifact_revision=proposal['artifact_revision'], proposal_status=proposal['status'],
            stale=proposal['stale'], summary=review['summary'], changes=page, issues=issues,
            total_changes=len(changes), total_issues=len(review['issues']),
            next_offset=offset + len(page) if offset + len(page) < len(changes) else None,
            next_issue_offset=issue_offset + len(issues) if issue_offset + len(issues) < len(review['issues']) else None)

    @tool
    @emit
    async def read_knowledge_tool(source_ids: list[str], offset: int = 0, limit: int = 20) -> dict:
        """Read attached or shared project evidence in explicit pages, retaining exact evidence IDs."""
        values = sources(source_ids)
        if offset < 0 or not 1 <= limit <= 100:
            raise DomainError('offset 需非负，limit 需为 1 至 100')
        rows = store.evidence([v['id'] for v in values])
        page = rows[offset:offset + limit]
        return _result('已读取资料片段。', evidence=page, total=len(rows),
                       next_offset=offset + len(page) if offset + len(page) < len(rows) else None)

    @tool
    @emit
    async def estimate_workload_tool(scenario_ids: list[str] | None = None,
                                      artifact_id: str | None = None) -> dict:
        """Estimate case counts from saved scenarios; never generate cases or approve the pipeline."""
        value = target(artifact_id, 'scenarios')
        ensure_artifact_allowed(store, value)
        estimate = await business.estimate(value, ids=selection(value, scenario_ids))
        estimate.setdefault('title', value['title'])
        return _result(estimate.get('summary', '已估算用例数量，当前确认点保持不变。'),
                       [{'type': 'estimate', 'data': estimate}])

    @tool
    @emit
    async def analyze_artifact_tool(instruction: str, artifact_id: str | None = None,
                                     item_ids: list[str] | None = None) -> dict:
        """Explain, summarize or assess saved content without changing it or advancing a confirmation."""
        value = target(artifact_id)
        answer = await business.analyze(value, instruction, ids=selection(value, item_ids))
        return _result('已根据保存内容回答。', [{'type': 'answer', 'text': answer['answer'],
                       'refs': answer.get('refs', []), 'artifact_id': value['id'], 'revision': value['revision']}])

    def stage_artifact_proposal(value, updated, message='修改建议已准备好，请打开成果工作区逐项查看并保存。'):
        from .review_proposals import stage_revision_proposal
        return stage_revision_proposal(store, value, updated, message)

    @tool
    @emit
    async def modify_artifact_tool(artifact_id: str | None = None, item_id: str | None = None,
                                    new_content: str | None = None, instruction: str = '',
                                    new_values: dict[str, Any] | None = None, preview: bool = True,
                                    add: bool = False, parent_id: str | None = None,
                                    use_dialogue_evidence: bool = False, independent: bool = False,
                                    delete: bool = False, item_ids: list[str] | None = None) -> dict:
        """Prepare requested row changes for review and saving in the artifact workspace.

        Use new_values for explicit field changes, or instruction for a natural-language revision.
        Changes always remain a proposal until the user saves in the artifact workspace. This tool never approves
        or resumes the pipeline after changing the result.
        For requested additions set add=true and parent_id to the user's specified requirement ID
        (adding scenarios) or scenario ID (adding cases). Existing rows remain unchanged. If no parent
        was specified, keep the added row independent with N/A upstream linkage; never create upstream rows.
        Set independent=true when the user explicitly asks to detach upstream linkage or show N/A.
        Set use_dialogue_evidence=true for user-supplied new business facts in an ordinary edit.
        These modes always stage a preview and save no evidence until the user approves it.
        Set delete=true only to remove explicitly named item_id/item_ids or browser-selected rows.
        Deletion always shows a preview and never means deleting all rows when no IDs were supplied.
        """
        if not instruction and new_content is None and new_values is None and not delete and not independent:
            raise DomainError('请说明需要修改的内容')
        preview = True
        if new_content is not None:
            instruction = instruction + '\n用户要求的新内容：' + new_content
        async with edit_session():
            value = target(artifact_id)
            if item_id and item_ids:
                raise DomainError('请只提供 item_id 或 item_ids 中的一种')
            ids = selection(value, item_ids if item_ids is not None else [item_id] if item_id else None)
            if delete:
                if not ids:
                    raise DomainError('删除条目需提供明确的条目编号或先选择条目')
                preview = True
            if add and body.get('selected_ids'):
                selected_artifact = target(body.get('artifact_id'))
                selected_rows = [r for r in selected_artifact['items'] if r['id'] in body['selected_ids']]
                allowed_parents = ({r['id'] for r in selected_rows}
                    if (value['type'], selected_artifact['type']) in (('cases', 'scenarios'), ('scenarios', 'analysis'))
                    else {r.get('scenario_id') for r in selected_rows}
                    if value['type'] == selected_artifact['type'] == 'cases' else None)
                if allowed_parents is not None:
                    if parent_id and parent_id not in allowed_parents:
                        raise DomainError('新增条目只能关联本轮选中的上游范围')
                    if not parent_id:
                        if len(allowed_parents) != 1:
                            raise DomainError('本轮选择了多个上游条目，请说明要在哪个条目下新增')
                        parent_id = next(iter(allowed_parents))
            dialogue_kwargs = {}
            if add or use_dialogue_evidence:
                dialogue_kwargs = {'dialogue_content': body.get('_user_request', body.get('content', '')), 'add': add, 'parent_id': parent_id}
                preview = True
            updated = await business.revise(value, ids=ids, instruction=instruction,
                                            new_values=new_values, preview=preview, **({'independent': True} if independent else {}),
                                            **({'delete': True} if delete else {}), **dialogue_kwargs)
            return stage_artifact_proposal(value, updated)

    @tool
    @emit
    async def update_from_sources_tool(source_ids: list[str], instruction: str,
                                        artifact_id: str | None = None,
                                        item_ids: list[str] | None = None, role: str = 'supplement') -> dict:
        """Adopt uploaded supplementary requirements and update the requested current artifact.

        The chosen artifact alone is updated; the pipeline asks to confirm upstream changes before
        regenerating related downstream scenarios or cases. Examples are never business evidence.
        """
        if role not in ('primary', 'supplement', 'change', 'clarification', 'knowledge'):
            raise DomainError('补充业务资料不能采用样例角色')
        async with edit_session():
            values = sources(source_ids)
            value = target(artifact_id)
            updated = await business.revise(value, ids=selection(value, item_ids), instruction=instruction,
                source_ids=[s['id'] for s in values], source_roles={s['id']: role for s in values}, preview=True)
            return stage_artifact_proposal(value, updated, '已根据补充资料准备修改建议，请打开成果工作区查看并保存。')

    @tool
    @emit
    async def add_knowledge_tool(content: str, role: str = 'clarification', share: bool = True,
                                  confirmed: bool = True) -> dict:
        """Save user-supplied supplementary facts. Confirmed clarifications are shared within this project.

        Set confirmed=false for a proposed assumption; unconfirmed assumptions remain local and are
        excluded from current requirements. Saving alone does not regenerate or approve any stage.
        """
        if role not in ROLES:
            raise DomainError('资料用途无效')
        text, chunks = parse_text(content)
        with store.transaction():
            source = store.add_source(chat['id'], '对话澄清' if role == 'clarification' else '对话补充资料',
                                      role, text, chunks)
            source = store.put('source', {**source, 'status': 'confirmed' if confirmed else 'provisional'})
            if share and confirmed and role == 'clarification':
                source = share_clarification(store, source['id'], chat['project_id'])
        return _result('已保存到项目澄清，同项目成员可复用。' if source.get('_project_shared') else '已保存本次资料。',
                       source=public(source))

    @tool
    @emit
    async def start_pipeline_tool(requirements: str = '', intent: str = 'generate_case',
                                    stop_after: str = 'review', mode: str | None = None,
                                    artifact_id: str | None = None, skip_scenarios: bool = False) -> dict:
        """Start the background test-design pipeline. Honor the user's requested stopping stage.

        intent is review_requirement, generate_scenario, generate_case or review_case;
        stop_after defaults to review: ordinary test-case generation INCLUDES AI review.
        Omit stop_after unless the user explicitly requests an earlier stopping point.
        cases means DRAFTS ONLY without AI review; do not choose it merely because the user says
        "generate test cases". Other stops are analysis, scenarios and review.
        Set skip_scenarios=true ONLY when the user explicitly asks to skip scenario generation and
        generate cases directly from requirements. Keep understanding and clarification first.
        mode is auto or hitp (Human); Human pauses for approvals, it does not skip AI review.
        Set artifact_id explicitly to continue from saved understanding/scenarios or review saved cases.
        Omit artifact_id for fresh generation from requirements, regardless of the currently viewed card.
        A nonempty browser row selection must belong to the explicit starting artifact; never ignore it.
        """
        if intent not in ('review_requirement', 'generate_scenario', 'generate_case', 'review_case'):
            raise DomainError('请选择需求理解、场景生成、用例生成或用例评审')
        if stop_after not in ('analysis', 'scenarios', 'cases', 'review'):
            raise DomainError('停止阶段无效')
        requested_mode = body.get('mode') or mode or 'auto'
        if requested_mode not in ('auto', 'hitp'):
            raise DomainError('模式需为 auto 或 hitp')
        request = {k: copy.deepcopy(body[k]) for k in ('profile_id', 'profile_override', 'source_ids',
            'depth', 'case_types', '_conversation_turn_id') if k in body}
        if body.get('_turn_id'):
            request['_conversation_turn_id'] = body['_turn_id']
        if request.get('source_ids'):
            sources(request['source_ids'])
        if body.get('selected_ids') and (not artifact_id or artifact_id != body.get('artifact_id')):
            raise DomainError('本轮已选中具体条目，请指定其所属成果作为起点，或清除选择后开始新任务')
        if artifact_id:
            starting = target(artifact_id)
            request['artifact_id'] = starting['id']
            ids = selection(starting)
            if ids:
                request['selected_ids'] = ids
        request.update(content=requirements or body.get('content', ''), intent=intent,
                       mode=requested_mode, stop_after=stop_after, skip_scenarios=skip_scenarios)
        # A narrow explicit task cannot silently grow into later stages.
        if intent == 'review_requirement':
            request['stop_after'] = 'analysis'
        elif intent == 'generate_scenario':
            request['stop_after'] = 'scenarios'
        result = await pipeline.start_run(chat['id'], request)
        run = result[1] if isinstance(result, tuple) else result
        started = {'review_current_cases': '已从选定用例开始生成评审建议，确认建议后再修改用例。',
            'generate_downstream': '已从指定成果继续生成下游内容。'}.get(
            (run.get('start_context') or {}).get('behavior'), '已启动新的测试设计任务。')
        return _result(started + ' 当前对话仍可提问；到人工确认点会提示你回复。',
                       run_id=run.get('id', run.get('run_id')), run=public(run))

    async def resume_once(run_id, action, payload=None):
        run = own_run(run_id)
        kinds = {'clarification'} if action == 'clarify' else {
            'strategy_review', 'scenario_review', 'case_draft_review', 'case_result_review'}
        async with approval(kinds, run_id=run['id']):
            latest = store.get('chat', chat['id'])
            if latest.get('_native_artifact_prompt') or latest.get('_native_template_prompt'):
                raise DomainError('请先处理输入框上方的修改预览，再确认主流程', 409)
            result = await pipeline.resume(run['id'], action=action,
                expected_prompt_id=reply_token, payload=payload)
        return _result('已提交当前节点的回复；下一确认点需要你另行回复。',
                       run_id=result.get('id', run['id']) if isinstance(result, dict) else run['id'],
                       run=public(result) if isinstance(result, dict) else None)

    @tool
    @emit
    async def resume_pipeline_tool(run_id: str | None = None, action: str = 'approved',
                                   draft_only: bool = False) -> dict:
        """Approve exactly the frozen, currently presented pipeline confirmation.

        Use only when the user agrees to that result. Never call after making a modification in the
        same turn merely to approve the new version. Questions and estimates do not need this tool.
        At understanding approval, explicit "skip scenarios, generate cases directly" uses action=skip_to_cases.
        draft_only=true is only for an explicit request to stop at drafts without AI review.
        Artifact proposal decisions must be saved in the workspace; this tool cannot approve them.
        """
        if prompt.get('proposal_id') or prompt.get('review_proposal_id'):
            return _result('请打开成果工作区查看并保存评审选择。', status='needs_confirmation',
                parts=[{'type': 'artifact_proposal', 'artifact_id': prompt.get('artifact_id'),
                    'proposal_id': prompt.get('proposal_id') or prompt.get('review_proposal_id')}], pending=[prompt])
        if action == 'rejected' and prompt.get('kind') != 'case_result_review':
            return _result('当前结果尚未确认，请直接说明需要修改的内容。', status='needs_input')
        if action not in ('approved', 'skip_to_cases', 'rejected'):
            raise DomainError('确认动作为 approved、skip_to_cases 或 rejected')
        result = await resume_once(run_id, action, {'draft_only': draft_only})
        if action == 'rejected':
            result['message'] = '已拒绝这份评审建议，保留当前用例。'
            result['resolution'] = 'rejected'
        elif action == 'skip_to_cases':
            result['message'] = '已按你的要求跳过场景，直接根据已确认需求生成用例。'
        return result

    @tool
    @emit
    async def answer_clarification_tool(answers: dict[str, str] | None = None,
                                         adopt_suggestions: bool = False,
                                         save_to_project: bool = True,
                                         run_id: str | None = None) -> dict:
        """Submit clarification answers or adopt all presented suggestions after user agreement.

        Adoption updates understanding then waits for its separate confirmation. Never invent an
        answer for a question that has no presented suggestion. Project sharing defaults to enabled.
        """
        if prompt.get('kind') != 'clarification':
            raise DomainError('当前不是澄清问题，请说明要补充的需求内容')
        questions = prompt.get('questions', [])
        command = body.get('command') or {}
        if command.get('name') == 'clarification.answer':
            from .clarification import bound_button_answer
            if body.get('reply_kind') != 'clarification' or reply_token != prompt.get('id'):
                raise DomainError('澄清问题已更新，请查看当前问题后重新选择。', 409)
            answers = bound_button_answer(command, questions)
            adopt_suggestions = False
            run_id = prompt.get('run_id')
        identities = {str(row[key]).strip(): row['id'] for row in questions for key in ('id', 'question') if row.get(key)}
        values = {}
        for key, value in (answers or {}).items():
            question_id = identities.get(key.strip())
            if not question_id:
                raise DomainError('答案没有对应当前澄清问题，请使用当前问题编号或完整问题文本')
            if question_id in values and values[question_id] != value:
                raise DomainError('同一澄清问题有不同答案，请保留一个明确答案')
            values[question_id] = value
        if adopt_suggestions:
            for row in questions:
                suggestion = row.get('suggestion') or row.get('suggested_answer') or row.get('suggested_assumption')
                if not suggestion and row['id'] not in values:
                    raise DomainError('问题 ' + row['id'] + ' 尚无建议答案，请补充后再提交')
                values.setdefault(row['id'], suggestion)
        if not values or any(not isinstance(v, str) or not v.strip() for v in values.values()):
            raise DomainError('请提供澄清答案，或明确采用当前提示中的建议')
        return await resume_once(run_id, 'clarify', {'answers': values, 'save_to_project': save_to_project})

    @tool
    @emit
    async def control_pipeline_tool(action: str, run_id: str | None = None) -> dict:
        """Pause, cancel or retry a background task when the user asks. Does not approve review results."""
        run = own_run(run_id)
        handlers = {'pause': pipeline.request_pause, 'cancel': pipeline.cancel, 'retry': pipeline.retry}
        if action not in handlers:
            raise DomainError('控制动作为 pause、cancel 或 retry')
        value = await _await(handlers[action](run['id']))
        current = value if isinstance(value, dict) else store.run(run['id'])
        receipt = _control_receipt(action, run, current)
        return _result(receipt['message'], run_id=receipt['run_id'], run=public(current),
                       control_receipt=receipt)

    @tool
    @emit
    async def read_profile_tool(profile_id: str | None = None) -> dict:
        """Read current Profile columns/preferences and any saved pending changes; no documents or writes."""
        return _result('当前 Profile 配置；pending_config 是等待确认的修改草稿。',
                       **read_profile(store, chat['id'], profile(profile_id)))

    @tool
    @emit
    async def modify_profile_tool(upsert_columns: list[ProfileColumnEdit] | None = None,
                                  remove_columns: list[str] | None = None,
                                  column_order: list[str] | None = None,
                                  preferences: dict[str, Any] | None = None,
                                  kind: str = 'cases', summary: str = '',
                                  profile_id: str | None = None) -> dict:
        """Propose incremental Profile/Excel changes directly from conversation; no uploaded template required.

        upsert_columns merges ONLY supplied properties by field; new fields append at the end.
        remove_columns removes named fields. column_order optionally lists ALL final fields in order.
        kind is cases or scenarios. preferences changes named settings such as language, case_level,
        scenario_level, case_types, additional_rules, scope, template_rules, excel_layout, sheet_name,
        filename_pattern, scenario_sheet_name or scenario_filename_pattern. Empty strings clear rules.
        Execution status/tester/results use manual fields; fixed values use value_source=default.
        Prior pending edits are retained for follow-up requests. Use read_profile_tool if field names
        or order are unclear. Always stage changes for preview and later user agreement; never apply
        them in this call. summary is a brief user-facing explanation, not a complete config dump.
        """
        result = propose_profile_edit(store, chat['id'], profile(profile_id), kind=kind,
            upsert_columns=upsert_columns or [], remove_columns=remove_columns or [],
            column_order=column_order, preferences=preferences, summary=summary)
        return _result(result.pop('message'), **result)

    @tool
    @emit
    async def modify_case_columns_tool(upsert_columns: list[ProfileColumnEdit] | None = None,
                                       remove_columns: list[str] | None = None,
                                       hide_columns: list[str] | None = None,
                                       column_order: list[str] | None = None,
                                       instruction: str = '', export_after_approval: bool = False,
                                       artifact_id: str | None = None,
                                       profile_id: str | None = None) -> dict:
        """Change CURRENT CASE TABLE columns first, then propose synchronized Profile changes.

        Use for requests to add/delete case columns or change their headers, rather than changing only
        Profile. AI design columns get evidence-grounded content; new execution/manual columns stay
        blank, and existing values are preserved. remove_columns previews real custom field deletion;
        protected core data such as id/steps cannot be deleted. hide_columns hides export mappings only.
        The human approves the case preview, then separately approves the exact Profile column diff.
        Set export_after_approval=true when the user also requested export; the server remembers this
        request and produces Excel immediately after the required approvals, without another instruction.
        Never approve either preview yourself. Column operations apply to the whole case table.
        """
        if body.get('_supervised'):
            export_after_approval = False
        if not upsert_columns and not remove_columns and not hide_columns and column_order is None:
            raise DomainError('请说明要新增、删除、隐藏或修改的用例列')
        async with edit_session():
            value, current = target(artifact_id, 'cases'), profile(profile_id)
            if body.get('selected_ids') or selection(value) is not None:
                raise DomainError('列修改作用于整张用例表，请先清除条目选择后再修改列')
            from .case_columns import prepare_case_columns
            updated = await prepare_case_columns(business, value, current,
                upserts=upsert_columns or [], removals=remove_columns or [], hidden=hide_columns or [],
                order=column_order, instruction=instruction, export_after_approval=export_after_approval)
            return stage_artifact_proposal(value, updated,
                '用例列修改建议已准备好，请打开成果工作区查看并保存，再核对同步到 Profile 的列更改。')

    @tool
    @emit
    async def revise_review_tool(feedback: str, run_id: str | None = None,
                                 item_ids: list[str] | None = None) -> dict:
        """Refine pending AI review recommendations without applying them to cases or approving the gate.

        Use when the human supplies additional review opinions and wants to inspect revised suggestions.
        A background review produces a new confirmation prompt. Existing case rows remain unchanged.
        Browser-selected rows bound the feedback scope. Only those rows are sent for AI revision;
        other rows and review issues from the current pending proposal remain unchanged.
        """
        run = own_run(run_id)
        if prompt.get('kind') != 'case_result_review' or prompt.get('run_id') != run['id']:
            raise DomainError('请先打开当前等待确认的评审建议后补充意见', 409)
        if not reply_token or reply_token != prompt.get('id'):
            raise DomainError('评审建议已改变，请重新打开当前建议', 409)
        if body.get('artifact_id') and body['artifact_id'] != prompt.get('artifact_id'):
            raise DomainError('所选用例不属于当前评审建议', 409)
        if body.get('artifact_revision') is not None and body['artifact_revision'] != prompt.get('artifact_revision'):
            raise DomainError('所选用例版本已改变，请重新打开评审建议', 409)
        selected = body.get('selected_ids')
        ids = copy.deepcopy(item_ids if item_ids is not None else selected)
        if selected is not None and ids is not None and not set(ids) <= set(selected):
            raise DomainError('评审意见只能作用于本轮选中的用例', 409)
        value = await _await(pipeline.revise_review(run['id'], feedback, selected_ids=ids,
            expected_prompt_id=reply_token, artifact_id=prompt.get('artifact_id'),
            artifact_revision=prompt.get('artifact_revision'),
            proposal_id=prompt.get('proposal_id') or prompt.get('review_proposal_id')))
        return _result('已提交补充评审意见，正在重新生成建议；用例尚未修改，完成后请确认新评审建议。',
                       run_id=value['id'], run=public(value))

    @tool
    @emit
    async def learn_template_tool(source_ids: list[str], kind: str = 'both',
                                    instruction: str = '学习字段定义、列顺序与填写规则',
                                    apply: bool = False, profile_id: str | None = None) -> dict:
        """Learn scenario/case Excel templates from attachments, preserving the other template family.

        kind is scenarios, cases or both. Set apply=true only when the user explicitly requests
        immediate application; otherwise summarize the changes and save a suggestion. Users can
        open Profile changes above the composer to review and selectively confirm the saved values.
        """
        if kind not in ('scenarios', 'cases', 'both'):
            raise DomainError('模板类型需为 scenarios、cases 或 both')
        values, current = sources(source_ids), profile(profile_id)
        requested = ['scenarios', 'cases'] if kind == 'both' else [kind]
        schema = {'title': 'TemplateSuggestion', 'type': 'object', 'properties': {
            'summary': {'type': 'string'}, 'template_kinds': {'type': 'array', 'items': {
                'type': 'string', 'enum': requested}}, 'config': {'type': 'object'}},
            'required': ['summary', 'template_kinds', 'config']}
        learned = await business.gateway.generate_native('learn_template', {
            'profile': current['config'], 'format_references': store.evidence([s['id'] for s in values]),
            'evidence': [], 'template_kinds': requested}, schema,
            instruction + '。附件仅作格式参考；场景列与用例列独立，人工执行字段不能由 AI 填写。'
            'summary 用一至两句话总结更改目的和范围，不逐列罗列表头或配置内容。')
        kinds = learned.get('template_kinds', [])
        if not kinds or not set(kinds) <= set(requested):
            raise DomainError('模板识别结果缺少所请求的模板类型')
        config, notes = merge_template_config(current['config'], learned['config'], kinds)
        changes = config_changes(current['config'], config)
        suggestion = {'id': uid('tmpl_'), 'project_id': chat['project_id'], 'chat_id': chat['id'],
            'created_at': now(), 'source_ids': source_ids, 'template_kinds': kinds,
            'config': learned['config'], 'summary': learned['summary'], 'profile_id': current['id'],
            'base_profile_version': current['version']}
        with store.transaction():
            if profile(current['id'])['version'] != current['version']:
                raise DomainError('Profile 在学习期间已改变，请重新学习', 409)
            store.put('template', suggestion)
            if not changes:
                selected = store.get('chat', chat['id'])
                store.put('chat', {**selected, '_native_template_prompt': None})
                return _result(learned['summary'] + '\n' + change_summary(changes),
                               profile=public(current), notes=notes)
            if apply:
                updated = store.update_profile(current['id'], current['name'], config, current['version'])
                store.put('template', {**suggestion, '_applied': True})
                selected = store.get('chat', chat['id'])
                store.put('chat', {**selected, 'profile_id': updated['id'], '_native_template_prompt': None})
                return _result('已学习并应用模板。', [{'type': 'answer', 'text': learned['summary']}],
                               profile=public(updated), notes=notes)
            pending = {'id': 'template:' + suggestion['id'] + ':' + str(current['version']),
                'kind': 'profile', 'title': '查看并确认 Profile 更改', 'type': 'profile',
                'template_ids': [suggestion['id']], 'profile_id': current['id'],
                'expected_version': current['version'],
                'summary': learned['summary'], 'change_summary': change_summary(changes),
                'message': learned['summary'] + '\n' + change_summary(changes) +
                    '\n可从输入框上方“查看 Profile 更改”逐项查看并确认，也可以继续说明修改意见。'}
            selected = store.get('chat', chat['id'])
            store.put('chat', {**selected, '_native_template_prompt': pending})
        return _result(pending['message'], status='needs_confirmation', pending=[pending], notes=notes,
                       proposal={key: suggestion[key] for key in ('id', 'summary', 'template_kinds')})

    @tool
    @emit
    async def apply_profile_tool(template_ids: list[str] | None = None,
                                   profile_id: str | None = None,
                                   selected_keys: list[str] | None = None) -> dict:
        """Apply presented Profile changes (learned template or direct edits) after explicit user agreement.

        Use the original Profile version. Omit selected_keys to apply all proposed changes, or
        include only saved top-level config keys the user explicitly approved. Never invent values.
        """
        ids = template_ids or prompt.get('template_ids')
        if not ids:
            raise DomainError('请先提出 Profile 更改并查看建议')
        async with approval({'profile'}, template_ids=ids,
                            profile_id=profile_id or prompt.get('profile_id')):
            applied = apply_profile_change(store, chat['id'], reply_token,
                                           prompt['expected_version'], selected_keys)
        return _result(applied['message'], applied.get('parts', []), profile=applied['profile'])

    @tool
    @emit
    async def discard_template_tool() -> dict:
        """Dismiss the currently presented template suggestion without changing the Profile."""
        with store.transaction():
            current = store.get('chat', chat['id'])
            pending = current.get('_native_template_prompt')
            if not pending or pending.get('id') != reply_token:
                raise DomainError('当前模板提示已改变，请查看新的提示', 409)
            from .case_columns import cancel_deferred_export
            cancel_deferred_export(store, pending)
            for template_id in pending.get('template_ids', []):
                template = store.get('template', template_id)
                store.put('template', {**template, '_rejected': True})
            store.put('chat', {**current, '_native_template_prompt': None})
            store.put('native_approval_receipt', {'id': 'approval:' + reply_token,
                'chat_id': chat['id'], 'project_id': chat['project_id'], 'status': 'cancelled', 'parts': []})
        return _result('已取消模板建议，Profile 保持原配置。')

    @tool
    @emit
    async def save_samples_tool(case_ids: list[str], artifact_id: str | None = None,
                                  profile_id: str | None = None) -> dict:
        """Save 1–5 selected cases as reusable project Profile writing samples, stripping business refs and manual results."""
        value, current = target(artifact_id, 'cases'), profile(profile_id)
        ids = selection(value, case_ids)
        updated = pin_samples(store, value['id'], current['id'], current['version'], ids)
        return _result('已保存用例格式样例；同项目成员使用此 Profile 时可复用写法。', profile=public(updated))

    @tool
    @emit
    async def complete_template_fields_tool(artifact_id: str | None = None,
                                            profile_id: str | None = None,
                                            item_ids: list[str] | None = None) -> dict:
        """Fill missing design fields from the applied template without regenerating cases.

        Preserve existing values and user-owned execution fields. Unsupported business facts remain
        explicitly unresolved, with field notes; this tool never invents facts to fill a spreadsheet.
        """
        async with edit_session():
            value, current = target(artifact_id, 'cases'), profile(profile_id)
            updated = await business.complete_fields(value, profile=current,
                                                       ids=selection(value, item_ids), preview=True)
            return stage_artifact_proposal(value, updated, '已准备模板缺项补全建议，请打开成果工作区查看并保存。')

    @tool
    @emit
    async def export_artifact_tool(artifact_ids: list[str] | None = None,
                                     item_ids: list[str] | None = None,
                                     profile_id: str | None = None) -> dict:
        """Export saved scenarios or cases to downloadable Excel files using their separate template columns."""
        import json

        step_id = body.get('_plan_step_id')
        receipt_id = 'receipt:' + step_id if step_id else None
        request = {'artifact_ids': artifact_ids, 'item_ids': item_ids, 'profile_id': profile_id,
            'scope': {key: copy.deepcopy(body[key]) for key in ('artifact_id', 'selected_ids', 'profile_id',
                '_scope_artifact_id', '_export_artifact_ids', '_expected_revisions', '_expected_profile') if key in body}}
        fingerprint = hashlib.sha256(json.dumps(request, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        def completed_export():
            if not receipt_id:
                return None
            try:
                saved = store.get('execution_step_receipt', receipt_id)
            except DomainError as exc:
                if exc.status != 404:
                    raise
                return None
            if (saved.get('chat_id') != chat['id'] or saved.get('project_id') != chat['project_id'] or
                    saved.get('tool_name') != 'export_artifact_tool' or
                    saved.get('request_fingerprint') != fingerprint):
                raise DomainError('本计划步骤已用于另一项导出，不能改变其目标、选择或模板', 409)
            return _result(saved['message'], copy.deepcopy(saved['parts']), status=saved['status'])

        completed = completed_export()
        if completed:
            # A committed file is immutable; replay returns that receipt, even if the head changed later.
            return completed
        ids = artifact_ids or [target()['id']]
        if body.get('_export_artifact_ids') and ids != body['_export_artifact_ids']:
            raise DomainError('本计划导出必须使用刚刚确认的成果', 409)
        snapshots = [copy.deepcopy(target(aid)) for aid in ids]
        chosen = profile(profile_id) if (profile_id or body.get('profile_id') or
            store.get('chat', chat['id']).get('profile_id')) else None
        expected_profile = body.get('_expected_profile')
        if expected_profile and (not chosen or chosen['id'] != expected_profile['id'] or
                chosen['version'] != expected_profile['version']):
            raise DomainError('本计划绑定的 Profile 已改变，尚未导出；请查看当前模板后重新提出要求', 409)
        # A chained model call cannot export the pre-edit rows while the user's preview awaits approval.
        pending_chat = store.get('chat', chat['id'])
        revision_prompt = pending_chat.get('_native_artifact_prompt')
        if revision_prompt and revision_prompt.get('artifact_id') in ids:
            proposal = store.get('artifact_proposal', revision_prompt['proposal_id'])
            if len(snapshots) == 1 and not item_ids and proposal.get('column_change'):
                if chosen and chosen['id'] != proposal['column_change']['profile_id']:
                    raise DomainError('待确认的用例列已绑定另一份 Profile，请先确认该修改', 409)
                proposal['column_change']['export_after_approval'] = True
                with store.transaction():
                    store.put('artifact_proposal', proposal)
            message = ('已记住导出请求。确认用例修改和 Profile 列更改后，会导出修改后的版本。'
                if len(snapshots) == 1 and not item_ids and proposal.get('column_change') else
                '当前成果修改尚未确认，请先查看并确认修改，再导出新版本。')
            return _result(message, status='needs_confirmation', pending=[revision_prompt])
        template_prompt = pending_chat.get('_native_template_prompt')
        if template_prompt and template_prompt.get('case_binding', {}).get('artifact_id') in ids:
            if len(snapshots) != 1 or item_ids:
                raise DomainError('请先确认用例列同步，再选择多份成果或部分条目导出', 409)
            if chosen and chosen['id'] != template_prompt['profile_id']:
                raise DomainError('请先确认当前 Profile 的列同步，再使用其他模板导出', 409)
            from .case_columns import request_export_after_profile
            with store.transaction():
                request_export_after_profile(store, chat['id'], template_prompt)
            return _result('已记住导出请求。确认当前 Profile 列更改后，会自动生成 Excel。',
                status='needs_confirmation', pending=[template_prompt])
        records = []
        for value in snapshots:
            if value['type'] not in ('scenarios', 'cases'):
                raise DomainError('Excel 导出支持场景和用例')
            selected = selection(value, item_ids) if len(snapshots) == 1 else selection(value)
            if item_ids and len(snapshots) != 1:
                raise DomainError('按条目导出时请只指定一份成果')
            if chosen:
                value['_profile'] = chosen['config']
            content = export_artifact(value, selected=selected)
            export_id = ('exp_' + hashlib.sha256((step_id + '\0' + value['id']).encode()).hexdigest()
                         if step_id else uid('exp_'))
            record = {'id': export_id, 'chat_id': chat['id'], 'project_id': chat['project_id'],
                'created_at': now(), 'name': 'tcg_' + value['type'] + '_v' + str(value['revision']) + '.xlsx',
                'artifact_id': value['id'], 'revision': value['revision'],
                'profile_id': chosen['id'] if chosen else None,
                'profile_version': chosen['version'] if chosen else None,
                '_bytes': base64.b64encode(content).decode(), 'sha256': hashlib.sha256(content).hexdigest()}
            records.append(record)
        files = [{k: r[k] for k in ('name', 'artifact_id', 'revision', 'profile_id', 'profile_version')}
                 | {'url': '/api/exports/' + r['id']} for r in records]
        result = _result('已导出 Excel。', [{'type': 'files', 'files': files}])
        with store.transaction():
            completed = completed_export()
            if completed:
                return completed
            # Recheck heads at the same commit boundary as the bytes and durable step receipt.
            for value in snapshots:
                current = store.get('artifact', value['id'])
                expected = body.get('_expected_revisions', {}).get(value['id'], value['revision'])
                if (current['chat_id'] != chat['id'] or current['project_id'] != chat['project_id'] or
                        current['revision'] != value['revision'] or current['revision'] != expected):
                    raise DomainError('导出期间成果已改变，尚未保存文件；请基于当前版本重新提出要求', 409)
            if chosen:
                current_profile = store.get('profile', chosen['id'])
                if (current_profile['project_id'] != chat['project_id'] or
                        current_profile['version'] != chosen['version'] or
                        expected_profile and (current_profile['id'] != expected_profile['id'] or
                            current_profile['version'] != expected_profile['version'])):
                    raise DomainError('导出期间 Profile 已改变，尚未保存文件；请查看当前模板后重试', 409)
            current_chat = store.get('chat', chat['id'])
            current_revision_prompt = current_chat.get('_native_artifact_prompt') or {}
            current_template_prompt = current_chat.get('_native_template_prompt') or {}
            if (current_revision_prompt.get('artifact_id') in ids or
                    current_template_prompt.get('case_binding', {}).get('artifact_id') in ids):
                raise DomainError('导出期间出现新的修改预览，请先确认当前修改，尚未保存文件', 409)
            for record in records:
                store.put('frozen_export', record)
            if receipt_id:
                store.put('execution_step_receipt', {'id': receipt_id, 'step_id': step_id,
                    'chat_id': chat['id'], 'project_id': chat['project_id'], 'created_at': now(),
                    'tool_name': 'export_artifact_tool', 'request_fingerprint': fingerprint,
                    **copy.deepcopy(result)})
        return result

    reads = [list_context_tool, list_artifacts_tool, list_sources_tool, read_artifact_tool, read_review_proposal_tool, read_knowledge_tool, read_profile_tool,
             estimate_workload_tool, analyze_artifact_tool]
    # A clicked reply is an explicit user scope, not a model-predicted intent.
    # Typed conversation retains the full registry and native tool selection.
    reply_kind = body.get('reply_kind')
    if reply_kind in ('question', 'clarification', 'confirm'):
        confirm_tools = ([] if prompt.get('kind') == 'artifact_proposal' or prompt.get('proposal_id') else
            [apply_profile_tool, discard_template_tool] if prompt.get('kind') == 'profile' else [resume_pipeline_tool])
        return reads + {'question': [], 'clarification': [answer_clarification_tool],
                        'confirm': confirm_tools}[reply_kind]
    return [*reads, modify_artifact_tool,
            update_from_sources_tool, add_knowledge_tool,
            start_pipeline_tool, resume_pipeline_tool, answer_clarification_tool, control_pipeline_tool,
            learn_template_tool, modify_profile_tool, modify_case_columns_tool, revise_review_tool,
            apply_profile_tool, discard_template_tool, save_samples_tool,
            complete_template_fields_tool, export_artifact_tool]
