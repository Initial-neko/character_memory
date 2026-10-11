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


def test_daily_cap_survives_restart(tmp_path):
    store=SQLiteStore(tmp_path/"test.db")
    runner=Runner()
    base=request()
    req=replace(base,constraints={**base.constraints,**limits(claude_daily_max=1)})
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    assert executor.execute(req).status=="SUCCESS"
    store.close()
    store=SQLiteStore(tmp_path/"test.db")
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    other=replace(req,request_id="other",opportunity_id="other")
    assert executor.execute(other).reason=="CLAUDE_DAILY_LIMIT"
    assert len(runner.calls)==1
    store.close()


def test_unknown_and_interrupted_reservations_survive_restart(tmp_path):
    class UnknownRunner(Runner):
        def run(self, req):
            self.calls.append(req)
            return CapabilityResult(req.request_id,"TIMEOUT", {"execution":{"backend":"claude","usage_state":"UNKNOWN","estimated_cost_usd":None}})
    store=SQLiteStore(tmp_path/"test.db")
    runner=UnknownRunner()
    base=request()
    req=replace(base,constraints={**base.constraints,**limits(cost_saving=True,claude_daily_budget_usd=.5,claude_max_budget_usd=.3)})
    assert CapabilityExecutor(store,Observer(),claude_runner=runner).execute(req).status=="TIMEOUT"
    # Simulate a crash after reservation, before a final receipt.
    store.conn.execute("UPDATE capability_executions SET status='STARTED',result_json=NULL")
    store.conn.commit()
    store.close()
    store=SQLiteStore(tmp_path/"test.db")
    executor=CapabilityExecutor(store,Observer(),claude_runner=runner)
    assert executor.execute(req).reason=="IN_PROGRESS_OR_INTERRUPTED"
    assert executor.execute(replace(req,request_id="later",opportunity_id="later")).reason=="CLAUDE_DAILY_BUDGET"
    assert len(runner.calls)==1
    store.close()


def test_budget_reservation_blocks_competitor_and_resets_next_day(tmp_path):
    import threading
    from datetime import datetime,timedelta
    entered,release=threading.Event(),threading.Event()
    class Pending(Runner):
        def run(self,req):
            entered.set()
            assert release.wait(3)
            return super().run(req)
    first_store=SQLiteStore(tmp_path/"shared.db")
    second_store=SQLiteStore(tmp_path/"shared.db")
    pending=Pending()
    base=request()
    req=replace(base,constraints={**base.constraints,**limits(cost_saving=True,claude_daily_budget_usd=.5,claude_max_budget_usd=.3)})
    now=datetime.now().astimezone()
    first=CapabilityExecutor(first_store,Observer(),claude_runner=pending)
    second=CapabilityExecutor(second_store,Observer(),claude_runner=Runner())
    thread=threading.Thread(target=lambda:first.execute(req,now=now))
    thread.start()
    try:
        assert entered.wait(3)
        other=replace(req,request_id="competitor",opportunity_id="competitor")
        assert second.execute(other,now=now).reason=="CLAUDE_DAILY_BUDGET"
        assert second.execute(other,now=now+timedelta(days=1)).status=="SUCCESS"
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        first_store.close()
        second_store.close()


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


def test_world_chain_uses_one_planner_and_one_appraisal_without_private_cli_context(tmp_path):
    from types import SimpleNamespace
    from test_world_activity import make_access, NOW
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository
    store,access,model=make_access(tmp_path,count=2)
    access.settings=access.settings.model_copy(update={"world_browse_execution_backend":"claude"})
    runner=Runner()
    access.services=SimpleNamespace(claude_runner=runner)
    service=WorldActivityService(access,WorldPulseRepository(store))
    result=service.browse_character("c00",now=NOW)
    assert result["kept"] and len(runner.calls)==1
    assert [call[0] for call in model.calls]==["PersonalBrowsePlan","PersonalBrowseAppraisal"]
    sent=runner.calls[0]
    assert sent.character_id=="c00" and sent.arguments=={"query":"agent memory architecture"}
    assert sent.constraints["claude_max_turns"]==access.settings.claude_execution_max_turns
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE character_id='c00' AND event_type='WORLD_OBSERVATION'").fetchone()[0]==1
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE character_id='c01' AND event_type='WORLD_OBSERVATION'").fetchone()[0]==0
    model.browse=False
    service.browse_character("c01",now=NOW)
    assert len(runner.calls)==1 and len(model.calls)==3
    store.close()


def test_failed_claude_execution_skips_appraisal_and_observation(tmp_path):
    from types import SimpleNamespace
    from test_world_activity import make_access, NOW
    from character_memory.world_activity import WorldActivityService, WorldPulseRepository
    class Failure(Runner):
        def run(self,req):
            self.calls.append(req)
            return CapabilityResult(req.request_id,"FAILED",reason="NO_VERIFIED_CONTENT")
    store,access,model=make_access(tmp_path,count=1)
    access.settings=access.settings.model_copy(update={"world_browse_execution_backend":"claude"})
    access.services=SimpleNamespace(claude_runner=Failure())
    import pytest
    with pytest.raises(RuntimeError,match="NO_VERIFIED_CONTENT"):
        WorldActivityService(access,WorldPulseRepository(store)).browse_character("c00",now=NOW)
    assert [call[0] for call in model.calls]==["PersonalBrowsePlan"]
    assert store.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='WORLD_OBSERVATION'").fetchone()[0]==0
    store.close()
