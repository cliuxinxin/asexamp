"""Interpret composer requests before generation, keeping estimates read-only."""
import copy
import json

from .artifact_actions import preview_action
from .schemas import DomainError, INTENTS
from .storage import now, public, uid


def run_snapshot(store, chat_id):
    """Only state that changes authorization; polling timestamps are irrelevant."""
    return sorted((r['id'], r['status'], r.get('_interrupt_id'),
                   r.get('interrupt', {}).get('type'), r.get('interrupt', {}).get('artifact_id'),
                   r.get('_edit_token'))
                  for r in store.runs(chat_id=chat_id, statuses=('queued', 'running', 'waiting')))


def scoped_artifacts(store, chat):
    return [a for a in store.list('artifact', chat_id=chat['id'], project_id=chat['project_id'])
            if a.get('_visible') and a.get('type') in ('analysis', 'scenarios', 'cases')]


def scenario_metadata(artifact):
    rows = artifact.get('items', [])
    return {'id': artifact['id'], 'title': artifact.get('title', '')[:200],
            'revision': artifact['revision'], 'scenario_count': len(rows),
            'scenarios': [{'id': r['id'], 'title': r.get('title', '')[:100], 'ordinal': i}
                          for i, r in enumerate(rows[:40], 1)],
            'scenario_titles_partial': len(rows) > 40}


def latest_estimate(store, chat_id):
    history = sorted(store.list('message', chat_id=chat_id), key=lambda m: m.get('created_at', ''))
    for message in reversed(history):
        estimate = message.get('metadata', {}).get('case_estimate')
        if message.get('role') == 'assistant' and isinstance(estimate, dict):
            return estimate
    return None


def routing_context(store, chat, body, artifacts, runs):
    """No document bodies, profile samples, analysis, or case steps reach routing."""
    visible = {a['id']: a for a in artifacts}
    displayed = visible.get(body.get('artifact_id'))
    candidates = [a for a in artifacts if a['type'] == 'scenarios']
    anchor_ids = {displayed['id']} if displayed else set()
    for run in runs:
        anchor_ids.add(run.get('interrupt', {}).get('artifact_id'))
    if displayed and displayed['type'] == 'cases':
        anchor_ids.add(displayed.get('report', {}).get('lineage', {}).get('scenario_artifact_id'))
    previous = latest_estimate(store, chat['id'])
    if previous:
        anchor_ids.add(previous.get('artifact_id'))
    anchor_ids.update(a['id'] for a in candidates if a['id'] in body['content'] or
                      (a.get('title') and a['title'] in body['content']))
    # Keep current anchors even when a long chat has many historical artifacts.
    candidates.sort(key=lambda a: (a['id'] in anchor_ids, a.get('created_at', '')), reverse=True)
    candidates = candidates[:12]
    history = sorted(store.list('message', chat_id=chat['id']), key=lambda m: m.get('created_at', ''))[-4:]
    sources = store.list('source', chat_id=chat['id'], project_id=chat['project_id'])
    roles = {}
    for source in sources:
        role = source.get('role', 'auto')
        roles[role] = roles.get(role, 0) + 1
    return {'content': body['content'], 'intent_hint': body.get('intent_hint', 'auto'),
            'sources': {'count': len(sources), 'roles': roles,
                        'characters': sum(s.get('characters', 0) for s in sources),
                        'sample_files': [{'name': str(s.get('name', ''))[:240], 'role': s.get('role', 'auto')}
                                         for s in sources[:3]]},
            'displayed_artifact': {k: displayed.get(k) for k in ('id', 'type', 'title', 'revision')} if displayed else None,
            'selected_ids': body.get('selected_ids') or [],
            'pending': [{'id': r['id'], 'status': r['status'], 'type': r.get('interrupt', {}).get('type'),
                         'artifact_id': r.get('interrupt', {}).get('artifact_id')} for r in runs],
            'scenario_candidates': [scenario_metadata(a) for a in candidates],
            'anchor_artifact_ids': [a['id'] for a in candidates if a['id'] in anchor_ids],
            'scenario_candidates_partial': len([a for a in artifacts if a['type'] == 'scenarios']) > len(candidates),
            'previous_estimate': {'artifact_id': previous.get('artifact_id'),
                                  'artifact_revision': previous.get('artifact_revision'),
                                  'instruction': previous.get('instruction', '')[:500],
                                  'scenario_ids': [r['scenario_id'] for r in previous.get('scenarios', [])[:80]]} if previous else None,
            'recent_messages': [{'role': m.get('role'), 'excerpt': str(m.get('content', ''))[:500]} for m in history],
            'contract': '识别本条消息真实动作；intent_hint 只是可过期的界面选择。估算不生成、修改或确认。资料正文刻意省略。'}


