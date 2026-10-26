"""A real pre-binding table remains intact after the additive migration."""
import pytest

from character_memory.storage.sqlite import SQLiteStore
from character_memory.runtime.model_execution import ModelInvocationBudget, verified_structured_receipt
from character_memory.world_activity import PersonalBrowseAppraisal


def test_real_v1_table_gets_nullable_proof_once_without_backfill(tmp_path):
    store=SQLiteStore(tmp_path/'legacy.sqlite')
    def legacy():
        store.conn.execute('CREATE TABLE model_invocation_receipts('
            'identity_key TEXT PRIMARY KEY,character_id TEXT NOT NULL,opportunity_id TEXT NOT NULL,'
            'step TEXT NOT NULL,request_hash TEXT NOT NULL,budget_day TEXT NOT NULL,status TEXT NOT NULL,'
            'result_json TEXT,claimed_at TEXT NOT NULL,completed_at TEXT,error_class TEXT)')
    store.apply_schema_migration('execution/001-model-invocations',legacy,immediate=True)
    store.conn.execute("INSERT INTO model_invocation_receipts VALUES('old','rin','legacy','WEB_APPRAISAL','original-hash','2026-10-01','SUCCESS','{}','2026-10-01T00:00:00Z',NULL,NULL)")
    store.conn.commit()
    original=dict(store.conn.execute('SELECT * FROM model_invocation_receipts').fetchone())
    try:
        ModelInvocationBudget(store,daily_max=30)
        ModelInvocationBudget(store,daily_max=30)
        current=dict(store.conn.execute('SELECT * FROM model_invocation_receipts').fetchone())
        assert current.pop('binding_json') is None and current==original
        store.apply_schema_migration('execution/002-model-invocation-bindings',lambda:pytest.fail('migration replayed'),immediate=True)
        with pytest.raises(ValueError):
            verified_structured_receipt(store,character_id='rin',opportunity_id='legacy',step='WEB_APPRAISAL',
                prompt='unavailable original',schema=PersonalBrowseAppraisal,session_id='legacy')
    finally:store.close()


def test_shared_web_prompt_retains_legacy_page_order_and_format():
    from character_memory.domain.models import WorldObservation
    from character_memory.runtime.social_prompts import world_appraisal_prompt,world_pages_appraisal_prompt
    page=WorldObservation(title='标题',url='https://example.org/a',source_domain='example.org',snippet='摘录',content='第一行\n第二行')
    legacy='[Page 1]\nTitle: 标题\nURL: https://example.org/a\nSource: example.org\nSnippet: 摘录\nRendered text:\n第一行\n第二行\n'
    assert world_pages_appraisal_prompt('原人物','原查询',[page])==world_appraisal_prompt('原人物','原查询',legacy)
