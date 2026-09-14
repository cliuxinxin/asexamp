"""Read-only chat projections derived from native checkpoints and saved artifacts."""
import copy
import hashlib
import json

from .schemas import DomainError


def review_opinions(artifact):
    """Read the latest review belonging to this exact artifact revision."""
    report = artifact.get('report') or {}
    reports = report.get('review_reports')
    review = next((value for value in reversed(reports) if isinstance(value, dict)), {}) if isinstance(reports, list) else report
    issues = []
    for value in review.get('issues', []) if isinstance(review.get('issues'), list) else []:
        if isinstance(value, str):
            issues.append({'title': value})
        elif isinstance(value, dict):
            item = {'title': str(value.get('title') or '评审意见')}
            if isinstance(value.get('detail'), str):
                item['detail'] = value['detail']
            for key in ('case_ids', 'refs'):
                if isinstance(value.get(key), list):
                    item[key] = [entry for entry in value[key] if isinstance(entry, str)]
            issues.append(item)
    for question in review.get('questions', []) if isinstance(review.get('questions'), list) else []:
        if isinstance(question, str) and question.strip():
            issues.append({'title': '待确认问题', 'detail': question})
    exclusions = review.get('excluded_scenarios')
    for exclusion in exclusions if isinstance(exclusions, list) else []:
        if isinstance(exclusion, dict):
            issues.append({'title': '范围排除：' + str(exclusion.get('scenario_id') or '未指定场景'),
                'detail': str(exclusion.get('reason') or '未提供理由'),
                'refs': [ref for ref in exclusion.get('refs', []) if isinstance(ref, str)]
                        if isinstance(exclusion.get('refs'), list) else []})
    result = {'summary': str(review.get('summary') or ''), 'issues': issues}
    if isinstance(review.get('scope'), dict):
        result['scope'] = copy.deepcopy(review['scope'])
    notes = review.get('notes')
    if isinstance(notes, str):
        notes = [notes]
    if isinstance(notes, list):
        result['notes'] = [value for value in notes if isinstance(value, str)]
    return result


def pipeline_messages(store, chat_id):
    """Project saved stage events into the existing UI message envelope."""
    labels = {'understood': '需求理解已保存', 'scenarios_generated': '测试场景已保存',
              'cases_generated': '用例草稿已保存', 'reviewed': '用例评审结果已保存'}
    messages = []
    for event in store.list('pipeline_result', chat_id=chat_id):
        artifact = store.revision(event['artifact_id'], event['revision'])
        report = artifact.get('report') or {}
        content = labels.get(event['phase'], '阶段成果已保存') + f"：{len(artifact['items'])} 条 · v{artifact['revision']}。"
        summary = review_opinions(artifact)['summary'] if event['phase'] == 'reviewed' else report.get('summary')
        if summary:
            content += '\n' + str(summary)[:1200]
        if report.get('clarification_followups'):
            content += '\n另有待核实的补充问题，已保留在成果说明中；它们尚未确认为业务规则。'
        messages.append({'id': event['id'], 'role': 'assistant', 'content': content,
            'created_at': event['created_at'], 'metadata': {'run_id': event['run_id'],
                'stage': event['phase'], 'artifact_ids': [artifact['id']],
                'artifact_revisions': {artifact['id']: artifact['revision']}}})
    return messages


