"""World planning and appraisal must share durable actual-UTC admission."""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from character_memory.runtime.model_execution import InvocationDenied, ModelInvocationBudget
from test_world_execution_integration import setup, NOW


def count_receipts(store):
    return store.conn.execute('SELECT COUNT(*) FROM model_invocation_receipts').fetchone()[0]


def test_zero_budget_blocks_world_before_any_model_invocation_claim(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    access.settings.autonomous_model_daily_max = 0
    ModelInvocationBudget(store, daily_max=0)
    try:
        with pytest.raises(InvocationDenied) as error:
            service.browse_character('c00', now=NOW, opportunity_id='budget-zero')
        assert error.value.reason == 'BUDGET_EXHAUSTED'
        assert model.calls == []
        assert count_receipts(store) == 0
        assert not store.list_events('c00')
    finally:
        store.close()


def test_one_slot_admits_planning_but_refuses_paid_appraisal(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    access.settings.autonomous_model_daily_max = 1
    ModelInvocationBudget(store, daily_max=1)
    try:
        with pytest.raises(InvocationDenied) as error:
            service.browse_character('c00', now=NOW, opportunity_id='only-plan')
        assert error.value.reason == 'BUDGET_EXHAUSTED'
        assert [name for name, _, _ in model.calls] == ['PersonalBrowsePlan']
        assert count_receipts(store) == 1
        assert not store.list_events('c00')
    finally:
        store.close()


def test_distinct_opportunities_share_limit_and_business_date_cannot_reset_it(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    access.settings.autonomous_model_daily_max = 1
    model.browse = False
    ModelInvocationBudget(store, daily_max=1)
    try:
        result = service.browse_character('c00', now=NOW, opportunity_id='quiet-first')
        assert not result['browsed'] and len(model.calls) == 1
        with pytest.raises(InvocationDenied) as error:
            service.browse_character('c00', now=NOW + timedelta(days=45), opportunity_id='future-business-day')
        assert error.value.reason == 'BUDGET_EXHAUSTED'
        assert len(model.calls) == 1 and count_receipts(store) == 1
        row = store.conn.execute('SELECT budget_day FROM model_invocation_receipts').fetchone()
        assert row['budget_day'] == datetime.now(timezone.utc).date().isoformat()
    finally:
        store.close()


def test_appraised_local_recovery_needs_no_budget_or_model_calls(tmp_path):
    store, access, model, repo, service = setup(tmp_path)
    access.settings.autonomous_model_daily_max = 2
    ModelInvocationBudget(store, daily_max=2)
    try:
        store.conn.execute("CREATE TRIGGER reject_observation BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'local write failed'); END")
        store.conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match='local write failed'):
            service.browse_character('c00', now=NOW, opportunity_id='recover-local')
        assert repo.get_browse_decision('recover-local')['phase'] == 'APPRAISED'
        assert len(model.calls) == 2 and count_receipts(store) == 2
        store.conn.execute('DROP TRIGGER reject_observation')
        store.conn.commit()
        access.settings.autonomous_model_daily_max = 0
        recovered = service.browse_character('c00', now=NOW + timedelta(days=2), opportunity_id='recover-local')
        assert recovered['kept'] and len(model.calls) == 2 and count_receipts(store) == 2
        assert store.get_event(recovered['source_event_id']).event_time == NOW
    finally:
        store.close()


@pytest.mark.parametrize('limit', [0, 1, 2])
def test_rss_planner_and_batch_appraisal_share_model_budget(tmp_path, limit):
    from test_rss_world_perception import consumer_fixture
    store, access, model, rss, source, reading, service = consumer_fixture(tmp_path)
    access.settings.autonomous_model_daily_max = limit
    ModelInvocationBudget(store, daily_max=limit)
    try:
        if limit < 2:
            with pytest.raises(InvocationDenied) as error:
                service.browse_character('c00', now=NOW, opportunity_id='rss-model-budget')
            assert error.value.reason == 'BUDGET_EXHAUSTED'
        else:
            result = service.browse_character('c00', now=NOW, opportunity_id='rss-model-budget')
            assert result['reading_choice'] == 'READ_RSS' and result['kept']
        assert len(model.calls) == limit and count_receipts(store) == limit
        rows = store.conn.execute('SELECT capability,request_json FROM (SELECT json_extract(request_json,\'$.capability\') AS capability,request_json FROM capability_executions)').fetchall()
        assert len(rows) == int(limit > 0)
        if rows:
            import json
            assert rows[0]['capability'] == 'READ_RSS'
            assert json.loads(rows[0]['request_json'])['constraints']['max_calls'] == 1
        if limit < 2:
            assert not store.list_events('c00')
    finally:
        store.close()


@pytest.mark.parametrize('limit', [0, 1])
def test_continuation_refuses_extra_model_without_extra_tool_claim(tmp_path, limit):
    from test_world_continuation import setup as continuation_setup
    decisions = [{'action': 'CALL', 'capability': 'WEB_SEARCH', 'arguments': {'query': 'bounded research'}}, {'action': 'STOP'}]
    store, access, model, prompts, service = continuation_setup(tmp_path, decisions)
    access.settings.autonomous_model_daily_max = limit
    ModelInvocationBudget(store, daily_max=limit)
    try:
        result = service.browse_character('c00', now=NOW, opportunity_id='continuation-model-budget')
        assert result['execution_status'] == 'FAILED'
        assert any(error['error'] == 'BUDGET_EXHAUSTED' for error in result['errors'])
        assert len(prompts) == limit and count_receipts(store) == limit
        assert store.conn.execute('SELECT COUNT(*) FROM capability_executions').fetchone()[0] == limit
        assert not store.list_events('c00')
        repeated = service.browse_character('c00', now=NOW + timedelta(days=2), opportunity_id='continuation-model-budget')
        assert repeated == result and len(prompts) == limit and count_receipts(store) == limit
    finally:
        store.close()


def test_continuation_keeps_existing_three_tool_attempt_ceiling(tmp_path):
    import json
    from test_world_continuation import setup as continuation_setup
    decisions = [{'action': 'CALL', 'capability': 'WEB_SEARCH', 'arguments': {'query': 'query ' + str(i)}} for i in range(3)]
    store, access, model, prompts, service = continuation_setup(tmp_path, decisions)
    access.settings.autonomous_model_daily_max = 30
    try:
        result = service.browse_character('c00', now=NOW, opportunity_id='unchanged-tool-ceiling')
        assert result['stop_reason'] == 'STEP_BUDGET_EXHAUSTED'
        assert len(prompts) == len(result['steps']) == count_receipts(store) == 3
        rows = store.conn.execute('SELECT request_json FROM capability_executions').fetchall()
        assert len(rows) == 3
        assert all(json.loads(row[0])['constraints']['max_calls'] == 3 for row in rows)
        assert service.browse_character('c00', now=NOW, opportunity_id='unchanged-tool-ceiling') == result
        assert len(prompts) == 3
    finally:
        store.close()


def test_visual_structured_adapter_uses_same_budget_and_preserves_image_binding(tmp_path):
    from types import SimpleNamespace
    from pydantic import BaseModel
    from character_memory.runtime.model_execution import budgeted_structured
    from test_world_continuation import setup as continuation_setup
    class VisualDecision(BaseModel):
        action: str
    store, access, model, prompts, service = continuation_setup(tmp_path, [])
    access.settings.autonomous_model_daily_max = 1
    delivered = []
    def visual(prompt, images, schema, session):
        delivered.append((prompt, images, session))
        return schema(action='STOP')
    model.structured_with_images_for_session = visual
    images = ['data:image/png;base64,PRIVATE_IMAGE_FIXTURE']
    try:
        result = budgeted_structured(access, model, 'PRIVATE_VISUAL_PROMPT', VisualDecision, 'visual-session',
            character_id='c00', opportunity_id='visual-op', step='CONTINUATION:1', images=images)
        assert result.action == 'STOP' and len(delivered) == 1
        assert delivered[0][1] == images and count_receipts(store) == 1
        with pytest.raises(InvocationDenied) as error:
            budgeted_structured(access, model, 'PRIVATE_VISUAL_PROMPT', VisualDecision, 'visual-session',
                character_id='c00', opportunity_id='visual-op', step='CONTINUATION:1', images=['data:image/png;base64,changed'])
        assert error.value.reason == 'REQUEST_ID_CONFLICT' and len(delivered) == 1
        with pytest.raises(InvocationDenied) as error:
            budgeted_structured(access, model, 'another prompt', VisualDecision, 'next-visual',
                character_id='c00', opportunity_id='another-visual-op', step='CONTINUATION:1', images=images)
        assert error.value.reason == 'BUDGET_EXHAUSTED' and len(delivered) == 1
        dump = '\n'.join(store.conn.iterdump())
        assert 'PRIVATE_VISUAL_PROMPT' not in dump and 'PRIVATE_IMAGE_FIXTURE' not in dump
    finally:
        store.close()
