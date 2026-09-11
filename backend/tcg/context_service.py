"""Detached task ContextPacks with historical ancestry and explicit coverage."""
import copy
import hashlib
import json
import re

from .schemas import DomainError


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _refs(rows):
    return {ref for row in rows if isinstance(row, dict) for ref in row.get('refs', []) if isinstance(ref, str)}


def rule_projection(report, rows=()):
    """Close applicable rules; final ContextPack admission owns the token budget."""
    report = report if isinstance(report, dict) else {}
    mapping = report.get('requirement_map') or {}
    mapping = mapping if isinstance(mapping, dict) else {}
    refs, ids = _refs(rows), {row.get('id') for row in rows if isinstance(row, dict)}
    rules, omitted, size = [], [], 0
    candidates = []
    for field in ('rules', 'dependencies', 'exclusions', 'precedence', 'relationships', 'global_rules', 'conflicts'):
        values = mapping.get(field, report.get(field, []))
        if not isinstance(values, list):
            values = [values] if values else []
        for index, value in enumerate(values):
            rule = copy.deepcopy(value) if isinstance(value, dict) else {'text': str(value)}
            rule.setdefault('id', 'rule-' + _digest({'field': field, 'value': value})[:16])
            rule.setdefault('refs', [])
            rule['provenance'] = {'report_field': 'requirement_map.' + field if field in mapping else field, 'index': index}
            if field == 'global_rules':
                rule['mandatory'] = True
            candidates.append(rule)
    remaining = list(candidates)
    while remaining:
        included = []
        for rule in remaining:
            linked = set(rule.get('requirement_ids', [])) | set(rule.get('scenario_ids', []))
            scoped = bool(rule.get('refs') or linked)
            applicable = rule.get('mandatory') is True or not rows or not scoped or bool(refs & set(rule['refs']) or ids & linked)
            if applicable:
                rules.append(rule)
                included.append(rule)
                ids.update(linked)
                refs.update(rule['refs'])
        if not included:
            break
        remaining = [r for r in remaining if r not in included]
    omitted = [r['id'] for r in remaining]
    return {'rules': rules, 'coverage': {'included_rule_ids': [r['id'] for r in rules],
            'omitted_rule_ids': omitted, 'partial': bool(omitted)}}


def _ancestors(store, artifact, kind, rows):
    prefix = 'scenario' if kind == 'scenarios' else 'analysis'
    lineage = (artifact.get('report') or {}).get('lineage') or {}
    aid = lineage.get(prefix + '_artifact_id')
    if not aid:
        return []
    versions = lineage.get(prefix + '_revisions') or {}
    wanted = {row.get('scenario_id') for row in rows} if kind == 'scenarios' else {
        rid for row in rows for rid in row.get('requirement_ids', [])}
    grouped = {}
    for item_id in wanted - {None, ''}:
        revision = versions.get(item_id, lineage.get(prefix + '_revision'))
        if revision is not None:
            grouped.setdefault(revision, set()).add(item_id)
    result = []
    for revision, ids in grouped.items():
        value = store.revision(aid, revision)
        if value.get('type') != kind or any(value.get(key) != artifact.get(key) for key in ('project_id', 'chat_id')):
            raise DomainError('成果的历史依赖不属于当前资料范围', 409)
        result.append((value, [r for r in value.get('items', []) if r['id'] in ids]))
    return result


def _terms(value):
    text = json.dumps(value, ensure_ascii=False).lower()
    terms = set(re.findall(r'[a-z0-9_]{3,}', text))
    for phrase in re.findall(r'[\u4e00-\u9fff]{2,}', text):
        terms.update(phrase[i:i + width] for width in (2, 3) for i in range(len(phrase) - width + 1))
    return terms



