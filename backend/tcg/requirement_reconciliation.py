"""Bounded reconciliation of stable, provenance-bearing requirement projections."""
import hashlib
import json
import re
from collections import defaultdict

from .model import TASK_INSTRUCTIONS

TASK = 'reconcile_requirements'
TASK_INSTRUCTIONS[TASK] = '''Reconcile the supplied extracted requirements across sections, without regenerating or editing them.
Return exactly relationships, global_rules, conflicts arrays. Every entry must have type, text,
requirement_ids (exact supplied requirement IDs), refs (exact supporting evidence IDs).
Link definitions to thresholds, roles, exceptions and constraints in other sections. A rule's
requirement_ids enumerate its applicability, not just the defining requirement. Report contradictory
requirements as conflicts with status="unresolved"; never choose an unsupported resolution.
Only report grounded relationships; empty arrays are valid. Requirement projections are extracted
claims with provenance, not original evidence. If original_evidence_included=false, do not claim
original text was inspected. Never claim unprovided requirements or all semantic pairs were checked.'''


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _tokens(row):
    text = row.get('title', '') + ' ' + row.get('description', '')
    words = set(re.findall(r'[a-zA-Z0-9_]{3,}', text.lower()))
    for span in re.findall(r'[\u4e00-\u9fff]+', text):
        words.update(span[i:i + 2] for i in range(len(span) - 1))
    return words


def _validate(result, rows, evidence):
    errors = []
    allowed = {r['id']: set(r.get('refs', [])) for r in rows}
    refs = {e['id'] for e in evidence}
    if not isinstance(result, dict):
        return [{'path': '$', 'message': 'Expected reconciliation object'}]
    for field in ('relationships', 'global_rules', 'conflicts'):
        values = result.get(field)
        if not isinstance(values, list):
            errors.append({'path': field, 'message': 'Expected array'})
            continue
        for i, value in enumerate(values):
            path = f'{field}[{i}]'
            if not isinstance(value, dict):
                errors.append({'path': path, 'message': 'Expected grounded rule object'})
                continue
            ids, cited = value.get('requirement_ids'), value.get('refs')
            valid_ids = isinstance(ids, list) and ids and all(isinstance(x, str) and x in allowed for x in ids)
            valid_refs = isinstance(cited, list) and cited and all(isinstance(x, str) and x in refs for x in cited)
            if not valid_ids or not valid_refs:
                errors.append({'path': path, 'message': 'Use nonempty exact provided requirement_ids and refs'})
            elif not set(cited) <= set().union(*(allowed[x] for x in ids)):
                errors.append({'path': path + '.refs', 'message': 'Refs must support the linked requirements'})
            if not all(isinstance(value.get(k), str) and value[k].strip() for k in ('text', 'type')):
                errors.append({'path': path, 'message': 'Nonempty text and type required'})
            if field == 'conflicts' and value.get('status') != 'unresolved':
                errors.append({'path': path + '.status', 'message': 'Conflicts remain unresolved'})
    return errors


