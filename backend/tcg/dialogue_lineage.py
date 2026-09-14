"""Stage exact human evidence and explicit organizational parents for an edit.

Nothing here infers business rules. Proposed parents quote the human message and
are displayed in the same diff as the requested rows. Sources and all revisions
become live together only when that proposal is approved.
"""
import copy

from . import dependencies as deps
from .analysis_diagrams import complete_analysis_diagrams
from .schemas import DomainError
from .storage import now


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


def prepare_dialogue(store, artifact, content, *, add=False, parent_id=None):
    if not isinstance(content, str) or not content.strip():
        raise DomainError('请在当前消息中说明需要增加或补充的业务内容')
    identity = deps.digest({'artifact_id': artifact['id'], 'revision': artifact['revision'], 'content': content})[:24]
    sid = 'src_dialogue_' + identity
    chunk = {'text': content, 'locator': '用户对话原文'}
    evidence = {'id': sid + '#P1', 'source_id': sid, 'chat_id': artifact['chat_id'],
        'project_id': artifact['project_id'], 'role': 'supplement', **chunk}
    source = {'id': sid, 'chat_id': artifact['chat_id'], 'project_id': artifact['project_id'],
        'name': '对话补充事实', 'content': content, 'chunks': [chunk]}
    parents, originals, changes = {}, {}, []
    lineage = artifact.get('report', {}).get('lineage', {})

    def resolve(kind, preferred=None):
        if kind in parents:
            return parents[kind]
        key = 'analysis_artifact_id' if kind == 'analysis' else 'scenario_artifact_id'
        aid = preferred or lineage.get(key)
        if aid:
            value = _scope(store.get('artifact', aid), artifact)
            if value['type'] != kind:
                raise DomainError('上游成果类型不正确')
        else:
            matches = [a for a in store.list('artifact', chat_id=artifact['chat_id'])
                       if a['type'] == kind and a.get('_visible') and a['project_id'] == artifact['project_id']]
            if len(matches) > 1:
                raise DomainError('存在多份' + ('需求理解' if kind == 'analysis' else '场景') + '成果，请先明确要关联的成果')
            value = matches[0] if matches else None
        if value:
            originals[value['id']] = value
            parents[kind] = copy.deepcopy(value)
        else:
            parents[kind] = {'id': 'art_dialogue_' + kind + '_' + identity, 'type': kind,
                'title': '对话补充需求' if kind == 'analysis' else '对话补充场景',
                'chat_id': artifact['chat_id'], 'project_id': artifact['project_id'],
                'revision': 0, 'items': [], 'report': {'summary': '用户对话补充的关联记录。'},
                '_source_ids': [], '_source_roles': {}, '_profile': copy.deepcopy(artifact.get('_profile', {})),
                '_visible': True, '_runtime': 'native', 'created_at': now()}
        return parents[kind]

    def append(kind, row):
        parent = parents[kind]
        before = copy.deepcopy(parent)
        parent['items'].append(row)
        parent['revision'] += 1
        if kind == 'analysis':
            complete_analysis_diagrams(parent['report'], parent['items'], previous=before, whole_response=False)
        else:
            parent['report'] = link_report(parent['report'], kind, [parents['analysis']], row['requirement_ids'],
                existing_items=before['items'], new_item_ids=[row['id']])
        changes.append({'artifact_id': parent['id'], 'base_revision': before['revision'],
                        'before': before, 'value': copy.deepcopy(parent)})

    selected_parent = None
    if artifact['type'] == 'cases':
        scenario = resolve('scenarios')
        analysis_id = scenario.get('report', {}).get('lineage', {}).get('analysis_artifact_id') or lineage.get('analysis_artifact_id')
        if analysis_id:
            resolve('analysis', analysis_id)
    elif artifact['type'] == 'scenarios':
        resolve('analysis')
    if add and artifact['type'] in ('scenarios', 'cases'):
        kind = 'analysis' if artifact['type'] == 'scenarios' else 'scenarios'
        parent = parents[kind]
        if parent_id:
            if parent_id not in {r['id'] for r in parent['items']}:
                raise DomainError('指定的上游条目不存在，请提供当前需求或场景编号')
            selected_parent = parent_id
        else:
            resolve('analysis')
            rid = 'REQ-DIALOGUE-' + identity
            marker = {'source_id': sid, 'kind': 'dialogue_supplement', 'label': '用户对话补充'}
            append('analysis', {'id': rid, 'title': '对话补充需求', 'description': content,
                'refs': [evidence['id']], '_dialogue_origin': marker})
            selected_parent = rid
            if artifact['type'] == 'cases':
                selected_parent = 'SC-DIALOGUE-' + identity
                append('scenarios', {'id': selected_parent, 'title': '对话补充场景', 'description': content,
                    'priority': '', 'requirement_ids': [rid], 'refs': [evidence['id']], '_dialogue_origin': marker})
    # An unused empty draft must not turn into a phantom parent for a plain edit.
    kept = [p for p in parents.values() if p['revision'] > 0]
    legacy = copy.deepcopy(artifact.get('report', {}).get('_legacy_unlinked_cases', {}))
    if artifact['type'] == 'cases' and not lineage.get('scenario_artifact_id'):
        actual = {r['id'] for p in kept if p['type'] == 'scenarios' for r in p['items']}
        legacy.update({r['id']: r['scenario_id'] for r in artifact['items'] if r['scenario_id'] not in actual})
    for parent in kept:
        if parent['type'] == 'scenarios' and legacy:
            parent['_legacy_unlinked_cases'] = legacy
    return {'source': source, 'evidence': [evidence], 'parents': kept,
            'original_parents': list(originals.values()), 'parent_changes': changes,
            'addition_parent_id': selected_parent, 'add': add, 'legacy_unlinked_cases': legacy}


