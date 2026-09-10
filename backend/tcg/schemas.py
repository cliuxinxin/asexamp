"""Public request schemas and business validation shared by tools and REST."""
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

INTENTS = {'review_requirement', 'generate_scenario', 'generate_case', 'review_case', 'query', 'learn_template', 'modify'}
ROLES = {'primary', 'change', 'supplement', 'clarification', 'example', 'knowledge'}
DEFAULT_PROFILE = {
    'language': '中文', 'scenario_level': 'standard', 'case_level': 'standard',
    'case_types': ['Business', 'Negative', 'Boundary'], 'additional_rules': '',
    'scope': '', 'excel_layout': 'case', 'sheet_name': 'Test Cases', 'filename_pattern':'{project}_{date}.xlsx',
    'excel_columns':[{'field':f,'header':h} for f,h in [('id','Case ID'),('title','Title'),('type','Type'),('priority','Priority'),('preconditions','Preconditions'),('steps','Steps'),('expected','Expected Result')]],
    'scenario_sheet_name':'Test Scenarios', 'scenario_filename_pattern':'{project}_scenarios_{date}.xlsx',
    'scenario_excel_columns':[{'field':f,'header':h} for f,h in [('id','Scenario ID'),('title','Title'),('description','Description'),('priority','Priority'),('requirement_ids','Requirement IDs'),('refs','Evidence Refs')]],
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status
        super().__init__(message)


MISSING = object()


class OutputValidationError(DomainError):
    """Only server-built paths/rules/types are diagnostic-safe; never values."""
    def __init__(self, message, path, expected, value=MISSING, code='type_mismatch'):
        actual = 'missing' if value is MISSING else {
            str: 'string', list: 'array', dict: 'object', type(None): 'null',
            bool: 'boolean', int: 'number', float: 'number',
        }.get(type(value), 'unknown')
        self.issue = {'code': code, 'path': path, 'expected': expected, 'actual': actual}
        super().__init__(f'{path}: {message}（期望 {expected}，实际 {actual}）')


class NameInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator('name')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()


class ChatInput(BaseModel):
    title: str = Field(default='新对话', max_length=200)


class ProfileInput(NameInput):
    config: dict[str, Any]
    expected_version: int | None = None


class TextInput(NameInput):
    text: str = Field(min_length=1, max_length=2_000_000)
    role: Literal['primary', 'change', 'supplement', 'clarification', 'example', 'knowledge'] = 'primary'


class MessageInput(BaseModel):
    profile_override: dict[str, Any] | None = None
    experience: Literal['legacy', 'agent', 'reliable'] = 'legacy'
    case_types: list[Literal['Business', 'Negative', 'Boundary', 'Security']] | None = Field(default=None, min_length=1, max_length=4)
    depth: Literal['auto', 'quick', 'standard', 'deep'] = 'auto'
    confirm_strategy: bool = True
    content: str = Field(min_length=1, max_length=100_000)
    as_requirement: bool = False
    intent: Literal['auto', 'review_requirement', 'generate_scenario', 'generate_case', 'review_case', 'query', 'learn_template', 'modify'] = 'auto'
    mode: Literal['auto', 'hitp'] = 'auto'
    profile_id: str | None = None
    source_ids: list[str] | None = None
    artifact_id: str | None = None
    selected_ids: list[str] | None = None

    @field_validator('content')
    @classmethod
    def content_not_blank(cls, value):
        if not value.strip():
            raise ValueError('消息不能为空')
        return value.strip()


class RevisionInput(BaseModel):
    report: dict[str, Any] | None = None
    expected_revision: int = Field(ge=1)
    items: list[dict[str, Any]]


class RestoreInput(BaseModel):
    revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class ResumeInput(BaseModel):
    source_ids: list[str] | None = None
    depth: Literal['quick', 'standard', 'deep'] | None = None
    answer: str | None = Field(default=None, max_length=100_000)
    approved: bool | None = None


class SettingsInput(BaseModel):
    provider: Literal['ollama', 'openai']
    base_url: str = Field(max_length=1000)
    model: str = Field(max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    clear_api_key: bool = False
    headers: dict[str, str] | None = None
    clear_headers: bool = False
    auth_mode: Literal['bearer', 'headers'] = 'bearer'
    timeout_seconds: int = Field(default=3600, ge=5, le=3600)


def scenario_template_columns(columns):
    """Validate scenario mappings without applying case-only completion policies."""
    if not isinstance(columns, list) or not columns:
        raise DomainError('scenario_excel_columns 必须是包含 field/header 的非空数组')
    forbidden = {'source_ids', 'source_hash', 'evidence', 'report', 'profile', 'run_id',
                 'project_id', 'chat_id', 'revision', 'created_at', 'updated_at'}
    for index, column in enumerate(columns):
        path = f'scenario_excel_columns[{index}]'
        if not isinstance(column, dict) or not isinstance(column.get('field'), str) or not column['field'].strip():
            raise DomainError(f'{path}.field 必须为非空字符串')
        if not isinstance(column.get('header'), str):
            raise DomainError(f'{path}.header 必须为字符串')
        if 'definition' in column and not isinstance(column['definition'], str):
            raise DomainError(f'{path}.definition 必须为文本')
        if column['field'].startswith('_') or column['field'] in forbidden:
            raise DomainError(f'{path}.field（{column["field"]}）只能映射场景字段，不能映射内部记录')
    return columns


def profile_config(config):
    if not isinstance(config, dict):
        raise DomainError('Profile config 必须为对象')
    result = {**DEFAULT_PROFILE, **config}
    # Recognition metadata belongs to the proposal report, never saved preferences.
    result.pop('template_kinds', None)
    if not isinstance(result['case_types'], list) or not all(isinstance(x, str) and x for x in result['case_types']):
        raise DomainError('case_types 必须为字符串数组')
    for field in ('language', 'scenario_level', 'case_level', 'additional_rules', 'scope', 'sheet_name', 'scenario_sheet_name'):
        if not isinstance(result[field], str):
            raise DomainError(f'{field} 必须为字符串')
    if result['excel_layout'] not in ('case', 'step'):
        raise DomainError('excel_layout 必须为 case 或 step')
    columns = result.get('excel_columns')
    if columns is not None and (not isinstance(columns,list) or not columns or any(not isinstance(c,dict) or not isinstance(c.get('field'),str) or not c['field'] or not isinstance(c.get('header'),str) for c in columns)):
        raise DomainError('excel_columns 必须是包含 field/header 的非空数组')
    if columns and any('definition' in c and not isinstance(c['definition'],str) for c in columns):
        raise DomainError('Excel 列的 definition 必须为文本')
    for column in columns or []:
        source=column.get('value_source')
        if source not in (None,'ai','manual','derived','default'):
            raise DomainError('Excel 列的 value_source 必须为 ai/manual/derived/default')
        if 'required' in column and not isinstance(column['required'],bool):
            raise DomainError('Excel 列的 required 必须为布尔值')
        if source=='derived' and column['field'] not in ('steps','expected'):
            raise DomainError('自动汇总目前支持 steps / expected；其他列请选择 AI 生成、人工填写或固定默认值')
        if column['field'] in ('steps','expected') and source not in (None,'derived'):
            raise DomainError('steps / expected 必须从用例步骤自动汇总')
        if source=='default' and ('default_value' not in column or column['default_value'] is None):
            raise DomainError('固定默认值列需要填写 default_value（允许空字符串、0 和 false）')
        if column['field'].startswith('_') or column['field'] in {'refs','source_ids','source_hash','evidence','report','profile','run_id','requirement_ids'}:
            raise DomainError('Excel 列只能映射用例字段，不能映射来源或内部记录')
    scenario_template_columns(result.get('scenario_excel_columns'))
    from .case_fields import template_columns
    policies={}
    for column in template_columns(result):
        key=column['field'];policy={k:v for k,v in column.items() if k not in ('header','field')}
        if key in policies and policies[key]!=policy:raise DomainError(f'同一字段 {key} 的多列定义或填写方式不一致')
        policies[key]=policy
    for field in ('filename_pattern','scenario_filename_pattern'):
        if not isinstance(result.get(field,''),str):
            raise DomainError(f'{field} 必须为字符串')
    if len(json.dumps(result, ensure_ascii=False)) > 100_000:
        raise DomainError('Profile 配置过大')
    return result


def validate_items(kind, items, evidence, scenario_ids=None):
    """Evidence is a server-built map; examples never ground business facts."""
    if not isinstance(items, list):
        raise OutputValidationError('items 必须为数组', 'items', 'array', items)
    seen = set()
    for index, item in enumerate(items):
        path = f'items[{index}]'
        if not isinstance(item, dict):
            raise OutputValidationError('条目必须为对象', path, 'object', item)
        item_id = item.get('id')
        if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
            raise OutputValidationError('条目必须包含有效稳定 ID', path + '.id', 'string:1..200', item.get('id', MISSING), 'invalid_id')
        if item_id in seen:
            raise OutputValidationError('条目 ID 重复', path + '.id', 'unique_id', item_id, 'duplicate_id')
        seen.add(item_id)
        if not isinstance(item.get('title'), str) or not item['title'].strip():
            raise OutputValidationError('title 不能为空', path + '.title', 'nonempty_string', item.get('title', MISSING))
        refs = item.get('refs', [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise OutputValidationError('refs 必须为证据 ID 数组', path + '.refs', 'array_of_strings', refs)
        for ref_index, ref in enumerate(refs):
            if ref not in evidence:
                raise OutputValidationError('无效或不属于本次范围的 Evidence ref', path + f'.refs[{ref_index}]', 'provided_evidence_id', ref, 'invalid_reference')
            if evidence[ref]['role'] == 'example' and kind not in ('proposal',):
                raise OutputValidationError('Example 不能作为业务 Evidence', path + f'.refs[{ref_index}]', 'non_example_evidence_id', ref, 'example_reference')
        if kind in ('analysis', 'scenarios', 'cases', 'review') and not refs and not (kind == 'analysis' and item.get('assumption') is True):
            raise OutputValidationError('业务条目必须带有效 Evidence refs', path + '.refs', 'nonempty_evidence_refs', refs, 'missing_reference')
        if kind in ('analysis', 'scenarios') and not isinstance(item.get('description'), str):
            raise OutputValidationError('description 必须为字符串', path + '.description', 'string', item.get('description', MISSING))
        if kind == 'scenarios' and not isinstance(item.get('priority'), str):
            raise OutputValidationError('Scenario priority 必须为字符串', path + '.priority', 'string', item.get('priority', MISSING))
        if kind == 'cases':
            for field in ('type', 'priority', 'preconditions', 'scenario_id'):
                if not isinstance(item.get(field), str):
                    raise OutputValidationError('Case ' + field + ' 必须为字符串', path + '.' + field, 'string', item.get(field, MISSING))
            if scenario_ids is not None and item['scenario_id'] not in scenario_ids:
                raise OutputValidationError('Case scenario_id 不属于本次 Scenario', path + '.scenario_id', 'provided_scenario_id', item['scenario_id'], 'invalid_reference')
            if not isinstance(item.get('steps'), list) or not item['steps']:
                raise OutputValidationError('Case steps 至少包含一个步骤', path + '.steps', 'nonempty_array', item.get('steps', MISSING))
            for step_index, step in enumerate(item['steps']):
                step_path = path + f'.steps[{step_index}]'
                if not isinstance(step, dict):
                    raise OutputValidationError('步骤必须为对象', step_path, 'object', step)
                for field in ('action', 'expected'):
                    if not isinstance(step.get(field), str):
                        raise OutputValidationError('步骤字段必须为字符串', step_path + '.' + field, 'string', step.get(field, MISSING))
    return items


def apply_operations(items, operations, selected_ids=None):
    if not isinstance(operations, list):
        raise OutputValidationError('operations 必须为数组', 'operations', 'array', operations)
    result = {item['id']: dict(item) for item in items}
    for index, operation in enumerate(operations):
        path = f'operations[{index}]'
        if not isinstance(operation, dict):
            raise OutputValidationError('修改操作必须为对象', path, 'object', operation)
        op, item_id = operation.get('op'), operation.get('id')
        item = operation.get('item')
        if not isinstance(op, str) or op not in ('add', 'update', 'delete'):
            raise OutputValidationError('仅支持 add/update/delete 操作', path + '.op', 'add|update|delete', op, 'invalid_operation')
        if op in ('update', 'delete') and (not isinstance(item_id, str) or not item_id):
            raise OutputValidationError('修改目标 ID 必须为非空字符串', path + '.id', 'nonempty_string', operation.get('id', MISSING))
        if op == 'add':
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in result:
                raise OutputValidationError('ADD 必须使用新的条目 ID', path + '.item', 'object_with_new_id', item, 'invalid_add')
            result[item['id']] = item
        elif op in ('update', 'delete'):
            if item_id not in result:
                raise OutputValidationError('修改目标 ID 不存在', path + '.id', 'existing_item_id', item_id, 'unknown_target')
            if selected_ids is not None and item_id not in selected_ids:
                raise OutputValidationError('修改超出所选条目范围', path + '.id', 'selected_item_id', item_id, 'unselected_target')
            if op == 'delete':
                del result[item_id]
            else:
                if not isinstance(item, dict) or item.get('id', item_id) != item_id:
                    raise OutputValidationError('UPDATE 必须为对象且不能修改稳定 ID', path + '.item', 'object_with_unchanged_id', item, 'invalid_update')
                result[item_id] = {**result[item_id], **item, 'id': item_id}
    return list(result.values())
