"""Snapshot reads and validated, atomic versioned artifact changes."""
import copy
import hashlib
import json
import re
from contextlib import contextmanager

from .context_service import artifact_context
from .dependencies import manifest, assert_manifest
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
        if not isinstance(selected, list) or not selected or any(not isinstance(i, str) for i in selected) or len(set(selected)) != len(selected) or not set(selected) <= set(ids):
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


def checked_operations(artifact, result, allowed_ids, evidence, allowed_scenarios=None, allow_add=False, supplied_evidence=None, allow_relink=False):
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
        if op.get('op') == 'update' and isinstance(op.get('item'), dict) and target in before and not allow_relink:
            field = {'cases': 'scenario_id', 'scenarios': 'requirement_ids'}.get(artifact['type'])
            if field and field in op['item'] and op['item'][field] != before[target].get(field):
                raise DomainError('普通局部修改不能改变条目的上游关联；请使用有范围校验的联动更新')
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
        from .case_fields import MANUAL_FIELDS
        profile = copy.deepcopy(artifact.get('_profile', {}))
        columns = list(profile.get('excel_columns', []))
        declared = {column['field'] for column in columns}
        fields = {key for row in items for key in row}
        for field in fields:
            if field not in declared and re.sub(r'[\s_-]', '', field).lower() in MANUAL_FIELDS:
                columns.append({'field': field, 'header': field, 'value_source': 'manual'})
        profile['excel_columns'] = columns
        protected = protect_non_ai_fields([r for r in items if r['id'] in touched], profile, artifact['items'])
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


def report_patch(artifact, result, evidence=None):
    report = copy.deepcopy(artifact.get('report', {}))
    patch = result.get('report_patch')
    if patch is not None and patch != {}:
        if not isinstance(patch, dict):
            raise DomainError('报告修改必须为对象')
        allowed = {'analysis': {'summary', 'diagrams', 'questions', 'question_suggestions', 'assumptions',
                   'in_scope', 'out_of_scope', 'requirement_map', 'strategy', 'limitations',
                   'relationships', 'global_rules', 'conflicts'},
                   'cases': {'review_reports'},
                   'review': {'summary', 'issues', 'coverage', 'score', 'limitations'}}.get(artifact['type'], set())
        if 'review_reports' in patch and (not isinstance(patch['review_reports'], list) or any(not isinstance(r, dict) for r in patch['review_reports'])):
            raise DomainError('评审记录必须为报告对象数组')
        if set(patch) - allowed:
            raise DomainError('报告修改不能覆盖关联关系或执行记录')
        if 'diagrams' in patch and (not isinstance(patch['diagrams'], list) or any(
                not isinstance(d, dict) or not isinstance(d.get('mermaid'), str) for d in patch['diagrams'])):
            raise DomainError('业务图必须包含 Mermaid 文本')
        for key in ('relationships', 'global_rules', 'conflicts'):
            if key not in patch:
                continue
            if not isinstance(patch[key], list):
                raise DomainError('业务规则、关系和冲突必须为数组')
            for rule in patch[key]:
                refs = rule.get('refs') if isinstance(rule, dict) else None
                if not isinstance(refs, list) or not refs or any(
                        not isinstance(ref, str) or ref not in (evidence or {})
                        or evidence[ref].get('role') == 'example' for ref in refs):
                    raise DomainError('业务规则修改必须引用本批次提供的业务依据')
                ids = rule.get('requirement_ids', [])
                if not isinstance(ids, list) or any(not isinstance(rid, str) or rid not in {row['id'] for row in artifact['items']} for rid in ids):
                    raise DomainError('业务规则包含无效需求关联')
        report.update(patch)
    return report


def linked_gate(store, artifacts, gate_id):
    from .workspace_coverage import parent_artifact
    def ancestry(artifact):
        ids = {artifact['id']}
        while artifact['type'] in ('scenarios', 'cases'):
            artifact = parent_artifact(store, artifact, 'analysis' if artifact['type'] == 'scenarios' else 'scenarios')
            if not artifact or artifact['id'] in ids:
                break
            ids.add(artifact['id'])
        return ids
    if not gate_id:
        return False
    try:
        gate = store.get('artifact', gate_id)
    except DomainError:
        return False
    if gate['chat_id'] != artifacts[0]['chat_id'] or gate['project_id'] != artifacts[0]['project_id']:
        return False
    gate_ancestors = ancestry(gate)
    return any(gate_id in ancestry(artifact) or artifact['id'] in gate_ancestors for artifact in artifacts)


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
            safe_boundary = pending.get('type') == 'workflow_paused'
            matching_gate = (pending.get('type') in ('strategy_review', 'scenario_review', 'clarification','case_draft_review','case_result_review')
                             and (pending.get('artifact_id') in ids or linked_gate(store, artifacts, pending.get('artifact_id'))))
            if not safe_boundary and not matching_gate:
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
    if not artifact.get('_visible') or artifact['type'] not in ('analysis', 'scenarios', 'cases', 'review'):
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
             'expected_revision': artifact['revision'], 'before_items': copy.deepcopy(artifact['items']), 'items': items,
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


