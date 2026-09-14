"""Version-bound case columns, Profile synchronization, and requested Excel receipts."""
import base64
import copy
import hashlib
import json

from .case_fields import template_check, template_columns
from .documents import export_artifact
from .profile_edits import ProfileColumnEdit, _edit_columns, propose_profile_edit
from .schemas import DomainError, profile_config
from .storage import now, uid


CORE_FIELDS = {'id', 'title', 'scenario_id', 'requirement_ids', 'type', 'priority',
    'preconditions', 'steps', 'expected', 'expected_result', 'expected_results', 'refs', 'report',
    'source_ids', 'source_hash', 'evidence', 'profile', 'run_id', 'project_id', 'chat_id',
    'revision', 'created_at', 'updated_at', 'template_field_notes'}


def _patches(upserts):
    from pydantic import ValidationError
    if not isinstance(upserts, (list, tuple)):
        raise DomainError('新增列必须为包含 field、header 的数组')
    try:
        result = [(v if isinstance(v, ProfileColumnEdit) else ProfileColumnEdit.model_validate(v))
                  .model_dump(exclude_unset=True, exclude_none=True) for v in upserts]
    except ValidationError as exc:
        raise DomainError('列定义需要有效的 field、header 与填写方式') from exc
    if any(not p['field'].strip() for p in result):
        raise DomainError('列 field 不能为空')
    return result


def _field_list(values, label):
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise DomainError(label + '必须为非空字段名组成的数组')
    return list(values)


def column_plan(artifact, current, upserts=(), removals=(), hidden=(), order=None):
    if artifact['type'] != 'cases':
        raise DomainError('请指定当前测试用例成果')
    patches = _patches(upserts)
    removals, hidden = _field_list(removals, '删除列'), _field_list(hidden, '隐藏列')
    if order is not None:
        order = _field_list(order, '列顺序')
    fields = [p['field'] for p in patches]
    exportable_core = {'id', 'title', 'scenario_id', 'requirement_ids', 'type', 'priority', 'preconditions', 'steps', 'expected', 'refs'}
    if any(field.startswith('_') or field in CORE_FIELDS - exportable_core for field in fields):
        raise DomainError('列设置不能重写核心结构或内部字段；核心列可使用 hide_columns 隐藏导出')
    if any(f.startswith('_') or f in CORE_FIELDS for f in removals):
        raise DomainError('不能删除用例核心字段；请使用 hide_columns 仅从导出模板隐藏')
    if (len(removals) != len(set(removals)) or len(hidden) != len(set(hidden)) or
            set(removals) & set(hidden) or set(fields) & (set(removals) | set(hidden))):
        raise DomainError('同一列不能重复或同时新增、删除、隐藏')
    config = copy.deepcopy(current['config'])
    configured = {c['field'] for c in config['excel_columns']}
    actual = {f for r in artifact['items'] for f in r}
    if not set(removals) <= configured | actual or not set(hidden) <= configured:
        raise DomainError('请使用当前用例或 Profile 中的列名')
    _edit_columns(config, 'cases', patches, [f for f in [*removals, *hidden] if f in configured], order)
    config = profile_config(config)
    columns = {c['field']: c for c in template_columns(config)}
    return {'upsert_columns': patches, 'remove_columns': list(removals), 'hide_columns': list(hidden),
        'column_order': order, 'columns': [columns[p['field']] for p in patches],
        'config': config, 'profile_id': current['id'], 'profile_version': current['version']}


async def prepare_case_columns(business, artifact, current, *, upserts=(), removals=(), hidden=(),
                               order=None, instruction='', export_after_approval=False):
    plan = column_plan(artifact, current, upserts, removals, hidden, order)
    ai = [c for c in plan['columns'] if c['value_source'] == 'ai' and c['field'] not in CORE_FIELDS
          and any(c['field'] not in row for row in artifact['items'])]
    if ai:
        proposal = await business.revise(artifact, instruction=instruction + '\n仅填写下列用例字段，保留稳定编号和其他字段；'
            '根据当前证据填写，没有依据时留空并说明，不编造业务数据。列定义：' +
            json.dumps(ai, ensure_ascii=False), preview=True)
    else:
        proposal = await business.revise(artifact, new_values={}, preview=True)
    generated = {r['id']: r for r in proposal['items']}
    rows = copy.deepcopy(artifact['items'])
    for row in rows:
        output = generated.get(row['id'], {})
        for column in plan['columns']:
            field = column['field']
            # Column changes do not grant permission to rewrite existing content.
            if field in row or field in CORE_FIELDS:
                continue
            source = column['value_source']
            row[field] = (copy.deepcopy(column.get('default_value')) if source == 'default'
                          else copy.deepcopy(output.get(field, '')) if source == 'ai' else '')
        for field in removals:
            row.pop(field, None)
            row.get('_template_field_notes', {}).pop(field, None)
    proposal['items'] = rows
    proposal['report'] = copy.deepcopy(artifact.get('report', {}))
    proposal['report']['template_check'] = template_check(plan['config'], rows)
    proposal['report']['table_columns'] = copy.deepcopy(plan['config']['excel_columns'])
    proposal['column_change'] = {**plan, 'export_after_approval': bool(export_after_approval)}
    return proposal


