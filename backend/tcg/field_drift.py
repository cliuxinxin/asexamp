"""Saved case fields missing from export mappings, with explicit Profile approval."""
import copy
import re

from .case_fields import MANUAL_FIELDS, is_description, template_columns
from .profile_edits import propose_profile_edit, read_profile
from .schemas import DomainError
from .storage import now, uid

# The built-in case contract is always present even in deliberately minimal exports.
# Only additional business data is proposed; lineage and runtime keys are never columns.
BUILTIN_FIELDS = {'id', 'title', 'type', 'priority', 'preconditions', 'module', 'steps',
    'expected', 'expected_result', 'expected_results', 'scenario_id', 'requirement_ids',
    'refs', 'source_ids', 'source_hash', 'evidence', 'report', 'profile', 'run_id',
    'project_id', 'chat_id', 'revision', 'created_at', 'updated_at', 'template_field_notes'}


def _canonical(field):
    return 'description' if is_description(field) else field


def detect_field_drift(artifact, config):
    """Return metadata only; zero/false/empty manual data still represents a real field."""
    if artifact.get('type') != 'cases':
        return []
    configured = {column['field'] for column in template_columns(config)}
    hints = {column['field']: column for column in template_columns(artifact.get('_profile', {}))}
    result = {}
    for item in artifact.get('items', []):
        if not isinstance(item, dict):
            continue
        for raw in item:
            if not isinstance(raw, str) or not raw.strip() or raw.startswith('_') or raw in BUILTIN_FIELDS:
                continue
            field = _canonical(raw)
            if field in configured:
                continue
            if field not in result:
                hint = hints.get(field, {})
                header = hint.get('header') or field
                manual = any(re.sub(r'[\s_-]', '', label).lower() in MANUAL_FIELDS
                             for label in (field, header)) or field.lower() == 'status'
                # Synchronization adds mappings for existing data. It never creates a new
                # requirement to synthesize absent values or a default replacing values.
                result[field] = {'field': field, 'header': header,
                    'definition': hint.get('definition', ''),
                    'value_source': 'manual' if manual else 'ai', 'required': False, 'item_ids': []}
            item_id = item.get('id')
            if item_id and item_id not in result[field]['item_ids']:
                result[field]['item_ids'].append(item_id)
    return list(result.values())


def _read_inputs(store, chat_id, artifact_id=None, revision=None, profile_id=None):
    chat = store.get('chat', chat_id)
    chosen = profile_id or chat.get('profile_id')
    profile = store.get('profile', chosen) if chosen else store.list('profile', project_id=chat['project_id'])[0]
    if profile['project_id'] != chat['project_id']:
        raise DomainError('Profile 不属于当前项目', 404)
    if artifact_id:
        current = store.get('artifact', artifact_id)
        if (not current.get('_visible') or current.get('chat_id') != chat_id or
                current.get('project_id') != chat['project_id'] or current.get('type') != 'cases'):
            raise DomainError('未找到当前对话的已发布用例成果', 404)
    else:
        candidates = [a for a in store.list('artifact', chat_id=chat_id)
                      if a.get('_visible') and a.get('type') == 'cases' and a['project_id'] == chat['project_id']]
        current = max(enumerate(candidates), key=lambda pair: (
            pair[1].get('updated_at') or pair[1].get('created_at', ''), pair[0]))[1] if candidates else None
    artifact = current
    if current and revision is not None and revision != current['revision']:
        artifact = store.revision(current['id'], revision)
    return chat, profile, current, artifact


def chat_field_drift(store, chat_id, artifact_id=None, revision=None, profile_id=None):
    with store.transaction():
        _, profile, current, artifact = _read_inputs(store, chat_id, artifact_id, revision, profile_id)
        return {'artifact_id': artifact['id'] if artifact else None,
            'revision': artifact['revision'] if artifact else None,
            'head_revision': current['revision'] if current else None,
            'profile_id': profile['id'], 'profile_version': profile['version'],
            'profile_name': profile['name'],
            'candidates': detect_field_drift(artifact, profile['config']) if artifact else []}


def propose_field_sync(store, chat_id, artifact_id, revision, head_revision, profile_id, profile_version, fields):
    """Bind the selected saved keys, then reuse the same proposal/approval as chat tools."""
    with store.transaction():
        chat, profile, current, artifact = _read_inputs(store, chat_id, artifact_id, revision, profile_id)
        if current['revision'] != head_revision:
            raise DomainError('用例已更新，请刷新后重新查看字段同步建议', 409)
        if profile['version'] != profile_version:
            raise DomainError('Profile 已改变，请刷新后重新查看字段同步建议', 409)
        candidates = {row['field']: row for row in detect_field_drift(artifact, profile['config'])}
        if not fields or len(fields) != len(set(fields)) or not set(fields) <= candidates.keys():
            raise DomainError('请选择当前用例中尚未配置导出且不重复的字段')
        # Preserve any in-progress user edits to an already-staged column.
        pending_config = read_profile(store, chat_id, profile).get('pending_config', {})
        pending_fields = {_canonical(column['field']) for column in pending_config.get('excel_columns', [])}
        upserts = [{key: copy.deepcopy(value) for key, value in candidates[field].items() if key != 'item_ids'}
                   for field in fields if field not in pending_fields]
        labels = '、'.join(candidates[field]['header'] for field in fields)
        summary = '拟将现有用例字段“' + labels + '”加入导出模板。仅更新列配置，保留用例内容。'
        if upserts:
            result = propose_profile_edit(store, chat_id, profile, upsert_columns=upserts, summary=summary)
        else:
            pending = store.get('chat', chat_id).get('_native_template_prompt')
            result = {'status': 'needs_confirmation', 'message': pending['message'], 'pending': [pending]}
        audit = {'artifact_id': artifact_id, 'revision': revision, 'head_revision': head_revision,
                 'profile_id': profile_id, 'profile_version': profile_version, 'fields': fields}
        store.audit(chat_id, 'field_sync_proposed', audit)
        message_id = uid('field_sync_')
        for role, content in [('user', '查看将“' + labels + '”同步到导出模板的更改。'),
                              ('assistant', result['message'])]:
            store.put('message', {'id': message_id + ':' + role, 'chat_id': chat_id,
                'project_id': chat['project_id'], 'role': role, 'content': content, 'created_at': now(),
                'metadata': {'field_sync': audit}})
        return result