def resolved_children(store, artifact, explicit_ids=None):
    """Resolve descendants by stored lineage; row ID overlap is never ancestry."""
    from .workspace_coverage import lineage, resolve_related_case_artifacts
    if artifact['type'] not in ('analysis', 'scenarios'):
        raise DomainError('联动更新需要从需求理解或场景成果发起')
    if explicit_ids is not None:
        if not isinstance(explicit_ids, list) or not explicit_ids or any(not isinstance(i, str) for i in explicit_ids):
            raise DomainError('请选择有效关联成果')
        explicit_ids = list(dict.fromkeys(explicit_ids))
    if artifact['type'] == 'scenarios':
        children = resolve_related_case_artifacts(store, artifact, explicit_ids)
        if explicit_ids is None and len(children) > 1:
            raise DomainError('此场景有多套关联用例，请明确选择本次同步的用例成果')
        return children
    candidates = [a for a in store.list('artifact', chat_id=artifact['chat_id'])
                  if a.get('_visible') and a.get('project_id') == artifact['project_id']]
    scenarios = [a for a in candidates if a['type'] == 'scenarios'
                 and lineage(a).get('analysis_artifact_id') == artifact['id']]
    if explicit_ids is None:
        if len(scenarios) > 1:
            raise DomainError('此需求有多套关联场景，请明确选择本次同步的场景成果')
        children = list(scenarios)
        for scenario in scenarios:
            children += resolved_children(store, scenario)
        return children
    selected = [visible_artifact(store, aid) for aid in explicit_ids]
    valid_scenarios = {a['id']: a for a in scenarios}
    needed_scenarios = set()
    for child in selected:
        if child['type'] == 'scenarios' and child['id'] in valid_scenarios:
            needed_scenarios.add(child['id'])
        elif child['type'] == 'cases' and lineage(child).get('scenario_artifact_id') in valid_scenarios and child['chat_id'] == artifact['chat_id'] and child['project_id'] == artifact['project_id']:
            needed_scenarios.add(lineage(child)['scenario_artifact_id'])
        else:
            raise DomainError('所选成果不属于当前需求分支')
    return [a for a in scenarios if a['id'] in needed_scenarios] + [a for a in selected if a['type'] == 'cases']


def changed_requirement_ids(store, analysis, scenarios):
    from .workspace_coverage import changed_scenario_ids
    # The same version comparison applies to requirement→scenario lineage.
    source = scenarios.get('report', {}).get('lineage', {})
    translated = {'scenario_artifact_id': source.get('analysis_artifact_id'),
                  'scenario_revision': source.get('analysis_revision'),
                  'scenario_revisions': source.get('analysis_revisions', {})}
    rows = [{'scenario_id': rid} for row in scenarios['items'] for rid in row.get('requirement_ids', [])]
    changed = set(changed_scenario_ids(store, analysis, {'items': rows, 'report': {'lineage': translated}}))
    # Business rules can change while requirement rows stay byte-for-byte equal.
    # Summary/diagram wording and other presentation metadata are not inputs here.
    def rules(artifact):
        report = artifact.get('report') or {}
        return {key: report.get(key) or ({} if key == 'requirement_map' else [])
                for key in ('requirement_map', 'global_rules', 'relationships', 'conflicts')}
    current_rules, baselines = rules(analysis), {}
    versions = source.get('analysis_revisions') or {}
    for row in analysis['items']:
        revision = versions.get(row['id'], source.get('analysis_revision'))
        if type(revision) is not int:
            continue  # Unknown row lineage is already reported by the row check.
        if revision not in baselines:
            try:
                baselines[revision] = rules(store.revision(analysis['id'], revision))
            except DomainError as exc:
                if exc.status != 404:
                    raise
                baselines[revision] = None
        if baselines[revision] != current_rules:
            changed.add(row['id'])
    return sorted(changed)


