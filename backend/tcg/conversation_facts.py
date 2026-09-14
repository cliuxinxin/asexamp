"""Conversation-local rule selection and server-owned evidence disclosures."""
import copy

from .schemas import DomainError
from .storage import now


def excluded_source_ids(store, chat_id):
    return set(store.get('chat', chat_id).get('_excluded_project_source_ids', []))


def sources_allowed(store, chat_id, source_ids, strict=False):
    excluded = excluded_source_ids(store, chat_id)
    if strict and excluded.intersection(source_ids):
        raise DomainError('这些资料包含本次对话已禁用的项目规则；请启用对应规则，或根据当前资料重新理解需求。', 409)
    return [sid for sid in source_ids if sid not in excluded]


def ensure_artifact_allowed(store, artifact):
    """A derived artifact cannot reintroduce an excluded rule through its parent."""
    chat = store.get('chat', artifact['chat_id'])
    excluded = set(chat.get('_excluded_project_source_ids', []))
    if artifact.get('report', {}).get('_knowledge_preference_version', 1) != chat.get('_project_knowledge_version', 1):
        raise DomainError('这份成果早于本次项目知识库选择。请根据当前资料重新理解需求后继续；历史成果仍可查看和导出。', 409)
    if not excluded:
        return
    pending, visited = [artifact], set()
    while pending:
        current = pending.pop()
        if current['id'] in visited:
            continue
        visited.add(current['id'])
        dependency_sources = {ref['id'] for key in ('_dependencies', '_write_dependencies')
                              for ref in (current.get(key) or {}).get('sources', [])}
        item_sources = {ref.split('#', 1)[0] for row in current.get('items', []) for ref in row.get('refs', [])}
        if excluded.intersection(set(current.get('_source_ids', [])) | dependency_sources | item_sources):
            raise DomainError('这份成果使用过本次对话已禁用的项目规则。请根据当前资料重新理解需求后继续；历史成果仍可查看和导出。', 409)
        lineage = current.get('report', {}).get('lineage', {})
        parent_ids = [lineage.get(key) for key in ('analysis_artifact_id', 'scenario_artifact_id')]
        for aid in filter(None, parent_ids):
            parent = store.get('artifact', aid)
            if parent['chat_id'] == artifact['chat_id']:
                pending.append(parent)


def source_record(store, source, chat_id, refs=()):
    """Labels come from the captured source, never the model or ID appearance."""
    try:
        origin = store.get('chat', source['chat_id'])
        title = source.get('provenance', {}).get('origin_chat_title') or origin['title']
    except DomainError:
        title = '历史对话'
    role = source.get('role')
    historical = bool(source.get('_project_shared') and source.get('chat_id') != chat_id
                      and role == 'clarification')
    classification = ('project_knowledge' if historical else 'format_sample' if role == 'example'
        else 'chat_supplement' if role in ('clarification', 'change', 'supplement')
        else 'knowledge_document' if role == 'knowledge' else 'current_document' if role == 'primary'
        else 'unknown')
    return {'id': source['id'], 'source_id': source['id'], 'name': source.get('name', ''),
        'text': source.get('_text', '') if role == 'clarification' else '', 'source_version': source.get('version', 1),
        'origin_chat_id': source['chat_id'], 'origin_chat_title': title,
        'origin_created_at': source.get('provenance', {}).get('confirmed_at') or source.get('_shared_at') or source.get('created_at'),
        'classification': classification, 'refs': list(refs)}


def fact_projection(store, source, chat_id=None):
    from .project_facts import fact_record
    result = fact_record(source)
    record = source_record(store, source, chat_id or '')
    result.update({key: record[key] for key in ('source_version', 'origin_chat_id', 'origin_chat_title', 'origin_created_at')})
    result['excluded'] = source['id'] in excluded_source_ids(store, chat_id) if chat_id else False
    result['enabled_in_chat'] = result['active'] and not result['excluded']
    return result


def report_provenance(store, chat_id, report, rows, sources):
    """Capture exact versioned source labels without adding model-owned facts."""
    report = copy.deepcopy(report)
    report['_knowledge_preference_version'] = store.get('chat', chat_id).get('_project_knowledge_version', 1)
    refs = {ref for row in rows for ref in row.get('refs', [])}
    refs.update(report.get('source_coverage', {}).get('processed_evidence_ids', []))
    captured = []
    for reference in sources:
        sid = reference if isinstance(reference, str) else reference['id']
        version = None if isinstance(reference, str) else reference.get('version')
        snapshot = store.source_snapshot(sid, version)
        matched = [chunk['id'] for chunk in snapshot['chunks'] if chunk['id'] in refs]
        if matched:
            captured.append(source_record(store, snapshot['source'], chat_id, matched))
    report['source_provenance'] = [{key: value for key, value in record.items() if key != 'text'} for record in captured]
    report['shared_facts_used'] = [copy.deepcopy(record) for record in captured if record['classification'] == 'project_knowledge']
    return report


def artifact_projection(store, artifact):
    """Legacy artifacts get read-only labels from their immutable evidence manifest."""
    value = copy.deepcopy(artifact)
    report = value.get('report') or {}
    if 'source_provenance' not in report:
        guard = value.get('_write_dependencies') or value.get('_dependencies') or {}
        # No exact historical manifest means provenance is unknown. Never pretend
        # that the current source lifecycle reflects an old artifact's inputs.
        sources = [ref for ref in guard.get('sources', []) if isinstance(ref, dict) and ref.get('version')]
        if sources:
            try:
                value['report'] = report_provenance(store, value['chat_id'], report, value['items'], sources)
                value['report']['_knowledge_preference_version'] = report.get('_knowledge_preference_version', 1)
            except DomainError:
                pass
    return value


