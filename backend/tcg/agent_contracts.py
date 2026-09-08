"""Strict, evidence-grounded contracts for the version 2 design agent."""
import hashlib
import re

from .schemas import OutputValidationError, validate_items

DEPTH_GUIDANCE = {
    'quick': 'Cover critical happy paths and highest-risk rejection partitions. Make omitted low-risk combinations explicit; never omit confirmed requirements or branches.',
    'standard': 'Cover every confirmed requirement and branch with positive, negative, boundary and equivalence-partition cases. Apply decision tables and state transitions when relevant.',
    'deep': 'Cover all confirmed requirements and branches plus interacting rules, boundary neighbors, transition sequences, recovery paths and cross-module dependencies. Use decision tables, state-transition and combination techniques where applicable.',
}

GRAPH_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,79}')


def valid_graph_id(value):
    return isinstance(value, str) and GRAPH_ID.fullmatch(value) is not None


def namespaced_id(prefix, identifier, max_length):
    """Add a stable namespace without exceeding the receiving contract."""
    combined = prefix + identifier
    if len(combined) <= max_length:
        return combined
    digest = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return combined[:max_length - len(digest) - 1] + '_' + digest


def require(condition, path, expected='valid agent contract'):
    if not condition:
        raise OutputValidationError('智能设计输出未通过校验', path, expected, code='agent_contract')


def strings(value, path, nonempty=False):
    require(isinstance(value, list), path, 'array_of_nonempty_strings')
    require(not nonempty or bool(value), path, 'nonempty_array')
    for index, item in enumerate(value):
        require(isinstance(item, str) and bool(item.strip()), f'{path}[{index}]', 'nonempty_string')
    return value


def refs(value, evidence, path):
    strings(value, path, True)
    for index, ref in enumerate(value):
        require(ref in evidence and evidence[ref]['role'] != 'example', f'{path}[{index}]', 'provided_non_example_evidence_id')


def text(value, path):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 12000, path, 'nonempty_string_max_12000')
    return value


def plan(result, available, evidence, requested, allow_examples=False, require_insight_refs=False):
    require(result.get('depth') in DEPTH_GUIDANCE, 'depth', 'quick|standard|deep')
    require(requested == 'auto' or requested == result['depth'], 'depth', 'requested_depth')
    text(result.get('rationale'), 'rationale')
    require(result.get('next_action') in available, 'next_action', 'available_action_with_satisfied_prerequisites')
    steps = result.get('plan')
    require(isinstance(steps, list) and 1 <= len(steps) <= 12, 'plan', '1..12_steps')
    ids = set()
    for index, step in enumerate(steps):
        path = f'plan[{index}]'
        require(isinstance(step, dict), path, 'object')
        identifier = text(step.get('id'), path + '.id')
        require(identifier not in ids, path + '.id', 'unique_id')
        ids.add(identifier)
        text(step.get('title'), path + '.title')
    insight = result.get('insight')
    require(isinstance(insight, dict), 'insight', 'object')
    text(insight.get('summary'), 'insight.summary')
    if allow_examples:
        strings(insight.get('refs'), 'insight.refs')
        for index, ref in enumerate(insight['refs']):
            require(ref in evidence, f'insight.refs[{index}]', 'provided_sample_evidence_id')
    elif insight.get('refs'):
        refs(insight.get('refs'), evidence, 'insight.refs')
    elif require_insight_refs:
        require(False, 'insight.refs', 'nonempty_evidence_refs_for_grounded_insight')
    else:
        require(insight.get('refs') == [], 'insight.refs', 'empty_or_provided_evidence_refs')
    return result


