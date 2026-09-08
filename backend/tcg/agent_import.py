"""Exhaustive bounded input batching for importing existing test cases."""
import copy
import hashlib
import json

from . import agent_contracts as contract
from .agent_analysis import evidence_batches
from .agent_context import compact_items, model_view
from .schemas import DomainError, validate_items


async def import_cases(agent, state, context):
    run = agent.current(state)
    evidence = agent.documents.evidence(run['_source_ids'])
    groups = evidence_batches(evidence)
    if not groups:
        raise DomainError('没有可导入的用例来源。')
    imported = []
    for batch_index, group in enumerate(groups):
        agent.insight(run['id'], f'读取并导入第 {batch_index + 1}/{len(groups)} 批，共 {len(group)} 段；其他正文留在本地。',
                      [group[0]['id'], group[-1]['id']], 'decision')
        fingerprint = hashlib.sha256(json.dumps(group, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
        local, cursor, used = [], None, set()
        base = {key: copy.deepcopy(value) for key, value in context.items()
                if key not in ('evidence', 'document_access', 'document_observation', 'artifact', 'cases')}
        for page in range(200):
            page_context = {**base, 'evidence': group, 'previous_items': compact_items(local, 80),
                            'previous_item_count': len(local), 'cursor': cursor,
                            'batch_index': batch_index, 'batch_count': len(groups), 'input_batch': True}
            page_context = model_view(page_context, 'import_cases', budget=40_000)
            scoped = {item['id']: item for item in group}

            def validator(result):
                validate_items('cases', result.get('items'), scoped)
                contract.require(isinstance(result.get('has_more'), bool), 'has_more', 'boolean')
                return result

            call_state = {**state, 'iteration': f'{state["iteration"]}:import:{fingerprint}:{page}'}
            result = await agent.call(call_state, 'import_cases', page_context, validator)
            agent.current(state)
            contract.require(not {item['id'] for item in local}.intersection(item['id'] for item in result['items']),
                             'items.id', 'new_unique_ids')
            local.extend(result['items'])
            if not result['has_more']:
                break
            next_cursor = result.get('next_cursor')
            contract.require(bool(result['items']) and isinstance(next_cursor, str) and next_cursor and next_cursor not in used,
                             'next_cursor', 'new_cursor_and_new_items')
            cursor = next_cursor
            used.add(cursor)
        else:
            raise DomainError('导入输出分页达到执行预算，未接受不完整结果。')
        prefix = f'b{batch_index + 1}_'
        accepted = [{**item, 'id': contract.namespaced_id(prefix, item['id'], 200)} for item in local] if len(groups) > 1 else local
        contract.require(not {item['id'] for item in imported}.intersection(item['id'] for item in accepted),
                         'items.id', 'unique_ids_across_input_batches')
        imported.extend(accepted)
        with agent.store.transaction():
            agent.current(state)
            agent.update(run['id'], document_progress={
                'completed_batches': batch_index + 1, 'total_batches': len(groups),
                'reviewed_chunks': sum(len(batch) for batch in groups[:batch_index + 1]),
                'total_chunks': len(evidence), 'kind': 'case_import',
            })
    contract.require(bool(imported), 'items', 'nonempty_imported_cases')
    return imported
