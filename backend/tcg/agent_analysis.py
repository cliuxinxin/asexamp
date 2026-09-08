"""Exhaustive, bounded source analysis with durable accepted-batch reuse."""
import copy
import hashlib
import json

from . import agent_contracts as contract
from .agent_context import compact_items, model_view
from .schemas import DomainError, OutputValidationError


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def evidence_batches(evidence, budget=12000):
    groups, current = [], []
    for chunk in evidence:
        item = {k: chunk[k] for k in ('id', 'text', 'location', 'source_id', 'role')}
        if len(serialized([item])) > budget:
            raise DomainError('单段文档超过分析预算；请重新上传以按段落拆分。原资料保留，未截断分析。')
        if current and len(serialized(current + [item])) > budget:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups


def merge_analyses(results):
    if len(results) == 1:
        return copy.deepcopy(results[0])
    merged = {'items': [], 'report': {'summary': '', 'questions': [], 'assumptions': [],
        'business_model': {'nodes': [], 'edges': []}, 'strategy': copy.deepcopy(results[0]['report']['strategy']), 'evidence_review': []}}
    report = merged['report']
    report['strategy']['scope'] = []
    report['strategy']['techniques'] = []
    for index, result in enumerate(results, 1):
        prefix = f'b{index}_'
        merged['items'] += [{**i, 'id': contract.namespaced_id(prefix, i['id'], 200)} for i in result['items']]
        model = result['report']['business_model']
        node_ids = {n['id']: contract.namespaced_id(prefix, n['id'], 80) for n in model['nodes']}
        report['business_model']['nodes'] += [{**n, 'id': node_ids[n['id']]} for n in model['nodes']]
        report['business_model']['edges'] += [{**e, 'id': contract.namespaced_id(prefix, e['id'], 80),
            'from': node_ids[e['from']], 'to': node_ids[e['to']]} for e in model['edges']]
        for field in ('questions', 'assumptions', 'evidence_review', 'conflicts'):
            report.setdefault(field, []).extend(result['report'].get(field, []))
        for field in ('scope', 'techniques'):
            report['strategy'][field] += result['report']['strategy'][field]
    for field in ('questions', 'assumptions'):
        report[field] = list(dict.fromkeys(report[field]))
    for field in ('scope', 'techniques'):
        report['strategy'][field] = list(dict.fromkeys(report['strategy'][field]))
    report['summary'] = f'已按 {len(results)} 个批次检查全文，保留 {len(merged["items"])} 项需求；批次结果已复用。'
    return merged


