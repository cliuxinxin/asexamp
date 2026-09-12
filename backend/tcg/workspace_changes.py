"""One saved-branch view of changes, reconciliation and the current next action.

Freshness describes actual upstream row differences. Revision identity remains
the stricter concurrent-write guard, including report-only edits.
"""
import copy
import json
import re

from .artifact_actions import (changed_requirement_ids, fingerprint, preview_action,
                               assert_waiting_snapshots, resolved_children)
from .dependencies import assert_manifest
from .schemas import DomainError
from .workspace_coverage import (PARENT_KEYS, changed_scenario_ids, lineage,
                                 parent_artifact, workspace_context)


STAGES = (('analysis', '需求理解'), ('scenarios', '测试场景'), ('cases', '测试用例'))


def _artifact(store, chat, artifact_id):
    artifact = store.get('artifact', artifact_id)
    if artifact.get('chat_id') != chat['id'] or artifact.get('project_id') != chat['project_id']:
        raise DomainError('成果不属于当前对话', 404)
    if not artifact.get('_visible') or artifact['type'] not in ('analysis', 'scenarios', 'cases'):
        raise DomainError('请选择已保存的需求理解、场景或用例', 404)
    return artifact


def _ancestors(store, artifact):
    result = [artifact]
    seen = {artifact['id']}
    while artifact['type'] in ('scenarios', 'cases'):
        parent = parent_artifact(store, artifact, 'analysis' if artifact['type'] == 'scenarios' else 'scenarios')
        if not parent or parent['id'] in seen:
            break
        result.append(parent)
        seen.add(parent['id'])
        artifact = parent
    return result


def _current_run(store, chat):
    runs = store.runs(chat_id=chat['id'], statuses=('queued', 'running', 'waiting', 'failed'))
    active = [run for run in runs if run['status'] != 'failed']
    return max(active or runs, key=lambda run: (run.get('created_at', ''), run['id']), default=None)


def _anchor(store, chat, artifact_id, run):
    artifacts = [a for a in store.list('artifact', chat_id=chat['id'])
                 if a.get('_visible') and a.get('project_id') == chat['project_id']
                 and a['type'] in ('analysis', 'scenarios', 'cases')]
    by_id = {a['id']: a for a in artifacts}
    gate = by_id.get((run or {}).get('interrupt', {}).get('artifact_id'))
    if artifact_id:
        chosen = _artifact(store, chat, artifact_id)
        # The current gate supplies the branch when the opened artifact is its
        # ancestor; an explicitly opened sibling branch keeps its own identity.
        if gate and chosen['id'] in {a['id'] for a in _ancestors(store, gate)}:
            return gate
        return chosen
    if gate:
        return gate
    try:
        focus = store.get('conversation_state', 'conversation:' + chat['id']).get('focus') or {}
        if focus.get('artifact_id') in by_id:
            return by_id[focus['artifact_id']]
    except DomainError as exc:
        if exc.status != 404:
            raise
    return max(artifacts, key=lambda a: (a.get('created_at', ''), a['id']), default=None)


def _source_scope_run(store, analysis):
    """Recover the exact creator, not a later run that merely reused this branch."""
    if not analysis:
        return None
    try:
        initial = store.revision(analysis['id'], 1)
    except DomainError as exc:
        if exc.status != 404:
            raise
        return None
    run_id = (initial.get('_write_dependencies') or {}).get('run', {}).get('id')
    if not run_id and getattr(store, 'db', None) is not None:
        # Older artifacts have no embedded run identity, but the immutable
        # creation receipt still binds revision one to its originating run.
        for receipt in store.db.execute(
                'SELECT run_id,payload FROM operation_receipts WHERE artifact_id=? AND run_id IS NOT NULL',
                (analysis['id'],)).fetchall():
            if json.loads(receipt['payload']).get('revision') == 1:
                run_id = receipt['run_id']
                break
    if not run_id:
        return None
    try:
        run = store.run(run_id)
    except DomainError as exc:
        if exc.status != 404:
            raise
        return None
    if any(run.get(key) != analysis.get(key) for key in ('chat_id', 'project_id')):
        return None
    return run


