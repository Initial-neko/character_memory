"""Local operator's explicit profile API; request payload never owns identity."""
from fastapi import HTTPException,Query
from character_memory.domain.experience_profile import ProfilePatch,ProfileRestore,MAX_VERSION
from character_memory.storage.experience_profiles import ExperienceProfileRepository,ProfileConflict,ProfileVersionMissing,ProfileCorrupt

def attach_experience_profile_routes(app,store):
    repo=ExperienceProfileRepository(store)
    def call(operation,*args):
        try:return operation(*args)
        except ProfileConflict as exc:raise HTTPException(409,str(exc)) from exc
        except ProfileVersionMissing as exc:raise HTTPException(404,str(exc)) from exc
        except ProfileCorrupt as exc:raise HTTPException(503,str(exc)) from exc

    @app.get('/v2/me/experience-profile')
    def read_profile():return call(repo.get)

    @app.patch('/v2/me/experience-profile')
    def patch_profile(request:ProfilePatch):
        return call(repo.patch,request.expected_version,request.changes.model_dump(exclude_unset=True))

    @app.delete('/v2/me/experience-profile')
    def clear_profile(expected_version:str=Query(...,pattern=r'^(0|[1-9][0-9]{0,18})$')):
        version=int(expected_version)
        if version>MAX_VERSION:raise HTTPException(422,'expected_version is outside the supported integer range')
        return call(repo.clear,version)

    @app.get('/v2/me/experience-profile/versions')
    def profile_versions():return {'versions':call(repo.versions)}

    @app.post('/v2/me/experience-profile/restore')
    def restore_profile(request:ProfileRestore):return call(repo.restore,request.expected_version,request.target_version)