async def reconcile_requirements(engine, run_id, key, items, reports, evidence):
    """Exhaustive input coverage is distinct from bounded semantic candidate coverage.

    Complete projections are used, never sliced original chunks. For large catalogues
    contiguous capacity groups plus lexical and adjacent pairs provide bounded overlap.
    """
    evidence = [e for e in evidence if e.get('role') != 'example']
    evidence_by_id = {e['id']: e for e in evidence}
    rows = [{k: row[k] for k in ('id', 'title', 'description', 'refs') if k in row} for row in items]

    def build(values):
        refs = {ref for row in values for ref in row.get('refs', [])}
        sources = [evidence_by_id[r] for r in sorted(refs) if r in evidence_by_id]
        return {'requirements': values, 'evidence': [{k: e[k] for k in ('id', 'source_id', 'location', 'role') if k in e} for e in sources],
                'original_evidence_included': False, 'scope': 'Only supplied requirement projections; reconcile relationships without replacing items.'}

    groups, omitted = [], []
    eligible = []
    for row in rows:
        if engine.fits(TASK, {**build([row]), 'budget_margin': 'x' * 3000}):
            eligible.append(row)
        else:
            omitted.append(row['id'])
    groups = engine.capacity_groups(TASK, eligible, build)
    initial_count = len(groups)
    candidate_pairs = set()
    if initial_count > 1:
        # Posting lists are bounded: this is candidate retrieval, not all-pairs review.
        postings = defaultdict(list)
        group_index = {r['id']: i for i, group in enumerate(groups) for r in group}
        for i, row in enumerate(eligible):
            scores = defaultdict(int)
            for token in sorted(_tokens(row)):
                for j in postings[token]:
                    scores[j] += 1
            candidates = sorted(scores, key=lambda j: (-scores[j], j))[:8]
            if i:
                candidates.append(i - 1)
            for j in candidates:
                if group_index[row['id']] != group_index[eligible[j]['id']]:
                    candidate_pairs.add((j, i))
            for token in _tokens(row):
                postings[token].append(i)
                if len(postings[token]) > 16:
                    postings[token].pop(0)
        for a, b in sorted(candidate_pairs):
            pair = [eligible[a], eligible[b]]
            if engine.fits(TASK, {**build(pair), 'budget_margin': 'x' * 3000}):
                groups.append(pair)
    result = {'relationships': [], 'global_rules': [], 'conflicts': []}
    completed, included, included_refs = [], set(), set()
    group_coverage = []
    seen = set()
    for group in groups:
        context = build(group)
        source_rows = [evidence_by_id[e['id']] for e in context['evidence']]
        original = {**context, 'evidence': source_rows, 'original_evidence_included': True}
        if engine.fits(TASK, {**original, 'budget_margin': 'x' * 3000}):
            context = original
        group_id = _digest({'policy': 'reconciliation-v1', 'prompt': TASK_INSTRUCTIONS[TASK], 'key': key, 'context': context})
        response = await engine.validated(run_id, key + ':reconcile:' + group_id, TASK, context,
                                          lambda value: _validate(value, group, source_rows))
        completed.append(group_id)
        group_coverage.append({'group_id': group_id, 'requirement_ids': [r['id'] for r in group],
                               'evidence_ids': [e['id'] for e in source_rows],
                               'original_evidence_included': context['original_evidence_included']})
        included.update(r['id'] for r in group)
        included_refs.update(e['id'] for e in source_rows)
        for field in result:
            for value in response[field]:
                value = {k: value[k] for k in ('type', 'text', 'requirement_ids', 'refs', 'status') if k in value}
                value['requirement_ids'] = sorted(set(value['requirement_ids']))
                value['refs'] = sorted(set(value['refs']))
                identity = _digest([field, value])
                if identity not in seen:
                    seen.add(identity)
                    result[field].append({'id': 'REC-' + identity[:16], **value})
    result['reconciliation_coverage'] = {
        'scope': 'bounded_requirement_projection_groups_and_lexical_adjacent_candidates',
        'semantic_completeness': 'not_claimed', 'all_pairs_examined': False,
        'partial': bool(initial_count > 1 or omitted),
        'requested_requirement_ids': [r['id'] for r in rows],
        'included_requirement_ids': sorted(included), 'omitted_requirement_ids': omitted,
        'included_evidence_ids': sorted(included_refs),
        'completed_group_ids': completed, 'groups': group_coverage, 'capacity_group_count': initial_count,
        'omission_reason': 'Single complete requirement projection exceeds configured request capacity' if omitted else None,
        'candidate_pair_count': len(candidate_pairs), 'completed_candidate_pair_count': len(groups) - initial_count,
        'sections': [{'evidence_id': e['id'], 'source_id': e.get('source_id'), 'location': e.get('location'),
                      'status': 'included' if e['id'] in included_refs else 'not_linked_to_requirement'} for e in evidence],
        'limitations': ['Requirements are preserved extracted projections; original chunks are included only when they fit intact.',
                        'Lexical and adjacent candidates do not establish comprehensive semantic cross-pair coverage.'],
    }
    return result
