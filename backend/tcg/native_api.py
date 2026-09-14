"""Compatibility routes around native tools and read-only project data."""
import base64
import json
from urllib.parse import quote

from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from .native_views import workspace_state
from .project_context import shared_context, unshare_clarification, pin_samples
from .schemas import DomainError
from .storage import public, uid


def register_routes(app):
    @app.get('/api/runs/{run_id}/candidates/{candidate_id}')
    def generation_candidate(run_id: str, candidate_id: str):
        from .generation_candidates import read_candidate
        return read_candidate(app.state.store, run_id, candidate_id)

    @app.get('/api/runs/{run_id}/candidates/{candidate_id}/download')
    def download_generation_candidate(run_id: str, candidate_id: str):
        from .generation_candidates import read_candidate
        record = read_candidate(app.state.store, run_id, candidate_id)
        return Response(json.dumps(record, ensure_ascii=False, indent=2), media_type='application/json',
            headers={'Content-Disposition': 'attachment; filename=tcg-unvalidated-draft.json'})

    @app.get('/api/chats/{chat_id}/profile-change')
    def profile_change(chat_id: str, prompt_id: str):
        from .profile_changes import preview_profile_change
        return preview_profile_change(app.state.store, chat_id, prompt_id)

    class ProfileChangeApproval(BaseModel):
        model_config = ConfigDict(extra='forbid')
        prompt_id: str = Field(min_length=1, max_length=300)
        expected_version: int = Field(ge=1)
        selected_keys: list[str] = Field(min_length=1, max_length=100)

    @app.post('/api/chats/{chat_id}/profile-change/apply')
    def apply_profile_change(chat_id: str, body: ProfileChangeApproval):
        from .profile_changes import apply_profile_change as apply_change
        return apply_change(app.state.store, chat_id, body.prompt_id, body.expected_version,
                            body.selected_keys, write_messages=True)

    @app.get('/api/chats/{chat_id}/field-drift')
    def field_drift(chat_id: str, artifact_id: str | None = None, revision: int | None = None,
                    profile_id: str | None = None):
        from .field_drift import chat_field_drift
        return chat_field_drift(app.state.store, chat_id, artifact_id, revision, profile_id)

    class FieldSyncProposal(BaseModel):
        model_config = ConfigDict(extra='forbid')
        artifact_id: str = Field(min_length=1, max_length=300)
        revision: int = Field(ge=1)
        head_revision: int = Field(ge=1)
        profile_id: str = Field(min_length=1, max_length=300)
        profile_version: int = Field(ge=1)
        fields: list[str] = Field(min_length=1, max_length=200)

    @app.post('/api/chats/{chat_id}/field-drift/propose')
    def propose_field_sync(chat_id: str, body: FieldSyncProposal):
        from .field_drift import propose_field_sync as propose
        return propose(app.state.store, chat_id, **body.model_dump())

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
    def get_shared(project_id: str, chat_id: str | None = None):
        return shared_context(app.state.store, project_id, chat_id=chat_id)

    @app.delete('/api/projects/{project_id}/shared-context/{source_id}')
    def remove_shared(project_id: str, source_id: str):
        return unshare_clarification(app.state.store, project_id, source_id)

    class ProjectKnowledgeSelection(BaseModel):
        model_config = ConfigDict(extra='forbid')
        enabled: bool
        expected_version: int = Field(ge=1)

    @app.patch('/api/chats/{chat_id}/project-knowledge/{source_id}')
    async def select_project_knowledge(chat_id: str, source_id: str, body: ProjectKnowledgeSelection):
        from .conversation_facts import set_fact_enabled
        async with app.state.engine.edit_session(chat_id):
            return set_fact_enabled(app.state.store, chat_id, source_id, body.enabled, body.expected_version)

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