def fit_routing_context(engine, value):
    """Reduce metadata, never the current instruction, when the model is smaller."""
    value = copy.deepcopy(value)

    def fits():
        return len(json.dumps(value, ensure_ascii=False)) <= 130000 and engine.fits('chat_interpret', value)

    if fits():
        return value
    for limit in (12, 4, 0):
        for candidate in value['scenario_candidates']:
            if len(candidate['scenarios']) > limit:
                candidate['scenarios'] = candidate['scenarios'][:limit]
                candidate['scenario_titles_partial'] = True
        if fits():
            return value
    anchors = set(value['anchor_artifact_ids'])
    for limit in (4, 1, 0):
        fixed = [a for a in value['scenario_candidates'] if a['id'] in anchors]
        others = [a for a in value['scenario_candidates'] if a['id'] not in anchors]
        reduced = fixed + others[:limit]
        if len(reduced) < len(value['scenario_candidates']):
            value['scenario_candidates_partial'] = True
            value['scenario_candidates'] = reduced
        if fits():
            return value
    value['recent_messages'] = value['recent_messages'][-2:]
    for message in value['recent_messages']:
        message['excerpt'] = message['excerpt'][:160]
    if value['previous_estimate']:
        value['previous_estimate']['instruction'] = value['previous_estimate']['instruction'][:160]
    if fits():
        return value
    value['sources']['sample_files'] = []
    if fits():
        return value
    raise DomainError('本次请求仍超过模型容量，请缩短本条消息或调整模型容量；尚未启动任务')


def validate_decision(value):
    if not isinstance(value, dict) or value.get('action') not in ('estimate', 'normal'):
        raise DomainError('未能可靠识别本次请求，请重新发送；尚未生成或修改内容')
    if value['action'] == 'normal':
        if value.get('intent') not in INTENTS:
            raise DomainError('任务识别结果无效，请重新发送；尚未生成或修改内容')
        return value
    if value.get('scope', 'all') not in ('all', 'selected', 'subset', 'inherit_previous'):
        raise DomainError('估算范围识别失败，请明确场景编号后重新发送')
    for key in ('scenario_ids', 'scenario_ordinals'):
        members = value.get(key, [])
        if not isinstance(members, list) or len(members) != len(set(str(x) for x in members)):
            raise DomainError('估算范围识别失败，请明确场景编号后重新发送')
        if key == 'scenario_ids' and any(not isinstance(x, str) or not x for x in members):
            raise DomainError('估算场景编号无效，请重新发送')
        if key == 'scenario_ordinals' and any(type(x) is not int or x < 1 for x in members):
            raise DomainError('估算场景序号无效，请重新发送')
    for key in ('artifact_id', 'artifact_title'):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise DomainError('估算成果识别失败，请重新发送')
    return value


def resolve_scenario(artifacts, runs, body, decision, previous=None):
    visible = {a['id']: a for a in artifacts}
    candidates = [a for a in artifacts if a['type'] == 'scenarios']
    content = body['content']
    # A model-proposed ID alone cannot authorize picking one of ambiguous sets.
    explicit_id = decision.get('artifact_id')
    explicit_title = decision.get('artifact_title')
    if explicit_id and explicit_id in content:
        chosen = visible.get(explicit_id)
        if chosen and chosen['type'] == 'scenarios':
            return chosen
        return None
    if explicit_title and explicit_title in content:
        matches = [a for a in candidates if a.get('title') == explicit_title]
        return matches[0] if len(matches) == 1 else None
    for run in runs:
        aid = run.get('interrupt', {}).get('artifact_id')
        if run['status'] == 'waiting' and aid in visible and visible[aid]['type'] == 'scenarios':
            return visible[aid]
    displayed = visible.get(body.get('artifact_id'))
    if displayed and displayed['type'] == 'scenarios':
        return displayed
    if displayed and displayed['type'] == 'cases':
        aid = displayed.get('report', {}).get('lineage', {}).get('scenario_artifact_id')
        if aid in visible and visible[aid]['type'] == 'scenarios':
            return visible[aid]
    if previous:
        aid = previous.get('artifact_id')
        if aid in visible and visible[aid]['type'] == 'scenarios':
            return visible[aid]
    return candidates[0] if len(candidates) == 1 else None