def _new_sources(store, chat, analysis):
    used = set((analysis or {}).get('_source_ids', []))
    roles = chat.get('_source_roles', {})
    origin = _source_scope_run(store, analysis)
    initial_ids = (origin or {}).get('_request', {}).get('source_ids')
    cutoff = origin.get('created_at') if origin and isinstance(initial_ids, list) else None
    chosen = set(initial_ids or []) | set((origin or {}).get('_source_ids', []))
    def excluded_at_start(source):
        available_at = max(source.get('created_at', ''), source.get('_shared_at', ''))
        return bool(cutoff and available_at and available_at <= cutoff and source['id'] not in chosen)
    return [s['id'] for s in store.list('source', project_id=chat['project_id'])
            if s.get('_active') and (s.get('chat_id') == chat['id'] or s.get('_project_shared'))
            and roles.get(s['id'], s.get('role')) != 'example' and s['id'] not in used
            and not excluded_at_start(s)]


def _pending_proposal(store, chat, branch_ids):
    proposals = [p for p in store.list('action_proposal', chat_id=chat['id'])
                 if p.get('project_id') == chat['project_id'] and p.get('artifact_id') in branch_ids
                 and not p.get('_applied') and not p.get('_discarded')]
    for proposal in sorted(proposals, key=lambda p: (p.get('created_at', ''), p['id']), reverse=True):
        try:
            from .conversation_receipts import assert_command_live
            assert_command_live(store, proposal.get('_command_id'))
            if any(store.get('artifact', aid)['revision'] != revision
                   for aid, revision in proposal.get('_input_versions', {}).items()):
                continue
            if any(fingerprint(store.get('source', sid)) != signature
                   for sid, signature in proposal.get('_source_versions', {}).items()):
                continue
            assert_waiting_snapshots(store, proposal.get('_waiting', []))
            if proposal.get('_dependency_manifest'):
                assert_manifest(store, proposal['_dependency_manifest'])
        except DomainError:
            continue
        return {key: copy.deepcopy(proposal[key]) for key in ('id', 'artifact_id', 'changes', 'summary')}
    return None


def _review_status(store, cases):
    if not cases or not cases.get('report', {}).get('review_reports'):
        return 'missing'
    reports = cases['report']['review_reports']
    baseline = cases
    for revision in range(cases['revision'] - 1, 0, -1):
        try:
            previous = store.revision(cases['id'], revision)
        except DomainError:
            return 'stale'  # An absent baseline is not proof of current review.
        if previous.get('report', {}).get('review_reports') != reports:
            break
        baseline = previous
    from .case_fields import MANUAL_FIELDS
    manual = {column['field'] for column in cases.get('_profile', {}).get('excel_columns', [])
              if column.get('value_source') in ('manual', 'default')}
    def design(artifact):
        return {row['id']: {key: value for key, value in row.items()
                if key not in manual and re.sub(r'[\s_-]', '', key).lower() not in MANUAL_FIELDS}
                for row in artifact['items']}
    return 'current' if design(cases) == design(baseline) else 'stale'