async def current_prompt(store, pipeline, chat):
    template = chat.get('_native_template_prompt')
    if template:
        try:
            profile = store.get('profile', template['profile_id'])
            if profile['version'] == template['expected_version']:
                return copy.deepcopy(template)
        except (DomainError, KeyError):
            pass
    proposal = chat.get('_native_artifact_prompt')
    if proposal:
        try:
            artifact = store.get('artifact', proposal['artifact_id'])
            if artifact['revision'] == proposal['artifact_revision']:
                return copy.deepcopy(proposal)
        except (DomainError, KeyError):
            pass
    runs = store.runs(chat_id=chat['id'])
    current = next((r for r in reversed(runs) if r['status'] in ('queued', 'running', 'waiting')),
                   runs[-1] if runs and runs[-1]['status'] == 'failed' else None)
    if not current:
        return None
    run = await pipeline.snapshot(current['id'])
    if run['status'] in ('queued', 'running'):
        return {'id': 'working:' + run['id'], 'kind': 'busy', 'busy': True, 'run_id': run['id'],
            'title': '正在处理当前步骤', 'message': '完成后会在这里显示结果。你可以继续提问，或要求在这一步完成后暂停。'}
    if run['status'] == 'failed':
        next_step = ' 请查看已有成果，再通过聊天重新开始任务。' if run.get('migration', {}).get('status') == 'restart_required' else ' 已有成果已保留。可以说明修改内容，或回复“重试当前步骤”。'
        return {'id': 'failed:' + run['id'], 'kind': 'failed', 'run_id': run['id'],
            'title': '当前步骤尚未完成', 'message': str(run.get('error') or '当前步骤未完成。')[:500] + next_step}
    gate = run.get('interrupt') or {}
    if not gate:
        return None
    result = {'id': gate.get('prompt_id') or gate.get('id'), 'kind': gate['type'],
        'run_id': run['id'], 'title': gate.get('title', '确认当前内容'),
        'message': gate.get('message', '回复同意继续，或直接说明修改意见。'),
        'artifact_id': gate.get('artifact_id'), 'artifact_revision': gate.get('artifact_revision')}
    if gate['type'] == 'case_result_review' and gate.get('artifact_id'):
        artifact = store.revision(gate['artifact_id'], gate['artifact_revision'])
        result['review'] = review_opinions(artifact)
    if gate['type'] == 'clarification':
        suggestions = {q.get('question'): q for q in gate.get('question_suggestions', [])}
        result['questions'] = []
        for entry in gate.get('questions', []):
            question = entry if isinstance(entry, str) else entry.get('question', '')
            detail = entry if isinstance(entry, dict) else suggestions.get(question, {})
            result['questions'].append({**{key: value for key, value in detail.items() if key != 'answer'},
                'id': detail.get('id') or 'q_' + hashlib.sha256(question.encode()).hexdigest()[:12],
                'question': question, 'suggestion': detail.get('suggestion') or detail.get('answer') or
                    '暂按现有明确需求设计，缺失规则保留待确认。'})
        result['message'] = ('请回答澄清问题，或采用建议答案。提交答案会更新需求理解，不等于确认理解；'
                             '人工模式下，回答完成后会单独请你确认更新后的理解。')
    return result


async def chat_context(store, pipeline, chat, body, prompt):
    artifacts = sorted((a for a in store.list('artifact', chat_id=chat['id']) if a.get('_visible')),
                       key=lambda a: (a.get('created_at', ''), a['id']))
    from .conversation_facts import sources_allowed
    allowed = set(sources_allowed(store, chat['id'], [s['id'] for s in store.list('source', project_id=chat['project_id'])]))
    sources = [s for s in store.list('source', project_id=chat['project_id']) if s.get('_active')
               and s['id'] in allowed and (s.get('chat_id') == chat['id'] or s.get('_project_shared'))]
    runs = [await pipeline.snapshot(r['id']) for r in store.runs(chat_id=chat['id'])[-4:]]
    for run in runs:
        run['shared_facts_used'] = [{key: fact[key] for key in ('source_id', 'source_version', 'name') if key in fact}
            for fact in run.get('shared_facts_used', []) if fact.get('source_id') in allowed]
    return {'project_id': chat['project_id'], 'chat_id': chat['id'], 'current_prompt': prompt,
        'knowledge_selection': {'version': chat.get('_project_knowledge_version', 1),
            'excluded_source_ids': list(chat.get('_excluded_project_source_ids', [])),
            'policy': 'Excluded project facts and earlier assistant answers are not current generation evidence. Explicit historical explanations remain read-only.'},
        'runs': [{k: r.get(k) for k in ('id', 'status', 'mode', 'stage', 'stop_after', 'artifact_ids', 'knowledge_rebuild_required', 'shared_facts_used')} for r in runs],
        'artifacts': [{'id': a['id'], 'type': a['type'], 'title': a['title'], 'revision': a['revision'],
                       'count': len(a['items'])} for a in artifacts[-60:]],
        'sources': [{'id': s['id'], 'name': s['name'], 'role': chat.get('_source_roles', {}).get(s['id'], s['role']),
                     'shared': bool(s.get('_project_shared'))} for s in sources[-60:]],
        'catalog_partial': len(artifacts) > 60 or len(sources) > 60,
        'selection': {k: body[k] for k in ('artifact_id', 'artifact_revision', 'selected_ids', 'view_order') if k in body},
        'requested_settings': {k: body[k] for k in ('mode', 'profile_id', 'depth', 'case_types', 'profile_override',
            'as_requirement', 'intent_hint') if k in body},
        'shortcut': body.get('command'), 'reply_kind': body.get('reply_kind')}