def synced_scenario_report(store, analysis, scenarios, requirement_ids):
    from .workspace_coverage import synced_case_report
    source = scenarios.get('report', {}).get('lineage', {})
    translated = {'scenario_artifact_id': source.get('analysis_artifact_id'),
                  'scenario_revision': source.get('analysis_revision'),
                  'scenario_revisions': source.get('analysis_revisions', {})}
    rows = [{'scenario_id': rid} for row in scenarios['items'] for rid in row.get('requirement_ids', [])]
    updated = synced_case_report(store, analysis, {'items': rows, 'report': {'lineage': translated}}, requirement_ids)['lineage']
    report = copy.deepcopy(scenarios.get('report', {}))
    report['lineage'] = {**source, **{key.replace('scenario_', 'analysis_'): value for key, value in updated.items()}}
    if 'scenario_revisions' not in updated:
        report['lineage'].pop('analysis_revisions', None)
    return report


async def snapshot_action(store, engine, artifact, body):
    """No action lease, run guard, proposal write or post-model freshness check."""
    artifact = copy.deepcopy(artifact)
    action = body['action']
    chosen = selected_rows(artifact, body.get('selected_ids'))
    instruction = body.get('instruction', '').strip()
    if action == 'estimate' and artifact['type'] != 'scenarios':
        raise DomainError('请打开场景成果后估算用例数量')
    _, _, evidence = evidence_for(store, [artifact], body.get('source_ids')) if action != 'estimate' else ([], {}, [])
    output = {'id': uid('action_'), 'artifact_id': artifact['id'], 'revision': artifact['revision'],
              'action': action, 'changes': [], 'project_id': artifact['project_id'], 'chat_id': artifact['chat_id']}
    if action == 'estimate':
        profile = artifact.get('_profile', {})
        def build(rows):
            return artifact_context(store, 'artifact_estimate', artifact, rows, [], instruction,
                extra={'contract': '仅估算设计工作量；不生成测试用例、步骤或需求事实。明确不确定性，逐场景给出范围。'}, admit=False)
        estimates = []
        for group in bounded_groups(engine, 'artifact_estimate', chosen, build):
            result = await engine.invoke_model('artifact_estimate', build(group), None)
            if not isinstance(result, dict):
                raise DomainError('估算输出必须为对象')
            estimates += validate_estimate(result, group)
        output['estimate'] = {'artifact_id': artifact['id'], 'artifact_revision': artifact['revision'], 'title': artifact['title'],
                              'instruction': instruction, 'scenarios': estimates,
                              'min_count': sum(r['min_count'] for r in estimates), 'max_count': sum(r['max_count'] for r in estimates)}
        output['summary'] = f'已估算 {len(estimates)} 个场景，建议设计 {output["estimate"]["min_count"]}–{output["estimate"]["max_count"]} 条用例。'
    else:
        def build(rows):
            return artifact_context(store, 'artifact_explain', artifact, rows, evidence, instruction,
                body.get('source_ids') or [], admit=False, extra={
                    'contract': '解释当前提供的成果和实际步骤，保留原文事实；不修改或生成用例，不声称已经执行测试。'})
        answers, refs = [], []
        groups = bounded_groups(engine, 'artifact_explain', chosen or [None], lambda rows: build([r for r in rows if r is not None]))
        for group in groups:
            context = build([r for r in group if r is not None])
            value = await engine.invoke_model('artifact_explain', context, None)
            supplied = {e['id'] for e in context['evidence']}
            if not isinstance(value, dict) or not isinstance(value.get('answer'), str) or not value['answer'].strip() or not isinstance(value.get('refs', []), list) or not all(isinstance(r, str) and r in supplied for r in value.get('refs', [])):
                raise DomainError('解释内容或引用格式无效，请重试')
            answers.append(value['answer'])
            refs += value.get('refs', [])
        output.update(answer='\n\n'.join(answers), refs=list(dict.fromkeys(refs)), summary='已根据选定版本解释成果。')
    return output


def validate_added_links(store, artifact, items):
    from .workspace_coverage import parent_artifact
    parent_type = {'scenarios': 'analysis', 'cases': 'scenarios'}.get(artifact['type'])
    if not parent_type:
        return
    parent = parent_artifact(store, artifact, parent_type)
    if not parent:
        return  # Imported legacy artifacts have no established parent branch.
    valid = {row['id'] for row in parent['items']}
    existing = {row['id'] for row in artifact['items']}
    for row in items:
        if row['id'] in existing:
            continue
        links = row.get('requirement_ids') if artifact['type'] == 'scenarios' else [row.get('scenario_id')]
        if not isinstance(links, list) or not links or any(not isinstance(i, str) or i not in valid for i in links):
            raise DomainError('新增条目必须引用当前分支中有效的上游条目')


