"""Server-built Profile diffs and version-bound, selective template approval."""
import copy

from .project_context import merge_template_config
from .schemas import DomainError, profile_config
from .storage import now, public, uid


LABELS = {
    'language': '输出语言',
    'scenario_level': '场景详细程度',
    'scope': '默认测试范围',
    'excel_columns': '用例列、顺序与填写规则',
    'excel_layout': '用例导出布局',
    'sheet_name': '用例工作表名称',
    'filename_pattern': '用例文件命名',
    'template_rules': '模板写作规则',
    'case_level': '用例详细程度',
    'case_types': '用例类型',
    'additional_rules': '附加规则',
    'scenario_excel_columns': '场景列与顺序',
    'scenario_sheet_name': '场景工作表名称',
    'scenario_filename_pattern': '场景文件命名',
}


def config_changes(before, after):
    """Keep column lists atomic so order and per-column policies stay together."""
    return [{'key': key, 'label': LABELS.get(key, key),
             'before': copy.deepcopy(before.get(key)), 'after': copy.deepcopy(after.get(key)),
             'before_present': key in before, 'after_present': key in after}
            for key in dict.fromkeys([*before, *after])
            if (key in before) != (key in after) or before.get(key) != after.get(key)]


def change_summary(changes):
    return ('拟更新 ' + str(len(changes)) + ' 项：' + '、'.join(row['label'] for row in changes) + '。'
            if changes else '与当前 Profile 一致，无需修改。')


def _proposal(store, chat_id, prompt_id):
    """Read and validate the stored proposal inside the caller's transaction."""
    chat = store.get('chat', chat_id)
    pending = chat.get('_native_template_prompt')
    if not pending or not prompt_id or pending.get('id') != prompt_id or pending.get('kind') != 'profile':
        raise DomainError('Profile 更改提示已改变，请查看当前建议后再确认', 409)
    current = store.get('profile', pending['profile_id'])
    if current['project_id'] != chat['project_id']:
        raise DomainError('Profile 不属于当前项目', 404)
    if current['version'] != pending['expected_version']:
        raise DomainError('Profile 已改变，请查看当前配置后重新提出更改建议', 409)
    ids = pending.get('template_ids') or []
    if not ids or len(ids) != len(set(ids)):
        raise DomainError('Profile 提示缺少有效建议，请重新提出更改', 409)
    templates, config = [], copy.deepcopy(current['config'])
    for template_id in ids:
        suggestion = store.get('template', template_id)
        if suggestion['chat_id'] != chat_id or suggestion['project_id'] != chat['project_id']:
            raise DomainError('Profile 更改建议不属于当前对话', 404)
        if suggestion.get('_applied'):
            raise DomainError('此更改已经应用，请查看当前 Profile', 409)
        if suggestion['profile_id'] != current['id'] or suggestion['base_profile_version'] != current['version']:
            raise DomainError('Profile 已改变，请查看当前配置后重新提出更改', 409)
        if suggestion.get('_proposal_type') == 'direct_profile':
            config = profile_config(copy.deepcopy(suggestion['config']))
        else:
            config, _ = merge_template_config(config, suggestion['config'], suggestion['template_kinds'])
        templates.append(suggestion)
    from .case_columns import validate_case_binding
    validate_case_binding(store, chat, pending, config)
    changes = config_changes(current['config'], config)
    summary = '\n'.join(dict.fromkeys(str(t.get('summary', '')).strip() for t in templates if t.get('summary')))
    preview = {'prompt_id': pending['id'], 'profile_id': current['id'], 'profile_name': current['name'],
        'expected_version': current['version'], 'summary': summary, 'template_ids': ids, 'changes': changes}
    return chat, current, templates, config, preview


def preview_profile_change(store, chat_id, prompt_id):
    """Preview the exact validated merge, including normalized and protected fields."""
    with store.transaction():
        return _proposal(store, chat_id, prompt_id)[-1]


def apply_profile_change(store, chat_id, prompt_id, expected_version, selected_keys=None, *, write_messages=False):
    """Use only saved proposal values; never accept a client-supplied config."""
    with store.transaction():
        chat, current, templates, proposed, preview = _proposal(store, chat_id, prompt_id)
        if expected_version != current['version']:
            raise DomainError('预览版本与当前 Profile 版本不一致，请重新查看更改', 409)
        available = {row['key']: row for row in preview['changes']}
        keys = list(available) if selected_keys is None else selected_keys
        if (not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys)
                or len(keys) != len(set(keys)) or not set(keys) <= set(available)):
            raise DomainError('请选择当前建议中不重复且非空的更改项')
        config = copy.deepcopy(current['config'])
        for key in keys:
            if key in proposed:
                config[key] = copy.deepcopy(proposed[key])
            else:
                config.pop(key, None)
        config = profile_config(config)
        updated = store.update_profile(current['id'], current['name'], config, current['version'])
        skipped = [key for key in available if key not in keys]
        for suggestion in templates:
            store.put('template', {**suggestion, '_applied': True, '_applied_keys': keys,
                '_skipped_keys': skipped, '_applied_profile_version': updated['version']})
        from .case_columns import complete_deferred_export
        parts = complete_deferred_export(store, chat, chat['_native_template_prompt'], updated, keys)
        store.put('chat', {**chat, 'profile_id': updated['id'], '_native_template_prompt': None})
        labels = '、'.join(available[key]['label'] for key in keys)
        message = f'已应用 {len(keys)} 项 Profile 更改：{labels}。' + (
            f'其余 {len(skipped)} 项保留原配置。' if skipped else '') + '当前成果可按最新 Profile 导出，后续新任务使用此 Profile。'
        if parts:
            message += '已按确认后的列自动导出本次修改的用例。'
        audit = {'prompt_id': prompt_id, 'profile_id': current['id'], 'template_ids': preview['template_ids'],
            'base_version': current['version'], 'version': updated['version'],
            'selected_keys': keys, 'skipped_keys': skipped}
        store.audit(chat_id, 'profile_template_confirmed', audit)
        if write_messages:
            confirmation_id = uid('profile_confirmation_')
            created_at = now()
            for role, content in [('user', '确认应用所选 Profile 更改：' + labels + '。'), ('assistant', message)]:
                store.put('message', {'id': confirmation_id + ':' + role, 'chat_id': chat_id,
                    'project_id': chat['project_id'], 'role': role, 'content': content,
                    'created_at': created_at if role == 'user' else now(),
                    'metadata': {'profile_confirmation': audit, **({'turn_response': {
                        'status': 'succeeded', 'message': message, 'parts': parts, 'pending': []}}
                        if role == 'assistant' and parts else {})}})
        return {'status': 'succeeded', 'profile': public(updated), 'message': message, 'parts': parts}
