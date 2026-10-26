"""Real Core/World routes with bounded local providers and an injected DB fault."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import e2e_realtime_app as fixture
from character_memory.domain.models import EventType, WorldObservation
from character_memory.rss_sources import RssRepository, ParsedFeedItem
from character_memory.rss_world import RssPersonalReading
from character_memory.world_web import attach_world_routes

ROOT=Path(__file__).resolve().parents[1]
NO_PROVIDERS=os.getenv('RECOVERY_NO_PROVIDERS')=='1'
KIND=os.environ['RECOVERY_KIND']
fixture.settings.autonomous_model_daily_max=10
fixture.settings.world_activity_enabled=False
fixture.settings.world_pulse_enabled=False
fixture.settings.world_browse_enabled=False
fixture.settings.world_rss_reading_enabled=KIND=='RSS'


class Observer:
    def observe(self, query, **kwargs):
        assert not NO_PROVIDERS, 'recovery initialized a browser/provider'
        return {'search_results':1,'errors':[], 'observations':[WorldObservation(
            title='Bounded reading',url='https://example.org/bounded',source_domain='example.org',
            snippet='Actual local fixture',content='A fixed reading with explicit provenance.').model_dump(mode='json')]}


fixture.app.state.character_memory.services.world_observer=Observer()
if KIND=='RSS':
    rss=RssRepository(fixture.store)
    if not fixture.store.conn.execute('SELECT 1 FROM rss_sources').fetchone():
        source=rss.create_source('https://example.org/feed.xml',name='Local fixture')
        now=datetime.now(timezone.utc)
        rss.upsert_items(source['id'],[ParsedFeedItem('fixture','Local reading','summary','Fixed local feed text',
            'https://example.org/article','',now)],fetched_at=now)


def structured(prompt,schema,session):
    assert not NO_PROVIDERS, 'recovery called a model'
    with Path(os.environ['MODEL_BUDGET_CALL_LOG']).open('a',encoding='utf-8') as stream:
        stream.write(json.dumps({'pid':os.getpid(),'schema':schema.__name__,'external_calls':0})+'\n')
    if schema.__name__=='PersonalBrowsePlan':return schema(browse=True,query='bounded reading')
    if schema.__name__=='PersonalWorldReadPlan':
        return schema(choice='READ_RSS',item_ids=[x['item_id'] for x in RssPersonalReading(fixture.store).candidates('rin')[:1]])
    if schema.__name__=='PersonalBrowseAppraisal':
        return schema(keep=True,summary='A completed fixture reading.',personal_note='I noticed a useful detail.')
    if schema.__name__=='RssBatchAppraisal':
        ids=fixture.store.conn.execute("SELECT item_id FROM world_rss_readings WHERE status='STARTED'").fetchall()
        return schema(items=[{'item_id':x[0],'keep':True,'summary':'A completed local RSS reading.','personal_note':'Useful detail.'} for x in ids])
    raise AssertionError('unexpected structured model schema')


fixture.model.structured_for_session=structured
app=fixture.app
attach_world_routes(app)
if NO_PROVIDERS:
    fixture.store.conn.execute('DROP TRIGGER IF EXISTS interrupt_appraisal')
    fixture.store.conn.commit()
    def forbidden():raise AssertionError('recovery initialized a model bundle')
    app.state.character_memory.require_bundle=forbidden
else:
    fixture.store.conn.execute("CREATE TRIGGER interrupt_appraisal BEFORE UPDATE ON world_browse_decisions WHEN NEW.phase='APPRAISED' BEGIN SELECT RAISE(ABORT,'appraisal persistence interrupted'); END")
    fixture.store.conn.commit()


@app.get('/__e2e/recovery-runtime')
def runtime():
    paths=['src/character_memory/runtime/model_execution.py','src/character_memory/world_recovery.py',
           'src/character_memory/world_activity.py','src/character_memory/runtime/social_prompts.py']
    rows=fixture.store.conn.execute('SELECT phase,started_at FROM world_browse_decisions').fetchall()
    model_rows=fixture.store.conn.execute('SELECT status,step FROM model_invocation_receipts').fetchall()
    events=[{'id':x.id,'at':x.event_time.isoformat()} for x in fixture.store.list_events('rin') if x.event_type==EventType.WORLD_OBSERVATION]
    return {'pid':os.getpid(),'commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
            'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in paths},
            'world':[dict(x) for x in rows],'models':[dict(x) for x in model_rows],
            'events':events,'no_providers':NO_PROVIDERS,'external_calls':0}
