"""Generate from bounded groups of accepted requirements/scenarios."""
import json

from . import agent_contracts as contract
from .agent_context import compact_items, model_view, project_coverage
from .graph import routing_excerpt
from .schemas import DomainError


def generation_views(context, kind, budget=14000):
    units = context['analysis'] if kind == 'scenarios' else context['scenarios']
    def view(group):
        req_ids = {r['id'] for r in group} if kind == 'scenarios' else {r for s in group for r in s['requirement_ids']}
        requirements = [r for r in context['analysis'] if r['id'] in req_ids]
        refs = {ref for r in requirements for ref in r['refs']}
        branches = {b for s in group for b in s['branch_ids']} if kind == 'cases' else None
        model = context['business_model']
        edges = [e for e in model['edges'] if e['id'] in branches] if branches is not None else [e for e in model['edges'] if refs.intersection(e['refs'])]
        nodes = {e[k] for e in edges for k in ('from', 'to')}
        subset = {'analysis': requirements, 'business_model': {'nodes': [n for n in model['nodes'] if n['id'] in nodes], 'edges': edges}}
        if kind == 'cases':
            subset['scenarios'] = group
        return subset
    groups, current = [], []
    for unit in units:
        if current and len(json.dumps(view(current + [unit]), ensure_ascii=False)) > budget:
            groups.append(view(current))
            current = []
        current.append(unit)
        if len(json.dumps(view(current), ensure_ascii=False)) > budget:
            raise DomainError('单个场景关联的需求或业务图超过生成预算；请拆分该场景。已有分析与批次结果保留。')
    if current:
        groups.append(view(current))
    return groups


def compact_previous(items, kind):
    fields = ('id', 'title', 'refs', 'requirement_ids', 'branch_ids', 'scenario_id', 'type', 'priority')
    result = []
    for item in items:
        value = {key: (routing_excerpt(item[key], 400) if isinstance(item[key], str) else item[key][:12])
                 for key in fields if key in item and isinstance(item[key], (str, list))}
        if kind == 'cases':
            value['preconditions'] = routing_excerpt(item.get('preconditions', ''), 240)
            value['steps'] = [{key: routing_excerpt(step.get(key, ''), 240) for key in ('action', 'expected')}
                              for step in item.get('steps', [])[:2] if isinstance(step, dict)]
            value['step_count'] = len(item.get('steps', []))
        elif isinstance(item.get('description'), str):
            value['description'] = routing_excerpt(item['description'], 600)
        result.append(value)
    return result


async def generate_items(agent, state, context, kind, previous):
    run_id = state['run_id']
    views = generation_views(context, kind)
    items = list(previous)
    source_ids = agent.current(state)['_source_ids']
    all_evidence = {e['id']: e for e in agent.documents.evidence(source_ids)}
    for group_index, view in enumerate(views):
        req_ids = {r['id'] for r in view['analysis']}
        refs = {r for i in view['analysis'] for r in i['refs']} | {r for e in view['business_model']['edges'] for r in e['refs']}
        reading = agent.documents.read(source_ids, sorted(refs))
        evidence = reading['evidence']
        present = {e['id'] for e in evidence}
        evidence += [{k: all_evidence[r][k] for k in ('id', 'role', 'source_id', 'location')} | {'via': 'accepted_artifact'} for r in sorted(refs - present)]
        local_previous = [i for i in previous if req_ids.intersection(i['requirement_ids'])]
        branch_ids = {edge['id'] for edge in view['business_model']['edges']}
        cursor, used = None, set()
        agent.insight(run_id, f'按已确认结论生成第 {group_index + 1}/{len(views)} 组{"场景" if kind == "scenarios" else "用例"}；只读取本组相关证据。', sorted(refs)[:8], 'decision')
        base = {k: v for k, v in context.items() if k not in ('analysis', 'business_model', 'scenarios', 'cases', 'evidence', 'document_access', 'document_observation')}
        for page in range(200):
            previous_view = compact_previous(local_previous[-8:], kind) if state['action'].startswith('repair_') else compact_items(local_previous, limit=80)
            page_context = {**base, **view, 'evidence': evidence, 'previous_items': previous_view,
                'previous_item_count': len(local_previous), 'previous_item_omitted_count': max(0, len(local_previous) - len(previous_view)), 'cursor': cursor,
                'batch_index': group_index, 'batch_count': len(views), 'repair': state['action'].startswith('repair_'),
                'coverage': project_coverage(agent.store.run(run_id)['agent'].get('coverage'), req_ids, branch_ids)}
            page_context = model_view(page_context, 'generation', budget=40_000)
            result = await agent.call({**state, 'iteration': f'{state["iteration"]}:group:{group_index}:{page}'}, 'agent_' + kind, page_context,
                lambda r: contract.generated(r, kind, {e['id']: e for e in evidence}, view['analysis'], view['business_model'], view.get('scenarios') if kind == 'cases' else None))
            new = result['items']
            contract.require(not {i['id'] for i in local_previous}.intersection(i['id'] for i in new), 'items.id', 'new_unique_ids')
            local_previous += new
            renamed = [{**i, 'id': contract.namespaced_id(f'g{group_index + 1}_', i['id'], 200)} for i in new] if len(views) > 1 else new
            contract.require(not {i['id'] for i in items}.intersection(i['id'] for i in renamed), 'items.id', 'new_unique_ids')
            items.extend(renamed)
            if not result['has_more']:
                break
            next_cursor = result.get('next_cursor')
            contract.require(bool(new) and isinstance(next_cursor, str) and next_cursor and next_cursor not in used, 'next_cursor', 'new_cursor_and_new_items')
            cursor = next_cursor
            used.add(cursor)
        else:
            raise DomainError('分页达到执行预算，未发布不完整结果；已完成调用保留。')
    return items
