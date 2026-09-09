"""Fit optional reads around immutable business inputs; keep full results local."""
import copy

from .incremental_workspace import fingerprint, _size
from .schemas import DomainError


def archive_result(session, tool, result):
    entry = {'tool': tool, 'result': result}
    result_id = 'read_' + fingerprint(entry)
    session.setdefault('tool_results', {})[result_id] = copy.deepcopy(entry)
    return result_id


def result_page(session, result_id, cursor=0, budget=4000):
    """Page whole list entries/map values, never truncate business values."""
    entry = session.get('tool_results', {}).get(result_id)
    if entry is None:
        raise DomainError('读取结果不属于当前工作项；请使用本轮提供的 result_id。')
    result = entry['result']
    fixed, rows = {}, []
    for field, value in result.items():
        if isinstance(value, list):
            fixed[field] = []
            rows.extend((field, None, item) for item in value)
        elif isinstance(value, dict):
            fixed[field] = {}
            rows.extend((field, key, item) for key, item in value.items())
        else:
            fixed[field] = value
    if type(cursor) is not int or not 0 <= cursor <= len(rows):
        raise DomainError('读取结果分页位置无效。')
    page = {'result_id': result_id, 'cursor': cursor, 'next_cursor': cursor,
            'total_entries': len(rows)}
    output = {**copy.deepcopy(fixed), 'page': page}
    if _size(output) > budget:
        return {'page': page, 'deferred': True}
    for index in range(cursor, len(rows)):
        field, key, item = rows[index]
        candidate = copy.deepcopy(output)
        if key is None:
            candidate[field].append(copy.deepcopy(item))
        else:
            candidate[field][key] = copy.deepcopy(item)
        candidate['page']['next_cursor'] = index + 1
        if _size(candidate) > budget:
            break
        output = candidate
    if output['page']['next_cursor'] == len(rows):
        output['page']['next_cursor'] = None
    return output


def fit_context(base, session, budget):
    """Rebuild on every attempt, including sessions saved by older versions.

    Mandatory job evidence, accepted rules, instructions and profile are pinned.
    Optional read bodies and tool results share the remaining serialized budget.
    Omitted whole entries stay in the session and have explicit retrieval cursors.
    """
    context = copy.deepcopy(base)
    pinned = {fingerprint(e) for e in context.get('evidence', [])}
    extras = [e for e in session.get('evidence', []) if fingerprint(e) not in pinned]
    observations = session.get('observations', [])
    context.setdefault('evidence', []).extend(copy.deepcopy(extras))
    if observations:
        context['observations'] = copy.deepcopy(observations)
    if _size(context) <= budget:
        return context

    context = copy.deepcopy(base)
    evidence_id = archive_result(session, 'read_evidence', {'evidence': extras}) if extras else None
    window = {'reason': 'supplemental_results_paged', 'full_results_saved': True,
              'omitted_evidence': len(extras)}
    if evidence_id:
        window['evidence_page'] = {'result_id': evidence_id, 'cursor': 0}
    context['context_window'] = window
    context['observations'] = []
    # Reserve space for fresh evidence, then share the rest among tool results.
    available = budget - _size(context) - 100
    reserve = min(3500, max(0, available // 3)) if extras else 0
    share = max(0, (available - reserve) // max(1, len(observations)) - 80)
    for observation in observations:
        prior = observation['result'].get('page', {})
        if prior.get('result_id') in session.get('tool_results', {}) and observation['tool'] == 'read_evidence':
            # Evidence bodies are fitted separately below. Keep the read receipt
            # and its original continuation instead of paging pagination metadata.
            result = copy.deepcopy(observation['result'])
            if _size(result) > share:
                result = {'page': copy.deepcopy(prior), 'deferred': True}
            context['observations'].append({'tool': observation['tool'], 'result': result})
            continue
        if prior.get('result_id') in session.get('tool_results', {}):
            # A continuation is a window onto the original result, not a new
            # result containing only the preceding page's entries.
            result_id, cursor = prior['result_id'], prior['cursor']
        else:
            result_id = archive_result(session, observation['tool'], observation['result'])
            cursor = 0
        result = result_page(session, result_id, cursor=cursor, budget=share)
        context['observations'].append({'tool': observation['tool'], 'result': result})

    selected = []
    for item in reversed(extras):
        candidate = copy.deepcopy(context)
        candidate.setdefault('evidence', []).extend([item] + selected)
        if _size(candidate) <= budget:
            selected.insert(0, copy.deepcopy(item))
    context.setdefault('evidence', []).extend(selected)
    window['omitted_evidence'] = len(extras) - len(selected)
    if _size(context) > budget:
        # Mandatory data is never shortened just to make a call appear valid.
        raise DomainError('当前工作项的必要内容未给补充资料留下分页空间；请缩小当前工作范围。')
    return context
