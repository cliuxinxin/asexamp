"""Single short-transaction artifact commit boundary for Run and Turn callers."""
import copy
import json

from . import dependencies as deps
from .schemas import DomainError, validate_items


def cancel_command(store, command_id):
    with store.transaction():
        store.db.execute('INSERT OR IGNORE INTO cancelled_commands VALUES(?)', (command_id,))


def _receipt(store, command_id, artifact_id=None, run_id=None):
    if not command_id:
        return None
    row = store.db.execute('SELECT artifact_id,run_id,payload FROM operation_receipts WHERE command_id=?', (command_id,)).fetchone()
    if row:
        if (artifact_id and row['artifact_id'] != artifact_id) or row['run_id'] != run_id:
            raise DomainError('命令已用于其他操作', 409)
        return json.loads(row['payload'])
    if store.db.execute('SELECT 1 FROM cancelled_commands WHERE command_id=?', (command_id,)).fetchone():
        raise DomainError('命令已取消', 409)
    return None


def _provenance(value, artifact_id):
    value = copy.deepcopy(value)
    value['artifacts'] = [a for a in value.get('artifacts', []) if a['id'] != artifact_id]
    value['digest'] = deps.digest({k: v for k, v in value.items() if k != 'digest'})
    return value


def _assert_project(value, project_id):
    for group in ('artifacts', 'sources', 'profiles'):
        if any(ref.get('project_id') != project_id for ref in value.get(group, [])):
            raise DomainError('依赖不可跨项目使用')
    if value.get('run') and value['run'].get('project_id') != project_id:
        raise DomainError('依赖不可跨项目使用')


def _save(store, value, reason, diff, command_id, run_id):
    from .storage import dump, now
    store.db.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?)',
                     (value['id'], value['revision'], dump(value), now(), reason, dump(diff)))
    store.put('artifact', value)
    deps.record_artifact(store, value)
    store.audit(value['id'], reason, {'revision': value['revision'], 'diff': diff, 'run_id': run_id, 'command_id': command_id})
    if command_id:
        store.db.execute('INSERT INTO operation_receipts VALUES(?,?,?,?)', (command_id, value['id'], run_id, dump(value)))
    return copy.deepcopy(value)


def create_artifact(store, run_id, key, kind, title, items, report=None, *, command_id=None, dependencies=None, provenance=None):
    from .storage import now, uid
    from .workspace_coverage import creation_report
    command_id = command_id or 'artifact:' + run_id + ':' + key
    with store.transaction():
        receipt = _receipt(store, command_id, run_id=run_id)
        if receipt is not None:
            return receipt
        store.assert_running(run_id)
        existing = store.cache_get(run_id, key)
        if existing:
            return store.revision(existing['id'], existing.get('revision', 1))
        run = store.run(run_id)
        if dependencies is None:
            dependencies = run.get('_commit_guards', {}).get(kind)
        if dependencies is not None:
            deps.assert_manifest(store, dependencies)
            _assert_project(dependencies, run['project_id'])
        validate_items(kind, items, {e['id']: e for e in store.evidence(run['_source_ids'], run.get('_source_roles'))})
        value = {'id': uid('art_'), 'chat_id': run['chat_id'], 'project_id': run['project_id'], 'type': kind,
                 'title': title, 'revision': 1, 'items': copy.deepcopy(items), 'created_at': now(),
                 '_source_ids': run['_source_ids'], '_source_roles': run.get('_source_roles', {}),
                 '_profile': run['_profile'], '_visible': False}
        generated_report = creation_report(store, run, kind, report)
        if report is not None or generated_report:
            value['report'] = generated_report
        if provenance is None:
            provenance = run.get('_consumed_inputs', {}).get(kind)
        if provenance is not None:
            _assert_project(provenance, run['project_id'])
        value['_dependencies'] = _provenance(provenance if provenance is not None else deps.manifest(store, source_ids=run['_source_ids']), value['id'])
        if dependencies is not None:
            value['_write_dependencies'] = copy.deepcopy(dependencies)
        result = _save(store, value, 'generated', {'added': [i['id'] for i in items], 'updated': [], 'deleted': []}, command_id, run_id)
        store.cache_set(run_id, key, {'id': value['id'], 'revision': 1})
        return result