def addition_scope(artifact, body):
    """Business-source additions never expand authority over existing rows."""
    allowed = body.get('allow_additions') is True
    if allowed and (artifact['type'] != 'analysis' or not body.get('source_ids')):
        raise DomainError('新增需求需要需求理解成果及明确的业务资料')
    return allowed


async def modify_draft(engine, artifact, body, evidence, store=None):
    from .generation_guards import merge_manifests
    store = store or engine.store
    consumed = []
    additions = addition_scope(artifact, body)
    chosen = [] if additions and body.get('selected_ids') == [] else selected_rows(artifact, body.get('selected_ids'))
    current, operations, summaries = copy.deepcopy(artifact), [], []
    addition_refs = {e['id'] for e in evidence if e['source_id'] in (body.get('source_ids') or []) and e['role'] != 'example'}
    def build(rows):
        return artifact_context(store, 'artifact_modify', artifact, rows, evidence, body['instruction'],
            body.get('source_ids') or [], admit=False, extra={
                'contract': '仅局部修改提供的条目，保留 ID、自定义字段及人工数据。删除需要 reason 和 refs；不得顺带修改其他成果。',
                **({'existing_requirement_ids': [r['id'] for r in artifact['items']],
                    'addition_basis': {'refs': sorted(addition_refs)},
                    'addition_instruction': '允许依据新增资料增加真正缺失的需求；已有条目仅可修改 selected_ids，不得重复已有稳定 ID。'} if additions else {})})
    for group in bounded_groups(engine, 'artifact_modify', chosen or [None], lambda rows: build([r for r in rows if r is not None])):
        context = build([r for r in group if r is not None])
        result = await engine.invoke_model('artifact_modify', context, None)
        consumed.append(context['dependency_manifest'])
        if not isinstance(result, dict):
            raise DomainError('修改输出必须为对象')
        current['items'] = checked_operations(current, result, context['selected_ids'], {e['id']: e for e in evidence},
            allow_add=additions or body.get('selected_ids') is None, supplied_evidence={e['id']: e for e in context['evidence']})
        if additions:
            for op in result['operations']:
                if op.get('op') == 'add' and not set(op['item'].get('refs', [])) & addition_refs:
                    raise DomainError('新增需求必须引用本次新增业务资料')
        current['report'] = report_patch(current, result, {e['id']: e for e in context['evidence']})
        operations.extend(result['operations'])
        summaries.append(str(result.get('summary', '已生成局部修改预览')))
    change = proposal_change(artifact, current['items'], current.get('report'), operations, body['instruction'])
    change['_provenance'] = merge_manifests(*consumed)
    current.update(revision=artifact['revision'] + 1, report=change.get('report', current.get('report', {})))
    return current, change, summaries


