"""Immutable input manifests and versioned, item-level artifact relations."""
import hashlib
import json
import re

from .schemas import DomainError


ARTIFACT_DIGEST_SCHEME = 'immutable_revision_v1'
ARTIFACT_BUSINESS_FIELDS = ('items', 'report', '_source_ids', '_source_roles', '_profile',
    'type', 'title', 'project_id', 'chat_id', '_dependencies', '_write_dependencies')
ARTIFACT_OPERATIONAL_FIELDS = {'_visible', '_agent_target'}


class DependencyConflict(DomainError):
    """Only identifiers, versions, hashes and server-owned labels are diagnostic."""
    def __init__(self, changes, message='依赖已改变，请重新生成'):
        super().__init__(message, 409)
        self.dependency_changes = changes


def conflict_context(exc, phase, task=None, kind=None):
    if isinstance(exc, DependencyConflict):
        exc.dependency_phase = phase
        if task is not None:
            exc.dependency_task = task
        if kind is not None:
            exc.dependency_kind = kind
    return exc


def _artifact_payload(value):
    return {k: v for k, v in value.items() if k not in ARTIFACT_OPERATIONAL_FIELDS}


def _saved_snapshot(store, artifact):
    try:
        return store.revision(artifact['id'], artifact['revision'])
    except DomainError as exc:
        if exc.status != 404:
            raise
        # Old imported artifacts can predate the immutable revision table.
        return artifact


def _safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:#-]{1,200}', value) else None


def _safe_digest(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) else None


def dependency_change(category, expected, current=None, reason='content_changed'):
    current = current or {}
    version = 'revision' if category == 'artifact' else 'version'
    return {'category': category, 'id': _safe_id(expected.get('id')), 'reason': reason,
            'expected_version': expected.get(version) if type(expected.get(version)) is int else None,
            'current_version': current.get(version) if type(current.get(version)) is int else None,
            'expected_digest': _safe_digest(expected.get('digest')),
            'current_digest': _safe_digest(current.get('digest'))}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def manifest(store, artifact_ids=(), source_ids=(), profile_ids=(), run_id=None):
    with store.transaction():
        value = {'version': 1, 'artifacts': [], 'sources': [], 'profiles': []}
        artifact_refs = {(ref, 0) if isinstance(ref, str) else (ref['id'], ref['revision']): ref for ref in artifact_ids}
        for (aid, _), ref in sorted(artifact_refs.items()):
            a = store.get('artifact', aid) if isinstance(ref, str) else store.revision(aid, ref['revision'])
            snapshot = _saved_snapshot(store, a)
            if any(a.get(key) != snapshot.get(key) for key in ARTIFACT_BUSINESS_FIELDS):
                raise DependencyConflict([dependency_change('artifact', {'id': aid, 'revision': a['revision']},
                    reason='current_snapshot_inconsistent')])
            value['artifacts'].append({'id': aid, 'revision': a['revision'], 'project_id': a['project_id'],
                                       'digest': digest(_artifact_payload(snapshot)), 'digest_scheme': ARTIFACT_DIGEST_SCHEME})
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
        raise DependencyConflict([dependency_change('manifest', {}, reason='invalid_manifest')], '依赖清单无效，请重新生成')
    changes = []
    with store.transaction():
        if value.get('digest') != digest({k: v for k, v in value.items() if k != 'digest'}):
            changes.append(dependency_change('manifest', value, reason='manifest_digest_changed'))
        for group, category in (('artifacts', 'artifact'), ('sources', 'source'), ('profiles', 'profile')):
            refs = value.get(group)
            if not isinstance(refs, list):
                changes.append(dependency_change(category, {}, reason='invalid_reference'))
                continue
            for expected in refs:
                if not isinstance(expected, dict) or not _safe_id(expected.get('id')):
                    changes.append(dependency_change(category, {}, reason='invalid_reference'))
                    continue
                try:
                    kwargs = {category + '_ids': [expected['id']]}
                    current = manifest(store, **kwargs)[group][0]
                except (KeyError, TypeError, DomainError) as exc:
                    if isinstance(exc, DependencyConflict):
                        changes.extend(exc.dependency_changes)
                    else:
                        changes.append(dependency_change(category, expected, reason='unavailable'))
                    continue
                comparable = dict(current)
                version = 'revision' if category == 'artifact' else 'version'
                if category == 'artifact' and 'digest_scheme' not in expected:
                    comparable.pop('digest_scheme', None)
                    # v2.7 hashes used the mutable head. Bridge only a verified
                    # digest from this exact immutable revision or current head.
                    # A newer business revision is never accepted by this bridge.
                    if expected.get('revision') == current['revision']:
                        head = store.get('artifact', expected['id'])
                        snapshot = _saved_snapshot(store, head)
                        legacy = {digest({k: v for k, v in item.items() if k != '_visible'}) for item in (head, snapshot)}
                        if expected.get('digest') in legacy:
                            comparable['digest'] = expected['digest']
                if expected == comparable:
                    continue
                reason = 'version_changed' if expected.get(version) != current.get(version) else 'content_changed'
                change = dependency_change(category, expected, current, reason)
                if category == 'artifact' and type(expected.get('revision')) is int:
                    try:
                        old = store.revision(expected['id'], expected['revision'])
                        head = store.get('artifact', expected['id'])
                        change['changed_fields'] = [key for key in ARTIFACT_BUSINESS_FIELDS if old.get(key) != head.get(key)]
                    except DomainError:
                        pass
                changes.append(change)
        if 'run' in value:
            expected = value['run']
            if not isinstance(expected, dict) or not _safe_id(expected.get('id')):
                changes.append(dependency_change('run', {}, reason='invalid_reference'))
            else:
                try:
                    current = manifest(store, run_id=expected['id'])['run']
                    if current != expected:
                        changes.append(dependency_change('run', expected, current, 'run_inputs_changed'))
                except (KeyError, TypeError, DomainError):
                    changes.append(dependency_change('run', expected, reason='unavailable'))
        if changes:
            raise DependencyConflict(changes)


def current_manifest(store, value):
    """Canonicalize only after asserting old guards, within the same transaction."""
    with store.transaction():
        assert_manifest(store, value)
        return manifest(store, [a['id'] for a in value['artifacts']], [s['id'] for s in value['sources']],
                        [p['id'] for p in value['profiles']], value.get('run', {}).get('id'))


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
