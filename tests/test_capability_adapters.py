from dataclasses import replace

from character_memory.runtime.capability_execution import CapabilityAdapter, CapabilityExecutor, CapabilityResult
from test_capability_execution import request
from character_memory.storage.sqlite import SQLiteStore


def test_registered_adapter_uses_existing_receipt_and_shared_budget(tmp_path):
    store = SQLiteStore(tmp_path/'adapters.sqlite')
    calls = []
    def perform(req):
        calls.append(req.request_id)
        return CapabilityResult(req.request_id, 'SUCCESS', {'observed':'actual result'})
    adapter = CapabilityAdapter(channels=frozenset({'WORLD'}), validate=lambda req:'', execute=perform)
    executor = CapabilityExecutor(store, None, adapters={'READ_PAGE':adapter})
    base = replace(request(), capability='READ_PAGE', arguments={'link_ref':'seen:1'}, constraints={'max_calls':2})
    first = executor.execute(base)
    assert first.status == 'SUCCESS' and executor.execute(base) == first
    assert executor.execute(replace(base, request_id='second')).status == 'SUCCESS'
    assert executor.execute(replace(base, request_id='third')).reason == 'BUDGET_EXHAUSTED'
    assert len(calls) == 2
    store.close()


def test_adapter_permission_revocation_and_channel_cannot_be_bypassed_by_cache(tmp_path):
    store = SQLiteStore(tmp_path/'adapters.sqlite')
    granted = [True]
    calls = []
    def perform(req):
        calls.append(req.request_id)
        return CapabilityResult(req.request_id, 'SUCCESS')
    adapter = CapabilityAdapter(channels=frozenset({'WORLD'}), validate=lambda req:'' if granted[0] else 'ROLE_CAPABILITY_DENIED', execute=perform)
    executor = CapabilityExecutor(store, None, adapters={'READ_PAGE':adapter})
    req = replace(request(), capability='READ_PAGE', constraints={'max_calls':1})
    assert executor.execute(req).status == 'SUCCESS'
    granted[0] = False
    assert executor.execute(req).reason == 'ROLE_CAPABILITY_DENIED'
    assert executor.execute(replace(req, channel='DIRECT')).reason == 'CHANNEL_DENIED'
    assert len(calls) == 1
    store.close()


def test_adapter_unknown_after_restart_and_payload_conflicts_do_not_repeat(tmp_path):
    store = SQLiteStore(tmp_path/'adapters.sqlite')
    calls = []
    def crash(req):
        calls.append(req.request_id)
        raise KeyboardInterrupt('crash after reserving')
    adapter = CapabilityAdapter(channels=frozenset({'WORLD'}), validate=lambda req:'', execute=crash)
    executor = CapabilityExecutor(store, None, adapters={'READ_PAGE':adapter})
    req = replace(request(), capability='READ_PAGE', constraints={'max_calls':1})
    import pytest
    with pytest.raises(KeyboardInterrupt): executor.execute(req)
    assert executor.execute(req).reason == 'IN_PROGRESS_OR_INTERRUPTED'
    assert executor.execute(replace(req, arguments={'query':'changed'})).reason == 'REQUEST_ID_CONFLICT'
    assert len(calls) == 1
    store.close()


def test_adapter_cannot_shadow_builtins_and_malformed_calls_never_execute(tmp_path):
    import pytest
    store = SQLiteStore(tmp_path/'bad.sqlite')
    calls = []
    adapter = CapabilityAdapter(channels={'WORLD'}, validate=lambda req:'', execute=lambda req:calls.append(req))
    with pytest.raises(ValueError): CapabilityExecutor(store, None, adapters={'WEB_SEARCH':adapter})
    executor = CapabilityExecutor(store, None, adapters={'EXTENSION':adapter})
    req = replace(request(), capability='EXTENSION', constraints={'max_calls':1})
    for bad in (replace(req, arguments=None), replace(req, channel=[]),
                replace(req, constraints={'max_calls':True}), replace(req, character_id=''),
                replace(req, constraints={'max_calls':1,'role_grant':'forged'})):
        assert executor.execute(bad).status == 'DENIED'
    assert calls == []
    store.close()


def test_adapter_completed_cache_survives_provider_loss_but_attempt_denials_consume_budget(tmp_path):
    store = SQLiteStore(tmp_path/'cache.sqlite')
    ready = [True]
    calls = []
    def perform(req):
        calls.append(req.request_id)
        return CapabilityResult(req.request_id,'DENIED',reason='REMOTE_DENIED')
    adapter = CapabilityAdapter(channels={'WORLD'},validate=lambda req:'',execute=perform,available=lambda:ready[0])
    executor = CapabilityExecutor(store,None,adapters={'EXTENSION':adapter})
    req = replace(request(),capability='EXTENSION',constraints={'max_calls':1})
    result = executor.execute(req)
    assert result.reason == 'REMOTE_DENIED'
    ready[0] = False
    assert executor.execute(req) == result
    ready[0] = True
    assert executor.execute(replace(req,request_id='another')).reason == 'BUDGET_EXHAUSTED'
    assert len(calls) == 1
    store.close()