async def sync_scenario_draft(store, engine, analysis, artifact, affected, body, evidence):
    from .generation_guards import merge_manifests
    consumed = []
    current, operations, summaries = copy.deepcopy(artifact), [], []
    by_id = {r['id']: r for r in analysis['items']}
    # Each scenario is sent once even when it depends on several selected requirements.
    affected_rows = [r for r in artifact['items'] if set(r.get('requirement_ids', [])) & set(affected)]
    units = [{'row': row} for row in affected_rows]
    assigned = {rid for row in affected_rows for rid in row.get('requirement_ids', [])}
    units += [{'requirement_id': rid} for rid in sorted(set(affected) - assigned)]
    def build(group):
        rows = [u['row'] for u in group if 'row' in u]
        own = set(rid for row in rows for rid in row.get('requirement_ids', []) if rid in affected)
        own.update(u['requirement_id'] for u in group if 'requirement_id' in u)
        requirements = [by_id[rid] for rid in sorted(own) if rid in by_id]
        deletion_basis = [v for rid, v in analysis.get('report', {}).get('item_deletions', {}).items() if rid in own]
        return artifact_context(store, 'artifact_sync_scenarios', artifact, rows, evidence, body['instruction'],
            body.get('source_ids') or [], admit=False, extra={
                'analysis': requirements, 'selected_requirement_ids': sorted(own),
                'removed_requirement_ids': sorted(own - set(by_id)), 'deletion_basis': deletion_basis,
                'upstream_draft': {'artifact_id': analysis['id'], 'revision': analysis['revision']}})
    for group in bounded_groups(engine, 'artifact_sync_scenarios', units, build):
        context = build(group)
        result = await engine.invoke_model('artifact_sync_scenarios', context, None)
        consumed.append(context['dependency_manifest'])
        if not isinstance(result, dict):
            raise DomainError('场景同步输出必须为对象')
        updated = checked_operations(current, result, context['selected_ids'], {e['id']: e for e in evidence},
            allow_add=True, supplied_evidence={e['id']: e for e in context['evidence']}, allow_relink=True)
        touched = set(item_diff(current['items'], updated)['added'] + item_diff(current['items'], updated)['updated'])
        old = {r['id']: r for r in current['items']}
        own = set(context['selected_requirement_ids'])
        for row in updated:
            if row['id'] not in touched:
                continue
            links = row.get('requirement_ids')
            if not isinstance(links, list) or not links or any(not isinstance(i, str) or i not in by_id for i in links) or not set(links) & own:
                raise DomainError('场景同步必须保留有效的所选需求关联')
            previous_other = set(old.get(row['id'], {}).get('requirement_ids', [])) - own
            if not previous_other <= set(links):
                raise DomainError('同步不能删除未选择的需求关联')
        if any(set(row.get('requirement_ids', [])) & set(context['removed_requirement_ids']) for row in updated):
            raise DomainError('已删除需求仍有关联场景，请重试同步')
        current['items'] = updated
        operations += result['operations']
        summaries.append(str(result.get('summary', '已生成关联场景修改预览')))
    covered = {rid for row in current['items'] for rid in row.get('requirement_ids', [])}
    if (set(affected) & set(by_id)) - covered:
        raise DomainError('同步后仍有选中需求缺少场景；请补齐覆盖或明确移除需求')
    report = synced_scenario_report(store, analysis, current, affected)
    change = proposal_change(artifact, current['items'], report, operations, body['instruction'])
    change['_provenance'] = merge_manifests(*consumed)
    change['_draft_inputs'] = [{'id': analysis['id'], 'revision': analysis['revision']}]
    current.update(revision=artifact['revision'] + 1, report=change['report'])
    return current, change, summaries


async def sync_case_draft(store, engine, scenarios, artifact, affected, body, evidence):
    from .generation_guards import merge_manifests
    consumed = []
    from .workspace_coverage import synced_case_report
    current, operations, summaries = copy.deepcopy(artifact), [], []
    current_scenarios = {r['id']: r for r in scenarios['items']}
    baselines = scenario_baselines(store, scenarios, artifact, affected)
    deletions = scenarios.get('report', {}).get('item_deletions', {})
    units = [{'scenario_id': sid, 'scenario': current_scenarios.get(sid),
              'cases': [r for r in current['items'] if r.get('scenario_id') == sid]} for sid in sorted(affected)]
    def build(group):
        rows = [r for unit in group for r in unit['cases']]
        selected = [u['scenario'] for u in group if u['scenario']]
        own_ids = {u['scenario_id'] for u in group}
        previous = [r for r in baselines if r['id'] in own_ids]
        deletion_basis = [deletions[sid] for sid in own_ids if sid in deletions]
        return artifact_context(store, 'artifact_sync', artifact, rows, evidence, body['instruction'],
            body.get('source_ids') or [], admit=False, extra={
                'scenarios': selected, 'removed_scenario_ids': [u['scenario_id'] for u in group if not u['scenario']],
                'baseline_scenarios': previous, 'deletion_basis': deletion_basis,
                'upstream_draft': {'artifact_id': scenarios['id'], 'revision': scenarios['revision']},
                'contract': '仅同步这些场景关联的用例；保留有效稳定 ID 和人工/自定义数据。新增覆盖缺失，删除有依据的失效用例；删除必须返回 reason 和 refs。'})
    for group in bounded_groups(engine, 'artifact_sync', units, build):
        context = build(group)
        result = await engine.invoke_model('artifact_sync', context, None)
        consumed.append(context['dependency_manifest'])
        if not isinstance(result, dict):
            raise DomainError('用例同步输出必须为对象')
        current['items'] = checked_operations(current, result, context['selected_ids'], {e['id']: e for e in evidence},
            {u['scenario_id'] for u in group if u['scenario']}, supplied_evidence={e['id']: e for e in context['evidence']})
        if any(r.get('scenario_id') in set(context['removed_scenario_ids']) for r in current['items']):
            raise DomainError('已删除场景仍有关联用例，请修改同步要求后重试')
        operations += result['operations']
        summaries.append(str(result.get('summary', '已生成关联用例修改预览')))
    covered = {r.get('scenario_id') for r in current['items']}
    if (set(affected) & set(current_scenarios)) - covered:
        raise DomainError('同步后仍有选中场景没有用例；请补齐覆盖或明确移除场景后重试')
    report = synced_case_report(store, scenarios, current, affected)
    change = proposal_change(artifact, current['items'], report, operations, body['instruction'])
    change['_provenance'] = merge_manifests(*consumed)
    change['_draft_inputs'] = [{'id': scenarios['id'], 'revision': scenarios['revision']}]
    current.update(revision=artifact['revision'] + 1, report=change['report'])
    return current, change, summaries