async def workspace_state(store, pipeline, chat_id, artifact_id=None):
    """Keep the existing table navigation API without a second workflow controller."""
    from .workspace_coverage import workspace_context
    chat = store.get('chat', chat_id)
    prompt = await current_prompt(store, pipeline, chat)
    chosen = artifact_id or (prompt or {}).get('artifact_id')
    visible = [a for a in store.list('artifact', chat_id=chat_id) if a.get('_visible') and a['type'] in ('analysis', 'scenarios', 'cases')]
    if not chosen and visible:
        chosen = visible[-1]['id']
    if not chosen:
        return {'stages': [], 'impact': {'status': 'current', 'affected': [], 'source_ids': []}, 'next_action': {'kind': 'chat'}}
    artifact = store.get('artifact', chosen)
    if artifact['chat_id'] != chat_id:
        raise DomainError('成果不属于当前对话', 403)
    context = workspace_context(store, artifact)
    labels = [('analysis','需求理解','analysis_artifact_id'), ('scenarios','测试场景','scenario_artifact_id'),
              ('cases','测试用例','selected_case_artifact_id')]
    stages = []
    stale = context['stale']
    outdated = {'scenarios': stale['analysis'], 'cases': stale['analysis'] or stale['scenarios']}
    linked = {}
    for key, label, field in labels:
        item = store.get('artifact', context[field]) if context.get(field) else None
        linked[key] = item
        stages.append({'key': key, 'label': label, 'artifact_id': item['id'] if item else None,
            'revision': item['revision'] if item else None, 'count': len(item['items']) if item else 0,
            'status': ('stale' if outdated.get(key) else 'current') if item else 'missing'})
    cases = linked['cases']
    reviewed = bool(cases and cases.get('report', {}).get('review_reports'))
    stages.append({'key': 'review', 'label': '评审', 'artifact_id': cases['id'] if reviewed else None,
        'revision': cases['revision'] if reviewed else None, 'count': len(cases['items']) if reviewed else 0,
        'status': ('stale' if outdated['cases'] else 'current') if reviewed else 'missing'})
    affected = []
    changed_scenarios = set(stale['changed_scenario_ids'])
    if stale['analysis'] and linked['scenarios']:
        ids = [s['id'] for s in linked['scenarios']['items']
               if set(s.get('requirement_ids', [])) & set(stale['changed_requirement_ids'])]
        changed_scenarios.update(ids)
        affected.append({'artifact_id': linked['scenarios']['id'], 'type': 'scenarios',
            'item_ids': ids, 'count': len(ids), 'reason': '关联需求已修改，确认理解后将更新关联场景。'})
    if changed_scenarios and cases:
        ids = [c['id'] for c in cases['items'] if c.get('scenario_id') in changed_scenarios]
        affected.append({'artifact_id': cases['id'], 'type': 'cases', 'item_ids': ids,
            'count': len(ids), 'reason': '关联场景待同步，确认场景后将更新相关用例。'})
    source_ids = [s['id'] for s in context['sources']]
    branch_ids = {a['id'] for a in linked.values() if a}
    gate = prompt if prompt and (not prompt.get('artifact_id') or prompt['artifact_id'] in branch_ids) else None
    summary = '上游内容已更新；可以在聊天中继续确认或指定修改范围。' if affected else '成果已保存；确认、修改与继续请在聊天中说明。'
    return {'artifact_id': chosen, 'stages': stages, 'current_gate': gate,
        'impact': {'status': 'pending' if affected or source_ids else 'current', 'summary': summary,
            'affected': affected, 'source_ids': source_ids},
        'next_action': {'kind': 'chat'}, 'context': context}
