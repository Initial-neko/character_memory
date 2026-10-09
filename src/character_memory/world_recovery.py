"""Local-only recovery of durable appraisals. No runtime, provider or schema writes on inspection."""
from __future__ import annotations

import json
from types import SimpleNamespace

from character_memory.domain.models import WorldObservation
from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityRequest, CapabilityResult, adapt_browse_decision
from character_memory.time_utils import parse_datetime


class WorldBrowseRecovery:
    def __init__(self, store):
        self.store = store

    def _tables(self):
        return {row[0] for row in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _prepare(self, row):
        # Exceptions may contain private provider payloads. Export only a stable code.
        try:
            return self._validated_receipt(row)
        except Exception as error:
            raise ValueError("INVALID_RECOVERY_RECEIPT") from error

    def _validated_receipt(self, row):
        from character_memory.world_activity import PersonalBrowseAppraisal, RssBatchAppraisal, PersonalBrowsePlan, PersonalWorldReadPlan
        now = parse_datetime(row['started_at'])
        snapshot = json.loads(row['plan_json'])
        decision = snapshot['decision']
        if 'choice' in decision or 'action' in decision:
            plan = PersonalWorldReadPlan.model_validate(decision)
            if plan.choice == 'NO_ACTION' or decision.get('browse') is not True:
                raise ValueError('NO_READING_DECISION')
            capability = plan.choice
        else:
            plan = PersonalBrowsePlan.model_validate(decision)
            if decision.get('browse') is not True or not plan.browse:
                raise ValueError('NO_READING_DECISION')
            capability = 'WEB_SEARCH' 
        request_id = f"{row['opportunity_id']}:{capability}:1"
        execution = self.store.conn.execute(
            "SELECT * FROM capability_executions WHERE request_id=? AND character_id=? AND opportunity_id=?",
            (request_id,row['character_id'],row['opportunity_id']),
        ).fetchone()
        if execution is None or execution['status'] != 'SUCCESS' or not execution['result_json']:
            raise ValueError('MISSING_SUCCESSFUL_EXECUTION_RECEIPT')
        request = CapabilityRequest(**json.loads(execution['request_json']))
        denial = CapabilityExecutor._denial(request)
        if denial or (request.request_id,request.character_id,request.opportunity_id,request.capability,request.source_event_id) != (request_id,row['character_id'],row['opportunity_id'],capability,row['source_event_id']):
            raise ValueError('EXECUTION_IDENTITY_OR_CONSTRAINT_MISMATCH')
        if capability == 'WEB_SEARCH':
            expected_request = adapt_browse_decision(
                plan, character_id=row['character_id'], opportunity_id=row['opportunity_id'],
                source_event_id=row['source_event_id'], max_pages=snapshot['max_pages'],
                max_chars_per_page=snapshot['max_chars_per_page'])
        else:
            candidates = snapshot['rss_candidates']
            if len(plan.item_ids) != len(candidates) or set(plan.item_ids) != {item['item_id'] for item in candidates}:
                raise ValueError('RSS_SELECTION_MISMATCH')
            expected_request = CapabilityRequest(request_id, row['character_id'], row['opportunity_id'],
                row['source_event_id'], 'WORLD', 'READ_RSS',
                {'items': [{'item_id': item['item_id'], 'source_generation': item['source_generation']} for item in candidates]},
                {'max_items': snapshot['rss_max_items'], 'max_chars': 12000, 'max_calls': 1})
        if request != expected_request:
            raise ValueError('REQUEST_PLAN_MISMATCH')
        result = CapabilityResult(**json.loads(execution['result_json']))
        if result.request_id != request_id or result.status != 'SUCCESS' or not isinstance(result.data,dict):
            raise ValueError('INVALID_EXECUTION_RESULT')
        appraisal = json.loads(row['appraisal_json'])
        if capability == 'WEB_SEARCH':
            if request.arguments['query'] != snapshot['decision']['query']:
                raise ValueError('QUERY_MISMATCH')
            pages = [WorldObservation.model_validate(item) for item in result.data['observations']]
            if len(pages) > request.constraints['max_pages']:
                raise ValueError('PAGE_LIMIT_MISMATCH')
            if not pages:raise ValueError('MISSING_READ_CONTENT')
            parsed = PersonalBrowseAppraisal.model_validate(appraisal)
            return now,snapshot,request,result,pages,int(parsed.keep)
        parsed = RssBatchAppraisal.model_validate(appraisal)
        items = result.data['items']
        expected = {item['item_id'] for item in request.arguments['items']}
        if not items or {item['item_id'] for item in items} != expected or len(items) != len(expected):
            raise ValueError('RSS_ITEM_MISMATCH')
        if {item.item_id for item in parsed.items} != expected or len(parsed.items) != len(expected):
            raise ValueError('RSS_APPRAISAL_MISMATCH')
        generations = {item['item_id']:item['source_generation'] for item in request.arguments['items']}
        for item in items:
            if item.get('source_generation') != generations[item['item_id']]:
                raise ValueError('RSS_GENERATION_MISMATCH')
            if item.get('content_scope') != 'RSS_FEED_TEXT' or not isinstance(item.get('content'),str) or not item['content'].strip():
                raise ValueError('INVALID_RSS_READ_SCOPE')
            reading = self.store.conn.execute(
                'SELECT * FROM world_rss_readings WHERE character_id=? AND item_id=? AND request_id=?',
                (row['character_id'],item['item_id'],request_id),
            ).fetchone()
            if reading is None or reading['status'] != 'STARTED' or json.loads(reading['snapshot_json']) != item:
                raise ValueError('RSS_READING_RECEIPT_MISMATCH')
        return now,snapshot,request,result,items,sum(item.keep for item in parsed.items)

    def inspect(self, *, limit=100):
        with self.store._lock:
            if 'world_browse_decisions' not in self._tables():return {'candidates':[],'read_only':True}
            rows = self.store.conn.execute(
                "SELECT * FROM world_browse_decisions WHERE phase='APPRAISED' ORDER BY started_at,opportunity_id LIMIT ?",
                (max(1,min(200,limit)),),
            ).fetchall()
            candidates=[]
            for row in rows:
                entry={'opportunity_id':row['opportunity_id'],'character_id':row['character_id'],'observed_at':row['started_at']}
                try:
                    *_,kept=self._prepare(row)
                    entry.update(recoverable=True,kept_items=kept)
                except Exception as error:
                    entry.update(recoverable=False,reason='INVALID_RECOVERY_RECEIPT')
                candidates.append(entry)
            return {'candidates':candidates,'read_only':True}

    def apply(self, opportunity_id):
        from character_memory.world_activity import WorldActivityService, WorldPulseRepository, PersonalBrowsePlan
        # One canonical connection owns the receipt check, event and final result.
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute('SELECT * FROM world_browse_decisions WHERE opportunity_id=?',(opportunity_id,)).fetchone()
            if row is None:raise KeyError('unknown browse opportunity')
            if row['phase']=='APPLIED':return json.loads(row['result_json'])
            if row['phase']!='APPRAISED':raise ValueError('ONLY_APPRAISED_IS_LOCALLY_RECOVERABLE')
            now,snapshot,request,executed,content,_=self._prepare(row)
            service = WorldActivityService(SimpleNamespace(store=lambda:self.store),WorldPulseRepository(self.store))
            if request.capability=='WEB_SEARCH':
                return service._apply_web_receipt(row['character_id'],opportunity_id,now=now,
                    plan=PersonalBrowsePlan.model_validate(snapshot['decision']),request=request,
                    observed=executed.data,observations=content)
            result={'character_id':row['character_id'],'opportunity_id':opportunity_id,
                'capability_request_id':request.request_id,'reading_choice':'READ_RSS',
                'execution_status':'SUCCESS','browsed':True,'observations':[],'kept':False,
                'appraisal_status':'NOT_COMPLETED','errors':executed.data.get('errors') or []}
            return service._apply_rss_receipt(row['character_id'],opportunity_id,now=now,
                request=request,items=content,result=result)
