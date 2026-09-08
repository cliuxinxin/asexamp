"""Task-specific model views. Raw documents and full artifacts stay local."""
import copy
import json

from .graph import routing_excerpt
from .schemas import DomainError
from .storage import public

MODEL_CONTEXT_BUDGET = 30_000


def compact_items(items, limit=40):
    result = []
    for item in items[:limit]:
        compact = {}
        for key, value in item.items():
            if key not in ('id', 'title', 'refs', 'requirement_ids', 'branch_ids', 'scenario_id'):
                continue
            if isinstance(value, str):
                compact[key] = routing_excerpt(value, 240)
            elif isinstance(value, list):
                compact[key] = value[:4]
                compact[key + '_count'] = len(value)
                compact[key + '_omitted_count'] = max(0, len(value) - 4)
            else:
                compact[key] = value
        result.append(compact)
    return result


def _projection(total, returned, truncated_content=False):
    return {'total_count': total, 'returned_count': returned, 'omitted_count': max(0, total - returned),
            'truncated': truncated_content or returned < total}


def _strings(values, limit=12, text_budget=400):
    values = values if isinstance(values, list) else []
    selected = values[-limit:]
    strings = [value for value in selected if isinstance(value, str)]
    projected = [routing_excerpt(value, text_budget) for value in strings]
    return projected, _projection(len(values), len(projected), any(value != result for value, result in zip(strings, projected)))


def project_decision(decision, text_budget=400, ref_limit=12):
    if not isinstance(decision, dict):
        return None
    if any(key.endswith(('_truncated', '_omitted_count')) for key in decision):
        return copy.deepcopy(decision)
    result = {key: copy.deepcopy(value) for key, value in decision.items()
              if key in ('id', 'status', 'run_id', 'epoch', 'mode', 'instruction_recorded')}
    for key in ('summary', 'supersession', 'content'):
        if isinstance(decision.get(key), str):
            result[key] = routing_excerpt(decision[key], text_budget)
            if result[key] != decision[key]:
                result[key + '_characters'] = len(decision[key])
                result[key + '_truncated'] = True
    for key in ('refs', 'superseded_by'):
        if isinstance(decision.get(key), list):
            result[key] = decision[key][:ref_limit]
            if len(decision[key]) > ref_limit:
                result[key + '_count'] = len(decision[key])
                result[key + '_omitted_count'] = len(decision[key]) - len(result[key])
    return result


def project_memory(memory, decision_limit=12, list_limit=12, text_budget=400, ref_limit=40,
                   decision_ref_limit=12):
    """Create a bounded model projection while leaving durable chat memory untouched."""
    memory = memory if isinstance(memory, dict) else {}
    if isinstance(memory.get('_projection'), dict):
        return copy.deepcopy(memory)
    result = {key: copy.deepcopy(value) for key, value in memory.items() if key in ('scope_confirmed', 'updated_at')}
    projection = {}
    decisions = memory.get('decisions', []) if isinstance(memory.get('decisions', []), list) else []
    selected_decisions = decisions[-decision_limit:] if decision_limit else []
    result['decisions'] = [project_decision(item, text_budget, decision_ref_limit)
                           for item in selected_decisions if isinstance(item, dict)]
    projection['decisions'] = _projection(len(decisions), len(result['decisions']), any(
        item.get('summary_truncated') or item.get('content_truncated') or item.get('supersession_truncated')
        for item in result['decisions']))
    for field in ('scope', 'open_questions', 'assumptions'):
        result[field], projection[field] = _strings(memory.get(field, []), list_limit, text_budget)
    refs = memory.get('source_refs', []) if isinstance(memory.get('source_refs', []), list) else []
    result['source_refs'] = refs[-ref_limit:] if ref_limit else []
    projection['source_refs'] = _projection(len(refs), len(result['source_refs']))
    if memory.get('clarification_decision'):
        result['clarification_decision'] = project_decision(memory['clarification_decision'], text_budget)
    result['_projection'] = projection
    return result


def project_strategy(strategy):
    if not isinstance(strategy, dict):
        return strategy
    if isinstance(strategy.get('_projection'), dict):
        return copy.deepcopy(strategy)
    rationale = strategy.get('rationale', '') if isinstance(strategy.get('rationale', ''), str) else ''
    result = {'depth': strategy.get('depth'), 'rationale': routing_excerpt(rationale, 800)}
    projection = {'rationale': {'characters': len(rationale), 'truncated': result['rationale'] != rationale}}
    for field in ('techniques', 'scope'):
        result[field], projection[field] = _strings(strategy.get(field, []), 12, 400)
    result['_projection'] = projection
    return result


