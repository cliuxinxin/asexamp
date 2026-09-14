"""Stage exact human evidence and an independent target-only edit preview."""
import copy

from . import dependencies as deps
from .schemas import DomainError


def _scope(value, artifact):
    if (value.get('chat_id'), value.get('project_id')) != (artifact['chat_id'], artifact['project_id']):
        raise DomainError('上游成果不属于当前对话', 404)
    return value


def _rules(artifact):
    return {key: artifact.get('report', {}).get(key) or [] for key in ('in_scope', 'out_of_scope', 'assumptions')}


def link_report(report, kind, parents, used_parent_ids=(), used_requirement_ids=(),
                existing_items=(), new_item_ids=()):
    """Record newly consumed links without clearing pre-existing stale hashes."""
    result = copy.deepcopy(report)
    by_kind = {p['type']: p for p in parents}
    parent_kind = 'analysis' if kind == 'scenarios' else 'scenarios'
    parent = by_kind.get(parent_kind)
    if kind not in ('scenarios', 'cases') or not parent:
        return result
    lineage = result.setdefault('lineage', {})
    prefix = 'analysis' if kind == 'scenarios' else 'scenario'
    prior_revision = lineage.get(prefix + '_revision')
    if prior_revision:
        versions = lineage.setdefault(prefix + '_revisions', {})
        for row in existing_items:
            ids = row.get('requirement_ids', []) if kind == 'scenarios' else [row.get('scenario_id')]
            for item_id in ids:
                if item_id:
                    versions.setdefault(item_id, prior_revision)
    if new_item_ids:
        lineage.setdefault(prefix + '_item_revisions', {}).update({item_id: parent['revision'] for item_id in new_item_ids})
    lineage.update({prefix + '_artifact_id': parent['id'], prefix + '_revision': parent['revision']})
    lineage.setdefault('parent_rules_hash', deps.digest(_rules(parent)))
    hashes = lineage.setdefault('parent_item_hashes', {})
    for row in parent['items']:
        if row['id'] in used_parent_ids:
            hashes.setdefault(row['id'], deps.digest(row))
    analysis = by_kind.get('analysis')
    if kind == 'cases' and analysis:
        lineage.update(analysis_artifact_id=analysis['id'], analysis_revision=analysis['revision'])
        lineage.setdefault('analysis_rules_hash', deps.digest(_rules(analysis)))
        hashes = lineage.setdefault('analysis_item_hashes', {})
        for row in analysis['items']:
            if row['id'] in used_requirement_ids:
                hashes.setdefault(row['id'], deps.digest(row))
    return result


def normalize_independent_rows(kind, items, existing, reason, *, authorized_ids=None,
                               allow_new=False, source_id=None, force=False):
    """Canonicalize explicit N/A edits while rejecting an AI's accidental unlink.

    A manual table edit authorizes the empty cells it submits (authorized_ids=None).
    Model callers must supply the exact selected IDs or allow_new for an explicit add.
    Server markers from a model/client are discarded; saved markers are preserved.
    """
    if kind not in ('scenarios', 'cases'):
        return copy.deepcopy(items)
    field, empty = ('requirement_ids', []) if kind == 'scenarios' else ('scenario_id', '')
    previous = {row['id']: row for row in existing}
    allowed = None if authorized_ids is None else set(authorized_ids)
    output = copy.deepcopy(items)
    for row in output:
        if not isinstance(row, dict):
            continue
        old = previous.get(row.get('id'))
        row.pop('_independent_origin', None)
        authorized = allowed is None or row.get('id') in allowed or (old is None and allow_new)
        if force and authorized:
            row[field] = copy.deepcopy(empty)
        if row.get(field) != empty:
            continue
        if old and old.get(field) == empty and old.get('_independent_origin'):
            row['_independent_origin'] = copy.deepcopy(old['_independent_origin'])
        elif authorized:
            row['_independent_origin'] = {'reason': reason or '用户明确设置为 N/A'}
            if source_id:
                row['_independent_origin']['source_id'] = source_id
        elif old is None or old.get(field) != empty:
            raise DomainError('修改意外移除了上游关联；仅在用户明确要求 N/A 或独立新增时才能解除关联')
    return output


