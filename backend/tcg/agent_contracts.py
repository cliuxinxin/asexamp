"""Strict, evidence-grounded contracts for the version 2 design agent."""
import re

from .schemas import OutputValidationError, validate_items

DEPTH_GUIDANCE = {
    'quick': 'Cover critical happy paths and highest-risk rejection partitions. Make omitted low-risk combinations explicit; never omit confirmed requirements or branches.',
    'standard': 'Cover every confirmed requirement and branch with positive, negative, boundary and equivalence-partition cases. Apply decision tables and state transitions when relevant.',
    'deep': 'Cover all confirmed requirements and branches plus interacting rules, boundary neighbors, transition sequences, recovery paths and cross-module dependencies. Use decision tables, state-transition and combination techniques where applicable.',
}


def require(condition, path, expected='valid agent contract'):
    if not condition:
        raise OutputValidationError('智能设计输出未通过校验', path, expected, code='agent_contract')


def strings(value, path, nonempty=False):
    require(isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value), path, 'array_of_nonempty_strings')
    require(not nonempty or bool(value), path, 'nonempty_array')
    return value


def refs(value, evidence, path):
    strings(value, path, True)
    require(all(r in evidence and evidence[r]['role'] != 'example' for r in value), path, 'provided_non_example_evidence_ids')


def text(value, path):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 12000, path, 'nonempty_string_max_12000')
    return value


def plan(result, available, evidence, requested, allow_examples=False):
    require(result.get('depth') in DEPTH_GUIDANCE, 'depth', 'quick|standard|deep')
    require(requested == 'auto' or requested == result['depth'], 'depth', 'requested_depth')
    text(result.get('rationale'), 'rationale')
    require(result.get('next_action') in available, 'next_action', 'available_action_with_satisfied_prerequisites')
    steps = result.get('plan')
    require(isinstance(steps, list) and 1 <= len(steps) <= 12, 'plan', '1..12_steps')
    ids = set()
    for step in steps:
        require(isinstance(step, dict), 'plan.step', 'object')
        identifier = text(step.get('id'), 'plan.id')
        require(identifier not in ids, 'plan.id', 'unique_id')
        ids.add(identifier)
        text(step.get('title'), 'plan.title')
    insight = result.get('insight')
    require(isinstance(insight, dict), 'insight', 'object')
    text(insight.get('summary'), 'insight.summary')
    if allow_examples:
        strings(insight.get('refs'), 'insight.refs')
        require(all(r in evidence for r in insight['refs']), 'insight.refs', 'provided_sample_evidence_ids')
    elif any(e['role'] != 'example' for e in evidence.values()):
        refs(insight.get('refs'), evidence, 'insight.refs')
    else:
        require(insight.get('refs') == [], 'insight.refs', 'empty_without_evidence')
    return result


def analysis(result, evidence, depth):
    items = result.get('items')
    validate_items('analysis', items, evidence)
    require(bool(items), 'items', 'nonempty_requirements')
    require(not any(i.get('assumption') for i in items), 'items.assumption', 'assumptions_separate_from_requirements')
    supplied = {r for r, e in evidence.items() if e['role'] in ('primary', 'change', 'supplement', 'clarification')}
    require(supplied <= {r for i in items for r in i['refs']}, 'items.refs', 'all_supplied_business_evidence_analyzed')
    report = result.get('report')
    require(isinstance(report, dict), 'report', 'object')
    strings(report.get('questions', []), 'questions')
    strings(report.get('assumptions', []), 'assumptions')
    model = report.get('business_model')
    require(isinstance(model, dict), 'business_model', 'object')
    nodes, edges = model.get('nodes'), model.get('edges')
    require(isinstance(nodes, list) and 1 <= len(nodes) <= 500 and isinstance(edges, list) and len(edges) <= 1000, 'business_model', 'bounded_nodes_and_edges')
    ids = set()
    for collection in (nodes, edges):
        for item in collection:
            require(isinstance(item, dict), 'business_model.item', 'object')
            identifier = item.get('id')
            require(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,79}', identifier) is not None and identifier not in ids, 'business_model.id', 'unique_stable_graph_id')
            ids.add(identifier)
            text(item.get('label'), 'business_model.label')
            refs(item.get('refs'), evidence, 'business_model.refs')
    node_ids = {n['id'] for n in nodes}
    for edge in edges:
        require(edge.get('from') in node_ids and edge.get('to') in node_ids, 'business_model.edge', 'existing_node_ids')
    strategy = report.get('strategy')
    require(isinstance(strategy, dict) and strategy.get('depth') == depth, 'strategy.depth', 'resolved_depth')
    text(strategy.get('rationale'), 'strategy.rationale')
    strings(strategy.get('techniques'), 'strategy.techniques', True)
    strings(strategy.get('scope'), 'strategy.scope', True)
    # Render only our validated structured model; arbitrary model Mermaid is never executed.
    def label(value):
        return re.sub(r'[^\w\s.,:()/\u4e00-\u9fff-]', ' ', value)[:160].replace('\n', ' ')
    mermaid = 'flowchart TD\n' + '\n'.join(f'  {n["id"]}["{label(n["label"])}"]' for n in nodes)
    mermaid += '\n' + '\n'.join(f'  {e["from"]} -->|"{label(e["label"])}"| {e["to"]}' for e in edges)
    require(len(mermaid) <= 30000, 'diagrams', 'max_30000_characters')
    report['diagrams'] = [{'id': 'business-model', 'title': '业务模型', 'mermaid': mermaid}]
    return result


def generated(result, kind, evidence, analysis_items, model, scenarios=None):
    items = result.get('items')
    validate_items(kind, items, evidence, {s['id'] for s in scenarios} if scenarios is not None else None)
    require(isinstance(result.get('has_more'), bool), 'has_more', 'boolean')
    requirements, branches = {i['id'] for i in analysis_items}, {e['id'] for e in model['edges']}
    by_scenario = {s['id']: s for s in scenarios or []}
    for item in items:
        reqs = strings(item.get('requirement_ids'), 'requirement_ids', True)
        paths = strings(item.get('branch_ids'), 'branch_ids')
        require(set(reqs) <= requirements and set(paths) <= branches, 'traceability', 'confirmed_requirement_and_branch_ids')
        if scenarios is not None:
            scenario = by_scenario[item['scenario_id']]
            require(set(reqs) <= set(scenario['requirement_ids']) and set(paths) <= set(scenario['branch_ids']), 'traceability', 'case_links_within_parent_scenario')
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
