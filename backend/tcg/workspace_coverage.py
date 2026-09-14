"""Artifact ancestry and structural requirement/scenario/case coverage.

Only persisted artifact IDs establish ancestry. Business row IDs may repeat in
unrelated generations, so matching them never establishes an artifact relation.
"""
import copy

from .schemas import DomainError, independent_item


PARENT_KEYS = {
    'analysis': ('workspace:analysis_parent', 'v7:analysis:artifact',
                 'v6:analysis:artifact', 'clarified_artifact',
                 'v4:analysis_artifact', 'analysis_artifact'),
    'scenarios': ('workspace:scenario_parent', 'v4:scenarios_artifact',
                  'scenarios_artifact'),
}


def lineage(artifact):
    value = (artifact.get('report') or {}).get('lineage', {})
    return value if isinstance(value, dict) else {}


def _same_scope(left, right):
    return (left.get('project_id') == right.get('project_id')
            and left.get('chat_id') == right.get('chat_id'))


def _get(store, artifact_id):
    if not isinstance(artifact_id, str) or not artifact_id:
        return None
    try:
        return store.get('artifact', artifact_id)
    except DomainError as exc:
        if exc.status != 404:
            raise
        return None


def parent_artifact(store, artifact, parent_type):
    prefix = 'scenario' if parent_type == 'scenarios' else 'analysis'
    parent = _get(store, lineage(artifact).get(prefix + '_artifact_id'))
    if parent and parent.get('type') == parent_type and _same_scope(parent, artifact):
        return parent
    return None


def creation_report(store, run, kind, report=None):
    """Attach exact run-local input identity before the first revision is saved."""
    result = copy.deepcopy(report) if isinstance(report, dict) else {}
    parent_type = {'scenarios': 'analysis', 'cases': 'scenarios'}.get(kind)
    if not parent_type:
        return result
    prefix = 'scenario' if parent_type == 'scenarios' else 'analysis'
    # A model report is not permitted to invent linkage, even to an existing ID.
    result.pop('lineage', None)
    parent, revision = None, None
    for key in PARENT_KEYS[parent_type]:
        cached = store.cache_get(run['id'], key)
        candidate = _get(store, cached.get('id')) if isinstance(cached, dict) else None
        if candidate and candidate.get('type') == parent_type and _same_scope(candidate, run):
            parent = candidate
            revision = cached.get('revision') or candidate['revision']
            break
    snapshot = run.get('_artifact_snapshot') or {}
    if parent is None and snapshot.get('type') == parent_type and _same_scope(snapshot, run):
        parent, revision = snapshot, snapshot['revision']
    if parent is not None:
        result['lineage'] = {prefix + '_artifact_id': parent['id'],
                             prefix + '_revision': revision}
    elif kind == 'cases' and snapshot.get('type') == 'cases' and _same_scope(snapshot, run):
        # Preserve the actual source branch when creating a new copy of cases.
        inherited = lineage(snapshot)
        if inherited.get('scenario_artifact_id'):
            result['lineage'] = copy.deepcopy(inherited)
    return result


def resolve_related_case_artifacts(store, scenario_artifact, explicit_ids=None):
    """Resolve precise children, or validate explicitly selected legacy children.

    Explicit selection can bind an unlinked old case artifact. An artifact already
    linked to a different scenario set cannot be silently rebound.
    """
    if scenario_artifact.get('type') != 'scenarios':
        raise DomainError('请选择场景成果', 400)
    if explicit_ids is not None:
        if not isinstance(explicit_ids, list) or not all(isinstance(x, str) for x in explicit_ids):
            raise DomainError('case_artifact_ids 必须为数组', 400)
        candidates = [store.get('artifact', aid) for aid in dict.fromkeys(explicit_ids)]
    else:
        candidates = store.list('artifact', chat_id=scenario_artifact['chat_id'])
    result = []
    for candidate in candidates:
        eligible = (candidate.get('type') == 'cases' and _same_scope(candidate, scenario_artifact)
                    and candidate.get('_visible', False))
        parent_id = lineage(candidate).get('scenario_artifact_id')
        if explicit_ids is not None:
            if not eligible or (parent_id and parent_id != scenario_artifact['id']):
                raise DomainError('所选用例不属于当前场景分支；请明确选择同一对话内的关联或旧用例成果', 400)
            result.append(candidate)
        elif eligible and parent_id == scenario_artifact['id']:
            result.append(candidate)
    return sorted(result, key=lambda item: (item.get('created_at', ''), item['id']))