def estimate_selection(artifact, body, decision, previous=None):
    rows = artifact.get('items', [])
    known = {r['id'] for r in rows}
    scope = decision.get('scope', 'all')
    ids = decision.get('scenario_ids', [])
    ordinals = decision.get('scenario_ordinals', [])
    if scope == 'all':
        if ids or ordinals:
            raise DomainError('估算范围同时包含全部和部分场景，请明确后重新发送')
        return None
    if scope == 'selected':
        if body.get('artifact_id') != artifact['id']:
            raise DomainError('当前选中条目不属于待估算场景，请直接提供场景编号')
        ids = body.get('selected_ids') or []
        ordinals = []
    if scope == 'inherit_previous':
        if not previous or previous.get('artifact_id') != artifact['id']:
            raise DomainError('当前场景与上一轮估算不同，请说明本次要估算的场景编号或范围')
        ids = [r['scenario_id'] for r in previous.get('scenarios', [])]
        ordinals = []
    if any(index > len(rows) for index in ordinals) or not set(ids) <= known:
        raise DomainError('所指定场景不在当前成果中，请核对场景编号或序号；尚未估算或生成')
    selected = set(ids) | {rows[index - 1]['id'] for index in ordinals}
    if not selected:
        raise DomainError('请说明要估算哪些场景，例如“只估算第 1、2 个场景”')
    return [r['id'] for r in rows if r['id'] in selected]


def assert_unchanged(store, chat, snapshot, revisions):
    if run_snapshot(store, chat['id']) != snapshot:
        raise DomainError('任务状态已变化，请重新发送估算请求；尚未生成或修改用例', 409)
    for aid, revision in revisions.items():
        current = store.get('artifact', aid)
        if not current.get('_visible') or current.get('chat_id') != chat['id'] or current.get('project_id') != chat['project_id'] or current['revision'] != revision:
            raise DomainError('场景版本已更新，请重新发送估算请求', 409)


def save_reply(store, chat, content, answer, snapshot, revisions, estimate=None):
    with store.transaction():
        assert_unchanged(store, chat, snapshot, revisions)
        stamp = now()
        for role, text in (('user', content), ('assistant', answer)):
            metadata = {'chat_interpret': 'estimate'}
            if role == 'assistant' and estimate is not None:
                metadata['case_estimate'] = estimate
            message = {'id': uid('msg_'), 'project_id': chat['project_id'], 'chat_id': chat['id'],
                       'role': role, 'content': text, 'created_at': stamp, 'metadata': metadata}
            store.put('message', message)
        store.put('chat', {**store.get('chat', chat['id']), 'updated_at': stamp})
    output = {'handled': True, 'kind': 'estimate' if estimate is not None else 'clarification', 'message': public(message)}
    if estimate is not None:
        output['estimate'] = estimate
    return output


