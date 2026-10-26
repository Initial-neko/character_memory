"""Durable per-invocation reservations never replay unknown paid work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

from pydantic import BaseModel
import pytest

from character_memory.runtime.model_execution import ModelInvocationBudget, InvocationDenied
from character_memory.storage.sqlite import SQLiteStore


class Answer(BaseModel):
    text: str


class OtherAnswer(BaseModel):
    number: int


NOW = datetime(2026, 10, 26, 23, 59, tzinfo=timezone.utc)
IDENTITY = {'prompt_sha256': 'a' * 64, 'model_sha256': 'b' * 64,
            'images_sha256': 'c' * 64, 'policy_sha256': 'd' * 64}


def invoke(budget, call, **changes):
    args = dict(character_id='rin', opportunity_id='reflection:1', step='appraise',
                identity=dict(IDENTITY), schema=Answer, call=call)
    args.update(changes)
    return budget.invoke(**args)


def denied(reason, operation):
    with pytest.raises(InvocationDenied) as error:
        operation()
    assert error.value.reason == reason


@pytest.fixture
def store(tmp_path):
    value = SQLiteStore(tmp_path / 'budget.sqlite')
    yield value
    value.close()


def test_success_is_validated_and_replayed_after_restart(tmp_path):
    path = tmp_path / 'restart.sqlite'
    calls = []
    def call():
        calls.append(1)
        return Answer(text='accepted')
    first = SQLiteStore(path)
    result = invoke(ModelInvocationBudget(first, daily_max=1, clock=lambda: NOW), call)
    assert isinstance(result, Answer) and result.text == 'accepted'
    first.close()
    second = SQLiteStore(path)
    try:
        replay = invoke(ModelInvocationBudget(second, daily_max=0, clock=lambda: NOW + timedelta(days=2)), call)
        assert replay == result and calls == [1]
        assert second.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 1
    finally:
        second.close()


@pytest.mark.parametrize('limit', [0, 1])
def test_budget_denial_does_not_claim_or_call(store, limit):
    calls = []
    budget = ModelInvocationBudget(store, daily_max=limit, clock=lambda: NOW)
    if limit:
        invoke(budget, lambda: calls.append(1) or Answer(text='first'))
    before = store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0]
    denied('BUDGET_EXHAUSTED', lambda: invoke(budget, lambda: calls.append(2) or Answer(text='extra'), opportunity_id='next'))
    assert len(calls) == limit
    assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == before


@pytest.mark.parametrize('mutation', ['identity', 'schema'])
def test_identity_or_schema_change_cannot_read_old_result(store, mutation):
    calls = []
    budget = ModelInvocationBudget(store, daily_max=4, clock=lambda: NOW)
    invoke(budget, lambda: calls.append(1) or Answer(text='private-result'))
    changes = {'identity': {**IDENTITY, 'prompt_sha256': 'e' * 64}} if mutation == 'identity' else {'schema': OtherAnswer}
    denied('REQUEST_ID_CONFLICT', lambda: invoke(budget, lambda: calls.append(2), **changes))
    assert calls == [1]
    other = invoke(budget, lambda: Answer(text='other-character'), character_id='momo')
    assert other.text == 'other-character'


def test_concurrent_same_identity_calls_provider_once(tmp_path):
    path = tmp_path / 'race.sqlite'
    stores = [SQLiteStore(path), SQLiteStore(path)]
    budgets = [ModelInvocationBudget(item, daily_max=2, clock=lambda: NOW) for item in stores]
    entered, release = threading.Event(), threading.Event()
    calls = []
    def call():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return Answer(text='one')
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(invoke, budgets[0], call)
            assert entered.wait(5)
            try:
                denied('IN_PROGRESS_OR_UNKNOWN', lambda: invoke(budgets[1], call))
            finally:
                release.set()
            assert first.result().text == 'one'
        assert calls == [1]
    finally:
        release.set()
        for item in stores:
            item.close()


def test_concurrent_different_steps_share_atomic_last_daily_slot(tmp_path):
    path = tmp_path / 'last-slot.sqlite'
    stores = [SQLiteStore(path), SQLiteStore(path)]
    budgets = [ModelInvocationBudget(item, daily_max=1, clock=lambda: NOW) for item in stores]
    gate = threading.Barrier(2)
    calls = []
    def run(index):
        gate.wait()
        try:
            invoke(budgets[index], lambda: calls.append(index) or Answer(text='one'), step='step-' + str(index))
            return 'SUCCESS'
        except InvocationDenied as error:
            return error.reason
    try:
        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(run, [0, 1])) == ['BUDGET_EXHAUSTED', 'SUCCESS']
        assert len(calls) == 1
        assert stores[0].conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 1
    finally:
        for item in stores:
            item.close()


def test_actual_utc_day_rollover_and_clock_rollback_preserve_claims(store):
    clock = [datetime(2026, 10, 27, 7, 59, tzinfo=timezone(timedelta(hours=8)))]
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda: clock[0])
    invoke(budget, lambda: Answer(text='first'))
    assert store.conn.execute('SELECT budget_day FROM model_invocation_receipts').fetchone()[0] == '2026-10-26'
    clock[0] += timedelta(minutes=2)
    invoke(budget, lambda: Answer(text='next-day'), opportunity_id='next')
    clock[0] -= timedelta(minutes=2)
    denied('BUDGET_EXHAUSTED', lambda: invoke(budget, lambda: pytest.fail('rollback freed budget'), opportunity_id='rollback'))
    assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 2


def test_provider_failure_is_durable_private_and_not_retried(store):
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda: NOW)
    def fail():
        raise RuntimeError('PRIVATE_PROMPT_PROVIDER_ERROR')
    with pytest.raises(RuntimeError, match='PRIVATE_PROMPT_PROVIDER_ERROR'):
        invoke(budget, fail)
    denied('PREVIOUS_FAILURE', lambda: invoke(budget, lambda: pytest.fail('failure replayed')))
    denied('BUDGET_EXHAUSTED', lambda: invoke(budget, lambda: pytest.fail('failed call refunded'), opportunity_id='next'))
    row = store.conn.execute('SELECT * FROM model_invocation_receipts').fetchone()
    assert row['status'] == 'FAILED' and row['error_class'] == 'RuntimeError'
    assert row['result_json'] is None
    assert 'PRIVATE_PROMPT_PROVIDER_ERROR' not in '\n'.join(store.conn.iterdump())


def test_success_receipt_write_failure_remains_unknown_across_restart_and_day(tmp_path):
    path = tmp_path / 'unknown.sqlite'
    first = SQLiteStore(path)
    budget = ModelInvocationBudget(first, daily_max=1, clock=lambda: NOW)
    first.conn.execute("CREATE TRIGGER reject_success BEFORE UPDATE ON model_invocation_receipts WHEN NEW.status='SUCCESS' BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
    first.conn.commit()
    calls = []
    with pytest.raises(sqlite3.IntegrityError, match='receipt failed'):
        invoke(budget, lambda: calls.append(1) or Answer(text='paid'))
    row = first.conn.execute('SELECT * FROM model_invocation_receipts').fetchone()
    assert row['status'] == 'STARTED' and row['result_json'] is None
    first.close()
    second = SQLiteStore(path)
    try:
        later = ModelInvocationBudget(second, daily_max=1, clock=lambda: NOW + timedelta(days=1))
        denied('IN_PROGRESS_OR_UNKNOWN', lambda: invoke(later, lambda: calls.append(2)))
        assert calls == [1]
        assert second.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 1
    finally:
        second.close()


def test_oversize_result_fails_closed_and_consumes_budget(store):
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda: NOW)
    with pytest.raises(Exception):
        invoke(budget, lambda: Answer(text='大' * 65536))
    row = store.conn.execute('SELECT * FROM model_invocation_receipts').fetchone()
    assert row['status'] == 'FAILED' and row['result_json'] is None
    denied('PREVIOUS_FAILURE', lambda: invoke(budget, lambda: pytest.fail('oversize retried')))
    denied('BUDGET_EXHAUSTED', lambda: invoke(budget, lambda: pytest.fail('oversize refunded'), opportunity_id='next'))


@pytest.mark.parametrize('field,value', [('character_id', ''), ('opportunity_id', ' '), ('step', ''),
                                         ('character_id', 'a' * 1024), ('step', 'a' * 1024)])
def test_invalid_identity_does_not_claim_or_call(store, field, value):
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda: NOW)
    with pytest.raises((ValueError, TypeError)):
        invoke(budget, lambda: pytest.fail('invalid identity executed'), **{field: value})
    assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 0


def test_naive_clock_is_refused_before_claim(store):
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda: datetime(2026, 10, 26))
    with pytest.raises((ValueError, TypeError)):
        invoke(budget, lambda: pytest.fail('naive clock executed'))
    assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 0


def test_caller_transaction_cannot_erase_a_paid_reservation(store):
    budget = ModelInvocationBudget(store, daily_max=1, clock=lambda:NOW)
    calls = []
    with store.transaction(immediate=True):
        with pytest.raises(ValueError, match='outer transaction'):
            invoke(budget, lambda:calls.append(1) or Answer(text='paid'))
    assert calls == []
    assert store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0] == 0