async def preview_action(store, engine, artifact_id, body, on_saved=None):
    with store.lock:
        artifact = copy.deepcopy(visible_artifact(store, artifact_id))
    action = body.get('action')
    if action not in ('estimate', 'explain', 'modify', 'sync'):
        raise DomainError('不支持的成果操作')
    instruction = body.get('instruction', '')
    if not isinstance(instruction, str) or not instruction.strip():
        raise DomainError('请填写本次操作要求')
    body = {**body, 'instruction': instruction.strip()}
    expected = body.get('expected_revision', body.get('artifact_revision'))
    if expected is not None and (type(expected) is not int or expected < 1):
        raise DomainError('请选择有效的成果版本')
    if expected is not None and artifact['revision'] != expected:
        if action in ('estimate', 'explain'):
            artifact = store.revision(artifact_id, expected)
        else:
            raise DomainError('成果已更新，请刷新后重试', 409)
    if action in ('estimate', 'explain'):
        return await snapshot_action(store, engine, artifact, body)
    additions = addition_scope(artifact, body)
    if not (additions and body.get('selected_ids') == []):
        selected_rows(artifact, body.get('selected_ids'))
    artifacts = [artifact]
    if action == 'sync' or body.get('sync_related'):
        artifacts += resolved_children(store, artifact, body.get('related_artifact_ids'))
        if len(artifacts) == 1:
            raise DomainError('没有关联成果，请先生成或选择需要联动的成果')
    source_ids, roles, evidence = evidence_for(store, artifacts, body.get('source_ids'))
    with action_lease(store, artifacts) as waiting:
        versions = {a['id']: a['revision'] for a in artifacts}
        sources = source_versions(store, source_ids)
        # Bind the complete known ancestry before any model call.
        inputs = {a['id']: a for a in artifacts}
        queue = list(artifacts)
        while queue:
            current = queue.pop()
            lineage = current.get('report', {}).get('lineage', {})
            for field in ('analysis_artifact_id', 'scenario_artifact_id'):
                parent_id = lineage.get(field)
                if parent_id and parent_id not in inputs:
                    parent = store.get('artifact', parent_id)
                    if parent['project_id'] != artifact['project_id'] or parent['chat_id'] != artifact['chat_id']:
                        raise DomainError('上游成果不属于当前工作范围', 403)
                    inputs[parent_id] = parent
                    queue.append(parent)
        dependency_manifest = manifest(store, artifact_ids=list(inputs), source_ids=source_ids)
        changes, summaries = [], []
        output = {'id': uid('action_'), 'artifact_id': artifact_id, 'revision': artifact['revision'], 'action': action,
                  'project_id': artifact['project_id'], 'chat_id': artifact['chat_id'], 'created_at': now(), 'changes': changes}
        upstream = artifact
        if action == 'modify':
            if body.get('source_adoption_only') is True:
                if artifact['type'] != 'analysis' or not body.get('source_ids'):
                    raise DomainError('纳入资料需要需求理解成果和已检查的业务资料')
                review = body.get('source_review') or {}
                report = {**copy.deepcopy(artifact.get('report', {})), 'source_review': copy.deepcopy(review)}
                change = proposal_change(artifact, copy.deepcopy(artifact['items']), report)
                upstream = {**copy.deepcopy(artifact), 'revision': artifact['revision'] + 1, 'report': report}
                notes = [str(review.get('summary') or '新增资料已检查，需求条目无需修改；应用后纳入本轮依据。')]
            else:
                upstream, change, notes = await modify_draft(engine, artifact, body, evidence, store)
            validate_added_links(store, artifact, upstream['items'])
            changes.append(change)
            summaries += notes
        if len(artifacts) > 1:
            from .workspace_coverage import changed_scenario_ids
            drafts = {artifact_id: upstream}
            affected_scenarios = {}
            for child in artifacts[1:]:
                source = child.get('report', {}).get('lineage', {})
                if child['type'] == 'scenarios':
                    drafts[child['id']] = child
                    affected_scenarios[child['id']] = []
                    affected = body.get('selected_ids')
                    if affected is not None and action == 'modify':
                        affected = list(dict.fromkeys(affected + changes[0]['diff']['added']))
                    if affected is not None and body.get('reconcile_all_drift'):
                        affected = sorted(set(affected) | set(changed_requirement_ids(store, upstream, child)))
                    if affected is None:
                        affected = changed_requirement_ids(store, upstream, child)
                    if not affected:
                        continue
                    draft, change, notes = await sync_scenario_draft(store, engine, upstream, child, affected, body, evidence)
                    drafts[child['id']] = draft
                    delta = change['diff']
                    affected_scenarios[child['id']] = delta['added'] + delta['updated'] + delta['deleted']
                else:
                    parent_id = source.get('scenario_artifact_id') or artifact_id
                    parent = drafts.get(parent_id)
                    if parent is None:
                        continue
                    affected = affected_scenarios.get(parent_id)
                    if affected is not None and body.get('reconcile_all_drift'):
                        affected = sorted(set(affected) | set(changed_scenario_ids(store, parent, child)))
                    if affected is None:
                        affected = body.get('selected_ids')
                    if affected is None:
                        affected = changed_scenario_ids(store, parent, child)
                    if not affected:
                        continue
                    draft, change, notes = await sync_case_draft(store, engine, parent, child, affected, body, evidence)
                    drafts[child['id']] = draft
                changes.append(change)
                summaries += notes
        output['summary'] = '\n'.join(summaries) or '关联成果已与当前版本一致，无需修改。'
        with store.transaction():
            from .conversation_receipts import assert_command_live
            assert_command_live(store, body.get('_command_id'))
            assert_waiting_snapshots(store, waiting)
            assert_manifest(store, dependency_manifest)
            for aid, revision in versions.items():
                if store.get('artifact', aid)['revision'] != revision:
                    raise DomainError('成果已更新，请重新生成预览', 409)
            for sid, signature in sources.items():
                if fingerprint(store.get('source', sid)) != signature:
                    raise DomainError('本次资料已变化，请重新生成预览', 409)
            saved = {**copy.deepcopy(output), '_input_versions': versions, '_waiting': waiting,
                     '_source_versions': sources, '_source_ids': source_ids, '_source_roles': roles,
                     '_dependency_manifest': dependency_manifest, '_command_id': body.get('_command_id')}
            store.put('action_proposal', saved)
            if on_saved is not None:
                on_saved(output)
        return output


