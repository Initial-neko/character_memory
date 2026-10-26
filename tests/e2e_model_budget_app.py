"""Isolated real HTTP application; no external model/search is ever invoked."""
import json
import os
from pathlib import Path

import e2e_realtime_app as fixture
from character_memory.world_web import attach_world_routes

fixture.settings.autonomous_model_daily_max = 1
fixture.settings.world_pulse_enabled = False
fixture.settings.world_browse_enabled = False


def structured(prompt, schema, session):
    if schema.__name__ != 'PersonalBrowsePlan':
        raise AssertionError('unexpected model path in budget HTTP fixture')
    with Path(os.environ['MODEL_BUDGET_CALL_LOG']).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'schema': schema.__name__,
                                 'session': session, 'external_calls': 0})+'\n')
    return schema(browse=False)


fixture.model.structured_for_session = structured
app = fixture.app
attach_world_routes(app)


@app.get('/__e2e/model-budget-runtime')
def runtime():
    return {'pid': os.getpid(), 'external_model_calls': 0}
