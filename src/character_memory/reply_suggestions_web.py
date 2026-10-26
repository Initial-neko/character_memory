"""Thin local-operator draft API; generation never submits a chat message."""
from uuid import UUID
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from character_memory.application.reply_suggestions import ReplySuggestionService, SuggestionStale, SuggestionUnavailable
from character_memory.storage.reply_suggestions import SuggestionConflict
from character_memory.storage.experience_profiles import ProfileCorrupt
from character_memory.domain.reply_suggestions import SuggestionRequest
from character_memory.config import load_persona, resolve_persona_path


def attach_reply_suggestion_routes(app, access, *, persona_for=None):
    service = ReplySuggestionService(access.read_store,
        ensure_character=access.ensure_character,
        persona_for=persona_for or (lambda char:load_persona(resolve_persona_path(access.settings,char))),
        model_for=lambda:access.require_bundle().model)

    def call(operation, *args):
        try:
            result = operation(*args)
        except (SuggestionStale, SuggestionConflict) as exc:
            raise HTTPException(409,str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404,'suggestion batch not found') from exc
        except SuggestionUnavailable as exc:
            raise HTTPException(502,str(exc)) from exc
        except ProfileCorrupt as exc:
            raise HTTPException(503,str(exc)) from exc
        return JSONResponse(result,status_code=202 if result['status']=='PENDING' else 200)

    @app.post('/v2/reply-suggestions')
    def generate_suggestions(request:SuggestionRequest):
        return call(service.generate,request)

    @app.get('/v2/reply-suggestions/{batch_id}')
    def read_suggestions(batch_id:UUID):
        return call(service.get,str(batch_id))

    return service
