from dataclasses import replace
import json

from character_memory.runtime.capability_execution import CapabilityExecutor, CapabilityResult
from character_memory.storage.sqlite import SQLiteStore
from test_claude_process import request, Observer


class Runner:
    def __init__(self): self.calls=[]
    def run(self, req):
        self.calls.append(req)
        return CapabilityResult(req.request_id,"SUCCESS", {"observations":Observer().observe("q")["observations"],
            "execution":{"backend":"claude","usage_state":"COMPLETE","estimated_cost_usd":.02,"input_tokens":20,"output_tokens":10}})


def limits(**extra):
    return {"backend":"claude", "claude_daily_max":3,"claude_daily_budget_usd":None,"claude_max_budget_usd":None,
            "cost_saving":False, **extra}


def test_claude_backend_uses_existing_receipt_and_never_replays(tmp_path):
    store=SQLiteStore(tmp_path/"test.db")
    runner=Runner()
    req=replace(request(), constraints={**request().constraints, **limits()})
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    result=executor.execute(req)
    assert result.status == "SUCCESS" and len(runner.calls)==1
    assert executor.execute(req)==result and len(runner.calls)==1
    meta=json.loads(store.conn.execute("SELECT execution_metadata_json FROM capability_executions").fetchone()[0])
    assert meta["backend"]=="claude" and meta["estimated_cost_usd"]==.02
    store.close()
    store=SQLiteStore(tmp_path/"test.db")
    assert CapabilityExecutor(store,Observer(),claude_runner=runner).execute(req)==result
    assert len(runner.calls)==1
    store.close()


def test_daily_cap_and_unknown_reservation_survive_restart(tmp_path):
    store=SQLiteStore(tmp_path/"test.db")
    runner=Runner()
    base=request()
    req=replace(base,constraints={**base.constraints,**limits(claude_daily_max=1)})
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    assert executor.execute(req).status=="SUCCESS"
    other=replace(req,request_id="other",opportunity_id="other")
    assert executor.execute(other).reason=="CLAUDE_DAILY_LIMIT"
    assert len(runner.calls)==1
    store.close()


def test_claude_budget_cannot_be_enforced_without_per_run_reservation(tmp_path):
    store=SQLiteStore(tmp_path/"test.db")
    runner=Runner()
    base=request()
    req=replace(base,constraints={**base.constraints,**limits(cost_saving=True,claude_daily_budget_usd=1.0)})
    assert CapabilityExecutor(store,Observer(),claude_runner=runner).execute(req).reason=="INVALID_CONSTRAINTS"
    assert not runner.calls
    store.close()


def test_direct_backend_ignores_missing_claude_and_remains_compatible(tmp_path):
    store=SQLiteStore(tmp_path/"test.db")
    assert CapabilityExecutor(store,Observer()).execute(request()).status=="SUCCESS"
    store.close()
