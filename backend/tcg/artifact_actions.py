"""Explicit artifact actions: preview first, then atomically apply versioned changes."""
import copy
import hashlib
import json
from contextlib import contextmanager

from .schemas import DomainError, apply_operations, validate_items
from .storage import now, public, uid
from .case_fields import protect_non_ai_fields


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def item_diff(before, after):
    old, new = {r['id']: r for r in before}, {r['id']: r for r in after}
    return {'added': [i for i in new if i not in old],
            'updated': [i for i in new if i in old and new[i] != old[i]],
            'deleted': [i for i in old if i not in new]}


def selected_rows(artifact, selected=None):
    ids = [r['id'] for r in artifact['items']]
    if selected is not None:
        if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or not set(selected) <= set(ids):
            raise DomainError('请选择当前成果中的有效条目')
        ids = selected
    return [r for r in artifact['items'] if r['id'] in ids]


def validate_estimate(value, scenarios):
    rows = value.get('scenarios')
    expected = {r['id'] for r in scenarios}
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise DomainError('估算必须逐一覆盖所选场景')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('scenario_id'), str) or row['scenario_id'] not in expected or row['scenario_id'] in seen:
            raise DomainError('估算包含未知或重复的场景')
        seen.add(row['scenario_id'])
        low, high = row.get('min_count'), row.get('max_count')
        if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 100000:
            raise DomainError('用例估算必须是有效的非负整数范围')
        if not isinstance(row.get('rationale'), str) or not row['rationale'].strip():
            raise DomainError('每个场景估算需要说明依据')
        if not isinstance(row.get('assumptions'), list) or not all(isinstance(v, str) for v in row['assumptions']):
            raise DomainError('估算假设必须是文本数组')
        if set(row) - {'scenario_id', 'min_count', 'max_count', 'rationale', 'assumptions'}:
            raise DomainError('估算仅返回数量、依据及假设，不生成用例')
    return rows


def checked_operations(artifact, result, allowed_ids, evidence, allowed_scenarios=None, allow_add=False, supplied_evidence=None):
    operations = result.get('operations')
    if not isinstance(operations, list):
        raise DomainError('修改预览必须返回 operations')
    before = {r['id']: r for r in artifact['items']}
    seen = set()
    for op in operations:
        if not isinstance(op, dict):
            raise DomainError('修改操作必须为对象')
        target = (op.get('item') or {}).get('id') if op.get('op') == 'add' and isinstance(op.get('item'), dict) else op.get('id')
        if not isinstance(target, str) or not target:
            raise DomainError('每个修改操作必须包含有效条目 ID')
        if target in seen:
            raise DomainError('同一条目只能包含一个修改操作')
        seen.add(target)
        if isinstance(op.get('item'), dict) and any(k.startswith('_') for k in op['item']):
            raise DomainError('AI 不能修改内部字段')
        if op.get('op') == 'delete':
            if not isinstance(op.get('reason'), str) or not op['reason'].strip():
                raise DomainError('删除条目需要给出具体理由')
            refs = op.get('refs')
            if not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in evidence or evidence[r]['role'] == 'example' for r in refs):
                raise DomainError('删除条目需要有效需求依据')
        if allowed_scenarios is not None and op.get('op') in ('add', 'update'):
            row = {**before.get(target, {}), **(op.get('item') or {})}
            if row.get('scenario_id') not in allowed_scenarios:
                raise DomainError('联动修改超出所选场景范围')
        if op.get('op') == 'add' and artifact['type'] != 'cases' and allowed_ids is not None and not allow_add:
            raise DomainError('选中条目微调不增加其他条目；如需增加请使用全部成果范围')
        if op.get('op') == 'add' and artifact['type'] == 'cases' and allowed_scenarios is None and allowed_ids is not None:
            linked = {before[i].get('scenario_id') for i in allowed_ids}
            if (op.get('item') or {}).get('scenario_id') not in linked:
                raise DomainError('新增用例必须属于所选用例的场景')
    items = apply_operations(artifact['items'], operations, allowed_ids)
    if artifact['type'] == 'cases':
        # Protect execution data only on changed/new rows, leaving all other rows byte-for-byte intact.
        touched = set(item_diff(artifact['items'], items)['updated'] + item_diff(artifact['items'], items)['added'])
        protected = protect_non_ai_fields([r for r in items if r['id'] in touched], artifact.get('_profile', {}), artifact['items'])
        by_id = {r['id']: r for r in protected}
        items = [by_id.get(r['id'], r) for r in items]
    validate_items(artifact['type'], items, evidence)
    if supplied_evidence is not None:
        touched = set(item_diff(artifact['items'], items)['updated'] + item_diff(artifact['items'], items)['added'])
        validate_items(artifact['type'], [r for r in items if r['id'] in touched], supplied_evidence)
        for op in operations:
            if op.get('op') == 'delete' and any(ref not in supplied_evidence for ref in op['refs']):
                raise DomainError('删除引用必须来自当前批次提供的资料')
    return items