def _revision(store, artifact_id, revision):
    if type(revision) is not int or revision < 1:
        return None
    try:
        return store.revision(artifact_id, revision)
    except DomainError as exc:
        if exc.status != 404:
            raise
        return None


def _parent_snapshot(store, artifact, parent_type, parent_id=None, child_id=None):
    prefix = 'scenario' if parent_type == 'scenarios' else 'analysis'
    source = lineage(artifact)
    revision = (source.get(prefix + '_item_revisions') or {}).get(child_id,
        (source.get(prefix + '_revisions') or {}).get(parent_id, source.get(prefix + '_revision')))
    parent = _revision(store, source.get(prefix + '_artifact_id'), revision)
    return parent if parent and parent.get('type') == parent_type and _same_scope(parent, artifact) else None


def _row_evidence(store, artifact, row):
    refs = set(row.get('refs', []))
    versions = {}
    for source in (artifact.get('_dependencies') or {}).get('sources', []):
        versions[source['id']] = max(source['version'], versions.get(source['id'], 0))
    result = []
    for sid in sorted({ref.split('#', 1)[0] for ref in refs}):
        try:
            chunks = store.evidence_version(sid, versions[sid], artifact.get('_source_roles', {}).get(sid)) if sid in versions else store.evidence([sid], artifact.get('_source_roles'))
            result.extend({**e, 'evidence_basis': 'historical_snapshot' if sid in versions else 'current_unversioned'}
                          for e in chunks if e['id'] in refs and e.get('role') != 'example')
        except DomainError:
            continue  # Missing exact evidence is never replaced with a different version.
    return result


def lineage_rows(store, artifact):
    """Exact per-row ancestry, including mixed revision partial synchronization."""
    result = []
    for row in artifact['items']:
        linked = {'item_id': row['id'], 'scenario': None, 'requirements': [], 'status': 'linked'}
        current, current_row = artifact, row
        direct = (artifact['type'] == 'cases' and row.get('scenario_id') == ''
                  and lineage(artifact).get('generation_mode') == 'direct_requirements'
                  and (row.get('_independent_origin') or {}).get('mode') == 'direct_requirements')
        if direct:
            linked.update(scenario_skipped=True, reason='用户选择跳过场景，直接根据需求生成用例')
            ids = row.get('requirement_ids', [])
            if not ids:
                linked['status'] = 'missing_parent'
            for rid in ids:
                analysis = _parent_snapshot(store, artifact, 'analysis', rid, row['id'])
                requirement = next((r for r in analysis['items'] if r['id'] == rid), None) if analysis else None
                if requirement:
                    linked['requirements'].append({'artifact_id': analysis['id'], 'revision': analysis['revision'],
                        'item': copy.deepcopy(requirement), 'evidence': _row_evidence(store, analysis, requirement)})
                else:
                    linked['status'] = 'missing_parent'
        elif independent_item(artifact['type'], row):
            linked.update(status='independent', reason=row['_independent_origin']['reason'], stale=False)
            result.append(linked)
            continue
        if artifact['type'] == 'cases' and not direct:
            current = _parent_snapshot(store, artifact, 'scenarios', row.get('scenario_id'), row['id'])
            current_row = next((r for r in current['items'] if r['id'] == row.get('scenario_id')), None) if current else None
            if current_row:
                linked['scenario'] = {'artifact_id': current['id'], 'revision': current['revision'],
                    'item': copy.deepcopy(current_row), 'evidence': _row_evidence(store, current, current_row)}
            else:
                linked['status'] = 'missing_parent'
        if current and current_row and current['type'] == 'scenarios':
            ids = current_row.get('requirement_ids', [])
            if not ids:
                linked['status'] = 'independent' if independent_item('scenarios', current_row) else 'missing_parent'
                if linked['status'] == 'independent':
                    linked['reason'] = current_row['_independent_origin']['reason']
            for rid in ids:
                analysis = _parent_snapshot(store, current, 'analysis', rid, current_row['id'])
                requirement = next((r for r in analysis['items'] if r['id'] == rid), None) if analysis else None
                if requirement:
                    linked['requirements'].append({'artifact_id': analysis['id'], 'revision': analysis['revision'],
                        'item': copy.deepcopy(requirement), 'evidence': _row_evidence(store, analysis, requirement)})
                else:
                    linked['status'] = 'missing_parent'
        parents = ([linked['scenario']] if linked['scenario'] else []) + linked['requirements']
        linked['stale'] = linked['status'] == 'missing_parent'
        for parent in parents:
            head = _get(store, parent['artifact_id'])
            latest = next((r for r in head['items'] if r['id'] == parent['item']['id']), None) if head else None
            parent['stale'] = latest != parent['item']
            linked['stale'] = linked['stale'] or parent['stale']
        result.append(linked)
    return result