def freeze_case_export(store, artifact, current, export_id=None):
    value = copy.deepcopy(artifact)
    value['_profile'] = copy.deepcopy(current['config'])
    content = export_artifact(value)
    record = {'id': export_id or uid('exp_'), 'chat_id': value['chat_id'], 'project_id': value['project_id'],
        'created_at': now(), 'name': 'tcg_cases_v' + str(value['revision']) + '.xlsx',
        'artifact_id': value['id'], 'revision': value['revision'], 'profile_id': current['id'],
        'profile_version': current['version'], '_bytes': base64.b64encode(content).decode(),
        'sha256': hashlib.sha256(content).hexdigest()}
    store.put('frozen_export', record)
    return {'type': 'files', 'files': [{k: record[k] for k in
        ('name', 'artifact_id', 'revision', 'profile_id', 'profile_version')} | {'url': '/api/exports/' + record['id']}]}


def stage_column_profile(store, artifact, plan):
    """Called only after the user's case edit has been saved."""
    with store.transaction():
        current = store.get('profile', plan['profile_id'])
        if current['version'] != plan['profile_version']:
            raise DomainError('Profile 已改变，请重新查看用例列修改预览', 409)
        configured = {c['field'] for c in current['config']['excel_columns']}
        removed = [f for f in [*plan['remove_columns'], *plan.get('hide_columns', [])] if f in configured]
        result = propose_profile_edit(store, artifact['chat_id'], current,
            upsert_columns=plan['upsert_columns'], remove_columns=removed, column_order=plan.get('column_order'),
            summary='用例列已更新，请核对同步到 Profile 的列更改。') if (
                plan['upsert_columns'] or removed or plan.get('column_order') is not None) else {
                    'status': 'succeeded', 'message': '用例列已更新，导出模板无需修改。', 'parts': []}
        pending = result.get('pending', [])
        if pending:
            prompt = pending[0]
            binding = {'artifact_id': artifact['id'], 'revision': artifact['revision'],
                'profile_id': current['id'], 'profile_version': current['version'],
                'column_config': copy.deepcopy(plan['config']['excel_columns'])}
            prompt['case_binding'] = binding
            if plan.get('export_after_approval'):
                request_export_after_profile(store, artifact['chat_id'], prompt)
                result['message'] = prompt['message']
            chat = store.get('chat', artifact['chat_id'])
            store.put('chat', {**chat, '_native_template_prompt': prompt})
        elif plan.get('export_after_approval'):
            result['parts'] = [freeze_case_export(store, artifact, current)]
            result['message'] = '用例列已更新，已按当前模板导出 Excel。'
        return result


def request_export_after_profile(store, chat_id, pending):
    """Bind a later export request to the exact already-presented Profile and case revision."""
    if pending.get('deferred_export_id'):
        return
    binding = pending.get('case_binding')
    if not binding:
        raise DomainError('请先查看用例列的同步建议后再导出', 409)
    chat = store.get('chat', chat_id)
    request = {'id': uid('dex_'), 'chat_id': chat_id, 'project_id': chat['project_id'],
        'prompt_id': pending['id'], 'template_ids': pending['template_ids'], **copy.deepcopy(binding),
        'status': 'pending', 'created_at': now()}
    store.put('deferred_export', request)
    pending['deferred_export_id'] = request['id']
    pending['message'] += '\n确认这些列更改后，将自动导出本次修改后的用例。'
    store.put('chat', {**chat, '_native_template_prompt': pending})


