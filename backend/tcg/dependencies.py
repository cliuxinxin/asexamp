"""Immutable input manifests and versioned, item-level artifact relations."""
import hashlib
import json

from .schemas import DomainError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def manifest(store, artifact_ids=(), source_ids=(), profile_ids=(), run_id=None):
    with store.transaction():
        value = {'version': 1, 'artifacts': [], 'sources': [], 'profiles': []}
        artifact_refs = {(ref, 0) if isinstance(ref, str) else (ref['id'], ref['revision']): ref for ref in artifact_ids}
        for (aid, _), ref in sorted(artifact_refs.items()):
            a = store.get('artifact', aid) if isinstance(ref, str) else store.revision(aid, ref['revision'])
            value['artifacts'].append({'id': aid, 'revision': a['revision'], 'project_id': a['project_id'],
                                       'digest': digest({k: v for k, v in a.items() if k != '_visible'})})
        source_refs = {(ref, 0) if isinstance(ref, str) else (ref['id'], ref['version']): ref for ref in source_ids}
        for (sid, _), ref in sorted(source_refs.items()):
            snapshot = store.source_snapshot(sid, None if isinstance(ref, str) else ref['version'])
            source = snapshot['source']
            value['sources'].append({'id': sid, 'version': source.get('version', 1), 'project_id': source['project_id'],
                                     'role': source['role'], 'digest': digest(snapshot)})
        for pid in sorted(set(profile_ids)):
            p = store.get('profile', pid)
            value['profiles'].append({'id': pid, 'version': p['version'], 'project_id': p['project_id'],
                                      'digest': digest(p), 'snapshot': p})
        if run_id:
            run = store.run(run_id)
            request = run.get('_request', {})
            scope = {key: request.get(key) for key in ('selected_ids', 'artifact_id', 'source_ids', 'scope', 'additional_rules')}
            value['run'] = {'id': run_id, 'project_id': run['project_id'], 'digest': digest({
                'scope': scope, 'effective_scope': run.get('scope'), 'profile': run.get('_profile'), 'source_ids': run.get('_source_ids'),
                'source_roles': run.get('_source_roles'), 'input_version': run.get('input_version', run.get('_input_version', 0)),
                'instruction_version': run.get('_instruction_version', 0)})}
        value['digest'] = digest(value)
        return value


def assert_manifest(store, value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise DomainError('依赖清单无效，请重新生成', 409)
    try:
        current = manifest(store, [a['id'] for a in value['artifacts']], [s['id'] for s in value['sources']],
                           [p['id'] for p in value['profiles']], value.get('run', {}).get('id'))
    except (KeyError, TypeError, DomainError):
        raise DomainError('依赖已改变，请重新生成', 409) from None
    if current != value:
        raise DomainError('依赖已改变，请重新生成', 409)


def _relations(artifact):
    lineage = (artifact.get('report') or {}).get('lineage') or {}
    prefix = {'cases': 'scenario', 'scenarios': 'analysis'}.get(artifact['type'])
    if not prefix or not lineage.get(prefix + '_artifact_id'):
        return []
    versions = lineage.get(prefix + '_revisions') or {}
    result = []
    for item in artifact['items']:
        parents = [item.get('scenario_id')] if prefix == 'scenario' else item.get('requirement_ids', [])
        for parent_id in parents:
            if parent_id:
                result.append({'relation': 'derived_from', 'artifact_id': lineage[prefix + '_artifact_id'],
                               'revision': versions.get(parent_id, lineage.get(prefix + '_revision')),
                               'item_id': parent_id, 'dependent_item_id': item['id']})
    return result


def record_artifact(store, artifact):
    with store.transaction():
        value = {'relations': _relations(artifact), 'dependencies': artifact.get('_dependencies')}
        existing = store.db.execute('SELECT payload FROM artifact_dependencies WHERE artifact_id=? AND revision=?',
                                    (artifact['id'], artifact['revision'])).fetchone()
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if existing and existing[0] != payload:
            raise DomainError('成果依赖版本不可修改', 409)
        store.db.execute('INSERT OR IGNORE INTO artifact_dependencies VALUES(?,?,?)', (artifact['id'], artifact['revision'], payload))


def artifact_status(store, artifact):
    if isinstance(artifact, str):
        artifact = store.get('artifact', artifact)
    with store.transaction():
        row = store.db.execute('SELECT payload FROM artifact_dependencies WHERE artifact_id=? AND revision=?',
                               (artifact['id'], artifact['revision'])).fetchone()
        data = json.loads(row[0]) if row else {'relations': _relations(artifact), 'dependencies': artifact.get('_dependencies')}
        links, affected = [], set()
        for relation in data['relations']:
            state = 'current'
            try:
                current = store.get('artifact', relation['artifact_id'])
                old = store.revision(relation['artifact_id'], relation['revision'])
                if current['project_id'] != artifact['project_id'] or current['chat_id'] != artifact['chat_id']:
                    state = 'needs_review'
                else:
                    old_item = next((i for i in old['items'] if i['id'] == relation['item_id']), None)
                    current_item = next((i for i in current['items'] if i['id'] == relation['item_id']), None)
                    if old_item is None or current_item != old_item:
                        state = 'stale'
            except DomainError:
                state = 'needs_review'
            if state != 'current':
                affected.add(relation['dependent_item_id'])
            links.append({**relation, 'status': state})
        state = 'stale' if any(r['status'] == 'stale' for r in links) else 'needs_review' if affected else 'current'
        deps = data.get('dependencies')
        if deps:
            # Parent row precision is represented by relations; other inputs remain conservative.
            try:
                remaining = {**deps, 'artifacts': [a for a in deps.get('artifacts', [])
                                                  if a['id'] not in {artifact['id']} | {r['artifact_id'] for r in links}]}
                remaining['digest'] = digest({k: v for k, v in remaining.items() if k != 'digest'})
                assert_manifest(store, remaining)
            except DomainError:
                if state == 'current':
                    state = 'needs_review'
        return {'artifact_id': artifact['id'], 'revision': artifact['revision'], 'status': state,
                'affected_item_ids': sorted(affected), 'relations': links}


def impact(store, artifact_id, selected_ids=None):
    with store.transaction():
        target = store.get('artifact', artifact_id)
        selected = set(selected_ids) if selected_ids is not None else {i['id'] for i in target['items']}
        if selected_ids is not None and not selected <= {i['id'] for i in target['items']}:
            raise DomainError('所选条目不属于当前成果')
        affected, candidates = [], []
        for artifact in store.list('artifact', project_id=target['project_id']):
            if artifact['id'] == artifact_id or artifact['chat_id'] != target['chat_id']:
                continue
            status = artifact_status(store, artifact)
            links = [r for r in status['relations'] if r['artifact_id'] == artifact_id and
                     (selected_ids is None or r['item_id'] in selected)]
            if links:
                affected.append({**status, 'affected_item_ids': sorted({r['dependent_item_id'] for r in links}), 'relations': links})
            elif artifact['type'] in ('scenarios', 'cases'):
                candidates.append({'artifact_id': artifact['id'], 'status': 'needs_review', 'reason': '需核查范围内潜在影响'})
        return {'artifact_id': artifact_id, 'revision': target['revision'], 'selected_ids': sorted(selected),
                'affected': affected, 'candidates': candidates, 'partial': True}