def report_patch(artifact, result):
    report = copy.deepcopy(artifact.get('report', {}))
    patch = result.get('report_patch')
    if patch is not None and patch != {}:
        if artifact['type'] != 'analysis' or not isinstance(patch, dict):
            raise DomainError('仅需求理解成果支持报告修改')
        allowed = {'summary', 'diagrams', 'questions', 'question_suggestions', 'assumptions',
                   'in_scope', 'out_of_scope', 'requirement_map', 'strategy', 'limitations'}
        if set(patch) - allowed:
            raise DomainError('报告修改不能覆盖关联关系或执行记录')
        if 'diagrams' in patch and (not isinstance(patch['diagrams'], list) or any(
                not isinstance(d, dict) or not isinstance(d.get('mermaid'), str) for d in patch['diagrams'])):
            raise DomainError('业务图必须包含 Mermaid 文本')
        report.update(patch)
    return report


def active_guard(store, artifacts):
    """Reject generation writers and permit only the matching paused confirmation."""
    scopes = {a['chat_id'] for a in artifacts}
    ids = {a['id'] for a in artifacts}
    waiting = []
    for chat_id in scopes:
        for run in store.runs(chat_id=chat_id, statuses=('queued', 'running', 'waiting')):
            if run['status'] != 'waiting':
                raise DomainError('当前资料正在生成，请完成或停止任务后再操作成果', 409)
            pending = run.get('interrupt', {})
            if pending.get('type') not in ('strategy_review', 'scenario_review') or pending.get('artifact_id') not in ids:
                raise DomainError('请先完成当前确认，再操作其他成果', 409)
            if run.get('_edit_token'):
                raise DomainError('当前成果正在修改，请稍候', 409)
            waiting.append(run)
    return waiting


@contextmanager
def action_lease(store, artifacts):
    chat_id = artifacts[0]['chat_id']
    token = uid('actlock_')
    with store.transaction():
        tokens = getattr(store, '_workspace_action_tokens', None)
        if tokens is None:
            tokens = store._workspace_action_tokens = {}
        if chat_id in tokens:
            raise DomainError('项目成果操作正在进行，请稍候', 409)
        waiting = active_guard(store, artifacts)
        tokens[chat_id] = token
        for run in waiting:
            run['_edit_token'] = token
            store.save_run(run)
        snapshots = [{'id': r['id'], 'interrupt_id': r.get('_interrupt_id'),
                      'artifact_id': r.get('interrupt', {}).get('artifact_id')} for r in waiting]
    try:
        yield snapshots
    finally:
        with store.transaction():
            for snapshot in snapshots:
                run = store.run(snapshot['id'])
                if run.get('_edit_token') == token:
                    run['_edit_token'] = None
                    store.save_run(run)
            if tokens.get(chat_id) == token:
                tokens.pop(chat_id, None)


