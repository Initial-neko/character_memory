"""Local operator Goal metadata APIs, with no model initialization or dispatch."""
from fastapi import HTTPException,Query
from character_memory.domain.goals import GoalCreate,GoalAction
from character_memory.storage.person_goals import GoalRepository,GoalMissing,GoalConflict

def attach_goal_routes(app):
    access=app.state.character_memory
    repository=GoalRepository(access.read_store)

    @app.post('/v1/characters/{character_id}/goals',status_code=201)
    def create_goal(character_id:str,body:GoalCreate):
        if character_id not in {item['id'] for item in access.character_profiles()}:
            raise HTTPException(status_code=404,detail='CHARACTER_NOT_FOUND')
        try:return repository.create(character_id,body)
        except GoalConflict as error:raise HTTPException(status_code=409,detail=str(error)) from error

    @app.get('/v1/characters/{character_id}/goals')
    def list_goals(character_id:str,limit:int=Query(default=50,ge=1,le=100)):
        return {'goals':repository.list(character_id,limit=limit)}

    @app.get('/v1/characters/{character_id}/goals/{goal_id}')
    def get_goal(character_id:str,goal_id:str):
        try:return repository.get(character_id,goal_id)
        except GoalMissing as error:raise HTTPException(status_code=404,detail='GOAL_NOT_FOUND') from error

    @app.post('/v1/characters/{character_id}/goals/{goal_id}/actions')
    def goal_action(character_id:str,goal_id:str,body:GoalAction):
        try:return repository.act(character_id,goal_id,body)
        except GoalMissing as error:raise HTTPException(status_code=404,detail='GOAL_NOT_FOUND') from error
        except GoalConflict as error:raise HTTPException(status_code=409,detail=str(error)) from error