def project_profile(profile):
    profile = profile if isinstance(profile, dict) else {}
    if isinstance(profile.get('_projection'), dict):
        return copy.deepcopy(profile)
    result, projection = {}, {}
    for key, value in profile.items():
        if isinstance(value, str):
            budget = 1000 if key in ('additional_rules', 'scope') else 300
            result[key] = routing_excerpt(value, budget)
            projection[key] = {'characters': len(value), 'truncated': result[key] != value}
        elif isinstance(value, list):
            result[key], projection[key] = _strings(value, 12, 200)
        elif isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
    result['_projection'] = projection
    return result


def project_coverage(coverage, relevant_requirements=None, relevant_branches=None):
    if not isinstance(coverage, dict):
        return coverage
    requirements, branches = set(relevant_requirements or []), set(relevant_branches or [])
    gaps = coverage.get('gaps', []) if isinstance(coverage.get('gaps'), list) else []
    if requirements or branches:
        gaps = [gap for gap in gaps if isinstance(gap, dict) and (
            gap.get('kind') == 'requirement' and gap.get('id') in requirements or
            gap.get('kind') == 'branch' and gap.get('id') in branches)]
    selected = gaps[:80]
    return {key: value for key, value in coverage.items() if key != 'gaps'} | {
        'gaps': [{key: routing_excerpt(value, 300) if isinstance(value, str) else value
                  for key, value in gap.items()} for gap in selected],
        'gap_projection': _projection(len(gaps), len(selected)),
    }