def analysis(result, evidence, depth, allow_empty=False):
    items = result.get('items')
    validate_items('analysis', items, evidence)
    require(bool(items) or allow_empty, 'items', 'nonempty_requirements')
    for index, item in enumerate(items):
        require(not item.get('assumption'), f'items[{index}].assumption', 'assumptions_separate_from_requirements')
    supplied = {r for r, e in evidence.items() if e['role'] in ('primary', 'change', 'supplement', 'clarification')}
    report = result.get('report')
    require(isinstance(report, dict), 'report', 'object')
    review = report.get('evidence_review', [])
    require(isinstance(review, list), 'report.evidence_review', 'array')
    reviewed = set()
    for index, entry in enumerate(review):
        path = f'report.evidence_review[{index}]'
        require(isinstance(entry, dict), path, 'object')
        require(entry.get('ref') in evidence and entry.get('ref') not in reviewed, path + '.ref', 'unique_provided_evidence_id')
        require(entry.get('classification') in ('context', 'non_requirement', 'uncertain'), path + '.classification', 'context|non_requirement|uncertain')
        text(entry.get('reason'), path + '.reason')
        reviewed.add(entry['ref'])
    require(supplied <= {r for i in items for r in i['refs']} | reviewed, 'items.refs', 'all_supplied_business_evidence_analyzed')
    strings(report.get('questions', []), 'report.questions')
    strings(report.get('assumptions', []), 'report.assumptions')
    model = report.get('business_model')
    require(isinstance(model, dict), 'report.business_model', 'object')
    nodes, edges = model.get('nodes'), model.get('edges')
    require(isinstance(nodes, list), 'report.business_model.nodes', 'array')
    require(1 <= len(nodes) <= 500, 'report.business_model.nodes', '1..500_nodes')
    require(isinstance(edges, list), 'report.business_model.edges', 'array')
    require(len(edges) <= 1000, 'report.business_model.edges', '0..1000_edges')
    ids = set()
    for field, collection in (('nodes', nodes), ('edges', edges)):
        for index, item in enumerate(collection):
            path = f'report.business_model.{field}[{index}]'
            require(isinstance(item, dict), path, 'object')
            identifier = item.get('id')
            require(valid_graph_id(identifier) and identifier not in ids, path + '.id', 'unique_stable_graph_id')
            ids.add(identifier)
            text(item.get('label'), path + '.label')
            refs(item.get('refs'), evidence, path + '.refs')
    node_ids = {n['id'] for n in nodes}
    for index, edge in enumerate(edges):
        for field in ('from', 'to'):
            require(edge.get(field) in node_ids, f'report.business_model.edges[{index}].{field}', 'existing_node_ids')
    strategy = report.get('strategy')
    require(isinstance(strategy, dict), 'report.strategy', 'object')
    require(strategy.get('depth') == depth, 'report.strategy.depth', 'resolved_depth')
    text(strategy.get('rationale'), 'report.strategy.rationale')
    strings(strategy.get('techniques'), 'report.strategy.techniques', True)
    strings(strategy.get('scope'), 'report.strategy.scope', True)
    # Render only our validated structured model; arbitrary model Mermaid is never executed.
    def label(value):
        return re.sub(r'[^\w\s.,:()/\u4e00-\u9fff-]', ' ', value)[:160].replace('\n', ' ')
    mermaid = 'flowchart TD\n' + '\n'.join(f'  {n["id"]}["{label(n["label"])}"]' for n in nodes)
    mermaid += '\n' + '\n'.join(f'  {e["from"]} -->|"{label(e["label"])}"| {e["to"]}' for e in edges)
    require(len(mermaid) <= 30000, 'report.diagrams', 'max_30000_characters')
    report['diagrams'] = [{'id': 'business-model', 'title': '业务模型', 'mermaid': mermaid}]
    return result


def generated(result, kind, evidence, analysis_items, model, scenarios=None):
    items = result.get('items')
    validate_items(kind, items, evidence, {s['id'] for s in scenarios} if scenarios is not None else None)
    require(isinstance(result.get('has_more'), bool), 'has_more', 'boolean')
    requirements, branches = {i['id'] for i in analysis_items}, {e['id'] for e in model['edges']}
    by_scenario = {s['id']: s for s in scenarios or []}
    for index, item in enumerate(items):
        path = f'items[{index}]'
        reqs = strings(item.get('requirement_ids'), path + '.requirement_ids', True)
        paths = strings(item.get('branch_ids'), path + '.branch_ids')
        require(set(reqs) <= requirements, path + '.requirement_ids', 'confirmed_requirement_ids')
        require(set(paths) <= branches, path + '.branch_ids', 'confirmed_branch_ids')
        if scenarios is not None:
            scenario = by_scenario[item['scenario_id']]
            require(set(reqs) <= set(scenario['requirement_ids']), path + '.requirement_ids', 'case_links_within_parent_scenario')
            require(set(paths) <= set(scenario['branch_ids']), path + '.branch_ids', 'case_links_within_parent_scenario')
    return result


def coverage(requirements, model, scenarios, cases):
    covered_reqs = {r for case in cases for r in case['requirement_ids']}
    covered_branches = {b for case in cases for b in case['branch_ids']}
    covered_scenarios = {c['scenario_id'] for c in cases}
    gaps = [{'kind': 'requirement', 'id': r['id'], 'title': r['title'], 'refs': r['refs']} for r in requirements if r['id'] not in covered_reqs]
    gaps += [{'kind': 'branch', 'id': b['id'], 'title': b['label'], 'refs': b['refs']} for b in model['edges'] if b['id'] not in covered_branches]
    gaps += [{'kind': 'scenario', 'id': s['id'], 'title': s['title'], 'refs': s['refs']} for s in scenarios if s['id'] not in covered_scenarios]
    return {'requirements_total': len(requirements), 'requirements_covered': len(covered_reqs),
            'branches_total': len(model['edges']), 'branches_covered': len(covered_branches), 'gaps': gaps}


def refreshed_report(kind, items, report, evidence):
    """Recompute derived links on every revision, including manual edits/restores."""
    report = dict(report or {})
    model, requirements = report.get('business_model'), report.get('requirements')
    scenarios = report.get('scenarios') if kind == 'cases' else items
    if kind in ('cases', 'scenarios') and model is not None and requirements is not None and scenarios is not None:
        generated({'items': items, 'has_more': False}, kind, evidence, requirements, model, scenarios if kind == 'cases' else None)
        report['traceability'] = [{**{k: i[k] for k in ('id', 'title', 'requirement_ids', 'branch_ids')}, 'scenario_id': i.get('scenario_id', i['id']),
            **({'case_id': i['id']} if kind == 'cases' else {}), 'refs': i['refs']} for i in items]
        report['coverage'] = coverage(requirements, model, scenarios, items if kind == 'cases' else [{**i, 'scenario_id': i['id']} for i in items])
        report.pop('coverage_invalidated', None)
    elif 'coverage' in report or 'traceability' in report:
        report.pop('coverage', None)
        report.pop('traceability', None)
        report['coverage_invalidated'] = '条目已修改，原覆盖与关联结果不再有效；需要重新分析确认模型后检查覆盖。'
    return report
