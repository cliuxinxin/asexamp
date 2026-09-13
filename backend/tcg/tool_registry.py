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
from .schemas import DomainError, ROLES
from .storage import now, public, uid
from .model_diagnostics import failure_part


def _result(message, parts=(), status='succeeded', **extra):
    return {'status': status, 'message': message, 'parts': list(parts), **extra}


async def _await(value):
    return await value if inspect.isawaitable(value) else value


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

    def sources(source_ids):
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise DomainError('请选择不重复的资料 ID')
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
        return _result('当前对话的成果与资料目录。', artifacts=[
            {k: a.get(k) for k in ('id', 'type', 'title', 'revision')} for a in artifacts()],
            sources=[{k: s.get(k) for k in ('id', 'name', 'role', 'characters')}
                     for s in store.list('source', chat_id=chat['id']) if s.get('_active')],
            prompt=prompt)

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

    @tool
    @emit
    async def modify_artifact_tool(artifact_id: str | None = None, item_id: str | None = None,
                                    new_content: str | None = None, instruction: str = '',
                                    new_values: dict[str, Any] | None = None, preview: bool = False) -> dict:
        """Modify the requested artifact rows and save a new version, preserving other rows and manual data.

        Use new_values for explicit field changes, or instruction for a natural-language revision.
        Set preview=true when the user asks to inspect changes before saving. This tool never approves
        or resumes the pipeline after changing the result.
        """
        if not instruction and new_content is None and new_values is None:
            raise DomainError('请说明需要修改的内容')
        if new_content is not None:
            instruction = instruction + '\n用户要求的新内容：' + new_content
        async with edit_session():
            value = target(artifact_id)
            updated = await business.revise(value, ids=selection(value, [item_id] if item_id else None),
                                            instruction=instruction, new_values=new_values, preview=preview)
            if preview:
                proposal = {**updated, 'id': uid('revprop_'), 'chat_id': chat['id'],
                    'project_id': chat['project_id'], 'created_at': now()}
                pending = {'id': 'revision:' + proposal['id'] + ':' + str(value['revision']),
                    'kind': 'artifact_proposal', 'type': 'artifact_proposal',
                    'artifact_id': value['id'], 'artifact_revision': value['revision'],
                    'proposal_id': proposal['id'], 'title': '确认修改预览',
                    'message': '修改预览已准备好，回复“同意”保存，或说明需要调整的地方。'}
                with store.transaction():
                    store.put('native_revision_proposal', proposal)
                    current = store.get('chat', chat['id'])
                    store.put('chat', {**current, '_native_artifact_prompt': pending})
                return _result(pending['message'], [{'type': 'diff', 'artifact_id': value['id'],
                    'proposal_id': proposal['id'], 'changes': [{'artifact_id': value['id'],
                        'title': value['title'], 'expected_revision': value['revision'],
                        'before_items': value['items'], 'items': proposal['items'],
                        'report': proposal['report']}]}], status='needs_confirmation', pending=[pending])
            await _await(pipeline.on_artifact_changed(updated))
        return artifact_result(updated, '修改已保存；请查看新版本，满意后再回复继续。')

    @tool
    @emit
    async def apply_artifact_preview_tool(proposal_id: str | None = None) -> dict:
        """Save the current native revision preview after user agreement; keep the pipeline waiting."""
        pid = proposal_id or prompt.get('proposal_id')
        if not pid:
            raise DomainError('当前没有待采用的修改预览')
        async with approval({'artifact_proposal'}, proposal_id=pid):
            async with edit_session():
                current = store.get('chat', chat['id'])
                pending = current.get('_native_artifact_prompt')
                if not pending or pending.get('id') != reply_token or pending.get('proposal_id') != pid:
                    raise DomainError('修改预览提示已改变，请查看当前预览后再确认', 409)
                proposal = store.get('native_revision_proposal', pid)
                if proposal['chat_id'] != chat['id'] or proposal['project_id'] != chat['project_id']:
                    raise DomainError('修改预览不属于当前对话', 404)
                if proposal.get('_applied'):
                    raise DomainError('此修改已保存，不需要再次采用', 409)
                updated = await _await(business.apply_revision_preview(proposal))
                with store.transaction():
                    store.put('native_revision_proposal', {**proposal, '_applied': True})
                    latest = store.get('chat', chat['id'])
                    store.put('chat', {**latest, '_native_artifact_prompt': None})
                await _await(pipeline.on_artifact_changed(updated))
        return artifact_result(updated, '已保存预览中的修改；请查看新的主流程确认提示。')

    @tool
    @emit
    async def discard_artifact_preview_tool() -> dict:
        """Dismiss the currently presented revision preview without modifying the artifact."""
        with store.transaction():
            current = store.get('chat', chat['id'])
            pending = current.get('_native_artifact_prompt')
            if not pending or pending.get('id') != reply_token:
                raise DomainError('修改预览提示已改变，请查看当前提示', 409)
            store.put('chat', {**current, '_native_artifact_prompt': None})
        return _result('已取消修改预览，成果保持原版本。')

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
                source_ids=[s['id'] for s in values], source_roles={s['id']: role for s in values})
            await _await(pipeline.on_artifact_changed(updated))
        return artifact_result(updated, '已采用补充资料更新当前成果；请确认新版本后继续关联步骤。')

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
                                    artifact_id: str | None = None) -> dict:
        """Start the background test-design pipeline. Honor the user's requested stopping stage.

        intent is review_requirement, generate_scenario, generate_case or review_case;
        stop_after is analysis, scenarios, cases or review. mode is auto or hitp (Human).
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
                       mode=requested_mode, stop_after=stop_after)
        # A narrow explicit task cannot silently grow into later stages.
        if intent == 'review_requirement':
            request['stop_after'] = 'analysis'
        elif intent == 'generate_scenario':
            request['stop_after'] = 'scenarios'
        result = await pipeline.start_run(chat['id'], request)
        run = result[1] if isinstance(result, tuple) else result
        started = {'review_current_cases': '已从选定用例开始评审，将保存评审后的新版本。',
            'generate_downstream': '已从指定成果继续生成下游内容。'}.get(
            (run.get('start_context') or {}).get('behavior'), '已启动新的测试设计任务。')
        return _result(started + ' 当前对话仍可提问；到人工确认点会提示你回复。',
                       run_id=run.get('id', run.get('run_id')), run=public(run))

    async def resume_once(run_id, action, payload=None):
        run = own_run(run_id)
        kinds = {'clarification'} if action == 'clarify' else {
            'strategy_review', 'scenario_review', 'case_draft_review', 'case_result_review'}
        async with approval(kinds, run_id=run['id']):
            result = await pipeline.resume(run['id'], action=action,
                expected_prompt_id=reply_token, payload=payload)
        return _result('已提交当前节点的回复；下一确认点需要你另行回复。', run_id=run['id'],
                       run=public(result) if isinstance(result, dict) else None)

    @tool
    @emit
    async def resume_pipeline_tool(run_id: str | None = None, action: str = 'approved') -> dict:
        """Approve exactly the frozen, currently presented pipeline confirmation.

        Use only when the user agrees to that result. Never call after making a modification in the
        same turn merely to approve the new version. Questions and estimates do not need this tool.
        """
        if action == 'rejected':
            return _result('当前结果尚未确认，请直接说明需要修改的内容。', status='needs_input')
        if action != 'approved':
            raise DomainError('确认动作为 approved 或 rejected')
        return await resume_once(run_id, action)

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
        return _result({'pause': '已请求暂停。', 'cancel': '已取消任务。', 'retry': '已请求重试失败步骤。'}[action],
                       run_id=run['id'], run=public(value) if isinstance(value, dict) else None)

    @tool
    @emit
    async def learn_template_tool(source_ids: list[str], kind: str = 'both',
                                    instruction: str = '学习字段定义、列顺序与填写规则',
                                    apply: bool = False, profile_id: str | None = None) -> dict:
        """Learn scenario/case Excel templates from attachments, preserving the other template family.

        kind is scenarios, cases or both. Set apply=true only when the user explicitly requests
        immediate application; otherwise save a suggestion for a later conversational approval.
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
            instruction + '。附件仅作格式参考；场景列与用例列独立，人工执行字段不能由 AI 填写。')
        kinds = learned.get('template_kinds', [])
        if not kinds or not set(kinds) <= set(requested):
            raise DomainError('模板识别结果缺少所请求的模板类型')
        config, notes = merge_template_config(current['config'], learned['config'], kinds)
        suggestion = {'id': uid('tmpl_'), 'project_id': chat['project_id'], 'chat_id': chat['id'],
            'created_at': now(), 'source_ids': source_ids, 'template_kinds': kinds,
            'config': learned['config'], 'summary': learned['summary'], 'profile_id': current['id'],
            'base_profile_version': current['version']}
        with store.transaction():
            if profile(current['id'])['version'] != current['version']:
                raise DomainError('Profile 在学习期间已改变，请重新学习', 409)
            store.put('template', suggestion)
            if apply:
                updated = store.update_profile(current['id'], current['name'], config, current['version'])
                store.put('template', {**suggestion, '_applied': True})
                selected = store.get('chat', chat['id'])
                store.put('chat', {**selected, 'profile_id': updated['id'], '_native_template_prompt': None})
                return _result('已学习并应用模板。', [{'type': 'answer', 'text': learned['summary']}],
                               profile=public(updated), notes=notes)
            pending = {'id': 'template:' + suggestion['id'] + ':' + str(current['version']),
                'kind': 'profile', 'title': '确认模板建议', 'type': 'profile',
                'template_ids': [suggestion['id']], 'profile_id': current['id'],
                'expected_version': current['version'],
                'message': learned['summary'] + '\n回复“同意”应用模板，或说明修改意见。'}
            columns = []
            for family in kinds:
                key = 'scenario_excel_columns' if family == 'scenarios' else 'excel_columns'
                label = '场景列' if family == 'scenarios' else '用例列'
                columns.append(label + '（按导出顺序）：' + ' → '.join(c.get('header', c.get('field', '')) for c in config.get(key, [])))
            pending['message'] = learned['summary'] + '\n' + '\n'.join(columns) + '\n回复“同意”应用模板，或说明修改意见。'
            selected = store.get('chat', chat['id'])
            store.put('chat', {**selected, '_native_template_prompt': pending})
        return _result(pending['message'], status='needs_confirmation', pending=[pending], notes=notes,
                       proposal=public(suggestion))

    @tool
    @emit
    async def apply_profile_tool(template_ids: list[str] | None = None,
                                   profile_id: str | None = None) -> dict:
        """Apply the presented template suggestion after user agreement; use its original Profile version."""
        ids = template_ids or prompt.get('template_ids')
        if not ids:
            raise DomainError('请先学习模板并查看模板建议')
        async with approval({'profile'}, template_ids=ids,
                            profile_id=profile_id or prompt.get('profile_id')):
            with store.transaction():
                current = profile(profile_id or prompt.get('profile_id'))
                config = current['config']
                templates = [store.get('template', tid) for tid in ids]
                for suggestion in templates:
                    if suggestion['chat_id'] != chat['id'] or suggestion['project_id'] != chat['project_id']:
                        raise DomainError('模板建议不属于当前对话', 404)
                    if suggestion.get('_applied'):
                        raise DomainError('此模板已经应用，请查看当前 Profile', 409)
                    if suggestion['profile_id'] != current['id'] or suggestion['base_profile_version'] != current['version']:
                        raise DomainError('Profile 已改变，请重新学习或确认新的模板建议', 409)
                    config, _ = merge_template_config(config, suggestion['config'], suggestion['template_kinds'])
                pending = store.get('chat', chat['id']).get('_native_template_prompt')
                if not pending or pending['id'] != reply_token:
                    raise DomainError('模板提示已改变，请查看当前建议后再确认', 409)
                updated = store.update_profile(current['id'], current['name'], config, current['version'])
                for suggestion in templates:
                    store.put('template', {**suggestion, '_applied': True})
                selected = store.get('chat', chat['id'])
                store.put('chat', {**selected, 'profile_id': updated['id'], '_native_template_prompt': None})
        return _result('已应用模板，后续新任务使用此 Profile。', profile=public(updated))

    @tool
    @emit
    async def discard_template_tool() -> dict:
        """Dismiss the currently presented template suggestion without changing the Profile."""
        with store.transaction():
            current = store.get('chat', chat['id'])
            pending = current.get('_native_template_prompt')
            if not pending or pending.get('id') != reply_token:
                raise DomainError('当前模板提示已改变，请查看新的提示', 409)
            store.put('chat', {**current, '_native_template_prompt': None})
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
                                                       ids=selection(value, item_ids))
            await _await(pipeline.on_artifact_changed(updated))
        return artifact_result(updated, '已检查并补全模板缺项；没有业务依据的字段已保留具体原因。')

    @tool
    @emit
    async def export_artifact_tool(artifact_ids: list[str] | None = None,
                                     item_ids: list[str] | None = None,
                                     profile_id: str | None = None) -> dict:
        """Export saved scenarios or cases to downloadable Excel files using their separate template columns."""
        ids = artifact_ids or [target()['id']]
        snapshots = [copy.deepcopy(target(aid)) for aid in ids]
        chosen = profile(profile_id) if (profile_id or body.get('profile_id') or
            store.get('chat', chat['id']).get('profile_id')) else None
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
            record = {'id': uid('exp_'), 'chat_id': chat['id'], 'project_id': chat['project_id'],
                'created_at': now(), 'name': 'tcg_' + value['type'] + '_v' + str(value['revision']) + '.xlsx',
                'artifact_id': value['id'], 'revision': value['revision'],
                'profile_id': chosen['id'] if chosen else None,
                'profile_version': chosen['version'] if chosen else None,
                '_bytes': base64.b64encode(content).decode(), 'sha256': hashlib.sha256(content).hexdigest()}
            records.append(record)
        with store.transaction():
            for record in records:
                store.put('frozen_export', record)
        files = [{k: r[k] for k in ('name', 'artifact_id', 'revision', 'profile_id', 'profile_version')}
                 | {'url': '/api/exports/' + r['id']} for r in records]
        return _result('已导出 Excel。', [{'type': 'files', 'files': files}])

    reads = [list_context_tool, list_artifacts_tool, list_sources_tool, read_artifact_tool, read_knowledge_tool,
             estimate_workload_tool, analyze_artifact_tool]
    # A clicked reply is an explicit user scope, not a model-predicted intent.
    # Typed conversation retains the full registry and native tool selection.
    reply_kind = body.get('reply_kind')
    if reply_kind in ('question', 'clarification', 'confirm'):
        return reads + {'question': [], 'clarification': [answer_clarification_tool],
                        'confirm': [resume_pipeline_tool]}[reply_kind]
    return [*reads, modify_artifact_tool, apply_artifact_preview_tool, discard_artifact_preview_tool,
            update_from_sources_tool, add_knowledge_tool,
            start_pipeline_tool, resume_pipeline_tool, answer_clarification_tool, control_pipeline_tool,
            learn_template_tool, apply_profile_tool, discard_template_tool, save_samples_tool,
            complete_template_fields_tool, export_artifact_tool]