def model_view(context, purpose, budget=MODEL_CONTEXT_BUDGET):
    """Bound common durable fields only in the model-facing copy."""
    result = copy.deepcopy(context)
    result['memory'] = project_memory(result.get('memory', {}))
    result['profile'] = project_profile(result.get('profile', {}))
    if result.get('strategy'):
        result['strategy'] = project_strategy(result['strategy'])
    if result.get('clarification_decision'):
        result['clarification_decision'] = project_decision(result['clarification_decision'], 500)
    for field in ('assumptions', 'deferred_questions', 'questions'):
        if field in result and field + '_projection' not in result:
            result[field], result[field + '_projection'] = _strings(result[field], 12, 400)
    if 'coverage' in result:
        if not isinstance(result['coverage'], dict) or 'gap_projection' not in result['coverage']:
            result['coverage'] = project_coverage(result['coverage'])
    if 'conversation_projection' not in result:
        conversation = result.get('conversation', []) if isinstance(result.get('conversation'), list) else []
        result['conversation'] = [{**message, 'content': routing_excerpt(message.get('content', ''), 300)}
                                  for message in conversation[-12:] if isinstance(message, dict)]
        result['conversation_projection'] = _projection(len(conversation), len(result['conversation']), any(
            message.get('content') != projected.get('content') for message, projected in zip(conversation[-12:], result['conversation'])))
    if 'instruction_projection' not in result:
        instructions = result.get('instructions', []) if isinstance(result.get('instructions'), list) else []
        result['instructions'] = [{**instruction, 'content': routing_excerpt(instruction.get('content', ''), 500),
                                   'refs': instruction.get('refs', [])[:8],
                                   'ref_count': len(instruction.get('refs', []))}
                                  for instruction in instructions[-8:] if isinstance(instruction, dict)]
        result['instruction_projection'] = _projection(len(instructions), len(result['instructions']), any(
            instruction.get('content') != projected.get('content') or len(instruction.get('refs', [])) > 8
            for instruction, projected in zip(instructions[-8:], result['instructions'])))
    if purpose in ('plan', 'summary', 'feedback') and len(json.dumps(result, ensure_ascii=False)) > budget:
        for field in ('analysis', 'scenarios', 'cases'):
            if isinstance(result.get(field), list) and len(result[field]) > 6:
                total = result.get(field + '_count', len(result[field]))
                result[field] = result[field][:6]
                result[field + '_projection'] = _projection(total, len(result[field]))
        for field in ('artifact', 'output'):
            artifact = result.get(field)
            if isinstance(artifact, dict) and isinstance(artifact.get('items'), list) and len(artifact['items']) > 6:
                artifact['items'] = artifact['items'][:6]
                artifact['item_projection'] = _projection(artifact.get('item_count', len(artifact['items'])), len(artifact['items']))
        graph = result.get('business_model')
        if isinstance(graph, dict):
            for field, count_field in (('nodes', 'node_count'), ('edges', 'edge_count')):
                if isinstance(graph.get(field), list) and len(graph[field]) > 6:
                    graph[field] = graph[field][:6]
                    graph[field + '_projection'] = _projection(graph.get(count_field, len(graph[field])), len(graph[field]))
        if isinstance(result.get('evidence'), list) and len(result['evidence']) > 24:
            total = result.get('evidence_projection', {}).get('total_count', len(result['evidence']))
            result['evidence'] = result['evidence'][:24]
            result['evidence_projection'] = _projection(total, len(result['evidence']))
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        for field in ('assumptions', 'deferred_questions', 'questions'):
            if isinstance(result.get(field), list):
                previous = result.get(field + '_projection', {})
                total = previous.get('total_count', len(result[field]))
                result[field] = [routing_excerpt(value, 200) for value in result[field][:4]]
                result[field + '_projection'] = _projection(total, len(result[field]), True)
        memory = result.get('memory')
        if isinstance(memory, dict):
            metadata = memory.get('_projection', {})
            decisions = memory.get('decisions', [])[-4:]
            memory['decisions'] = [{**decision,
                **({'summary': routing_excerpt(decision['summary'], 200)} if isinstance(decision.get('summary'), str) else {}),
                **({'content': routing_excerpt(decision['content'], 200)} if isinstance(decision.get('content'), str) else {}),
                **({'refs': decision['refs'][:2]} if isinstance(decision.get('refs'), list) else {})}
                for decision in decisions]
            total = metadata.get('decisions', {}).get('total_count', len(decisions))
            metadata['decisions'] = _projection(total, len(decisions), True)
            for field in ('scope', 'open_questions', 'assumptions'):
                values = [routing_excerpt(value, 200) for value in memory.get(field, [])[-4:]]
                total = metadata.get(field, {}).get('total_count', len(values))
                memory[field] = values
                metadata[field] = _projection(total, len(values), True)
            refs = memory.get('source_refs', [])[-12:]
            total = metadata.get('source_refs', {}).get('total_count', len(refs))
            memory['source_refs'] = refs
            metadata['source_refs'] = _projection(total, len(refs), True)
            memory['_projection'] = metadata
        strategy = result.get('strategy')
        if isinstance(strategy, dict):
            metadata = strategy.get('_projection', {})
            for field in ('scope', 'techniques'):
                values = [routing_excerpt(value, 200) for value in strategy.get(field, [])[:4]]
                total = metadata.get(field, {}).get('total_count', len(values))
                strategy[field] = values
                metadata[field] = _projection(total, len(values), True)
            strategy['rationale'] = routing_excerpt(strategy.get('rationale', ''), 400)
            strategy['_projection'] = metadata
        result['conversation'] = result.get('conversation', [])[-6:]
        total = result.get('conversation_projection', {}).get('total_count', len(result['conversation']))
        result['conversation_projection'] = _projection(total, len(result['conversation']), True)
        result['instructions'] = result.get('instructions', [])[-4:]
        total = result.get('instruction_projection', {}).get('total_count', len(result['instructions']))
        result['instruction_projection'] = _projection(total, len(result['instructions']), True)
    if purpose in ('plan', 'summary', 'feedback'):
        # Independently bounded previews can still exceed the aggregate budget.
        # Shrink previews together, retaining the original totals at every pass.
        def clip(owner, field, limit, total_key, metadata_key):
            values = owner.get(field)
            if not isinstance(values, list) or len(values) <= limit:
                return
            total = owner.get(metadata_key, {}).get('total_count', owner.get(total_key, len(values)))
            owner[field] = values[:limit]
            owner[metadata_key] = _projection(total, limit, True)

        for limit in (4, 2, 1):
            if len(json.dumps(result, ensure_ascii=False)) <= budget:
                break
            for field in ('analysis', 'scenarios', 'cases'):
                clip(result, field, limit, field + '_count', field + '_projection')
            for field in ('artifact', 'output'):
                if isinstance(result.get(field), dict):
                    clip(result[field], 'items', limit, 'item_count', 'item_projection')
            if isinstance(result.get('business_model'), dict):
                for field in ('nodes', 'edges'):
                    clip(result['business_model'], field, limit, field[:-1] + '_count', field + '_projection')
            clip(result, 'evidence', max(8, limit * 4), 'evidence_count', 'evidence_projection')
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        raise DomainError(f'{purpose} 模型上下文超过 {budget} 字符预算；完整资料和已接受结果仍保留。')
    return result