def artifact_context(store, task, artifact, rows, evidence, instruction='', explicit_source_ids=(), extra=None, *, admit=True):
    """Build a detached task pack; ``admit=False`` permits grouping candidates.

    Candidate mode retains every mandatory span even when the pack is oversized.
    Callers must run their shared fit/group check before invocation; final gateway
    admission remains mandatory. Optional retrieval may still be removed.
    """
    from .dependencies import manifest
    artifact, rows = copy.deepcopy(artifact), copy.deepcopy(list(rows))
    context = copy.deepcopy(extra or {})
    profile = artifact.get('_profile', {})
    consumed = [artifact]
    context.update(instruction=instruction, selected_ids=[r['id'] for r in rows])
    if task == 'artifact_estimate':
        context.update(scenarios=rows, profile={k: copy.deepcopy(profile[k]) for k in (
            'case_level', 'scenario_level', 'case_types', 'language', 'scope', 'additional_rules') if k in profile})
        context.pop('evidence', None)
        context.pop('analysis', None)
        context.pop('artifact', None)
        context['coverage'] = {'selected_row_ids': context['selected_ids'], 'included_row_ids': context['selected_ids'],
            'omitted_row_ids': [r['id'] for r in artifact.get('items', []) if r['id'] not in context['selected_ids']]}
        context['dependency_manifest'] = manifest(store, artifact_ids=[{'id': artifact['id'], 'revision': artifact['revision']}])
        return context
    selected = list(rows)
    report = artifact.get('report', {})
    analysis_inputs = []
    if artifact['type'] == 'cases':
        parents = _ancestors(store, artifact, 'scenarios', rows)
        context['scenarios'] = []
        for parent, scenarios in parents:
            consumed.append(parent)
            context['scenarios'] += scenarios
            selected += scenarios
            analysis_inputs += _ancestors(store, parent, 'analysis', scenarios)
        if not parents:
            analysis_inputs = _ancestors(store, artifact, 'analysis', rows)
    elif artifact['type'] == 'scenarios':
        analysis_inputs = _ancestors(store, artifact, 'analysis', rows)
    elif artifact['type'] == 'analysis':
        analysis_inputs = [(artifact, rows)]
    projections = []
    if analysis_inputs:
        context['analysis'] = []
        seen_rows = set()
        for analysis, requirements in analysis_inputs:
            consumed.append(analysis)
            requirements = list(requirements)
            while True:
                own_projection = rule_projection(analysis.get('report', {}), requirements)
                linked = {rid for rule in own_projection['rules'] for rid in rule.get('requirement_ids', [])}
                linked_refs = _refs(own_projection['rules'])
                present = {r['id'] for r in requirements}
                additions = [r for r in analysis.get('items', []) if r['id'] not in present
                             and (r['id'] in linked or bool(_refs([r]) & linked_refs))]
                if not additions:
                    break
                requirements.extend(additions)
            for row in requirements:
                key = (analysis['id'], analysis['revision'], row['id'])
                if key not in seen_rows:
                    seen_rows.add(key)
                    context['analysis'].append(row)
                    selected.append(row)
            projections.append(own_projection)
    else:
        projections = [rule_projection(report, selected)]
    rules = {r['id']: r for projection in projections for r in projection['rules']}
    omitted_rules = list(dict.fromkeys(rid for projection in projections for rid in projection['coverage']['omitted_rule_ids'] if rid not in rules))
    projection = {'rules': list(rules.values()), 'coverage': {'included_rule_ids': list(rules),
        'omitted_rule_ids': omitted_rules, 'partial': bool(omitted_rules)}}
    context['global_rules'] = projection
    def nested_refs(value):
        if isinstance(value, dict):
            own = {r for r in value.get('refs', []) if isinstance(r, str)} if isinstance(value.get('refs', []), list) else set()
            return own | set().union(*(nested_refs(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(nested_refs(v) for v in value))
        return set()
    for key in ('analysis', 'scenarios'):
        if extra and key in extra:
            context.setdefault('historical_ancestors', {})[key] = context.get(key, [])
            context[key] = copy.deepcopy(extra[key])
    mandatory = _refs(selected) | _refs(projection['rules']) | nested_refs(extra or {})
    source_ids = list(dict.fromkeys(sid for item in consumed for sid in item.get('_source_ids', [])))
    roles = {sid: role for item in reversed(consumed) for sid, role in item.get('_source_roles', {}).items()}
    pinned = {}
    missing_versions = []
    for item in consumed:
        for ref in (item.get('_dependencies') or {}).get('sources', []):
            pinned.setdefault(ref['id'], {})[ref['version']] = ref
    # Current caller evidence is explicitly identified when no historical snapshot exists.
    candidates = {e['id']: {**copy.deepcopy(e), 'role': roles.get(e['source_id'], e.get('role')),
                           'evidence_basis': 'current_unversioned'}
        for e in evidence if e['source_id'] not in pinned and roles.get(e['source_id'], e.get('role')) != 'example'}
    historical_extra = []
    for sid, versions in pinned.items():
        for version in sorted(versions):
            try:
                for item in store.evidence_version(sid, version, roles.get(sid)):
                    if item.get('role') == 'example':
                        continue
                    item['evidence_basis'] = 'historical_snapshot'
                    if item['id'] in candidates:
                        historical_extra.append(item)
                    else:
                        candidates[item['id']] = item
            except DomainError as exc:
                if exc.status != 404:
                    raise
                missing_versions.append({'source_id': sid, 'version': version, 'reason': '历史证据快照不可用'})
    for sid in source_ids:
        if sid not in pinned:
            missing_versions.append({'source_id': sid, 'version': None, 'reason': '旧成果未记录历史证据版本；可用内容仅为当前未绑定版本'})
    for sid in explicit_source_ids:
        if sid in pinned:
            snapshot = store.source_snapshot(sid)
            version = snapshot['source'].get('version', 1)
            if version not in pinned[sid]:
                for item in store.evidence_version(sid, version):
                    if item.get('role') != 'example':
                        historical_extra.append({**item, 'evidence_basis': 'explicit_current_snapshot'})
    mandatory |= {e['id'] for e in candidates.values() if e.get('role') == 'clarification' and e['source_id'] in source_ids}
    unpinned_ids = [sid for sid in source_ids if sid not in pinned]
    if mandatory - candidates.keys() and unpinned_ids:
        for item in store.evidence(unpinned_ids, roles):
            if item['id'] in mandatory and item.get('role') != 'example':
                candidates.setdefault(item['id'], {**item, 'evidence_basis': 'current_unversioned'})
    terms = _terms([instruction, [{k: v for k, v in row.items() if k not in ('id', 'refs')} for row in selected]])
    included = [e for e in candidates.values() if e['id'] in mandatory]
    included += [e for e in historical_extra if e['id'] in mandatory]
    optional = [e for e in list(candidates.values()) + historical_extra if e['id'] not in mandatory and (e.get('source_id') in explicit_source_ids or e.get('role') in ('change', 'clarification'))]
    optional.sort(key=lambda e: (-len(terms & _terms(e.get('text', ''))), e['id']))
    optional_size = 0
    for item in optional:
        if terms & _terms(item.get('text', '')) and optional_size + len(item.get('text', '')) <= 12000:
            included.append(item)
            optional_size += len(item.get('text', ''))
    included_ids = {e['id'] for e in included}
    omitted = [eid for eid in candidates if eid not in included_ids]
    missing = sorted(mandatory - candidates.keys())
    context.update(artifact={k: copy.deepcopy(artifact[k]) for k in ('id', 'type', 'title', 'revision') if k in artifact},
                   evidence=included, profile=copy.deepcopy(profile))
    context['artifact']['items'] = rows
    context['artifact']['report'] = {'global_rules': projection}
    context['coverage'] = {'selected_row_ids': context['selected_ids'], 'included_row_ids': context['selected_ids'],
        'omitted_row_ids': [r['id'] for r in artifact.get('items', []) if r['id'] not in context['selected_ids']],
        'selected_source_ids': list(explicit_source_ids), 'included_evidence_ids': [e['id'] for e in included],
        'omitted_evidence_ids': omitted, 'missing_referenced_evidence_ids': missing,
        'missing_historical_source_versions': missing_versions,
        'partial': bool(omitted or missing or missing_versions or projection['coverage']['partial']),
        'note': '仅依据实际提供的证据解释或修改；新资料仅检索相关片段，未提供的内容不代表已完整覆盖。'}
    used_sources = []
    for e in included:
        ref = {'id': e['source_id'], 'version': e['source_version']} if 'source_version' in e else e['source_id']
        if ref not in used_sources:
            used_sources.append(ref)
    context['dependency_manifest'] = manifest(store,
        artifact_ids=[{'id': item['id'], 'revision': item['revision']} for item in consumed], source_ids=used_sources)
    # Include all mandatory dependencies, then trim optional retrieval against the actual request.
    from .context_budget import request_budget
    while not request_budget(store.directory, task, context)['fits']:
        removable = next((i for i in reversed(context['evidence']) if i['id'] not in mandatory), None)
        if removable is None:
            if not admit:
                break
            raise DomainError('必要规则与证据超过模型上下文容量；请拆分任务或提高容量，未省略必要依赖。')
        context['evidence'].remove(removable)
        context['coverage']['included_evidence_ids'].remove(removable['id'])
        context['coverage']['omitted_evidence_ids'].append(removable['id'])
        context['coverage']['partial'] = True
        used_sources = [({'id': e['source_id'], 'version': e['source_version']} if 'source_version' in e else e['source_id']) for e in context['evidence']]
        context['dependency_manifest'] = manifest(store, artifact_ids=[{'id': item['id'], 'revision': item['revision']} for item in consumed], source_ids=used_sources)
    return copy.deepcopy(context)


def analysis_signature(store, source_ids, roles, profile, scope=None):
    semantic = {key: copy.deepcopy(profile[key]) for key in (
        'language', 'scope', 'scenario_level', 'additional_rules', 'analysis_rules', 'requirement_rules') if key in profile}
    sources = [{'id': sid, 'content_digest': _digest(store.get('source', sid).get('_text', '')),
                'role': roles.get(sid, store.get('source', sid).get('role'))} for sid in sorted(set(source_ids))]
    result = {'version': 1, 'sources': sources, 'roles_digest': _digest(roles), 'semantic_profile': semantic, 'scope': copy.deepcopy(scope)}
    return {**result, 'digest': _digest(result)}
