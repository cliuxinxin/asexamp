"""Compact native function schemas for test-design data, never command plans."""
TEXT = {'type': 'string'}
STRINGS = {'type': 'array', 'items': TEXT}


def object_schema(properties, required=(), *, extra=False):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': extra}


def report_schema(kind=None):
    suggestion = object_schema({'question': TEXT, 'answer': TEXT, 'basis': TEXT,
        'refs': STRINGS, 'confidence': {'type': 'string', 'enum': ['supported', 'assumption']}},
        ['question', 'answer', 'basis', 'refs', 'confidence'])
    schema = object_schema({'summary': TEXT, 'questions': STRINGS,
        'question_suggestions': {'type': 'array', 'items': suggestion},
        'assumptions': STRINGS, 'in_scope': STRINGS, 'out_of_scope': STRINGS,
        'issues': {'type': 'array', 'items': object_schema({'title': TEXT, 'detail': TEXT,
            'case_ids': STRINGS, 'refs': STRINGS}, ['title'], extra=True)},
        'diagrams': {'type': 'array', 'items': object_schema({'title': TEXT,
            'mermaid': TEXT}, ['title', 'mermaid'])},
        'excluded_scenarios': {'type': 'array', 'items': object_schema({
            'scenario_id': TEXT, 'reason': TEXT, 'refs': STRINGS}, ['scenario_id', 'reason', 'refs'])}},
        ['summary'], extra=True)
    if kind == 'analysis':
        schema['properties']['diagrams']['description'] = (
            'Three complementary current requirement views: 业务流程图 (flowchart TD), '
            '领域思维导图 (mindmap), 状态转换图 (stateDiagram-v2). '
            'Each view uses grounded business facts; mark unspecified transitions rather than invent them.')
    return schema


def rows_schema(kind, profile=None):
    """Explicit core types plus configured custom fields; no mutation envelope."""
    props = {'id': TEXT, 'title': TEXT, 'refs': STRINGS}
    required = list(props)
    if kind in ('analysis', 'scenarios'):
        props['description'] = TEXT
        required.append('description')
    if kind == 'scenarios':
        props.update(priority=TEXT, requirement_ids=STRINGS)
        required.extend(['priority', 'requirement_ids'])
    if kind == 'cases':
        props.update(scenario_id=TEXT, type=TEXT, priority=TEXT, preconditions=TEXT,
            steps={'type': 'array', 'minItems': 1, 'items': object_schema(
                {'action': TEXT, 'expected': TEXT}, ['action', 'expected'])})
        required.extend(['scenario_id', 'type', 'priority', 'preconditions', 'steps'])
    columns = (profile or {}).get('scenario_excel_columns' if kind == 'scenarios' else 'excel_columns', [])
    for column in columns if kind in ('scenarios', 'cases') else []:
        field = column.get('field', '')
        if field and field not in props and not field.startswith('_') and field != 'expected':
            props[field] = {'description': column.get('definition', column.get('header', field))}
    return object_schema({'items': {'type': 'array', 'items': object_schema(props, required, extra=True)},
                          'report': report_schema(kind)}, ['items', 'report'])


ESTIMATE_SCHEMA = object_schema({'summary': TEXT,
    'scenarios': {'type': 'array', 'items': object_schema({'scenario_id': TEXT,
        'min_count': {'type': 'integer', 'minimum': 0},
        'max_count': {'type': 'integer', 'minimum': 0}, 'rationale': TEXT,
        'assumptions': STRINGS}, ['scenario_id', 'min_count', 'max_count', 'rationale', 'assumptions'])}},
    ['summary', 'scenarios'])
ANSWER_SCHEMA = object_schema({'answer': TEXT, 'refs': STRINGS}, ['answer', 'refs'])


def completion_schema(fields):
    return object_schema({'items': {'type': 'array', 'items': object_schema({
        'id': TEXT, 'fields': object_schema({field: {} for field in fields}),
        'refs': STRINGS,
        'unresolved': {'type': 'array', 'items': object_schema({'field': TEXT, 'reason': TEXT},
                                                            ['field', 'reason'])}},
        ['id', 'fields', 'refs', 'unresolved'])}}, ['items'])