def workspace_state(store, chat, artifact_id=None):
    """Return one current branch; a GET never writes or invokes a model."""
    with store.lock:
        chat = store.get('chat', chat['id'])
        run = _current_run(store, chat)
        anchor = _anchor(store, chat, artifact_id, run)
        context = workspace_context(store, anchor) if anchor else {}
        branch = {kind: store.get('artifact', context[key]) if context.get(key) else None
                  for kind, key in (('analysis', 'analysis_artifact_id'),
                                    ('scenarios', 'scenario_artifact_id'),
                                    ('cases', 'selected_case_artifact_id'))}
        analysis, scenarios, cases = (branch[k] for k in ('analysis', 'scenarios', 'cases'))
        requirement_drift = changed_requirement_ids(store, analysis, scenarios) if analysis and scenarios else []
        scenario_drift = changed_scenario_ids(store, scenarios, cases) if scenarios and cases else []
        source_ids = _new_sources(store, chat, analysis)
        affected = []
        scenario_ids = set(scenario_drift)
        if requirement_drift and scenarios:
            impacted = [r['id'] for r in scenarios['items']
                        if set(r.get('requirement_ids', [])) & set(requirement_drift)]
            affected.append({'artifact_id': scenarios['id'], 'type': 'scenarios', 'item_ids': impacted,
                             'count': len(impacted), 'upstream_count': len(requirement_drift),
                             'reason': '需求条目已变化，场景待同步' if impacted else
                                 f'有 {len(requirement_drift)} 条需求变化待同步，可能需要新增场景',
                             'upstream_item_ids': requirement_drift})
            scenario_ids.update(impacted)
        if (scenario_ids or requirement_drift) and cases:
            impacted = [r['id'] for r in cases['items'] if r.get('scenario_id') in scenario_ids]
            affected.append({'artifact_id': cases['id'], 'type': 'cases', 'item_ids': impacted,
                             'count': len(impacted), 'upstream_count': len(scenario_ids),
                             'reason': '场景已变化，用例待同步' if impacted else '上游变化可能需要新增用例覆盖',
                             'upstream_item_ids': sorted(scenario_ids)})
        stages = [{'key': kind, 'label': label, 'artifact_id': branch[kind]['id'] if branch[kind] else None,
                   'revision': branch[kind]['revision'] if branch[kind] else None,
                   'count': len(branch[kind]['items']) if branch[kind] else 0,
                   'status': 'missing' if not branch[kind] else
                       'stale' if kind == 'scenarios' and requirement_drift or kind == 'cases' and (scenario_ids or requirement_drift) else 'current'}
                  for kind, label in STAGES]
        review_status = _review_status(store, cases)
        reviewed = review_status != 'missing'
        stages.append({'key': 'review', 'label': '用例评审', 'artifact_id': cases['id'] if cases and reviewed else None,
                       'revision': cases['revision'] if cases and reviewed else None,
                       'count': len(cases['items']) if reviewed else 0,
                       'status': 'stale' if reviewed and (scenario_ids or requirement_drift) else review_status})
        branch_ids = {a['id'] for a in branch.values() if a}
        pending = _pending_proposal(store, chat, branch_ids)
        interrupt = (run or {}).get('interrupt') or {}
        belongs = not interrupt.get('artifact_id') or interrupt.get('artifact_id') in branch_ids
        gate = {'run_id': run['id'], 'interrupt_id': run.get('_interrupt_id'), 'type': interrupt.get('type'),
                'artifact_id': interrupt.get('artifact_id'), 'revision': interrupt.get('artifact_revision'),
                'control_version': run.get('control_version', 0)} if run and run['status'] == 'waiting' and belongs else None
        if gate and gate['artifact_id']:
            gate['revision'] = store.get('artifact', gate['artifact_id'])['revision']
        next_action = {'kind': 'none', 'label': '当前成果已就绪', 'arguments': {}}
        if run and run['status'] in ('queued', 'running'):
            next_action['label'] = '正在完成当前步骤'
        elif run and run['status'] == 'failed':
            next_action['label'] = '当前任务需要处理失败原因'
        elif gate and gate['type'] in ('clarification', 'source_review', 'input_required', 'source_roles'):
            next_action['label'] = '请先完成当前资料或澄清确认'
        elif pending:
            next_action = {'kind': 'apply', 'label': '应用更新',
                           'arguments': {'artifact_id': pending['artifact_id'], 'proposal_id': pending['id']}}
        elif affected or source_ids and analysis:
            start = analysis if source_ids or requirement_drift else scenarios
            descendants = [a['id'] for kind in ('scenarios', 'cases') if (a := branch[kind]) and a['id'] != start['id']]
            next_action = {'kind': 'reconcile', 'label': '预览更新受影响内容', 'arguments': {
                'artifact_id': start['id'], 'expected_revision': start['revision'],
                'related_artifact_ids': descendants, **({'source_ids': source_ids} if source_ids else {})}}
        elif gate:
            next_action = {'kind': 'confirm', 'label': interrupt.get('confirm_label') or '确认当前结果并继续',
                           'arguments': {'run_id': run['id'], 'interrupt_id': run.get('_interrupt_id'),
                               'expected_control_version': run.get('control_version', 0), 'approved': True,
                               **({'expected_revision': gate['revision']} if gate['revision'] else {})}}
        elif not anchor and not source_ids:
            next_action = {'kind': 'none', 'label': '添加需求资料后开始', 'arguments': {}}
        elif not analysis or not scenarios or not cases:
            parent = scenarios or analysis
            intent = 'generate_case' if scenarios else 'generate_scenario' if analysis else 'review_requirement'
            label = '生成测试用例' if scenarios else '生成测试场景' if analysis else '理解需求'
            next_action = {'kind': 'generate', 'label': label, 'arguments': {
                'intent': intent, 'content': label, 'as_requirement': False,
                **({'artifact_id': parent['id']} if parent else {})}}
        elif review_status != 'current':
            next_action = {'kind': 'generate', 'label': '评审当前用例', 'arguments': {
                'intent': 'review_case', 'artifact_id': cases['id'], 'content': '评审当前用例', 'as_requirement': False}}
        summary = '当前需求、场景和用例与各自依据一致。'
        if not anchor:
            summary = '先添加需求资料，或在聊天中描述业务。'
        elif not scenarios or not cases:
            summary = '已有成果已保存，可以继续完成后续测试设计。'
        if affected:
            summary = '；'.join('已保存的修改影响' + ('场景' if a['type'] == 'scenarios' else '用例') + str(a['count']) + ' 条'
                               if a['count'] else a['reason'] for a in affected) + '；可统一预览更新。'
        if source_ids:
            summary = f'有 {len(source_ids)} 份新增业务资料待纳入需求理解。' + (summary if affected else '')
        return {'artifact_id': anchor['id'] if anchor else None, 'stages': stages,
                'impact': {'status': 'pending' if affected or source_ids else 'current', 'summary': summary,
                           'affected': affected, 'source_ids': source_ids},
                'next_action': next_action, 'current_gate': gate, 'pending_proposal': pending,
                'review_note': '用例设计已修改，已有评审需要重新检查。' if review_status == 'stale' else ''}