async def interpret_chat(store, engine, chat_id, body):
    chat = store.get('chat', chat_id)
    content = body.get('content', '').strip()
    if not content or len(content) > 100000:
        raise DomainError('请填写本次对话内容（最多 100000 字符）')
    body = {**body, 'content': content}
    if body.get('as_requirement'):
        return {'handled': False}
    artifacts = scoped_artifacts(store, chat)
    if body.get('artifact_id') and body['artifact_id'] not in {a['id'] for a in artifacts}:
        raise DomainError('当前成果不属于此对话，请刷新后重新发送', 404)
    with store.transaction():
        snapshot = run_snapshot(store, chat_id)
        runs = store.runs(chat_id=chat_id, statuses=('queued', 'running', 'waiting'))
        context = routing_context(store, chat, body, artifacts, runs)
        previous = latest_estimate(store, chat_id)
        revisions = {a['id']: a['revision'] for a in artifacts if a['type'] == 'scenarios' or a['id'] == body.get('artifact_id')}
    context = fit_routing_context(engine, context)
    decision = validate_decision(await engine.invoke_model('chat_interpret', context, None))
    with store.transaction():
        assert_unchanged(store, chat, snapshot, revisions)
    if decision['action'] == 'normal':
        return {'handled': False, 'intent': decision['intent']}
    if any(r['status'] in ('queued', 'running') for r in runs):
        return save_reply(store, chat, content, '当前任务正在生成。请等到场景确认节点或任务完成后，再直接问我估算用例数量。', snapshot, revisions)
    artifact = resolve_scenario(artifacts, runs, body, decision, previous)
    if artifact is None:
        candidates = [a for a in artifacts if a['type'] == 'scenarios']
        if not candidates:
            answer = '当前对话还没有已保存的场景，暂时无法按场景估算。请先生成场景或打开已有场景，再直接问“这些场景大概需要多少条用例，只估算，不生成”。当前确认节点保留。'
        else:
            choices = '\n'.join(f'- {a.get("title", a["id"])}（{a["id"]}，v{a["revision"]}）' for a in candidates[:12])
            answer = '请告诉我要估算哪一份场景，可以直接回复成果编号，或打开目标场景后再问：\n' + choices
            if len(candidates) > 12:
                answer += '\n还有其他场景成果；打开目标场景后即可估算。'
        return save_reply(store, chat, content, answer, snapshot, revisions)
    if not artifact.get('items'):
        return save_reply(store, chat, content, '这份场景成果目前没有场景条目。请补充场景后再问我估算；当前任务和成果保持不变。', snapshot, revisions)
    selected = estimate_selection(artifact, body, decision, previous)
    # The reusable action lease permits the matching paused scene confirmation.
    # Other paused nodes need an explanatory reply, not a lock or a resume.
    if any(r.get('interrupt', {}).get('type') not in ('strategy_review', 'scenario_review') or
           r.get('interrupt', {}).get('artifact_id') != artifact['id'] for r in runs):
        return save_reply(store, chat, content, '当前任务还在其他确认节点。请先完成当前确认，进入这份场景的确认节点后，再问我估算；这条提问不会提交澄清答案或生成用例。', snapshot, revisions)
    instruction = content
    if previous and previous.get('artifact_id') == artifact['id'] and previous.get('instruction'):
        instruction += '\n最近一次估算请求（仅作对话背景，以本次要求为准）：' + previous['instruction'][:1000]
    result = await preview_action(store, engine, artifact['id'],
                                 {'action': 'estimate', 'instruction': instruction, 'selected_ids': selected})
    titles = {r['id']: r.get('title', r['id']) for r in artifact['items']}
    estimate = {'artifact_id': artifact['id'], 'artifact_revision': artifact['revision'], 'title': artifact.get('title', ''), 'instruction': content,
                **copy.deepcopy(result['estimate'])}
    estimate['scenarios'] = [{**row, 'title': titles[row['scenario_id']]} for row in estimate['scenarios']]
    answer = f'根据「{artifact.get("title", "当前场景")}」v{artifact["revision"]} 的 {len(estimate["scenarios"])} 个场景，预计需要 {estimate["min_count"]}–{estimate["max_count"]} 条测试用例。以下是数量范围、理由和假设；本次只估算，未生成或修改用例。'
    return save_reply(store, chat, content, answer, snapshot, revisions, estimate)


def register_chat_estimate_routes(app):
    from pydantic import BaseModel, Field

    class InterpretInput(BaseModel):
        content: str = Field(min_length=1, max_length=100000)
        artifact_id: str | None = None
        selected_ids: list[str] | None = None
        intent_hint: str = 'auto'
        as_requirement: bool = False

    @app.post('/api/chats/{chat_id}/interpret')
    async def chat_interpret(chat_id: str, body: InterpretInput):
        return await interpret_chat(app.state.store, app.state.engine, chat_id, body.model_dump())