def assert_waiting_snapshots(store, snapshots):
    for value in snapshots:
        current = store.run(value['id'])
        if current['status'] != 'waiting' or current.get('_interrupt_id') != value['interrupt_id'] or current.get('interrupt', {}).get('artifact_id') != value['artifact_id']:
            raise DomainError('确认节点已继续或取消，请重新预览修改', 409)


def visible_artifact(store, artifact_id):
    artifact = store.get('artifact', artifact_id)
    if not artifact.get('_visible') or artifact['type'] not in ('analysis', 'scenarios', 'cases'):
        raise DomainError('请选择已保存的需求理解、场景或用例成果', 404)
    return artifact


def evidence_for(store, artifacts, supplied_ids=None):
    ids = list(dict.fromkeys(sid for a in artifacts for sid in a.get('_source_ids', [])))
    roles = {sid: role for a in artifacts for sid, role in a.get('_source_roles', {}).items()}
    for sid in supplied_ids or []:
        source = store.get('source', sid)
        if source['project_id'] != artifacts[0]['project_id'] or not source.get('_active'):
            raise DomainError('补充资料必须属于当前项目且仍有效')
        if source['role'] == 'example':
            raise DomainError('格式示例不能作为本次修改的业务依据')
        if sid not in ids:
            ids.append(sid)
        roles[sid] = source['role']
    evidence = store.evidence(ids, roles)
    return ids, roles, [e for e in evidence if e['role'] != 'example']


def source_versions(store, ids):
    return {sid: fingerprint(store.get('source', sid)) for sid in ids}


def bounded_groups(engine, task, rows, build):
    groups, group = [], []
    for row in rows:
        candidate = group + [row]
        context = build(candidate)
        fits = len(json.dumps(context, ensure_ascii=False)) <= 490000 and engine.fits(task, context)
        if group and not fits:
            groups.append(group)
            group = [row]
        else:
            group = candidate
        context = build(group)
        if len(json.dumps(context, ensure_ascii=False)) > 490000 or not engine.fits(task, context):
            raise DomainError('单个所选条目及其依据超过模型容量，请缩小补充资料或调整模型容量；尚未应用修改')
    if group:
        groups.append(group)
    return groups


def local_evidence(evidence, rows, explicit_ids=()):
    refs = {ref for row in rows for ref in row.get('refs', [])}
    return [e for e in evidence if e['id'] in refs or e['source_id'] in explicit_ids or e['role'] in ('change', 'clarification')]


def proposal_change(artifact, items, report=None, operations=None, instruction=''):
    value = {'artifact_id': artifact['id'], 'title': artifact['title'],
             'expected_revision': artifact['revision'], 'items': items,
             'diff': item_diff(artifact['items'], items)}
    if report is not None:
        value['report'] = copy.deepcopy(report)
    if operations is not None:
        value['operations'] = copy.deepcopy(operations)
        previous = {r['id']: r for r in artifact['items']}
        saved_report = value.setdefault('report', copy.deepcopy(artifact.get('report', {})))
        deletions = copy.deepcopy(saved_report.get('item_deletions', {}))
        for op in operations:
            if op.get('op') == 'delete':
                deletions[op['id']] = {'id': op['id'], 'reason': op['reason'], 'refs': op['refs'],
                    'original_item': previous[op['id']], 'revision': artifact['revision'] + 1}
            elif op.get('op') == 'add':
                deletions.pop(op['item']['id'], None)
        if deletions:
            saved_report['item_deletions'] = deletions
        else:
            saved_report.pop('item_deletions', None)
        saved_report['last_action'] = {'instruction': instruction, 'operations': copy.deepcopy(operations), 'at': now()}
    return value


def scenario_baselines(store, scenario_artifact, case_artifact, ids):
    source = (case_artifact.get('report') or {}).get('lineage', {})
    per_item = source.get('scenario_revisions', {})
    found, revisions = [], {}
    if source.get('scenario_artifact_id') != scenario_artifact['id']:
        return []
    for sid in ids:
        version = per_item.get(sid, source.get('scenario_revision'))
        if not isinstance(version, int):
            continue
        if version not in revisions:
            revisions[version] = store.revision(scenario_artifact['id'], version)
        row = next((r for r in revisions[version]['items'] if r['id'] == sid), None)
        if row:
            found.append(row)
    return found