async def prepare_reconciliation(store, engine, chat, args, command_id=None):
    """Preview current saved drift; never replay the original edit instruction."""
    if args.get('selected_ids'):
        raise DomainError('本入口预览当前分支的受影响内容；如只同步所选条目，请使用所选条目的联动预览。')
    state = workspace_state(store, chat, args.get('artifact_id'))
    if args.get('artifact_id') and args.get('expected_revision') is not None:
        if _artifact(store, chat, args['artifact_id'])['revision'] != args['expected_revision']:
            raise DomainError('成果已更新，请刷新后重新预览', 409)
    explicit = args.get('related_artifact_ids')
    scoped = None
    if explicit is not None:
        if not isinstance(explicit, list) or any(not isinstance(aid, str) for aid in explicit):
            raise DomainError('请选择有效的关联成果')
        origin = _artifact(store, chat, args.get('artifact_id') or state['artifact_id'])
        if origin['type'] == 'cases':
            origin = parent_artifact(store, origin, 'scenarios')
            if not origin:
                raise DomainError('当前用例缺少可验证的场景关联')
        scoped = resolved_children(store, origin, explicit) if explicit else []
        if any(sum(a['type'] == kind for a in scoped) > 1 for kind in ('scenarios', 'cases')):
            raise DomainError('请选择同一分支的一套场景和用例进行统一更新')
        if scoped:
            deepest = next((a for a in scoped if a['type'] == 'cases'), scoped[0])
            state = workspace_state(store, chat, deepest['id'])
        pending = state.get('pending_proposal')
        allowed = {a['id'] for a in scoped} | {a['id'] for a in _ancestors(store, origin)}
        if pending and not {c['artifact_id'] for c in pending['changes']} <= allowed:
            state['pending_proposal'] = None
    pending = state.get('pending_proposal')
    if pending and args.get('source_ids'):
        saved = store.get('action_proposal', pending['id'])
        if not set(args['source_ids']) <= set(saved.get('_source_ids', [])):
            state['pending_proposal'] = None
    branch = {stage['key']: stage['artifact_id'] for stage in state['stages'][:3]}
    related_ids = [a['id'] for a in scoped] if scoped is not None else [
        branch[kind] for kind in ('scenarios', 'cases') if branch[kind]]
    if state.get('pending_proposal'):
        proposal = state['pending_proposal']
    else:
        sources = args.get('source_ids') or state['impact']['source_ids']
        if sources:
            from .conversation_project import preview_from_sources
            if not branch['analysis']:
                raise DomainError('请先理解需求，再纳入资料更新已有成果', 409)
            analysis = _artifact(store, chat, branch['analysis'])
            proposal = await preview_from_sources(store, engine, chat, {**args, 'source_ids': sources,
                'artifact_id': analysis['id'], 'expected_revision': analysis['revision'],
                'sync_targets': ['analysis', 'scenarios', 'cases'], 'reconcile_all_drift': True,
                'related_artifact_ids': related_ids,
                '_command_id': command_id}, command_id)
            if proposal.get('status') == 'needs_input':
                return proposal
        elif not state['impact']['affected']:
            return {'status': 'succeeded', 'message': '当前关联成果已与保存版本一致，无需更新。', 'parts': []}
        else:
            action = state['next_action']
            if action['kind'] not in ('reconcile', 'apply'):
                raise DomainError('请先完成当前步骤或确认，再预览更新', 409)
            start_id = branch['analysis'] if any(a['type'] == 'scenarios' for a in state['impact']['affected']) else branch['scenarios']
            start = _artifact(store, chat, start_id)
            targets = [aid for aid in related_ids if aid != start_id]
            if not targets:
                raise DomainError('请明确选择需要同步的关联成果')
            targets = [a['id'] for a in resolved_children(store, start, targets)]
            request = {'artifact_id': start_id, 'expected_revision': start['revision'],
                       'related_artifact_ids': targets, 'action': 'sync', 'reconcile_all_drift': True,
                       '_command_id': command_id,
                       'instruction': '依据当前已保存的上游内容同步受影响条目；保留其他条目及人工数据。'}
            proposal = await preview_action(store, engine, request['artifact_id'], request)
    result = {'status': 'needs_confirmation', 'message': proposal['summary'],
              'parts': [{'type': 'diff', 'artifact_id': proposal['artifact_id'],
                         'proposal_id': proposal['id'], 'changes': proposal['changes']}],
              'pending': [{'type': 'artifact_proposal', 'id': proposal['id'], 'proposal_id': proposal['id'],
                           'artifact_id': proposal['artifact_id'], 'label': '应用更新',
                           'actions': ['artifact.apply', 'artifact.discard']}]}
    if proposal.get('source_impact'):
        result['parts'].append({'type': 'source_impact', 'data': proposal['source_impact']})
    if command_id:
        from .conversation_receipts import assert_command_live, commit_result
        with store.transaction():
            assert_command_live(store, command_id)
            return commit_result(store, command_id, result)
    return result


