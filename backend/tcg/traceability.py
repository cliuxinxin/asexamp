"""Read-only project traceability from persisted artifact and row identities."""
from .schemas import DomainError, independent_item
from .workspace_coverage import lineage


KINDS = ('analysis', 'scenarios', 'cases')
NOTES = [
    '仅统计已保存成果的结构关联，不代表测试已经执行、通过或业务覆盖完整。',
    '按成果编号与条目编号共同去重；不同成果中的相同条目编号不会自动关联。',
    '待同步按实际引用的上游条目版本比较；独立 N/A 与主动跳过场景不算缺失上游。',
]


def _key(artifact_id, item_id):
    # Length prefixes avoid delimiter collisions in externally supplied row IDs.
    return f'{len(artifact_id)}:{artifact_id}{item_id}'


class TraceProjector:
    def __init__(self, store, artifacts):
        self.store = store
        self.artifacts = {a['id']: a for a in artifacts}
        self.rows = {a['id']: {row['id']: row for row in a['items']} for a in artifacts}
        self.snapshots = {}
        self.snapshot_artifacts = {}
        self.nodes = {}

    def snapshot(self, artifact_id, version):
        key = (artifact_id, version)
        if key not in self.snapshots:
            value = None
            if artifact_id in self.artifacts and type(version) is int and version > 0:
                try:
                    value = self.store.revision(artifact_id, version)
                except DomainError as exc:
                    if exc.status != 404:
                        raise
            self.snapshot_artifacts[key] = value
            self.snapshots[key] = ({r['id']: r for r in value['items']} if value else {})
        return self.snapshots[key]

    def attach(self, node, artifact, parent_type, parent_ids, *, connect=True, child_id=None):
        prefix = 'scenario' if parent_type == 'scenarios' else 'analysis'
        source = lineage(artifact)
        parent_id = source.get(prefix + '_artifact_id')
        parent = self.artifacts.get(parent_id)
        valid_scope = (parent and parent['type'] == parent_type
                       and parent['chat_id'] == artifact['chat_id']
                       and parent['project_id'] == artifact['project_id'])
        for rid in dict.fromkeys(parent_ids):
            version = (source.get(prefix + '_item_revisions') or {}).get(child_id or node['item_id'],
                (source.get(prefix + '_revisions') or {}).get(rid, source.get(prefix + '_revision')))
            latest = self.rows.get(parent_id, {}).get(rid) if valid_scope else None
            before = self.snapshot(parent_id, version).get(rid) if valid_scope else None
            exists = latest is not None and before is not None
            stale = not exists or latest != before
            node['basis'].append({'artifact_id': parent_id, 'item_id': rid,
                'revision': version, 'current_revision': parent['revision'] if valid_scope else None,
                'stale': stale, 'missing': not exists})
            if latest is not None and connect:
                node['parent_keys'].append(_key(parent_id, rid))
            if not exists:
                node['statuses'].append('missing_parent')
            node['stale'] = node['stale'] or stale
        if not parent_ids:
            node['statuses'].append('missing_parent')

    def build(self):
        for artifact in self.artifacts.values():
            for row in artifact['items']:
                key = _key(artifact['id'], row['id'])
                self.nodes[key] = {'key': key, 'kind': artifact['type'], 'item_id': row['id'],
                    'title': row.get('title', ''), 'artifact_id': artifact['id'],
                    'artifact_title': artifact.get('title', ''), 'revision': artifact['revision'],
                    'chat_id': artifact['chat_id'], 'parent_keys': [], 'statuses': [],
                    'basis': [], 'stale': False, 'independent': False, 'direct': False}
        for artifact in self.artifacts.values():
            for row in artifact['items']:
                node = self.nodes[_key(artifact['id'], row['id'])]
                direct = (artifact['type'] == 'cases'
                          and lineage(artifact).get('generation_mode') == 'direct_requirements'
                          and not row.get('scenario_id') and bool(row.get('requirement_ids')))
                if direct:
                    node['direct'] = True
                    node['statuses'].append('skipped_scenarios')
                    self.attach(node, artifact, 'analysis', row['requirement_ids'])
                elif independent_item(artifact['type'], row):
                    node['independent'] = True
                    node['statuses'].append('independent')
                    node['reason'] = row['_independent_origin']['reason']
                elif artifact['type'] == 'scenarios':
                    self.attach(node, artifact, 'analysis', row.get('requirement_ids') or [])
                elif artifact['type'] == 'cases':
                    self.attach(node, artifact, 'scenarios', [row['scenario_id']] if row.get('scenario_id') else [])
                    for basis in list(node['basis']):
                        historical = self.snapshot_artifacts.get((basis['artifact_id'], basis['revision']))
                        scenario = self.snapshots.get((basis['artifact_id'], basis['revision']), {}).get(basis['item_id'])
                        if historical and scenario:
                            if independent_item('scenarios', scenario):
                                node['independent'] = True
                                node['statuses'].append('independent')
                                node['reason'] = scenario['_independent_origin']['reason']
                            else:
                                self.attach(node, historical, 'analysis', scenario.get('requirement_ids') or [],
                                            connect=False, child_id=scenario['id'])
        children = {key: [] for key in self.nodes}
        for node in self.nodes.values():
            for key in node['parent_keys']:
                children[key].append(node)
        # Levels are fixed, so no recursive graph traversal or guessed linkage is needed.
        for kind in KINDS:
            for node in self.nodes.values():
                if node['kind'] != kind:
                    continue
                parents = [self.nodes[key] for key in node['parent_keys']]
                if any(parent['stale'] for parent in parents):
                    node['stale'] = True
                descendants = children[node['key']]
                if kind == 'analysis' and not descendants:
                    node['statuses'].append('missing_scenarios')
                elif kind == 'analysis' and any(child['kind'] == 'scenarios' and not children[child['key']] for child in descendants):
                    node['statuses'].append('missing_cases')
                elif kind == 'scenarios' and not descendants:
                    node['statuses'].append('missing_cases')
                if node['stale']:
                    node['statuses'].append('stale')
                if not node['statuses']:
                    node['statuses'].append('linked')
                node['statuses'] = list(dict.fromkeys(node['statuses']))
                node['missing'] = any(status.startswith('missing_') for status in node['statuses'])
        return list(self.nodes.values())


