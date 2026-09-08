"""Bounded, local access to evidence stored in SQLite."""

import copy
import json
import re
from collections import Counter

from .schemas import DomainError


MAX_OUTPUT_CHARS = 5_999
MAX_QUERY_CHARS = 500
MAX_READ_BUDGET = 12_000
MIN_READ_BUDGET = 512
MAX_REFS = 10_000
MAX_REF_CHARS = 200

_ENGLISH_WORD = re.compile(r"[a-z0-9]+(?:['’-][a-z0-9]+)*", re.IGNORECASE)
_CHINESE_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")


def _serialized_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def _source_ids(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) or not item for item in value):
        raise DomainError('source_ids 必须为来源 ID 数组')
    if len(value) > MAX_REFS:
        raise DomainError(f'source_ids 不能超过 {MAX_REFS} 项')
    return list(value)


def _terms(text):
    """Return English words and Chinese character ngrams for lexical search."""
    result = [word.casefold() for word in _ENGLISH_WORD.findall(text)]
    for run in _CHINESE_RUN.findall(text):
        result.extend(run[index:index + 2] for index in range(len(run) - 1))
        if len(run) == 1:
            result.append(run)
    return result


def _preview(text, query_terms, query):
    folded = text.casefold()
    positions = []
    direct = folded.find(query.casefold())
    if direct >= 0:
        positions.append(direct)
    for term in query_terms:
        position = folded.find(term.casefold())
        if position >= 0:
            positions.append(position)
    position = min(positions) if positions else 0
    start = max(0, position - 120)
    if start + 400 > len(text):
        start = max(0, len(text) - 400)
    return text[start:start + 400]


