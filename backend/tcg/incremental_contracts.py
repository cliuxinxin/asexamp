"""Small model replies become server-owned, evidence-grounded work patches."""
import copy
import hashlib

from . import agent_contracts as contract
from .schemas import OutputValidationError, apply_operations, validate_items


def stable_id(prefix, namespace, alias):
    return prefix + hashlib.sha256((namespace + ':' + str(alias)).encode()).hexdigest()[:20]


def analysis_patch(value, evidence, namespace, depth):
    value = copy.deepcopy(value)
    items = value.get('items')
    validate_items('analysis', items, evidence)
    for field in ('questions', 'assumptions', 'techniques'):
        contract.strings(value.get(field, []), field)
    resolved = value.get('depth', depth)
    contract.require(resolved in contract.DEPTH_GUIDANCE, 'depth', 'quick|standard|deep')
    contract.require(resolved == depth, 'depth', 'selected_depth')
    nodes, edges = value.get('nodes', []), value.get('edges', [])
    contract.require(isinstance(nodes, list), 'nodes', 'array')
    contract.require(isinstance(edges, list), 'edges', 'array')
    aliases = {}
    for i, node in enumerate(nodes):
        contract.require(isinstance(node, dict), f'nodes[{i}]', 'object')
        alias = contract.text(node.get('id'), f'nodes[{i}].id')
        contract.require(alias not in aliases, f'nodes[{i}].id', 'unique_id')
        contract.text(node.get('label'), f'nodes[{i}].label')
        contract.refs(node.get('refs'), evidence, f'nodes[{i}].refs')
        aliases[alias] = stable_id('N', namespace, alias)
    edge_ids = set()
    for i, edge in enumerate(edges):
        contract.require(isinstance(edge, dict), f'edges[{i}]', 'object')
        alias = contract.text(edge.get('id'), f'edges[{i}].id')
        contract.require(alias not in edge_ids, f'edges[{i}].id', 'unique_id')
        edge_ids.add(alias)
        for field in ('from', 'to'):
            contract.require(edge.get(field) in aliases, f'edges[{i}].{field}', 'provided_local_node_alias')
        contract.text(edge.get('label'), f'edges[{i}].label')
        contract.refs(edge.get('refs'), evidence, f'edges[{i}].refs')
    for node in nodes:
        node['id'] = aliases[node['id']]
    for edge in edges:
        edge.update(id=stable_id('B', namespace, edge['id']), **{f: aliases[edge[f]] for f in ('from', 'to')})
    for item in items:
        item['id'] = stable_id('R', namespace, item['id'])
    review = value.get('evidence_review', [])
    contract.require(isinstance(review, list), 'evidence_review', 'array')
    reviewed = set()
    for i, entry in enumerate(review):
        contract.require(isinstance(entry, dict), f'evidence_review[{i}]', 'object')
        contract.require(entry.get('ref') in evidence, f'evidence_review[{i}].ref', 'provided_evidence_id')
        contract.require(entry.get('classification') in ('context','non_requirement','uncertain'), f'evidence_review[{i}].classification', 'context|non_requirement|uncertain')
        contract.text(entry.get('reason'), f'evidence_review[{i}].reason')
        reviewed.add(entry['ref'])
    required = {r for r,e in evidence.items() if e['role'] in ('primary','supplement','change','clarification')}
    contract.require(required <= {r for item in items for r in item['refs']} | reviewed, 'evidence_review', 'all_supplied_business_evidence_analyzed')
    report = {'summary': value.get('summary', '已保存本组需求分析。'), 'questions': value.get('questions', []),
        'assumptions': value.get('assumptions', []), 'evidence_review': review,
        'business_model': {'nodes': nodes, 'edges': edges}, 'strategy': {'depth': resolved,
        'rationale': '依据当前范围和业务规则选择测试方法。', 'techniques': value.get('techniques') or ['等价类与边界分析'],
        'scope': [i['title'] for i in items] or ['背景资料']}, 'diagrams': []}
    contract.text(report['summary'], 'summary')
    if nodes:
        # Render from validated graph data, never execute model-authored Mermaid.
        import re
        def label(text):
            return re.sub(r'[^\w\s.,:()/\u4e00-\u9fff-]', ' ', text)[:120].replace('\n', ' ')
        mermaid = 'flowchart TD\n' + '\n'.join(f'  {n["id"]}["{label(n["label"])}"]' for n in nodes)
        mermaid += '\n' + '\n'.join(f'  {e["from"]} -->|"{label(e["label"])}"| {e["to"]}' for e in edges)
        report['diagrams'] = [{'id': 'business-' + namespace, 'title': '当前部分业务图', 'mermaid': mermaid}]
    return {'items': items, 'report': report}


def generated_patch(value, kind, evidence, data, namespace):
    value = copy.deepcopy(value)
    contract.generated(value, kind, evidence, data['analysis'], data['business_model'], data.get('scenarios') if kind == 'cases' else None)
    for item in value['items']:
        item['id'] = stable_id('C' if kind == 'cases' else 'S', namespace, item['id'])
    return value


def operation_patch(value, kind, original, evidence, namespace, selected=None, data=None):
    value = copy.deepcopy(value)
    operations = value.get('operations')
    contract.require(isinstance(operations, list), 'operations', 'array')
    for index, op in enumerate(operations):
        contract.require(isinstance(op, dict), f'operations[{index}]', 'object')
        if op.get('op') == 'add' and isinstance(op.get('item'), dict):
            op['item']['id'] = stable_id('C' if kind == 'cases' else 'I', namespace, op['item'].get('id', index))
    items = apply_operations(original, operations, selected)
    validate_items(kind, items, evidence)
    if data and kind in ('cases','scenarios'):
        contract.generated({'items': items, 'has_more': False}, kind, evidence, data['analysis'], data['business_model'], data.get('scenarios') if kind == 'cases' else None)
    report = value.get('report', {})
    contract.require(isinstance(report, dict), 'report', 'object')
    return {'items': items, 'operations': operations, 'report': report, 'summary': value.get('summary') or report.get('summary') or '已完成定向检查。'}
