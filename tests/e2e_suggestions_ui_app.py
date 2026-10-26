"""Deterministic suggestions over the existing real HTTP application."""
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
import e2e_realtime_app as fixture

app = fixture.app
app.state.character_memory.reaction_scheduler.quiet_seconds = 60
app.state.character_memory.reaction_scheduler.max_burst_seconds = 60


def structured(prompt, schema, session):
    context = json.loads(prompt.split('Visible conversation JSON:\n', 1)[1])
    entry = {'session': session, 'external_calls': 0, 'profile_version': context['profile']['version'], 'preferences': context['profile']['preferences']}
    with Path(os.environ['SUGGESTION_CALL_LOG']).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')
    if 'FAIL_SUGGESTION' in prompt:
        raise RuntimeError('deterministic suggestion provider failure')
    return SimpleNamespace(value=schema.model_validate({'suggestions': [
        {'intent': '澄清', 'text': '你更在意哪一部分？'},
        {'intent': '感受', 'text': '我有点紧张，想慢慢来。'},
        {'intent': '计划', 'text': '我们先试一个开场吧。'},
    ]}))


fixture.model.structured_call_for_session = structured


@app.get('/__e2e/suggestions-runtime')
def suggestions_runtime():
    return {'pid': os.getpid(), 'external_model_calls': 0}


_original_reaction = fixture.model._reaction
def logged_reaction(context):
    match = re.search(r'profile_version=(\d+)', context)
    entry = {'profile_version': int(match.group(1)) if match else None,
             'brief': 'verbosity=BRIEF' in context, 'group': '# Group Conversation Contract' in context, 'pid': os.getpid(), 'external_calls': 0}
    with Path(os.environ['SUGGESTION_CALL_LOG']).with_name('profile-contexts.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(entry) + '\n')
    return _original_reaction(context)
fixture.model._reaction = logged_reaction
