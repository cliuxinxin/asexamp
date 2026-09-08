"""Single-field repairs applied to a server-owned draft, never whole-set rewrites."""
import copy
import json
import re

from .schemas import OutputValidationError

REPORT_FIELDS = {'business_model', 'strategy', 'questions', 'assumptions', 'diagrams', 'conflicts'}


def parts(path):
    if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*(?:\[\d+\]|\.[A-Za-z_][A-Za-z_0-9]*)*', path):
        raise OutputValidationError('修正位置无效', 'repair.path', 'server_selected_field')
    return [int(index) if index else name for name, index in re.findall(r'([A-Za-z_][A-Za-z_0-9]*)|\[(\d+)\]', path)]


def repair_fragment(result, issue, flat_fields=()):
    path = issue['path']
    if path.split('.')[0] in REPORT_FIELDS and path.split('.')[0] not in flat_fields:
        path = 'report.' + path
    keys = parts(path)
    if issue.get('expected') == 'max_30000_characters':
        raise OutputValidationError('该派生字段无法局部修正，原草稿已保留', path, 'bounded_source_fields')
    parent = result
    try:
        for key in keys[:-1]:
            parent = parent[key]
    except (KeyError, IndexError, TypeError):
        raise OutputValidationError('修正位置不可寻址，原草稿已保留', path, 'concrete_existing_parent') from None
    key = keys[-1]
    if isinstance(parent, dict) and isinstance(key, str):
        value = parent.get(key)
    elif isinstance(parent, list) and isinstance(key, int) and key < len(parent):
        value = parent[key]
    else:
        raise OutputValidationError('修正位置不可寻址，原草稿已保留', path, 'concrete_existing_parent')
    expected = issue.get('expected', '')
    valid_container = isinstance(value, dict) and expected == 'object'
    valid_container = valid_container or isinstance(value, list) and (
        'array' in expected or expected[:1].isdigit() or expected.endswith(('_nodes', '_edges', '_steps', '_items')))
    if value and valid_container:
        raise OutputValidationError('不能用整段替换已有结构，请修正具体局部字段', path, 'concrete_leaf_repair')
    if len(json.dumps(value, ensure_ascii=False)) > 16000:
        raise OutputValidationError('待修字段过大，请缩小当前批次后重试', path, 'bounded_repair_fragment')
    return {'path': path, 'value': copy.deepcopy(value), 'validation_error': issue}


def apply_repair(result, fragment, response):
    path = fragment['path']
    if set(response) != {'path', 'value'} or response.get('path') != path:
        raise OutputValidationError('模型修正超出指定字段，原草稿已保留', path, 'replacement_for_exact_requested_field')
    if len(json.dumps(response['value'], ensure_ascii=False)) > 16000:
        raise OutputValidationError('修正字段过大，原草稿已保留', path, 'bounded_repair_value')
    corrected = copy.deepcopy(result)
    target = corrected
    keys = parts(path)
    try:
        for key in keys[:-1]:
            target = target[key]
        key = keys[-1]
        if isinstance(target, dict) and isinstance(key, str):
            target[key] = copy.deepcopy(response['value'])
        elif isinstance(target, list) and isinstance(key, int) and key < len(target):
            target[key] = copy.deepcopy(response['value'])
        else:
            raise TypeError()
    except (KeyError, IndexError, TypeError):
        raise OutputValidationError('指定字段的父对象无效，原草稿已保留', path, 'existing_parent_for_repair') from None
    match = re.fullmatch(r'report\.business_model\.nodes\[(\d+)\]\.id', path)
    if match and isinstance(fragment['value'], str) and isinstance(response['value'], str):
        model = result.get('report', {}).get('business_model', {})
        nodes = model.get('nodes', [])
        node_index = int(match.group(1))
        if any(index != node_index and isinstance(node, dict) and node.get('id') == response['value']
               for index, node in enumerate(nodes)):
            raise OutputValidationError('修正后的节点 ID 与已有节点冲突，原草稿已保留', path, 'new_unique_graph_id')
        if sum(isinstance(node, dict) and node.get('id') == fragment['value'] for node in nodes) == 1:
            for edge in corrected.get('report', {}).get('business_model', {}).get('edges', []):
                if isinstance(edge, dict):
                    for endpoint in ('from', 'to'):
                        if edge.get(endpoint) == fragment['value']:
                            edge[endpoint] = response['value']
    return corrected
