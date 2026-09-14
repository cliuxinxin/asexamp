"""Pure case-table projection shared by review workspaces and Excel export."""
import json
import re

from .case_fields import field_value, template_columns
from .schemas import DomainError


DEFAULT_CASE_COLUMNS = [
    {'field': field, 'header': header}
    for field, header in [('id', 'Case ID'), ('title', 'Title'), ('type', 'Type'),
                          ('priority', 'Priority'), ('preconditions', 'Preconditions'),
                          ('steps', 'Steps'), ('expected', 'Expected Result')]
]
FORBIDDEN_CASE_FIELDS = {'refs', 'source_ids', 'source_hash', 'evidence', 'report', 'profile', 'run_id'}


def cell_safe(value):
    """Preserve display text while preventing formula and control-character injection."""
    text = str(value) if value is not None else ''
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        text = "'" + text
    return text


def case_table_columns(profile):
    """Return canonical field policies and exactly the headers written to Excel."""
    columns = profile.get('excel_columns') or DEFAULT_CASE_COLUMNS
    if not isinstance(columns, list) or not columns or any(
            not isinstance(column, dict) or not isinstance(column.get('field'), str)
            or not isinstance(column.get('header'), str) or column['field'].startswith('_')
            or column['field'] in FORBIDDEN_CASE_FIELDS for column in columns):
        raise DomainError('Excel 列映射无效；只允许 Case 字段，不导出来源或内部记录。')
    return [{**column, 'header': cell_safe(column['header'])}
            for column in template_columns({**profile, 'excel_columns': columns})]


def case_table_projection(artifact, layout='case', selected=None):
    """Project canonical items without demanding completeness during human review.

    Row step indexes are zero-based for addressing canonical steps; visible step
    numbering remains one-based. Selection never changes persisted item order.
    """
    if artifact['type'] != 'cases':
        raise DomainError('仅 Case Artifact 支持 Excel 导出')
    if layout not in ('case', 'step'):
        raise DomainError('layout 必须为 case 或 step')
    items = artifact['items']
    if selected is not None:
        wanted = set(selected)
        if not wanted or not wanted.issubset({item['id'] for item in items}):
            raise DomainError('导出所选条目 ID 无效')
        items = [item for item in items if item['id'] in wanted]
    columns = case_table_columns(artifact.get('_profile', {}))
    rows = []
    for item in items:
        steps = item.get('steps', [])
        row_steps = list(enumerate(steps)) if layout == 'step' else [(None, None)]
        for step_index, current_step in row_steps:
            selected_steps = [current_step] if step_index is not None else steps
            cells = []
            for column in columns:
                field = column['field']
                if field in ('steps', 'expected'):
                    part = 'action' if field == 'steps' else 'expected'
                    value = '\n'.join(
                        f'{step_index + 1 if step_index is not None else index}. {step.get(part, "")}'
                        for index, step in enumerate(selected_steps, 1))
                else:
                    value = field_value(item, column)
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                cells.append(cell_safe(value))
            rows.append({'item_id': item['id'], 'step_index': step_index, 'cells': cells})
    return {'columns': columns, 'rows': rows, 'layout': layout}