def revise_artifact(store, artifact_id, expected_revision, items, reason='manual_edit', run_id=None,
                    cache_key=None, report=None, *, source_ids=None, source_roles=None, command_id=None,
                    dependencies=None, fields=None, provenance=None):
    if cache_key and not run_id:
        raise DomainError('缓存提交需要运行任务')
    command_id = command_id or ('revision:' + run_id + ':' + cache_key if cache_key else None)
    with store.transaction():
        receipt = _receipt(store, command_id, artifact_id, run_id)
        if receipt is not None:
            return receipt
        if run_id:
            store.assert_running(run_id)
            if cache_key:
                cached = store.cache_get(run_id, cache_key)
                if cached:
                    return store.revision(artifact_id, cached.get('revision', expected_revision + 1))
        previous = store.get('artifact', artifact_id)
        if reason != 'workspace_action' and getattr(store, '_workspace_action_tokens', {}).get(previous['chat_id']):
            raise DomainError('项目成果正在预览或应用修改，请稍候', 409)
        if previous['revision'] != expected_revision:
            raise DomainError('Artifact 已更新，请刷新后重试', 409)
        if dependencies is not None:
            deps.assert_manifest(store, dependencies)
            _assert_project(dependencies, previous['project_id'])
        sources = list(previous['_source_ids'])
        roles = dict(previous.get('_source_roles', {}))
        if run_id:
            run = store.run(run_id)
            if run['project_id'] != previous['project_id'] or run['chat_id'] != previous['chat_id']:
                raise DomainError('成果不属于当前运行任务')
            sources.extend(run['_source_ids'])
            roles.update(run.get('_source_roles', {}))
        if source_ids is not None:
            if not isinstance(source_ids, (list, tuple)) or not all(isinstance(s, str) for s in source_ids):
                raise DomainError('source_ids 必须为数组')
            sources.extend(source_ids)
        sources = list(dict.fromkeys(sources))
        for sid in sources:
            source = store.get('source', sid)
            if source['project_id'] != previous['project_id']:
                raise DomainError('来源不可跨项目使用')
        if source_roles is not None:
            if not isinstance(source_roles, dict) or not set(source_roles) <= set(sources):
                raise DomainError('来源用途不属于当前范围')
            roles.update(source_roles)
        evidence = {e['id']: e for e in store.evidence(sources, roles)}
        validate_items(previous['type'], items, evidence)
        before, after = {i['id']: i for i in previous['items']}, {i['id']: i for i in items}
        diff = {'added': [i for i in after if i not in before], 'deleted': [i for i in before if i not in after],
                'updated': [i for i in after if i in before and before[i] != after[i]]}
        result = {**previous, 'items': copy.deepcopy(items), 'revision': expected_revision + 1,
                  '_source_ids': sources, '_source_roles': roles}
        if fields is None and previous.get('report'):
            from .agent_contracts import refreshed_report
            result['report'] = refreshed_report(previous['type'], items, previous['report'], evidence)
        if report is not None:
            if not isinstance(report, dict):
                raise DomainError('分析报告必须为对象')
            result['report'] = copy.deepcopy(report)
        if fields:
            if set(fields) - {'report', 'title'}:
                raise DomainError('仅可附加报告或标题；业务内容请创建新版本')
            if 'report' in fields and not isinstance(fields['report'], dict):
                raise DomainError('分析报告必须为对象')
            result.update(copy.deepcopy(fields))
        saved_provenance = copy.deepcopy(previous.get('_dependencies'))
        additions = copy.deepcopy(provenance) if fields is None else None
        if additions is not None:
            _assert_project(additions, previous['project_id'])
        new_sources = [sid for sid in (source_ids or []) if sid not in previous['_source_ids']]
        if additions is None and new_sources and fields is None:
            additions = deps.manifest(store, source_ids=new_sources)
        if additions is not None and saved_provenance is None:
            saved_provenance = {'version': 1, 'artifacts': [], 'sources': [], 'profiles': []}
        if saved_provenance is not None:
            # Fresh write guards do not describe which historical evidence was consumed.
            if additions:
                for group in ('artifacts', 'sources', 'profiles'):
                    identity = lambda ref: (ref['id'], ref.get('revision', ref.get('version')))
                    known = {identity(ref) for ref in saved_provenance.get(group, [])}
                    saved_provenance.setdefault(group, []).extend(copy.deepcopy(ref) for ref in additions.get(group, []) if identity(ref) not in known)
                    saved_provenance[group].sort(key=identity)
            result['_dependencies'] = _provenance(saved_provenance, artifact_id)
        else:
            # Legacy provenance remains unknown; do not claim current inputs as its origin.
            result.pop('_dependencies', None)
        if dependencies is not None:
            result['_write_dependencies'] = copy.deepcopy(dependencies)
        result = _save(store, result, reason, diff, command_id, run_id)
        from .workflow_bindings import refresh_confirmation
        refresh_confirmation(store, result)
        if cache_key:
            store.cache_set(run_id, cache_key, {'id': artifact_id, 'revision': result['revision']})
        return result
