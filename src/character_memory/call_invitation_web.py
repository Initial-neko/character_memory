from pydantic import BaseModel, Field
from fastapi import HTTPException
from character_memory.call_invitations import CallInvitationRepository, SESSION_LEASE_SECONDS

class AcceptCallRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=100)

class CallSessionRequest(BaseModel):
    session_token: str = Field(default='', max_length=100)
    connected: bool = False

def attach_call_invitation_routes(app):
    access = app.state.character_memory
    repository = CallInvitationRepository(access.read_store)

    def permission():
        enabled = bool(getattr(access.settings, 'direct_call_invitations_enabled', False))
        if not enabled:
            repository.cancel_open()
        return enabled

    def ensure_available(identifier):
        if not permission():
            raise HTTPException(409, '人物来电邀请许可未开启；需要重启后生效')
        item = repository.get(identifier)
        profile = next((p for p in access.character_profiles() if p['id'] == item['character_id']), None)
        if profile is None or 'archived_at' in profile:
            repository.cancel_open(character_id=item['character_id'], reason='CHARACTER_UNAVAILABLE')
            raise HTTPException(409, '邀请人物已归档或不可用')
        return item

    @app.get('/v1/call-invitations')
    def invitations():
        enabled = permission()
        items = repository.list() if enabled else []
        available = {p['id'] for p in access.character_profiles() if 'archived_at' not in p}
        for item in items:
            if item['character_id'] not in available:
                repository.cancel_open(character_id=item['character_id'], reason='CHARACTER_UNAVAILABLE')
        return {'enabled': enabled, 'items': [i for i in items if i['character_id'] in available], 'session_lease_seconds': SESSION_LEASE_SECONDS}

    @app.post('/v1/call-invitations/{identifier}/accept')
    def accept(identifier: str, req: AcceptCallRequest):
        ensure_available(identifier)
        return repository.transition(identifier, 'accept', client_id=req.client_id)

    @app.post('/v1/call-invitations/{identifier}/decline')
    def decline(identifier: str):
        ensure_available(identifier)
        return repository.transition(identifier, 'decline')

    @app.post('/v1/call-invitations/{identifier}/cancel')
    def cancel(identifier: str, req: CallSessionRequest):
        return repository.transition(identifier, 'cancel', session_token=req.session_token)

    @app.post('/v1/call-invitations/{identifier}/lease')
    def lease(identifier: str, req: CallSessionRequest):
        ensure_available(identifier)
        return {'item': repository.lease(identifier, req.session_token, connected=req.connected)}
