"""Incremental Profile tools that stage the same version-bound approval as uploads."""
import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .case_fields import MANUAL_FIELDS
from .profile_changes import _proposal, change_summary, config_changes
from .schemas import DEFAULT_PROFILE, DomainError, profile_config
from .storage import now, public, uid


class ProfileColumnEdit(BaseModel):
    """Only supplied attributes change; an existing column keeps its other settings."""
    model_config = ConfigDict(extra='forbid')
    field: str = Field(min_length=1, max_length=200)
    header: str | None = None
    definition: str | None = None
    value_source: Literal['ai', 'manual', 'derived', 'default'] | None = None
    required: bool | None = None
    default_value: Any = None


PREFERENCE_KEYS = (set(DEFAULT_PROFILE) | {'template_rules'}) - {'excel_columns', 'scenario_excel_columns'}


def _base_config(store, chat, current):
    pending = chat.get('_native_template_prompt')
    if pending and pending.get('profile_id') == current['id']:
        return _proposal(store, chat['id'], pending['id'])[3]
    return copy.deepcopy(current['config'])


def read_profile(store, chat_id, current):
    """Include saved pending values so follow-up edits can refine a prior proposal."""
    with store.transaction():
        chat = store.get('chat', chat_id)
        if current['project_id'] != chat['project_id']:
            raise DomainError('Profile 不属于当前项目', 404)
        pending = chat.get('_native_template_prompt')
        result = {'profile': public(current)}
        if pending and pending.get('profile_id') == current['id']:
            result.update(pending_config=_base_config(store, chat, current), prompt_id=pending['id'])
        return result


def _edit_columns(config, kind, upserts, removals, order):
    key = 'scenario_excel_columns' if kind == 'scenarios' else 'excel_columns'
    columns = copy.deepcopy(config[key])
    known = {column['field'] for column in columns}
    if len(removals) != len(set(removals)) or not set(removals) <= known:
        raise DomainError('删除列需使用现有且不重复的 field；可先查看当前 Profile')
    patches = [edit.model_dump(exclude_unset=True, exclude_none=True)
               if isinstance(edit, ProfileColumnEdit) else dict(edit) for edit in upserts]
    fields = [edit.get('field') for edit in patches]
    if len(fields) != len(set(fields)) or set(fields) & set(removals):
        raise DomainError('同一字段不能重复修改，或同时修改和删除')
    columns = [column for column in columns if column['field'] not in removals]
    for patch in patches:
        field = patch['field']
        if not field.strip():
            raise DomainError('列 field 不能为空')
        existing = [column for column in columns if column['field'] == field]
        if existing:
            for column in existing:
                column.update(copy.deepcopy(patch))
        else:
            columns.append({'header': field, **copy.deepcopy(patch)})
        # Execution fields are never synthesized from requirements.
        for column in columns:
            if column['field'] != field or kind != 'cases':
                continue
            manual_name = any(re.sub(r'[\s_-]', '', label).lower() in MANUAL_FIELDS
                              for label in (field, column.get('header', '')))
            default_status = field.lower() == 'status' and 'value_source' not in column
            if (manual_name or default_status) and column.get('value_source') != 'default':
                column.update(value_source='manual', required=False)
    if order is not None:
        if len(order) != len(columns) or len(order) != len(set(order)) or set(order) != {c['field'] for c in columns}:
            raise DomainError('column_order 必须包含修改后所有列的 field，且每列恰好一次')
        by_field = {column['field']: column for column in columns}
        columns = [by_field[field] for field in order]
    config[key] = columns


def propose_profile_edit(store, chat_id, current, *, kind='cases', upsert_columns=(),
                         remove_columns=(), column_order=None, preferences=None, summary=''):
    """Stage trusted incremental tool arguments; approving is a separate user turn."""
    if kind not in ('cases', 'scenarios'):
        raise DomainError('列类型需为 cases 或 scenarios')
    preferences = preferences or {}
    if not set(preferences) <= PREFERENCE_KEYS:
        raise DomainError('不支持直接修改这些 Profile 设置：' + '、'.join(sorted(set(preferences) - PREFERENCE_KEYS))
                          + '。格式样例请使用保存样例功能；列请通过 upsert_columns 修改。')
    if not upsert_columns and not remove_columns and column_order is None and not preferences:
        raise DomainError('请说明要修改的列或 Profile 设置')
    with store.transaction():
        chat = store.get('chat', chat_id)
        latest = store.get('profile', current['id'])
        if latest['project_id'] != chat['project_id']:
            raise DomainError('Profile 不属于当前项目', 404)
        if latest['version'] != current['version']:
            raise DomainError('Profile 已改变，请查看当前配置后重新提出修改', 409)
        config = _base_config(store, chat, current)
        if upsert_columns or remove_columns or column_order is not None:
            _edit_columns(config, kind, upsert_columns, remove_columns, column_order)
        config.update(copy.deepcopy(preferences))
        config = profile_config(config)
        changes = config_changes(current['config'], config)
        if not changes:
            store.put('chat', {**chat, '_native_template_prompt': None})
            return {'status': 'succeeded', 'message': change_summary(changes), 'profile': public(current)}
        summary = summary.strip() or change_summary(changes)
        suggestion = {'id': uid('tmpl_'), 'project_id': chat['project_id'], 'chat_id': chat_id,
            'created_at': now(), 'source_ids': [], 'template_kinds': [kind],
            'config': config, 'summary': summary, 'profile_id': current['id'],
            'base_profile_version': current['version'], '_proposal_type': 'direct_profile'}
        store.put('template', suggestion)
        pending = {'id': 'template:' + suggestion['id'] + ':' + str(current['version']),
            'kind': 'profile', 'title': '查看并确认 Profile 更改', 'type': 'profile',
            'template_ids': [suggestion['id']], 'profile_id': current['id'],
            'expected_version': current['version'], 'summary': summary,
            'change_summary': change_summary(changes),
            'message': summary + ('\n' + change_summary(changes) if summary != change_summary(changes) else '') +
                '\n可从输入框上方“查看 Profile 更改”逐项查看并确认，也可以继续说明修改意见。'}
        store.put('chat', {**chat, '_native_template_prompt': pending})
        return {'status': 'needs_confirmation', 'message': pending['message'], 'pending': [pending],
                'proposal': {key: suggestion[key] for key in ('id', 'summary', 'template_kinds')}}