def project_traceability(store, project_id, chat_id=None):
    """Return a consistent compact projection; never retrieve source document text."""
    with store.lock:
        store.get('project', project_id)
        if chat_id and store.get('chat', chat_id).get('project_id') != project_id:
            raise DomainError('对话不属于当前项目', 404)
        chats = store.list('chat', project_id=project_id)
        if chat_id:
            chats = [chat for chat in chats if chat['id'] == chat_id]
        chat_ids = {chat['id'] for chat in chats}
        artifacts = [artifact for artifact in store.list('artifact', project_id=project_id, chat_id=chat_id)
                     if artifact.get('_visible') and artifact.get('type') in KINDS
                     and artifact.get('chat_id') in chat_ids]
        nodes = TraceProjector(store, artifacts).build()
        summary = {name: sum(n['kind'] == kind for n in nodes)
                   for name, kind in (('requirements', 'analysis'), ('scenarios', 'scenarios'), ('cases', 'cases'))}
        summary.update({name: sum(bool(n[name]) for n in nodes) for name in ('missing', 'stale', 'independent')})
        summary['direct_cases'] = sum(n['direct'] for n in nodes)
        grouped = {chat['id']: [] for chat in chats}
        for node in nodes:
            grouped[node['chat_id']].append(node)
        return {'project_id': project_id, 'chat_id': chat_id, 'summary': summary,
                'chats': [{'id': chat['id'], 'title': chat.get('title', ''), 'nodes': grouped[chat['id']]}
                          for chat in chats if grouped[chat['id']] or chat_id], 'notes': NOTES}
