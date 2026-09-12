"""One reconciliation entry point shared by chat and the workspace next action."""
from .capability_contracts import contract
from .workspace_changes import prepare_reconciliation, workspace_state


CAPABILITIES = {
    'workspace.reconcile': contract({
        'effect': 'write',
        'description': '预览当前分支全部待同步内容：先将已保存的新资料纳入需求理解，再同步实际受影响的场景和用例；保留无关条目和人工字段。只生成可应用的预览，不确认工作流。content 可保存用户明确补充的新业务事实。若用户只选部分条目同步，请改用 artifact.preview(action:sync,selected_ids:...)，不要扩大范围。',
        'parameters': {'type': 'object', 'properties': {
            'artifact_id': {'type': 'string'},
            'expected_revision': {'type': 'integer'},
            'instruction': {'type': 'string'},
            'scope': {'type': 'string', 'enum': ['all']},
            'content': {'type': 'string'},
            'source_ids': {'type': 'array', 'items': {'type': 'string'}},
            'related_artifact_ids': {'type': 'array', 'items': {'type': 'string'}},
        }},
    }, target_types=('analysis', 'scenarios', 'cases'), target_required=True,
       context_policy='artifact_evidence'),
}


async def execute(store, engine, chat, name, args, turn_id=None):
    if args.get('selected_ids'):
        from .schemas import DomainError
        raise DomainError('当前选择了部分条目。请使用“仅同步所选条目”的修改预览；统一更新需要明确采用当前分支全部受影响内容。')
    if (args.get('content') or '').strip():
        from .conversation_project import update_from_sources
        from .schemas import DomainError
        state = workspace_state(store, chat, args.get('artifact_id'))
        if args.get('artifact_id') and args.get('expected_revision') is not None:
            if store.get('artifact', args['artifact_id'])['revision'] != args['expected_revision']:
                raise DomainError('成果已更新，请刷新后重新预览', 409)
        branch = {s['key']: s for s in state['stages'][:3]}
        analysis = branch['analysis']
        if not analysis['artifact_id']:
            raise DomainError('请先保存需求理解，再纳入补充需求')
        return await update_from_sources(store, engine, chat, {**args,
            'artifact_id': analysis['artifact_id'], 'expected_revision': analysis['revision'],
            'source_ids': args.get('source_ids') or state['impact']['source_ids'],
            'preview': True, 'sync_targets': ['analysis','scenarios','cases'],
            'reconcile_all_drift': True,
            'related_artifact_ids': args['related_artifact_ids'] if 'related_artifact_ids' in args else
                [branch[k]['artifact_id'] for k in ('scenarios','cases') if branch[k]['artifact_id']],
        }, turn_id)
    return await prepare_reconciliation(store, engine, chat, args, turn_id)


def register_routes(app):
    @app.get('/api/chats/{chat_id}/workspace-state')
    def state(chat_id: str, artifact_id: str | None = None):
        store = app.state.store
        return workspace_state(store, store.get('chat', chat_id), artifact_id)