async def preview_action(store, engine, artifact_id, body):
    artifact = visible_artifact(store, artifact_id)
    action = body.get('action')
    if action not in ('estimate', 'explain', 'modify', 'sync'):
        raise DomainError('不支持的成果操作')
    instruction = body.get('instruction', '').strip()
    if not instruction:
        raise DomainError('请填写本次操作要求')
    chosen = selected_rows(artifact, body.get('selected_ids'))
    artifacts = [artifact]
    if action == 'estimate' and artifact['type'] != 'scenarios':
        raise DomainError('请打开场景成果后估算用例数量')
    if action == 'sync':
        if artifact['type'] != 'scenarios':
            raise DomainError('联动更新需要从场景成果发起')
        from .workspace_coverage import resolve_related_case_artifacts
        artifacts += resolve_related_case_artifacts(store, artifact, body.get('related_artifact_ids'))
        if len(artifacts) > 2 and body.get('related_artifact_ids') is None:
            raise DomainError('此场景有多套关联用例，请明确选择本次同步的用例成果')
        if len(artifacts) == 1:
            raise DomainError('没有关联用例，请先选择需要联动的用例成果')
    source_ids, roles, evidence = evidence_for(store, artifacts, body.get('source_ids')) if action != 'estimate' else ([], {}, [])
    with action_lease(store, artifacts) as waiting:
        versions = {a['id']: a['revision'] for a in artifacts}
        sources = source_versions(store, source_ids)
        changes, summaries = [], []
        output = {'id': uid('action_'), 'artifact_id': artifact_id, 'action': action,
                  'project_id': artifact['project_id'], 'chat_id': artifact['chat_id'],
                  'created_at': now(), 'changes': changes}
        profile = artifact.get('_profile', {})
        if action == 'estimate':
            def build(rows):
                return {'instruction': instruction, 'scenarios': rows,
                        'profile': {k: profile[k] for k in ('case_level', 'scenario_level', 'case_types', 'language', 'scope') if k in profile},
                        'contract': '仅估算设计工作量；不生成测试用例、步骤或需求事实。明确不确定性，逐场景给出范围。'}
            estimates = []
            for group in bounded_groups(engine, 'artifact_estimate', chosen, build):
                estimates += validate_estimate(await engine.invoke_model('artifact_estimate', build(group), None), group)
            output['estimate'] = {'scenarios': estimates, 'min_count': sum(r['min_count'] for r in estimates),
                                  'max_count': sum(r['max_count'] for r in estimates)}
            summaries.append(f'已估算 {len(estimates)} 个场景，建议设计 {output["estimate"]["min_count"]}–{output["estimate"]["max_count"]} 条用例；未生成用例。')
        elif action == 'explain':
            def build(rows):
                return {'instruction': instruction, 'artifact': {**public(artifact), 'items': rows},
                        'evidence': local_evidence(evidence, rows, body.get('source_ids') or []),
                        'contract': '解释当前提供的成果，保留原文事实；不修改或生成用例，不声称已经执行测试。'}
            answers, refs = [], []
            for group in bounded_groups(engine, 'artifact_explain', chosen, build):
                context = build(group)
                value = await engine.invoke_model('artifact_explain', context, None)
                supplied = {e['id'] for e in context['evidence']}
                if not isinstance(value.get('answer'), str) or not value['answer'].strip() or not isinstance(value.get('refs', []), list) or not all(isinstance(r, str) for r in value.get('refs', [])) or not set(value.get('refs', [])) <= supplied:
                    raise DomainError('解释内容或引用格式无效，请重试')
                answers.append(value['answer']); refs += value.get('refs', [])
            output.update(answer='\n\n'.join(answers), refs=list(dict.fromkeys(refs)))
            summaries.append('已根据选定版本解释成果，原成果保持不变。')
        elif action == 'modify':
            current = copy.deepcopy(artifact)
            operations = []
            def build(rows):
                return {'instruction': instruction, 'artifact': {**public(artifact), 'items': rows},
                        'selected_ids': [r['id'] for r in rows], 'profile': profile,
                        'evidence': local_evidence(evidence, rows, body.get('source_ids') or []),
                        'contract': '仅局部修改提供的条目，保留 ID、自定义字段及人工数据。删除需要 reason 和 refs；不得顺带修改其他成果。'}
            for group in bounded_groups(engine, 'artifact_modify', chosen, build):
                context = build(group)
                result = await engine.invoke_model('artifact_modify', context, None)
                allowed = [r['id'] for r in group]
                current['items'] = checked_operations(current, result, allowed, {e['id']: e for e in evidence},
                    allow_add=body.get('selected_ids') is None, supplied_evidence={e['id']: e for e in context['evidence']})
                current['report'] = report_patch(current, result)
                operations.extend(result['operations'])
                summaries.append(str(result.get('summary', '已生成局部修改预览')))
            changes.append(proposal_change(artifact, current['items'], current.get('report'), operations, instruction))
        else:
            from .workspace_coverage import changed_scenario_ids, synced_case_report
            current_scenarios = {r['id']: r for r in artifact['items']}
            for case_artifact in artifacts[1:]:
                affected = set(body['selected_ids']) if body.get('selected_ids') is not None else set(changed_scenario_ids(store, artifact, case_artifact))
                if not affected:
                    continue
                current = copy.deepcopy(case_artifact)
                operations = []
                baselines = scenario_baselines(store, artifact, case_artifact, affected)
                deletions = artifact.get('report', {}).get('item_deletions', {})
                units = [{'scenario_id': sid, 'scenario': current_scenarios.get(sid),
                          'cases': [r for r in current['items'] if r.get('scenario_id') == sid]} for sid in sorted(affected)]
                def build(group):
                    rows = [r for unit in group for r in unit['cases']]
                    scenarios = [u['scenario'] for u in group if u['scenario']]
                    own_ids = {u['scenario_id'] for u in group}
                    previous_scenarios = [r for r in baselines if r['id'] in own_ids]
                    deletion_basis = [deletions[sid] for sid in own_ids if sid in deletions]
                    return {'instruction': instruction, 'artifact': {**public(case_artifact), 'items': rows},
                            'scenarios': scenarios, 'removed_scenario_ids': [u['scenario_id'] for u in group if not u['scenario']],
                            'baseline_scenarios': previous_scenarios, 'deletion_basis': deletion_basis,
                            'selected_ids': [r['id'] for r in rows], 'profile': case_artifact.get('_profile', {}),
                            'evidence': local_evidence(evidence, rows + scenarios + previous_scenarios + deletion_basis, body.get('source_ids') or []),
                            'contract': '仅同步这些场景关联的用例；保留有效稳定 ID 和人工/自定义数据。新增覆盖缺失，删除有依据的失效用例；删除必须返回 reason 和 refs。'}
                for group in bounded_groups(engine, 'artifact_sync', units, build):
                    context = build(group)
                    result = await engine.invoke_model('artifact_sync', context, None)
                    allowed = context['selected_ids']
                    current['items'] = checked_operations(current, result, allowed, {e['id']: e for e in evidence},
                        {u['scenario_id'] for u in group if u['scenario']}, supplied_evidence={e['id']: e for e in context['evidence']})
                    removed = set(context['removed_scenario_ids'])
                    if any(r.get('scenario_id') in removed for r in current['items']):
                        raise DomainError('已删除场景仍有关联用例，请修改同步要求后重试')
                    operations.extend(result['operations'])
                    summaries.append(str(result.get('summary', '已生成关联用例修改预览')))
                covered = {r.get('scenario_id') for r in current['items']}
                if (affected & set(current_scenarios)) - covered:
                    raise DomainError('同步后仍有选中场景没有用例；请补齐覆盖或明确移除场景后重试')
                report = synced_case_report(store, artifact, current, sorted(affected))
                changes.append(proposal_change(case_artifact, current['items'], report, operations, instruction))
        output['summary'] = '\n'.join(summaries) or '关联用例已与当前场景版本一致，无需修改。'
        with store.transaction():
            assert_waiting_snapshots(store, waiting)
            for aid, revision in versions.items():
                if store.get('artifact', aid)['revision'] != revision:
                    raise DomainError('成果已更新，请重新生成预览', 409)
            saved = {**copy.deepcopy(output), '_input_versions': versions, '_waiting': waiting,
                     '_source_versions': sources, '_source_ids': source_ids, '_source_roles': roles}
            store.put('action_proposal', saved)
        return output