def preview_changes(proposal, artifact):
    changes = [{'artifact_id': c['artifact_id'], 'title': c['value']['title'],
        'expected_revision': c['base_revision'], 'before_items': c['before']['items'],
        'items': c['value']['items'], 'report': c['value']['report']}
        for c in proposal.get('dialogue', {}).get('parent_changes', [])]
    changes.append({'artifact_id': artifact['id'], 'title': artifact['title'],
        'expected_revision': artifact['revision'], 'before_items': artifact['items'],
        'items': proposal['items'], 'report': proposal['report']})
    return changes


def commit_dialogue(service, proposal):
    """One transaction makes source, implicit parents and target revision visible."""
    from .operations import _save, native_writes
    store = service.store
    bundle = proposal['dialogue']
    draft = bundle['source']
    with store.transaction(), native_writes():
        artifact = store.get('artifact', proposal['artifact_id'])
        if (artifact['chat_id'], artifact['project_id']) != (draft['chat_id'], draft['project_id']):
            raise DomainError('对话补充不属于当前成果', 404)
        deps.assert_manifest(store, proposal['dependencies'])
        if artifact['revision'] != proposal['base_revision']:
            raise DomainError('成果已改变，请重新查看修改预览', 409)
        for change in bundle['parent_changes']:
            _scope(change['value'], artifact)
            if change['base_revision'] == 0:
                if any(a['id'] == change['artifact_id'] for a in store.list('artifact')):
                    raise DomainError('对话补充父节点已存在，请重新查看预览', 409)
            elif store.get('artifact', change['artifact_id'])['revision'] != change['base_revision']:
                raise DomainError('上游成果已改变，请重新查看修改预览', 409)
        store.add_source(draft['chat_id'], draft['name'], 'supplement', draft['content'],
                         draft['chunks'], source_id=draft['id'])
        for change in bundle['parent_changes']:
            value = change['value']
            source_ids = list(dict.fromkeys(value.get('_source_ids', []) + [draft['id']]))
            roles = {**value.get('_source_roles', {}), draft['id']: 'supplement'}
            evidence = store.evidence(source_ids, roles)
            current_parents = [store.get('artifact', p['id']) for p in bundle['parents']
                               if p['type'] == 'analysis' and value['type'] == 'scenarios']
            service._validate(value['type'], value['items'], evidence, current_parents, value.get('_profile'))
            guard = service._manifest(source_ids, current_parents)
            if change['base_revision']:
                store.revise_artifact(value['id'], change['base_revision'], value['items'],
                    reason='native_dialogue_parent', report=value['report'], source_ids=source_ids,
                    source_roles=roles, dependencies=guard, provenance=guard)
            else:
                value = {**value, '_source_ids': source_ids, '_source_roles': roles,
                         '_dependencies': guard, '_write_dependencies': guard}
                _save(store, value, 'native_dialogue_parent',
                      {'added': [r['id'] for r in value['items']], 'updated': [], 'deleted': []}, None, None)
        sources = list(dict.fromkeys(proposal['source_ids'] + [draft['id']]))
        roles = {**proposal['source_roles'], draft['id']: 'supplement'}
        parents = [_scope(store.get('artifact', p['id']), artifact) for p in bundle['parents']]
        for parent in parents:
            if parent['type'] == 'scenarios':
                parent['_legacy_unlinked_cases'] = bundle.get('legacy_unlinked_cases', {})
        evidence = store.evidence(sources, roles)
        service._validate(artifact['type'], proposal['items'], evidence, parents, artifact.get('_profile'))
        guard = service._manifest(sources, parents + [artifact])
        result = store.revise_artifact(artifact['id'], proposal['base_revision'], proposal['items'],
            reason='native_dialogue_edit', report=proposal['report'], source_ids=sources, source_roles=roles,
            dependencies=guard, provenance=guard)
        store.audit(artifact['id'], 'dialogue_supplement_applied', {'source_id': draft['id'],
            'parent_artifact_ids': [c['artifact_id'] for c in bundle['parent_changes']]})
        return result