def context_for(agent, state, purpose, **extra):
    run = agent.store.run(state['run_id'])
    source_ids = run['_source_ids']
    depth = run.get('agent', {}).get('depth', 'standard')
    from .agent_contracts import DEPTH_GUIDANCE
    request = {k: run['_request'][k] for k in ('content', 'intent', 'mode')}
    original = request['content']
    request.update(content=routing_excerpt(original, 2000), original_characters=len(original),
                   content_preview_truncated=len(json.dumps(original, ensure_ascii=False)) > 2000,
                   experience='agent', confirm_strategy=run['_request'].get('confirm_strategy', True))
    context = {'request': request, 'profile': run['_profile'],
        'conversation': [{**m, 'content': routing_excerpt(m['content'], 500)} for m in run['_conversation']],
        'memory': copy.deepcopy(agent.store.get('chat', run['chat_id']).get('memory', {})),
        'instructions': [{**i, 'content': routing_excerpt(i['content'], 1000)} for i in run.get('_instructions', [])[-16:]],
        'depth': depth, 'requested_depth': run['_request'].get('depth', 'auto'), 'depth_guidance': DEPTH_GUIDANCE[depth],
        'artifact': public(run['_artifact_snapshot']) if run.get('_artifact_snapshot') else None,
        'selected_ids': run['_request'].get('selected_ids'), 'documents': agent.documents.catalog(source_ids)}
    full = agent.documents.evidence(source_ids)
    by_id = {e['id']: e for e in full}
    references, priority = [], []

    def add_refs(target, refs):
        for ref in refs:
            if ref in by_id and ref not in target:
                target.append(ref)

    if state.get('output_ref'):
        output = agent.store.get('artifact', state['output_ref'])
        add_refs(priority, (ref for item in output['items'] for ref in item.get('refs', [])))
    for key, name in (('analysis_ref', 'analysis'), ('scenario_ref', 'scenarios'), ('cases_ref', 'cases')):
        if state.get(key):
            artifact = agent.store.get('artifact', state[key])
            context[name] = artifact['items']
            add_refs(references, (ref for item in artifact['items'] for ref in item.get('refs', [])))
            if name == 'analysis':
                report = artifact['report']
                context.update(business_model=report['business_model'], strategy=report['strategy'],
                    assumptions=report.get('assumptions', []), deferred_questions=report.get('deferred_questions', []))
    if context['artifact']:
        selected = context['selected_ids']
        add_refs(references, (ref for item in context['artifact']['items'] if not selected or item['id'] in selected
                              for ref in item.get('refs', [])))
    decision = agent.continuation(run)
    if decision:
        context['clarification_decision'] = decision
    observation = run.get('_document_observation')
    if observation and observation.get('epoch') == state.get('epoch') and purpose not in ('summary', 'feedback'):
        context['document_observation'] = observation
        observed = observation['result'].get('evidence', observation['result'].get('matches', []))
        add_refs(priority, (item['id'] for item in observed))
    else:
        observation = None
    metadata_only = purpose in ('plan', 'summary', 'feedback', 'analyze', 'check')
    if metadata_only:
        ids = priority + [ref for ref in references if ref not in priority]
        if not ids:
            ids = list(by_id)
        total_ids = len(ids)
        ids = ids[:40]
        context['evidence'] = [{k: by_id[ref][k] for k in ('id', 'source_id', 'role', 'location')}
                               for ref in ids if ref in by_id]
        context['evidence_projection'] = _projection(total_ids, len(ids))
    else:
        if purpose == 'query':
            if observation and observation['tool'] == 'read_document':
                ids = [e['id'] for e in observation['result']['evidence']]
            else:
                ids = [m['id'] for m in agent.documents.search(source_ids, original[:500])['matches']]
        elif purpose in ('learn_template', 'import_cases'):
            ids = list(by_id)
        else:
            ids = [e['id'] for e in full if e['id'] in references or e['role'] in ('change', 'clarification')]
        reading = agent.documents.read(source_ids, ids, budget=12000)
        context['evidence'] = reading['evidence']
        context['document_access'] = {k: v for k, v in reading.items() if k != 'evidence'}
        seen = {e['id'] for e in context['evidence']}
        context['evidence'] += [{k: by_id[ref][k] for k in ('id', 'source_id', 'role', 'location')} | {'via': 'accepted_artifact'}
                                for ref in references if ref not in seen]
    if purpose in ('plan', 'summary', 'feedback'):
        for field in ('analysis', 'scenarios', 'cases'):
            if field in context:
                context[field + '_count'] = len(context[field])
                context[field] = compact_items(context[field])
        if context.get('business_model'):
            graph = context['business_model']
            context['business_model'] = {'node_count': len(graph['nodes']), 'edge_count': len(graph['edges']),
                'nodes': [{k: routing_excerpt(n[k], 300) if k == 'label' else n[k] for k in ('id', 'label')} for n in graph['nodes'][:30]],
                'edges': [{k: routing_excerpt(n[k], 300) if k == 'label' else n[k] for k in ('id', 'label', 'from', 'to')} for n in graph['edges'][:30]],
                'node_omitted_count': max(0, len(graph['nodes']) - 30), 'edge_omitted_count': max(0, len(graph['edges']) - 30)}
        if context['artifact']:
            artifact = context['artifact']
            context['artifact'] = {'id': artifact['id'], 'type': artifact['type'], 'title': routing_excerpt(artifact['title'], 300),
                                   'item_count': len(artifact['items']), 'items': compact_items(artifact['items'])}
    context.update(extra)
    return model_view(context, purpose) if purpose in ('plan', 'summary', 'feedback') else context