class DocumentWorkspace:
    """Caches immutable evidence per source and exposes bounded views of it."""

    def __init__(self, store):
        self.store = store
        self._evidence_by_source = {}
        self._metadata_by_source = {}

    def evidence(self, source_ids):
        source_ids = _source_ids(source_ids)
        missing = list(dict.fromkeys(source_id for source_id in source_ids if source_id not in self._evidence_by_source))
        if missing:
            fetched = self.store.evidence(missing)
            grouped = {source_id: [] for source_id in missing}
            for item in fetched:
                source_id = item.get('source_id')
                if source_id in grouped:
                    grouped[source_id].append(copy.deepcopy(item))
            for source_id in missing:
                self._evidence_by_source[source_id] = tuple(grouped[source_id])
        return [copy.deepcopy(item) for source_id in source_ids for item in self._evidence_by_source[source_id]]

    def catalog(self, source_ids):
        source_ids = _source_ids(source_ids)
        evidence = self.evidence(source_ids)
        grouped = {source_id: [] for source_id in source_ids}
        for item in evidence:
            if item.get('source_id') in grouped:
                grouped[item['source_id']].append(item)

        summaries = []
        for source_id in source_ids:
            if source_id not in self._metadata_by_source:
                source = self.store.get('source', source_id)
                self._metadata_by_source[source_id] = {
                    'id': source_id,
                    'name': source.get('name', ''),
                    'role': source.get('role', ''),
                }
            source = self._metadata_by_source[source_id]
            chunks = grouped[source_id]
            summaries.append({
                **source,
                'chunk_count': len(chunks),
                'first_ref': chunks[0]['id'] if chunks else None,
                'last_ref': chunks[-1]['id'] if chunks else None,
            })

        result = {
            'sources': [],
            'total_sources': len(summaries),
            'total_chunks': len(evidence),
            'omitted_count': len(summaries),
            'truncated': bool(summaries),
        }
        for summary in summaries:
            candidate = {**result, 'sources': result['sources'] + [summary]}
            candidate['omitted_count'] = len(summaries) - len(candidate['sources'])
            candidate['truncated'] = bool(candidate['omitted_count'])
            if _serialized_size(candidate) > MAX_OUTPUT_CHARS:
                break
            result = candidate
        return result

    def search(self, source_ids, query):
        source_ids = _source_ids(source_ids)
        if not isinstance(query, str) or not query.strip():
            raise DomainError('查询不能为空')
        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            raise DomainError(f'查询不能超过 {MAX_QUERY_CHARS} 字符')
        query_terms = _terms(query)
        if not query_terms:
            raise DomainError('查询必须包含英文单词或中文文字')
        wanted = Counter(query_terms)

        ranked = []
        for position, item in enumerate(self.evidence(source_ids)):
            text = item.get('text', '')
            if not isinstance(text, str):
                continue
            found = Counter(_terms(text))
            overlap = sum(min(count, found.get(term, 0)) for term, count in wanted.items())
            direct_count = text.casefold().count(query.casefold())
            if not overlap and not direct_count:
                continue
            score = overlap + (direct_count * (len(query_terms) + 1))
            ranked.append((-score, position, {
                'id': item['id'],
                'location': item.get('location', ''),
                'source_id': item['source_id'],
                'role': item.get('role', ''),
                'text': _preview(text, wanted, query),
            }))
        ranked.sort(key=lambda value: (value[0], value[1]))
        matches = [value[2] for value in ranked]

        result = {'query': query, 'matches': [], 'total_matches': len(matches), 'truncated': bool(matches)}
        for match in matches:
            candidate = {**result, 'matches': result['matches'] + [match]}
            candidate['truncated'] = len(candidate['matches']) < len(matches)
            if _serialized_size(candidate) > MAX_OUTPUT_CHARS:
                break
            result = candidate
        if not matches:
            result['truncated'] = False
        return result

    def read(self, source_ids, refs, budget=MAX_READ_BUDGET):
        source_ids = _source_ids(source_ids)
        if not isinstance(refs, (list, tuple)) or any(not isinstance(ref, str) or not ref for ref in refs):
            raise DomainError('refs 必须为证据引用 ID 数组')
        if len(refs) > MAX_REFS:
            raise DomainError(f'refs 不能超过 {MAX_REFS} 项')
        if any(len(ref) > MAX_REF_CHARS for ref in refs):
            raise DomainError(f'refs 每项不能超过 {MAX_REF_CHARS} 字符')
        if isinstance(budget, bool) or not isinstance(budget, int) or not MIN_READ_BUDGET <= budget <= MAX_READ_BUDGET:
            raise DomainError(f'读取预算必须是 {MIN_READ_BUDGET}–{MAX_READ_BUDGET} 的整数')
        refs = list(refs)
        scoped = {item['id']: item for item in self.evidence(source_ids)}
        unknown = list(dict.fromkeys(ref for ref in refs if ref not in scoped))
        if unknown:
            raise DomainError(f'{len(unknown)} 个证据引用不在当前来源范围内')

        result = {
            'evidence': [],
            'requested_count': len(refs),
            'returned_count': 0,
            'omitted_refs': [],
            'omitted_ref_count': len(refs),
            'truncated': bool(refs),
        }
        returned_refs = 0
        excerpted = False
        for ref in refs:
            item = copy.deepcopy(scoped[ref])
            candidate = copy.deepcopy(result)
            candidate['evidence'].append(item)
            candidate['returned_count'] = len(candidate['evidence'])
            candidate['omitted_ref_count'] = len(refs) - candidate['returned_count']
            candidate['truncated'] = bool(candidate['omitted_ref_count']) or excerpted
            if _serialized_size(candidate) <= budget:
                result = candidate
                returned_refs += 1
                continue

            text = item.get('text')
            if not isinstance(text, str) or not text:
                break
            low, high, best = 0, len(text), None
            while low <= high:
                end = (low + high) // 2
                excerpt = {**item, 'text': text[:end], 'excerpt': {'start': 0, 'end': end, 'total': len(text)}, 'truncated': True}
                trial = copy.deepcopy(result)
                trial['evidence'].append(excerpt)
                trial['returned_count'] = len(trial['evidence'])
                trial['omitted_ref_count'] = len(refs) - trial['returned_count']
                trial['truncated'] = True
                if _serialized_size(trial) <= budget:
                    best = trial
                    low = end + 1
                else:
                    high = end - 1
            if best is not None:
                result = best
                returned_refs += 1
                excerpted = True
            break

        result['omitted_ref_count'] = len(refs) - returned_refs
        result['truncated'] = excerpted or bool(result['omitted_ref_count'])
        for ref in refs[returned_refs:]:
            candidate = {**result, 'omitted_refs': result['omitted_refs'] + [ref]}
            if _serialized_size(candidate) > budget:
                break
            result = candidate
        return result