def prepare_dialogue(store, artifact, content, *, add=False, parent_id=None):
    """Stage exact human evidence; never create or mutate upstream artifacts."""
    if not isinstance(content, str) or not content.strip():
        raise DomainError('请在当前消息中说明需要增加或补充的业务内容')
    identity = deps.digest({'artifact_id': artifact['id'], 'revision': artifact['revision'], 'content': content})[:24]
    sid = 'src_dialogue_' + identity
    chunk = {'text': content, 'locator': '用户对话原文'}
    evidence = {'id': sid + '#P1', 'source_id': sid, 'chat_id': artifact['chat_id'],
        'project_id': artifact['project_id'], 'role': 'supplement', **chunk}
    source = {'id': sid, 'chat_id': artifact['chat_id'], 'project_id': artifact['project_id'],
        'name': '对话补充事实', 'content': content, 'chunks': [chunk]}
    lineage = artifact.get('report', {}).get('lineage', {})
    parents = []
    for kind, key in (('analysis', 'analysis_artifact_id'), ('scenarios', 'scenario_artifact_id')):
        if lineage.get(key):
            value = _scope(store.get('artifact', lineage[key]), artifact)
            if value['type'] != kind:
                raise DomainError('上游成果类型不正确')
            parents.append(value)
    selected_parent = None
    if parent_id and artifact['type'] in ('scenarios', 'cases'):
        kind = 'analysis' if artifact['type'] == 'scenarios' else 'scenarios'
        linked = [parent for parent in parents if parent['type'] == kind]
        candidates = linked or [parent for parent in store.list('artifact', chat_id=artifact['chat_id'])
            if parent['type'] == kind and parent.get('_visible') and parent['project_id'] == artifact['project_id']]
        matches = [parent for parent in candidates if parent_id in {row['id'] for row in parent['items']}]
        if len(matches) != 1:
            raise DomainError('指定的上游条目不存在或不唯一，请提供当前需求或场景编号')
        selected_parent = parent_id
        if not linked:
            parents.append(matches[0])
    legacy = copy.deepcopy(artifact.get('report', {}).get('_legacy_unlinked_cases', {}))
    if artifact['type'] == 'cases' and not lineage.get('scenario_artifact_id'):
        actual = {r['id'] for parent in parents if parent['type'] == 'scenarios' for r in parent['items']}
        legacy.update({r['id']: r['scenario_id'] for r in artifact['items'] if r['scenario_id'] not in actual})
    originals = copy.deepcopy(parents)
    for parent in parents:
        if parent['type'] == 'scenarios' and legacy:
            parent['_legacy_unlinked_cases'] = legacy
    return {'source': source, 'evidence': [evidence], 'parents': parents,
            'original_parents': originals, 'parent_changes': [],
            'addition_parent_id': selected_parent, 'add': add, 'legacy_unlinked_cases': legacy}


def commit_dialogue(service, proposal):
    """Publish the source and target revision atomically; upstream stays read-only."""
    from .operations import native_writes
    store = service.store
    bundle = proposal['dialogue']
    draft = bundle['source']
    if bundle.get('parent_changes'):
        raise DomainError('旧修改预览包含自动上游修改，请重新准备仅修改当前成果的预览', 409)
    with store.transaction(), native_writes():
        artifact = store.get('artifact', proposal['artifact_id'])
        if (artifact['chat_id'], artifact['project_id']) != (draft['chat_id'], draft['project_id']):
            raise DomainError('对话补充不属于当前成果', 404)
        deps.assert_manifest(store, proposal['_dependencies'])
        if artifact['revision'] != proposal['artifact_revision']:
            raise DomainError('成果已改变，请重新查看修改预览', 409)
        store.add_source(draft['chat_id'], draft['name'], 'supplement', draft['content'],
                         draft['chunks'], source_id=draft['id'])
        sources = list(dict.fromkeys(proposal['_source_ids'] + [draft['id']]))
        roles = {**proposal['_source_roles'], draft['id']: 'supplement'}
        parents = [_scope(store.get('artifact', p['id']), artifact) for p in bundle['parents']]
        for parent in parents:
            if parent['type'] == 'scenarios':
                parent['_legacy_unlinked_cases'] = bundle.get('legacy_unlinked_cases', {})
        evidence = store.evidence(sources, roles)
        service._validate(artifact['type'], proposal['items'], evidence, parents, artifact.get('_profile'))
        guard = service._manifest(sources, parents + [artifact])
        result = store.revise_artifact(artifact['id'], proposal['artifact_revision'], proposal['items'],
            reason='native_dialogue_edit', report=proposal['report'], source_ids=sources, source_roles=roles,
            dependencies=guard, provenance=guard)
        store.audit(artifact['id'], 'dialogue_supplement_applied', {'source_id': draft['id'],
            'parent_artifact_ids': []})
        return result