async def analyze_documents(agent, state, context):
    run = agent.current(state)
    evidence = [e for e in agent.documents.evidence(run['_source_ids']) if e['role'] != 'example']
    groups = evidence_batches(evidence)
    results = []
    for index, group in enumerate(groups):
        agent.current(state)
        batch_context = {k: copy.deepcopy(v) for k, v in context.items()
                         if k not in ('evidence', 'document_observation', 'analysis', 'business_model', 'scenarios', 'cases', 'artifact')}
        if context.get('artifact'):
            artifact = context['artifact']
            batch_context['artifact'] = {'id': artifact['id'], 'type': artifact['type'], 'title': artifact['title'],
                                         'item_count': len(artifact['items']), 'items': compact_items(artifact['items'], 12)}
        batch_context.update(evidence=group, batch_index=index, batch_count=len(groups), analysis_batch=True)
        batch_context = model_view(batch_context, 'analysis', budget=30_000)
        fingerprint = hashlib.sha256(serialized({k: v for k, v in batch_context.items() if k not in ('documents', 'conversation', 'memory')}).encode()).hexdigest()
        # Include durable decisions: a changed confirmation must not reuse an old interpretation.
        fingerprint = hashlib.sha256((fingerprint + serialized(context.get('memory', {}))).encode()).hexdigest()
        cache_key = 'accepted_analysis:v1:' + fingerprint
        accepted = agent.store.cache_get(run['id'], cache_key)
        if accepted is not None:
            agent.insight(run['id'], f'复用第 {index + 1}/{len(groups)} 批已校验的文档分析。', [], 'decision')
        else:
            agent.insight(run['id'], f'读取并分析第 {index + 1}/{len(groups)} 批，共 {len(group)} 段；其他正文留在本地。', [group[0]['id'], group[-1]['id']], 'decision')
            batch_state = {**state, 'iteration': f'{state["iteration"]}:document:{fingerprint[:16]}'}
            batch_evidence = {e['id']: e for e in group}
            accepted = await agent.call(batch_state, 'agent_analyze', batch_context,
                lambda r: contract.analysis(r, batch_evidence, context['depth'], allow_empty=True))
            agent.current(state)
            agent.store.cache_set(run['id'], cache_key, accepted)
        results.append(accepted)
        with agent.store.transaction():
            agent.current(state)
            agent.update(run['id'], document_progress={'completed_batches': index + 1, 'total_batches': len(groups),
                'reviewed_chunks': sum(len(g) for g in groups[:index + 1]), 'total_chunks': len(evidence)})
    if not results:
        raise DomainError('没有可分析的业务来源。')
    merged = merge_analyses(results)
    all_evidence = {e['id']: e for e in evidence}
    if len(results) > 1:
        # Integrate accepted facts only. Never reread the raw source for graph joins.
        summaries = [{'batch': i + 1, 'summary': r['report'].get('summary', ''),
                      'requirements': [{'id': contract.namespaced_id(f'b{i + 1}_', item['id'], 200), 'title': item['title']} for item in r['items']],
                      'nodes': [n for n in merged['report']['business_model']['nodes'] if n['id'].startswith(f'b{i + 1}_')]} for i, r in enumerate(results)]
        for index, group in enumerate(summary_batches(summaries)):
            node_ids = {n['id'] for s in group for n in s['nodes']}
            refs = {r for s in group for n in s['nodes'] for r in n['refs']}
            def validate(result):
                contract.require(isinstance(result.get('edges'), list), 'edges', 'array')
                seen = set()
                for i, e in enumerate(result['edges']):
                    contract.require(isinstance(e, dict) and e.get('from') in node_ids and e.get('to') in node_ids, f'edges[{i}]', 'existing_supplied_nodes')
                    contract.require(contract.valid_graph_id(e.get('id')), f'edges[{i}].id', 'stable_graph_id')
                    contract.require(e['id'] not in seen, f'edges[{i}].id', 'unique_id')
                    seen.add(e['id'])
                    contract.text(e.get('label'), f'edges[{i}].label')
                    contract.refs(e.get('refs'), {r: all_evidence[r] for r in refs}, f'edges[{i}].refs')
                contract.strings(result.get('limitations', []), 'limitations')
                return result
            integrated = await agent.call({**state, 'iteration': f'{state["iteration"]}:integration:{index}'}, 'agent_integrate',
                {'accepted_batches': group, 'evidence': [{k: all_evidence[r][k] for k in ('id', 'role', 'source_id', 'location')} for r in sorted(refs)]}, validate)
            merged['report']['business_model']['edges'] += [{**e, 'id': contract.namespaced_id(f'join{index}_', e['id'], 80)} for e in integrated['edges']]
            merged['report']['assumptions'] += integrated.get('limitations', [])
        if len(summary_batches(summaries)) > 1:
            merged['report']['assumptions'].append('跨批次关联按有限窗口检查；超出窗口的跨模块依赖仍需业务复核。')
    # Every batch was checked exhaustively; recheck merged references and graph.
    return contract.analysis(merged, all_evidence, context['depth'])


def summary_batches(summaries, budget=24000):
    groups, current = [], []
    for summary in summaries:
        if len(serialized(summary)) > budget:
            raise DomainError('已提取的单批业务模型超过关联预算；请缩小设计范围。已完成的文档分析保留。')
        if current and len(serialized(current + [summary])) > budget:
            groups.append(current)
            current = []
        current.append(summary)
    if current:
        groups.append(current)
    return groups


async def complete_evidence(agent, state, key, context, result, attempt):
    """An additive semantic pass for unread evidence; never a schema rewrite."""
    covered = {r for i in result['items'] for r in i['refs']}
    covered.update(e['ref'] for e in result['report'].get('evidence_review', []))
    missing = [e for e in context['evidence'] if e['id'] not in covered and e['role'] in ('primary', 'change', 'supplement', 'clarification')]
    agent.insight(state['run_id'], f'发现 {len(missing)} 段尚未归类，正在补充检查；已提取需求保留。', [e['id'] for e in missing[:8]], 'decision')
    model = result['report'].get('business_model', {})
    completion_context = {'evidence': missing, 'existing_item_ids': [i['id'] for i in result['items']],
        'existing_nodes': [{k: n[k] for k in ('id', 'label')} for n in model.get('nodes', [])], 'depth': context['depth']}
    addition = await agent.engine.call(state['run_id'], key + f':evidence_completion:{attempt}', 'agent_complete_analysis', completion_context)
    agent.current(state)
    contract.require(isinstance(addition.get('items'), list), 'completion.items', 'array')
    existing = {i['id'] for i in result['items']}
    for item in addition['items']:
        contract.require(isinstance(item, dict) and item.get('id') not in existing, 'completion.items.id', 'new_requirement_id')
        contract.refs(item.get('refs'), {e['id']: e for e in missing}, 'completion.items.refs')
    combined = copy.deepcopy(result)
    combined['items'] += addition['items']
    review = addition.get('evidence_review', [])
    contract.require(isinstance(review, list), 'completion.evidence_review', 'array')
    combined['report'].setdefault('evidence_review', []).extend(review)
    for field in ('nodes', 'edges'):
        entries = addition.get(field, [])
        contract.require(isinstance(entries, list), 'completion.' + field, 'array')
        combined['report']['business_model'][field].extend(entries)
    return combined