def record_context_usage(store, run, evidence):
    """Disclose actual evidence being passed to a model call, not a guessed hit."""
    ids = list(dict.fromkeys(entry['id'].split('#', 1)[0] for entry in evidence if entry.get('id')))
    facts = []
    for sid in ids:
        source = store.get('source', sid)
        if source.get('_project_shared') and source.get('chat_id') != run['chat_id'] and source['role'] == 'clarification':
            facts.append(source_record(store, source, run['chat_id'],
                [entry['id'] for entry in evidence if entry['id'].split('#', 1)[0] == sid]))
    if not facts:
        return
    with store.transaction():
        current = store.run(run['id'])
        if current['status'] not in ('queued', 'running'):
            return
        saved = {(row['source_id'], row['source_version']): row for row in current.get('shared_facts_used', [])}
        for fact in facts:
            key = (fact['source_id'], fact['source_version'])
            previous = saved.get(key)
            if previous:
                fact['refs'] = list(dict.fromkeys(previous['refs'] + fact['refs']))
            saved[key] = fact
        used = list(saved.values())
        store.update_run(run['id'], shared_facts_used=used)
        message_id = 'project-knowledge:' + run['id']
        content = f'已将 {len(used)} 条项目历史规则纳入本次模型上下文。可打开项目知识库查看来源或调整本次使用范围。'
        try:
            created_at = store.get('message', message_id)['created_at']
        except DomainError:
            created_at = now()
        store.put('message', {'id': message_id, 'project_id': run['project_id'], 'chat_id': run['chat_id'],
            'role': 'assistant', 'content': content, 'created_at': created_at, 'metadata': {'run_id': run['id'],
                'turn_response': {'id': message_id, 'status': 'succeeded', 'message': content,
                    'parts': [{'type': 'project_knowledge', 'count': len(used), 'facts': used, 'run_id': run['id']}],
                    'pending': [], 'actions': []}}})


def set_fact_enabled(store, chat_id, source_id, enabled, expected_version):
    from .project_context import shared_context
    with store.transaction():
        chat = store.get('chat', chat_id)
        version = chat.get('_project_knowledge_version', 1)
        if version != expected_version:
            raise DomainError('本次对话的知识库选择已更新，请刷新后再操作。', 409)
        source = store.get('source', source_id)
        if source['project_id'] != chat['project_id'] or source['role'] != 'clarification' or not source.get('_project_shared'):
            raise DomainError('未找到本项目的共享澄清规则。', 404)
        if not source.get('_active') or source.get('status', 'confirmed') != 'confirmed':
            raise DomainError('这条规则已停用或被替代，请刷新项目知识库。', 409)
        runs = store.runs(chat_id=chat_id, statuses=('queued', 'running', 'waiting', 'failed'))
        if any(run['status'] in ('queued', 'running') for run in runs):
            raise DomainError('当前步骤正在生成，请等待本步骤完成或暂停到确认点，再调整本次使用的项目规则。', 409)
        all_runs = store.runs(chat_id=chat_id)
        runs = [run for run in runs if run['status'] == 'waiting' or (all_runs and run['id'] == all_runs[-1]['id'])]
        if any(run['intent'] == 'review_case' and run.get('_request', {}).get('selected_ids') for run in runs):
            raise DomainError('当前是所选用例的局部评审，变更规则需要重新理解需求。请先取消本次局部评审，再调整规则并开始新任务；不会扩大评审范围。', 409)
        excluded = set(chat.get('_excluded_project_source_ids', []))
        changed = (source_id in excluded) == enabled
        if changed:
            excluded.discard(source_id) if enabled else excluded.add(source_id)
            version += 1
            store.put('chat', {**chat, '_excluded_project_source_ids': sorted(excluded), '_project_knowledge_version': version})
            for run in runs:
                enabled_sources = set(run.get('_knowledge_enabled_source_ids', []))
                enabled_sources.add(source_id) if enabled else enabled_sources.discard(source_id)
                store.update_run(run['id'], knowledge_rebuild_required=True,
                    knowledge_preference_version=version, _knowledge_enabled_source_ids=sorted(enabled_sources))
            store.audit(chat_id, 'conversation_fact_selection', {'source_id': source_id, 'enabled': enabled, 'version': version})
        rebuild = bool(runs) and (changed or any(run.get('knowledge_rebuild_required') for run in runs))
        message = ('已在本次对话中启用这条规则。' if enabled else '已在本次对话中禁用这条规则，其他对话仍可使用。')
        if rebuild:
            message += '已保存的成果保留；下次同意继续时，将按当前知识选择重新理解需求，再进入人工确认。'
        else:
            message += '下次生成时按此选择使用资料；已有成果保留历史依据。'
        if changed:
            store.put('message', {'id': f'knowledge-selection:{chat_id}:{version}', 'project_id': chat['project_id'],
                'chat_id': chat_id, 'role': 'assistant', 'content': message, 'created_at': now(),
                'metadata': {'project_knowledge_changed': True}})
        return {**shared_context(store, chat['project_id'], chat_id=chat_id), 'message': message, 'requires_rebuild': rebuild}