def rebind_waiting_runs(store, artifacts, source_ids=None, source_roles=None):
    """Refresh saved inputs at the current gate, inside the caller's transaction."""
    if not artifacts:
        return
    waiting = store.runs(chat_id=artifacts[0]['chat_id'], statuses=('waiting',))
    if not waiting:
        return
    if any(not run.get('_edit_token') for run in waiting):
        with action_lease(store, artifacts):
            _rebind_waiting_runs(store, artifacts, source_ids, source_roles)
    else:
        _rebind_waiting_runs(store, artifacts, source_ids, source_roles)


def _rebind_waiting_runs(store, artifacts, source_ids=None, source_roles=None):
    from .workspace_coverage import PARENT_KEYS
    if not artifacts:
        return
    for run in store.runs(chat_id=artifacts[0]['chat_id'], statuses=('waiting',)):
        input_changed = False
        for artifact in artifacts:
            kind = artifact['type']
            if kind not in ('analysis', 'scenarios', 'cases'):
                continue
            cached_ids = {cached.get('id') for key in PARENT_KEYS.get(kind, ())
                          if isinstance(cached := store.cache_get(run['id'], key), dict)}
            snapshot_matches = (run.get('_artifact_snapshot') or {}).get('id') == artifact['id']
            gate_matches = linked_gate(store, [artifact], run.get('interrupt', {}).get('artifact_id'))
            if artifact['id'] not in cached_ids and not snapshot_matches and not gate_matches:
                continue
            input_changed = True
            if snapshot_matches:
                run['_artifact_snapshot'] = store.get('artifact', artifact['id'])
            if kind == 'analysis':
                store.cache_set(run['id'], 'v6:requirement_map',
                    {**artifact.get('report', {}), 'confirmed_requirements': artifact['items']})
                store.cache_set(run['id'], 'workspace:analysis_parent',
                    {'id': artifact['id'], 'revision': artifact['revision']})
            elif kind == 'scenarios':
                store.cache_set(run['id'], 'workspace:scenario_parent',
                    {'id': artifact['id'], 'revision': artifact['revision']})
        # Do not attach evidence to another, unrelated paused branch.
        if not input_changed:
            continue
        previous_ids = run.get('_source_ids', [])
        run['_source_ids'] = list(dict.fromkeys(previous_ids + list(source_ids or [])))
        run['_source_roles'] = {**run.get('_source_roles', {}), **(source_roles or {})}
        run['input_version'] = run['_input_version'] = max(run.get('input_version', 0), run.get('_input_version', 0)) + 1
        store.save_run(run)


