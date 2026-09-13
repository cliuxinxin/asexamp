"""Compatibility routes around native tools and read-only project data."""
import base64
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import BaseModel, Field

from .native_views import workspace_state
from .project_context import shared_context, unshare_clarification, pin_samples
from .schemas import DomainError
from .storage import public, uid


def register_routes(app):
    @app.get('/api/chats/{chat_id}/workspace-state')
    async def workspace(chat_id: str, artifact_id: str | None = None):
        return await workspace_state(app.state.store, app.state.engine, chat_id, artifact_id)

    @app.get('/api/exports/{export_id}')
    def download(export_id: str):
        value = app.state.store.get('frozen_export', export_id)
        encoded = value.get('_bytes') or value.get('content_base64') or value.get('_content_base64')
        if not encoded:
            raise DomainError('请重新导出这份历史文件', 409)
        name = value.get('name', value.get('filename', 'tcg-export.xlsx'))
        return Response(base64.b64decode(encoded), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': "attachment; filename=tcg-export.xlsx; filename*=UTF-8''" + quote(name)})

    @app.get('/api/projects/{project_id}/shared-context')
    def get_shared(project_id: str):
        return shared_context(app.state.store, project_id)

    @app.delete('/api/projects/{project_id}/shared-context/{source_id}')
    def remove_shared(project_id: str, source_id: str):
        return unshare_clarification(app.state.store, project_id, source_id)

    class Samples(BaseModel):
        profile_id: str
        expected_version: int = Field(ge=1)
        selected_ids: list[str] = Field(min_length=1, max_length=5)

    @app.post('/api/artifacts/{artifact_id}/pin-samples')
    async def save_samples(artifact_id: str, body: Samples):
        artifact = app.state.store.get('artifact', artifact_id)
        async with app.state.engine.edit_session(artifact['chat_id']):
            return pin_samples(app.state.store, artifact_id, body.profile_id, body.expected_version, body.selected_ids)

    @app.get('/api/runs/{run_id}/clarification-draft')
    async def clarification_draft(run_id: str):
        from .native_views import current_prompt
        run = app.state.store.run(run_id)
        chat = app.state.store.get('chat', run['chat_id'])
        prompt = await current_prompt(app.state.store, app.state.engine, chat)
        return {'run_id': run_id, 'questions': (prompt or {}).get('questions', []),
                'submitted': (prompt or {}).get('kind') != 'clarification', 'answer': '', 'revision': 1}

    class Supplement(BaseModel):
        source_ids: list[str] = Field(default_factory=list)
        content: str = ''

    @app.post('/api/runs/{run_id}/supplement')
    async def supplement(run_id: str, body: Supplement):
        run = app.state.store.run(run_id)
        return await app.state.conversation.submit(run['chat_id'], {
            'client_message_id': uid('supplement_'), 'content': body.content or '请采用所选补充资料更新需求理解，更新后等我确认。',
            'source_ids': body.source_ids, 'mode': run['mode']})
