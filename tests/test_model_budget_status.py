"""Budget observability is a read-only metadata projection, not a model call."""
from datetime import datetime, timezone
import sqlite3

from pydantic import BaseModel

from character_memory.runtime.model_execution import ModelInvocationBudget, model_budget_status
from character_memory.storage.sqlite import SQLiteStore


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def readonly(action, *args):
    return sqlite3.SQLITE_DENY if action in {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
    } else sqlite3.SQLITE_OK


def test_status_before_first_use_does_not_migrate_or_initialize(tmp_path):
    store = SQLiteStore(tmp_path/'status.sqlite')
    store.conn.set_authorizer(readonly)
    try:
        status = model_budget_status(store, daily_max=30, now=NOW)
        assert not status['initialized']
        assert status['characters'] == status['unfinished'] == []
        assert status['budget_day'] == '2026-10-26' and status['daily_max'] == 30
        assert store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='model_invocation_receipts'").fetchone() is None
    finally:
        store.close()


def test_status_does_not_expose_results_or_identity_payload(tmp_path):
    class Answer(BaseModel):
        text: str
    store = SQLiteStore(tmp_path/'status.sqlite')
    budget = ModelInvocationBudget(store, daily_max=2, clock=lambda:NOW)
    budget.invoke(character_id='rin', opportunity_id='op-1', step='PLAN',
                  identity={'private_prompt':'PRIVATE_INPUT'}, schema=Answer,
                  call=lambda:Answer(text='PRIVATE_OUTPUT'))
    store.conn.execute("INSERT INTO model_invocation_receipts(identity_key,character_id,opportunity_id,step,request_hash,budget_day,status,claimed_at) VALUES('unknown','rin','op-2','APPRAISE','hash','2026-10-26','STARTED',?)",(NOW.isoformat(),))
    store.conn.commit()
    store.conn.set_authorizer(readonly)
    try:
        status = model_budget_status(store, daily_max=2, now=NOW)
        assert status['initialized']
        assert status['characters'] == [{'character_id':'rin','used':2,'remaining':0}]
        assert len(status['unfinished']) == 1 and status['unfinished'][0]['opportunity_id'] == 'op-2'
        assert 'PRIVATE_INPUT' not in str(status) and 'PRIVATE_OUTPUT' not in str(status)
        assert 'result_json' not in str(status) and 'request_hash' not in str(status)
    finally:
        store.close()
