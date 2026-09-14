"""Upgrade known legacy review positions without importing an old executor."""
import copy

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .schemas import DomainError
from .storage import now, public


REF_TYPES = {'analysis_ref': 'analysis', 'scenario_ref': 'scenarios', 'cases_ref': 'cases'}
GATE_REFS = {'clarification': 'analysis_ref', 'strategy_review': 'analysis_ref',
             'scenario_review': 'scenario_ref', 'case_draft_review': 'cases_ref',
             'case_result_review': 'cases_ref'}
PARENT_FIELDS = {'analysis_ref': 'analysis_artifact_id', 'scenario_ref': 'scenario_artifact_id'}
CACHE_REFS = {
    'analysis_ref': ('workspace:analysis_parent', 'v7:analysis:artifact', 'v6:analysis:artifact',
                     'clarified_artifact', 'v4:analysis_artifact', 'analysis_artifact'),
    'scenario_ref': ('workspace:scenario_parent', 'v4:scenarios_artifact', 'scenarios_artifact'),
    'cases_ref': ('v4:cases_artifact', 'cases_artifact'),
}


def _clean(run):
    obsolete = {'_edit_token', '_interrupt_id', '_control_hold', '_resume', '_upstream_confirmation',
                '_control_version', 'control_version', 'pause_contract'}
    return {key: value for key, value in run.items()
            if key not in obsolete and not key.startswith(('_edit_', '_boundary_', 'boundary_', '_control_'))}


async def _checkpoint_refs(store, run_id):
    path = store.directory / 'checkpoints.sqlite3'
    if not path.is_file():
        return {}
    # Reading serialized pointer values does not import a graph, replay a node,
    # consume an interrupt, or create a second runner.
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        checkpoint = await saver.aget({'configurable': {'thread_id': run_id}})
    values = (checkpoint or {}).get('channel_values', {})
    return {key: values[key] for key in REF_TYPES if isinstance(values.get(key), str)}


def _artifact(store, run, artifact_id, kind):
    value = store.get('artifact', artifact_id)
    if (value['type'] != kind or value['chat_id'] != run['chat_id']
            or value['project_id'] != run['project_id']):
        raise DomainError('旧任务的成果关联不属于同一业务分支')
    return value


def _pointers(store, run, checkpoint):
    pending = run.get('interrupt') or {}
    gate = pending.get('type')
    if gate not in GATE_REFS or not pending.get('artifact_id'):
        raise DomainError('旧任务没有可可靠恢复的成果确认位置')
    gate_key = GATE_REFS[gate]
    current = _artifact(store, run, pending['artifact_id'], REF_TYPES[gate_key])
    result = {}
    for key, artifact_id in checkpoint.items():
        result[key] = _artifact(store, run, artifact_id, REF_TYPES[key])['id']
    result[gate_key] = current['id']
    # The visible current gate wins over an earlier checkpoint's pointer. Exact
    # persisted ancestry wins over stage cache entries, never row-ID matching.
    queue = [current]
    seen = set()
    while queue:
        child = queue.pop()
        if child['id'] in seen:
            continue
        seen.add(child['id'])
        lineage = (child.get('report') or {}).get('lineage') or {}
        for key, field in PARENT_FIELDS.items():
            if lineage.get(field):
                parent = _artifact(store, run, lineage[field], REF_TYPES[key])
                if key == gate_key and parent['id'] != current['id']:
                    raise DomainError('旧任务的当前成果与上游关联存在冲突')
                result[key] = parent['id']
                queue.append(parent)
    for key, keys in CACHE_REFS.items():
        if key in result:
            continue
        for cache_key in keys:
            cached = store.cache_get(run['id'], cache_key)
            if isinstance(cached, dict) and cached.get('id'):
                result[key] = _artifact(store, run, cached['id'], REF_TYPES[key])['id']
                break
    # Some early versions saved no checkpoint pointers, but a unique artifact
    # of the required kind in the run remains an explicit persisted identity.
    for key, kind in REF_TYPES.items():
        if key in result:
            continue
        candidates = [store.get('artifact', aid) for aid in run.get('artifact_ids', [])]
        candidates = [a for a in candidates if a.get('type') == kind]
        if len(candidates) == 1:
            result[key] = _artifact(store, run, candidates[0]['id'], kind)['id']
    required = {'analysis_ref', 'scenario_ref'} if gate == 'scenario_review' else {gate_key}
    if not required <= result.keys():
        raise DomainError('旧任务缺少下一阶段所需的明确上游成果')
    for key, artifact_id in result.items():
        child = _artifact(store, run, artifact_id, REF_TYPES[key])
        lineage = (child.get('report') or {}).get('lineage') or {}
        for parent_key, field in PARENT_FIELDS.items():
            if lineage.get(field) and result.get(parent_key) != lineage[field]:
                raise DomainError('旧任务的成果属于不同分支，不能自动合并恢复')
    if gate == 'clarification' and not (current.get('report') or {}).get('questions'):
        raise DomainError('旧澄清问题未保存在需求理解中，无法可靠恢复')
    return {**result, 'current_artifact_id': current['id']}, gate


def _failed(store, run, reason, previous):
    value = _clean(store.run(run['id']))
    value.update(status='failed', stage='migration_required', error=reason + '；已有资料和成果版本已保留，请通过聊天重新开始任务。',
                 interrupt=None, interrupt_id=None,
                 migration={'status': 'restart_required', **previous, 'at': now()},
                 recovery={'category': 'migration', 'suggestions': ['查看已有成果和资料，再通过聊天重新开始任务。']})
    with store.transaction():
        store.save_run(value)
    return public(value)


async def migrate_legacy_runs(store, pipeline):
    """Return one outcome per upgraded or safely stopped legacy active run."""
    outcomes = []
    for original in store.runs():
        if original.get('graph_version') in (8, 9, 10) or original.get('migration'):
            continue
        run = copy.deepcopy(original)
        previous = {'from_graph_version': run.get('graph_version'), 'from_stage': run.get('stage')}
        if run['status'] in ('queued', 'running'):
            outcomes.append(_failed(store, run, '旧版本任务在执行途中，无法确定尚未完成步骤的安全恢复位置', previous))
            continue
        if run['status'] != 'waiting':
            cleaned = _clean(run)
            if cleaned != run:
                with store.transaction():
                    store.save_run(cleaned)
            continue
        try:
            pointers, gate = _pointers(store, run, await _checkpoint_refs(store, run['id']))
            cleaned = _clean(run)
            cleaned.update(stop_after=run.get('stop_after') or run.get('_request', {}).get('stop_after') or
                           {'review_requirement': 'analysis', 'generate_scenario': 'scenarios'}.get(run.get('intent')),
                           _source_roles=run.get('_source_roles') or {
                               sid: store.get('source', sid)['role'] for sid in run.get('_source_ids', [])})
            with store.transaction():
                store.save_run(cleaned)
            restored = await pipeline.restore_waiting(run['id'], pointers, gate)
            if restored['status'] != 'waiting' or (restored.get('interrupt') or {}).get('type') != gate:
                raise DomainError('旧确认位置未能形成对应的原生确认节点')
            current = _clean(store.run(run['id']))
            current.update(migration={'status': 'restored', **previous, 'at': now()})
            with store.transaction():
                store.save_run(current)
            outcomes.append(public(current))
        except Exception as exc:
            outcomes.append(_failed(store, run, '旧任务确认点恢复失败：' + str(exc), previous))
    return outcomes