def assert_current_inputs(store, run):
    """Check only the input consumed by this gate/boundary, never descendants."""
    pending = run.get('interrupt') or {}
    if pending.get('type') in ('clarification', 'source_review'):
        return
    artifact_id = pending.get('artifact_id')
    if not artifact_id:
        node = pending.get('node')
        kind = 'scenarios' if node in ('cases', 'scenario_gate') else 'cases' if node in (
            'review', 'case_draft_gate', 'review_result_gate') else 'analysis'
        keys = PARENT_KEYS.get(kind, ('v4:cases_artifact', 'cases_artifact'))
        for key in keys:
            cached = store.cache_get(run['id'], key)
            if isinstance(cached, dict) and cached.get('id'):
                artifact_id = cached['id']
                break
        snapshot = run.get('_artifact_snapshot') or {}
        if not artifact_id and snapshot.get('type') == kind:
            artifact_id = snapshot.get('id')
    if not artifact_id:
        return
    artifact = store.get('artifact', artifact_id)
    chain = _ancestors(store, artifact)
    if pending.get('type') in ('strategy_review', 'scenario_review', 'case_draft_review', 'case_result_review', 'workflow_paused'):
        analysis = next((a for a in chain if a['type'] == 'analysis'), None)
        if analysis and _new_sources(store, store.get('chat', run['chat_id']), analysis):
            raise DomainError('有新增业务资料尚未纳入需求理解，请先预览并应用资料更新，再确认继续。', 409)
    for child, parent in zip(chain, chain[1:]):
        changed = changed_scenario_ids(store, parent, child) if child['type'] == 'cases' else changed_requirement_ids(store, parent, child)
        if changed:
            raise DomainError('当前成果的上游条目已变化，请先同步受影响内容，再确认继续。', 409)