def source_discrepancies(store, artifact, *, drafts=None, historical=False):
    """Resolve only explicit saved evidence on every named parent row."""
    result = copy.deepcopy((artifact.get('report') or {}).get('upstream_discrepancies', []))
    if historical:
        return result
    for value in result:
        refs = set(value.get('refs', []))
        resolved = bool(value.get('parents'))
        for parent in value.get('parents', []):
            target = (drafts or {}).get(parent['artifact_id']) or _get(store, parent['artifact_id'])
            rows = {r['id']: r for r in (target or {}).get('items', [])}
            matches = target and _same_scope(target, artifact) and all(
                pid in rows and refs <= set(rows[pid].get('refs', [])) for pid in parent.get('item_ids', []))
            if matches:
                parent['resolved_revision'] = target['revision']
            else:
                parent.pop('resolved_revision', None)
            resolved = resolved and bool(matches)
        value['status'] = 'resolved' if resolved else 'pending'
    return result


def changed_scenario_ids(store, scenario_artifact, case_artifact):
    """Return changed/new/deleted IDs against the exact generation/sync snapshot."""
    source = lineage(case_artifact)
    current = {item['id']: item for item in scenario_artifact['items']}
    if source.get('scenario_artifact_id') != scenario_artifact['id']:
        return sorted(set(current) | {row.get('scenario_id') for row in case_artifact['items']
                                      if row.get('scenario_id')})
    baseline = _revision(store, scenario_artifact['id'], source.get('scenario_revision'))
    baseline_items = {item['id']: item for item in baseline['items']} if baseline else {}
    per_item = source.get('scenario_revisions', {})
    per_item = per_item if isinstance(per_item, dict) else {}
    ids = set(current) | set(baseline_items) | set(per_item)
    ids.update(row.get('scenario_id') for row in case_artifact['items'] if row.get('scenario_id'))
    revisions = {}
    changed = []
    for item_id in sorted(ids):
        dependents = [r for r in case_artifact['items'] if r.get('scenario_id') == item_id]
        row_versions = source.get('scenario_item_revisions') or {}
        if row_versions and dependents and any(r.get('id') in row_versions for r in dependents):
            for row in dependents:
                version = row_versions.get(row['id'], per_item.get(item_id, source.get('scenario_revision')))
                old = _revision(store, scenario_artifact['id'], version)
                old_row = next((r for r in old['items'] if r['id'] == item_id), None) if old else None
                if not old or old_row != current.get(item_id):
                    changed.append(item_id)
                    break
            continue
        version = per_item.get(item_id)
        if version is not None:
            if type(version) is not int or version < 1:
                changed.append(item_id)
                continue
            if version not in revisions:
                old = _revision(store, scenario_artifact['id'], version)
                revisions[version] = {item['id']: item for item in old['items']} if old else None
            old_items = revisions[version]
        else:
            old_items = baseline_items if baseline else None
        if old_items is None or old_items.get(item_id) != current.get(item_id):
            changed.append(item_id)
    return changed


def synced_case_report(store, scenario_artifact, case_artifact, scenario_ids, item_ids=None):
    """Advance only the synchronized scenario versions; retain all other drift."""
    selected = set(scenario_ids)
    known = {item['id'] for item in scenario_artifact['items']}
    source = lineage(case_artifact)
    previous = _revision(store, scenario_artifact['id'], source.get('scenario_revision'))
    if previous and source.get('scenario_artifact_id') == scenario_artifact['id']:
        known.update(item['id'] for item in previous['items'])
    known.update(row.get('scenario_id') for row in case_artifact['items'] if row.get('scenario_id'))
    report = copy.deepcopy(case_artifact.get('report') or {})
    new = copy.deepcopy(source) if source.get('scenario_artifact_id') == scenario_artifact['id'] else {}
    new['scenario_artifact_id'] = scenario_artifact['id']
    if item_ids is not None:
        versions = dict(new.get('scenario_item_revisions') or {})
        versions.update({item_id: scenario_artifact['revision'] for item_id in item_ids})
        new['scenario_item_revisions'] = versions
    elif known <= selected:
        new['scenario_revision'] = scenario_artifact['revision']
        new.pop('scenario_revisions', None)
        new.pop('scenario_item_revisions', None)
    else:
        versions = dict(new.get('scenario_revisions') or {})
        versions.update({item_id: scenario_artifact['revision'] for item_id in selected})
        new['scenario_revisions'] = versions
        row_versions = dict(new.get('scenario_item_revisions') or {})
        for row in case_artifact['items']:
            if row.get('scenario_id') in selected:
                row_versions.pop(row.get('id'), None)
        if row_versions:
            new['scenario_item_revisions'] = row_versions
        else:
            new.pop('scenario_item_revisions', None)
    report['lineage'] = new
    return report