def apply_action(store, artifact_id, proposal_id):
    with store.transaction():
        proposal = store.get('action_proposal', proposal_id)
        if proposal['artifact_id'] != artifact_id or proposal['action'] not in ('modify', 'sync'):
            raise DomainError('此预览不能应用到当前成果')
        if proposal.get('_applied'):
            return {'artifacts': [public(store.get('artifact', c['artifact_id'])) for c in proposal['changes']],
                    'summary': proposal['summary'], 'already_applied': True}
        artifacts = [visible_artifact(store, aid) for aid in proposal['_input_versions']]
        for artifact in artifacts:
            if artifact['revision'] != proposal['_input_versions'][artifact['id']]:
                raise DomainError('预览基于旧版本，成果已更新；请重新预览', 409)
        for sid, signature in proposal['_source_versions'].items():
            if fingerprint(store.get('source', sid)) != signature:
                raise DomainError('预览使用的资料已变更，请重新预览', 409)
        assert_waiting_snapshots(store, proposal['_waiting'])
        with action_lease(store, artifacts):
            updated = []
            for change in proposal['changes']:
                original = store.get('artifact', change['artifact_id'])
                # New references join only at the same atomic commit as the revision.
                enriched = {**original, '_source_ids': list(dict.fromkeys(original.get('_source_ids', []) + proposal['_source_ids'])),
                            '_source_roles': {**original.get('_source_roles', {}), **proposal['_source_roles']}}
                store.put('artifact', enriched)
                value = store.revise_artifact(change['artifact_id'], change['expected_revision'], change['items'],
                                              reason='workspace_action', report=change.get('report'))
                updated.append(public(value))
                for pending in proposal['_waiting']:
                    if pending['artifact_id'] == value['id']:
                        run = store.run(pending['id'])
                        run['interrupt']['items'] = value['items']
                        store.save_run(run)
            store.put('action_proposal', {**proposal, '_applied': True, '_applied_at': now()})
            store.put('message', {'id': uid('msg_'), 'project_id': proposal['project_id'], 'chat_id': proposal['chat_id'],
                                 'role': 'assistant', 'content': proposal['summary'], 'created_at': now(),
                                 'metadata': {'artifact_ids': [a['id'] for a in updated], 'action_proposal_id': proposal_id}})
            return {'artifacts': updated, 'summary': proposal['summary']}


def register_artifact_routes(app):
    """Imported by the app factory; model-free helper imports remain stdlib-testable."""
    from typing import Literal
    from pydantic import BaseModel, Field

    class PreviewInput(BaseModel):
        action: Literal['estimate', 'explain', 'modify', 'sync']
        instruction: str = Field(min_length=1, max_length=100000)
        selected_ids: list[str] | None = None
        source_ids: list[str] | None = None
        related_artifact_ids: list[str] | None = None

    class ApplyInput(BaseModel):
        proposal_id: str

    @app.post('/api/artifacts/{artifact_id}/actions/preview')
    async def action_preview(artifact_id: str, body: PreviewInput):
        return await preview_action(app.state.store, app.state.engine, artifact_id, body.model_dump())

    @app.post('/api/artifacts/{artifact_id}/actions/apply')
    def action_apply(artifact_id: str, body: ApplyInput):
        return apply_action(app.state.store, artifact_id, body.proposal_id)
