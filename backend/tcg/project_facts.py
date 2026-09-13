"""Explicit project facts use the existing source/chunk evidence identities."""
import copy

from .schemas import DomainError


def normalize_scope(value=None):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {'module', 'version'} or any(
            not isinstance(item, str) or len(item) > 200 for item in value.values()):
        raise DomainError('规则范围需要 module / version 文本，每项最多 200 字符')
    return {key: item.strip() for key, item in value.items() if item.strip()}


def scope_matches(fact_scope, requested=None):
    """Unscoped historical facts apply project-wide; selectors narrow known axes."""
    requested = normalize_scope(requested)
    return all(not fact_scope.get(key) or fact_scope[key] == value for key, value in requested.items())


def fact_record(source):
    """Additive, backwards-compatible projection; never rewrite source evidence."""
    status = source.get('status') or ('superseded' if source.get('_superseded_by') else 'confirmed')
    return {'id': source['id'], 'source_id': source['id'], 'project_id': source['project_id'],
            'chat_id': source['chat_id'], 'name': source['name'], 'text': source['_text'],
            'status': status, 'scope': copy.deepcopy(source.get('scope', {})),
            'fact_key': source.get('fact_key', ''), 'claims': copy.deepcopy(source.get('claims', [])),
            'created_at': source.get('_shared_at', source['created_at']),
            'active': bool(source.get('_project_shared') and source.get('_active') and status == 'confirmed'),
            'provenance': copy.deepcopy(source.get('provenance') or {
                'chat_id': source['chat_id'], 'source_id': source['id'], 'confirmed_by': '历史提交用户',
                'confirmed_at': source.get('_shared_at', source['created_at']), 'origin': 'legacy_clarification'}),
            'supersedes': copy.deepcopy(source.get('supersedes') or ([source['_supersedes']] if source.get('_supersedes') else [])),
            'superseded_by': source.get('superseded_by') or source.get('_superseded_by')}


class FactConflict(DomainError):
    def __init__(self, conflicts):
        self.conflicts = conflicts
        item = conflicts[0]
        scope = ' / '.join(item['scope'].values()) or '本项目'
        super().__init__(f'「{item["key"]}」在 {scope} 已确认「{item["current"]}」；'
                         f'本次回答是「{item["proposed"]}」。是否用本次回答替代这条规则？', 409)


def check_fact_conflicts(sources, source, scope, claims, supersedes):
    conflicts = []
    for previous in sources:
        if previous['id'] == source['id'] or previous['id'] in supersedes or not scope_matches(previous.get('scope', {}), scope):
            continue
        old_claims = previous.get('claims') or ([{'key': previous['fact_key'], 'content': previous['_text']}]
                    if previous.get('fact_key') else [])
        for claim in claims:
            for old in old_claims:
                if old['key'] == claim['key'] and old['content'].strip() != claim['content'].strip():
                    conflicts.append({'source_id': previous['id'], 'key': claim['key'],
                        'scope': previous.get('scope', {}), 'current': old['content'], 'proposed': claim['content']})
    if conflicts:
        raise FactConflict(conflicts)