def structural_coverage(analysis=None, scenarios=None, cases=None):
    """Count explicit references only; no semantic or execution claims."""
    requirements = analysis.get('items', []) if analysis else []
    scenario_items = scenarios.get('items', []) if scenarios else []
    case_items = cases.get('items', []) if cases else []
    scenario_ids = {item['id'] for item in scenario_items}
    requirement_ids = {item['id'] for item in requirements}
    by_scenario = {item_id: [] for item_id in scenario_ids}
    orphans = []
    for row in case_items:
        sid = row.get('scenario_id')
        if sid in by_scenario:
            by_scenario[sid].append(row['id'])
        else:
            orphans.append({'id': row['id'], 'title': row.get('title', ''),
                            'scenario_id': sid or '',
                            'status': 'independent' if independent_item('cases', row) else 'unlinked' if scenarios is None else 'missing_scenario'})
    scenario_rows = []
    by_requirement = {item_id: [] for item_id in requirement_ids}
    unknown_requirements = set()
    for row in scenario_items:
        linked = by_scenario[row['id']]
        refs = row.get('requirement_ids', [])
        refs = [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []
        for rid in dict.fromkeys(refs):
            if rid in by_requirement:
                by_requirement[rid].append(row['id'])
            else:
                unknown_requirements.add(rid)
        scenario_rows.append({'id': row['id'], 'title': row.get('title', ''),
                              'case_ids': linked, 'case_count': len(linked),
                              'requirement_ids': refs,
                              'status': 'covered' if linked else 'missing_cases',
                              'artifact_id': scenarios['id']})
    requirement_rows = []
    for row in requirements:
        linked = by_requirement[row['id']]
        linked_cases = list(dict.fromkeys(cid for sid in linked for cid in by_scenario[sid]))
        requirement_rows.append({'id': row['id'], 'title': row.get('title', ''),
                                 'scenario_ids': linked, 'case_ids': linked_cases,
                                 'status': 'covered' if linked_cases else 'missing_cases' if linked else 'missing_scenarios'})
    notes = ['覆盖仅统计当前成果中明确的需求、场景与用例引用关系，不代表测试已执行、已通过或业务覆盖完整。']
    if scenarios and not analysis:
        notes.append('场景尚无可验证的需求成果关联，无法统计需求覆盖。')
    if cases and not scenarios:
        notes.append('用例尚无可验证的场景成果关联；相同场景编号不自动建立关联。')
    if unknown_requirements:
        notes.append('场景含未找到的需求编号：' + '、'.join(sorted(unknown_requirements)))
    unassigned = [item['id'] for item in scenario_items if not item.get('requirement_ids') and not independent_item('scenarios', item)]
    if unassigned:
        notes.append('未分配需求的场景：' + '、'.join(unassigned))
    return {'requirements': requirement_rows, 'scenarios': scenario_rows,
            'totals': {'requirements': len(requirements),
                       'requirements_with_scenarios': sum(bool(row['scenario_ids']) for row in requirement_rows),
                       'requirements_with_cases': sum(bool(row['case_ids']) for row in requirement_rows),
                       'scenarios': len(scenario_items),
                       'scenarios_with_cases': sum(bool(row['case_ids']) for row in scenario_rows),
                       'cases': len(case_items)},
            'orphan_cases': orphans, 'notes': notes}


def workspace_context(store, artifact, case_artifact_id=None):
    """Current linked branch for display; other generations are never added together."""
    all_items = [item for item in store.list('artifact', chat_id=artifact['chat_id'])
                 if item.get('type') in ('analysis', 'scenarios', 'cases')
                 and _same_scope(item, artifact)
                 and (item.get('_visible') or item['id'] == artifact['id'])]
    analysis = artifact if artifact['type'] == 'analysis' else None
    scenarios = artifact if artifact['type'] == 'scenarios' else None
    cases = artifact if artifact['type'] == 'cases' else None
    if cases:
        scenarios = parent_artifact(store, cases, 'scenarios')
    if analysis:
        children = [item for item in all_items if item['type'] == 'scenarios'
                    and lineage(item).get('analysis_artifact_id') == analysis['id']]
        scenarios = max(children, key=lambda item: (item.get('created_at', ''), item['id'])) if children else None
    if scenarios:
        analysis = parent_artifact(store, scenarios, 'analysis')
        if not cases:
            children = [item for item in resolve_related_case_artifacts(store, scenarios) if item.get('_visible')]
            if case_artifact_id:
                matches = [item for item in children if item['id'] == case_artifact_id]
                if not matches:
                    raise DomainError('请选择当前场景分支中已关联的用例成果', 400)
                cases = matches[0]
            elif children:
                cases = children[-1]
    elif case_artifact_id:
        raise DomainError('当前成果没有可验证的场景关联', 400)
    coverage = structural_coverage(analysis, scenarios, cases)
    selected = {item['id'] for item in (analysis, scenarios, cases) if item}
    related = []
    for item in all_items:
        if item['id'] == artifact['id']:
            continue
        related.append({'id': item['id'], 'type': item['type'], 'title': item.get('title', ''),
                        'revision': item['revision'], 'linked': item['id'] in selected})
    if scenarios and artifact['type'] != 'cases':
        children = [item for item in resolve_related_case_artifacts(store, scenarios) if item.get('_visible')]
        child_ids = {item['id'] for item in children}
        for item in related:
            if item['id'] in child_ids:
                item['linked'] = True
        if len(children) > 1:
            coverage['notes'].append('存在多个用例成果分支；当前仅统计所选的一个分支，未累加重复生成的用例。')
    from .artifact_read import changed_requirement_ids
    changed_requirements = changed_requirement_ids(store, analysis, scenarios) if analysis and scenarios else []
    stale_analysis = bool(changed_requirements)
    changes = changed_scenario_ids(store, scenarios, cases) if scenarios and cases else []
    if stale_analysis:
        coverage['notes'].append('需求成果已更新，当前场景仍关联旧版本，请检查并联动更新。')
    if changes:
        coverage['notes'].append('场景已变化，以下场景对应的用例待同步：' + '、'.join(changes))
    used_source_ids = set(artifact.get('_source_ids', []))
    sources = [{'id': source['id'], 'name': source['name'], 'role': source['role'],
                'characters': source.get('characters', 0)}
               for source in store.list('source', chat_id=artifact['chat_id'])
               if _same_scope(source, artifact) and source.get('_active')
               and source.get('role') != 'example' and source['id'] not in used_source_ids]
    from .storage import public
    exact_scenarios = _parent_snapshot(store, artifact, 'scenarios') if artifact['type'] == 'cases' else artifact if artifact['type'] == 'scenarios' else None
    exact_analysis = artifact if artifact['type'] == 'analysis' else _parent_snapshot(store, exact_scenarios, 'analysis') if exact_scenarios else None
    historical = store.get('artifact', artifact['id'])['revision'] != artifact['revision']
    from .artifact_read import item_diff
    from .artifact_read import review_details
    previous = _revision(store, artifact['id'], artifact['revision'] - 1)
    revision_diff = item_diff(previous['items'] if previous else [], artifact['items'])
    return {'artifact_id': artifact['id'], 'revision': artifact['revision'],
            'related_artifacts': related, 'coverage': coverage, 'sources': sources,
            'lineage': copy.deepcopy(lineage(artifact)),
            'parents': {'analysis': public(exact_analysis) if exact_analysis else None,
                        'scenarios': public(exact_scenarios) if exact_scenarios else None},
            'lineage_rows': lineage_rows(store, artifact), 'revision_diff': revision_diff,
            'review': review_details(store, artifact if artifact['type'] == 'cases' else None),
            'source_adoptions': copy.deepcopy(artifact.get('report', {}).get('source_adoptions', [])),
            'upstream_discrepancies': source_discrepancies(store, artifact, historical=historical),
            'stale': {'analysis': stale_analysis, 'scenarios': bool(changes),
                      'changed_requirement_ids': changed_requirements,
                      'changed_scenario_ids': changes},
            'analysis_artifact_id': analysis['id'] if analysis else None,
            'scenario_artifact_id': scenarios['id'] if scenarios else None,
            'selected_case_artifact_id': cases['id'] if cases else None}


def register_coverage_routes(app):
    @app.get('/api/artifacts/{artifact_id}/workspace')
    def artifact_workspace(artifact_id: str, case_artifact_id: str | None = None, revision: int | None = None):
        store = app.state.store
        artifact = store.get('artifact', artifact_id)
        if not artifact.get('_visible'):
            raise DomainError('未找到已发布的 Artifact', 404)
        if revision is not None:
            artifact = store.revision(artifact_id, revision)
        return workspace_context(store, artifact, case_artifact_id)