def apply_action(store, artifact_id, proposal_id, emit_message=True):
    with store.transaction():
        proposal = store.get('action_proposal', proposal_id)
        if proposal['artifact_id'] != artifact_id or proposal['action'] not in ('modify', 'sync'):
            raise DomainError('此预览不能应用到当前成果')
        if proposal.get('_discarded'):
            raise DomainError('此修改预览已取消，不能应用', 409)
        if proposal.get('_applied'):
            return {'artifacts': copy.deepcopy(proposal.get('_applied_artifacts')) if proposal.get('_applied_artifacts') is not None else
                        [public(store.revision(c['artifact_id'], c['expected_revision'] + 1)) for c in proposal['changes']],
                    'summary': proposal['summary'], 'already_applied': True}
        from .conversation_receipts import assert_command_live
        assert_command_live(store, proposal.get('_command_id'))
        artifacts = [visible_artifact(store, aid) for aid in proposal['_input_versions']]
        for artifact in artifacts:
            if artifact['revision'] != proposal['_input_versions'][artifact['id']]:
                raise DomainError('预览基于旧版本，成果已更新；请重新预览', 409)
        for sid, signature in proposal['_source_versions'].items():
            if fingerprint(store.get('source', sid)) != signature:
                raise DomainError('预览使用的资料已变更，请重新预览', 409)
        assert_waiting_snapshots(store, proposal['_waiting'])
        if proposal.get('_dependency_manifest'):
            assert_manifest(store, proposal['_dependency_manifest'])
        with action_lease(store, artifacts):
            updated = []
            for change in proposal['changes']:
                original = store.get('artifact', change['artifact_id'])
                # New references join only at the same atomic commit as the revision.
                enriched = {**original, '_source_ids': list(dict.fromkeys(original.get('_source_ids', []) + proposal['_source_ids'])),
                            '_source_roles': {**original.get('_source_roles', {}), **proposal['_source_roles']}}
                validate_added_links(store, enriched, change['items'])
                from .generation_guards import merge_manifests
                consumed = change.get('_provenance')
                if change.get('_draft_inputs'):
                    # Upstream drafts have now been committed earlier in this same
                    # transaction. Record those exact versions, never today's head.
                    consumed = merge_manifests(consumed, manifest(store, artifact_ids=change['_draft_inputs']))
                value = store.revise_artifact(change['artifact_id'], change['expected_revision'], change['items'],
                    reason='workspace_action', report=change.get('report'), source_ids=proposal['_source_ids'],
                    source_roles=proposal['_source_roles'], command_id=proposal_id + ':' + change['artifact_id'],
                    provenance=consumed)
                updated.append(public(value))
                for pending in proposal['_waiting']:
                    if pending['artifact_id'] == value['id']:
                        run = store.run(pending['id'])
                        run['interrupt']['items'] = value['items']
                        store.save_run(run)
            rebind_waiting_runs(store, updated, proposal['_source_ids'], proposal['_source_roles'])
            store.put('action_proposal', {**proposal, '_applied': True, '_applied_at': now(), '_applied_artifacts': copy.deepcopy(updated)})
            if emit_message:
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
        sync_related: bool = False
        expected_revision: int | None = None

    class ApplyInput(BaseModel):
        proposal_id: str

    @app.post('/api/artifacts/{artifact_id}/actions/preview')
    async def action_preview(artifact_id: str, body: PreviewInput):
        return await preview_action(app.state.store, app.state.engine, artifact_id, body.model_dump())

    @app.post('/api/artifacts/{artifact_id}/actions/apply')
    def action_apply(artifact_id: str, body: ApplyInput):
        return apply_action(app.state.store, artifact_id, body.proposal_id)