def rebind_case_profile(store, chat, previous, pending, config):
    """A refinement inherits the saved case revision and the user's existing export request."""
    if (not previous or previous.get('profile_id') != pending['profile_id'] or
            not previous.get('case_binding')):
        return
    pending['case_binding'] = {**copy.deepcopy(previous['case_binding']),
        'column_config': copy.deepcopy(config['excel_columns'])}
    request_id = previous.get('deferred_export_id')
    if not request_id:
        return
    request = store.get('deferred_export', request_id)
    if (request.get('chat_id') != chat['id'] or request.get('project_id') != chat['project_id'] or
            request.get('prompt_id') != previous['id'] or request.get('template_ids') != previous['template_ids']):
        raise DomainError('导出请求与当前列同步建议不一致，请重新查看建议', 409)
    if request.get('status') != 'pending':
        return
    binding_history = list(request.get('binding_history', []))
    binding_history.append({'prompt_id': request['prompt_id'], 'template_ids': request['template_ids'],
                            'column_config': copy.deepcopy(request['column_config'])})
    store.put('deferred_export', {**request, **copy.deepcopy(pending['case_binding']),
        'prompt_id': pending['id'], 'template_ids': pending['template_ids'],
        'binding_history': binding_history, 'updated_at': now()})
    pending['deferred_export_id'] = request_id
    pending['message'] += '\n已保留之前的导出请求；确认新建议后，将自动导出绑定版本的用例。'


def validate_case_binding(store, chat, pending, proposed):
    binding = pending.get('case_binding')
    if not binding:
        return
    artifact = store.get('artifact', binding['artifact_id'])
    if (artifact['chat_id'] != chat['id'] or artifact['project_id'] != chat['project_id'] or
            artifact['type'] != 'cases' or artifact['revision'] != binding['revision']):
        raise DomainError('用例已改变，请重新查看列修改并同步 Profile，尚未导出旧版本', 409)
    if proposed['excel_columns'] != binding['column_config']:
        raise DomainError('列同步建议已改变，请重新查看 Profile 更改，尚未导出', 409)


def complete_deferred_export(store, chat, pending, updated, keys):
    request_id = pending.get('deferred_export_id')
    if not request_id:
        return []
    request = store.get('deferred_export', request_id)
    if (request['chat_id'] != chat['id'] or request['project_id'] != chat['project_id'] or
            request['prompt_id'] != pending['id'] or request['template_ids'] != pending['template_ids']):
        raise DomainError('导出请求与本次列确认不一致', 409)
    if request['status'] != 'pending':
        return copy.deepcopy(request.get('parts', [])) if request['status'] == 'completed' else []
    if 'excel_columns' not in keys:
        store.put('deferred_export', {**request, 'status': 'cancelled', 'reason': 'column_change_not_approved'})
        return []
    artifact = store.get('artifact', request['artifact_id'])
    parts = [freeze_case_export(store, artifact, updated, export_id='exp_' + request['id'])]
    store.put('deferred_export', {**request, 'status': 'completed', 'parts': parts,
        'applied_profile_version': updated['version'], 'completed_at': now()})
    return parts


def cancel_deferred_export(store, pending):
    if pending and pending.get('deferred_export_id'):
        request = store.get('deferred_export', pending['deferred_export_id'])
        if request['status'] == 'pending':
            store.put('deferred_export', {**request, 'status': 'cancelled', 'reason': 'user_discarded'})


def manual_column_plan(store, before, column_changes, profile_id=None):
    if (not isinstance(column_changes, dict) or set(column_changes) - {'added', 'removed'}):
        raise DomainError('列修改必须包含 added 新增列数组和 removed 删除字段数组')
    added = _patches(column_changes.get('added', []))
    removed = _field_list(column_changes.get('removed', []), '删除列')
    chat = store.get('chat', before['chat_id'])
    chosen = profile_id or chat.get('profile_id')
    current = store.get('profile', chosen) if chosen else store.list('profile', project_id=chat['project_id'])[0]
    if current['project_id'] != chat['project_id']:
        raise DomainError('Profile 不属于当前项目', 404)
    added = [{**patch, 'value_source': patch.get('value_source', 'manual'), 'required': False} for patch in added]
    return column_plan(before, current, added, removed)


def attach_manual_column_sync(store, artifact, before, column_changes, profile_id=None):
    return stage_column_profile(store, artifact,
        manual_column_plan(store, before, column_changes, profile_id))
