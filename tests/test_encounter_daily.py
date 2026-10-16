from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest

from character_memory.encounter import EncounterScheduler
from character_memory.encounter_store import EncounterRepository
from character_memory.storage.sqlite import SQLiteStore


@pytest.fixture
def stores():
    opened = []
    def create(path):
        store = SQLiteStore(str(path))
        opened.append(store)
        return store
    yield create
    for store in opened:
        try:
            store.close()
        except Exception:
            pass


def test_message_window_contains_latest_not_oldest(tmp_path, stores):
    store = stores(tmp_path / 'history.db')
    repo = EncounterRepository(store)
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    c = repo.create_candidate(source_type='GENERATED', draft={'name': 'test'}, encounter_hook='hi', opening_message='hi', now=now)
    for n in range(30):
        repo.append_open_message(c['id'], 'USER', str(n), now + timedelta(seconds=n))
    messages = repo.list_messages(c['id'], limit=24)
    assert [m['content'] for m in messages] == [str(n) for n in range(6, 30)]
    store.close()


def test_daily_slot_claim_survives_restart_and_is_unique(tmp_path, stores):
    path = str(tmp_path / 'slots.db')
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    store = stores(path)
    repo = EncounterRepository(store)
    assert repo.claim_daily_slot(now, 'CLASSIC') is not None
    assert repo.claim_daily_slot(now, 'CLASSIC') is None
    assert repo.claim_daily_slot(now, 'ORIGINAL') is not None
    store.close()
    store = stores(path)
    repo = EncounterRepository(store)
    assert repo.claim_daily_slot(now, 'CLASSIC') is None
    assert repo.claim_daily_slot(now + timedelta(days=1), 'CLASSIC') is not None
    store.close()


def test_daily_scheduler_generates_each_kind_and_does_not_duplicate(tmp_path, stores):
    store = stores(tmp_path / 'daily.db')
    repo = EncounterRepository(store)
    settings = SimpleNamespace(api_key='test', encounter_enabled=True, encounter_daily_enabled=True, encounter_max_pending=3)
    scheduler = EncounterScheduler(SimpleNamespace(settings=settings), repo)
    generated = []
    def create_candidate(**kwargs):
        generated.append(kwargs['encounter_kind'])
        c = repo.create_candidate(source_type='CLASSIC' if kwargs['encounter_kind'] == 'CLASSIC' else 'GENERATED',
            draft={'name': kwargs['encounter_kind']}, encounter_hook='hi', opening_message='hi',
            now=kwargs['now'], encounter_kind=kwargs['encounter_kind'], daily_key=kwargs['daily_key'])
        return {'candidate': c, 'details': {}}
    scheduler.service.create_candidate = create_candidate
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    assert len(scheduler.run_once(now)) == 2
    assert scheduler.run_once(now) == []
    assert generated == ['CLASSIC', 'ORIGINAL']
    assert {x['encounter_kind'] for x in repo.list_candidates()} == {'CLASSIC', 'ORIGINAL'}
    store.close()


def test_expiry_keeps_started_conversations_and_expires_only_unstarted(tmp_path, stores):
    repo=EncounterRepository(stores(tmp_path/'expiry.db'))
    yesterday=datetime(2026,10,15,12,tzinfo=timezone.utc)
    def candidate(name):
        return repo.create_candidate(source_type='GENERATED',draft={'name':name},encounter_hook='hi',opening_message='hi',now=yesterday)
    untouched=candidate('untouched'); chatting=candidate('chatting')
    repo.append_open_message(chatting['id'],'USER','hello',yesterday)
    repo.expire_unstarted(yesterday+timedelta(days=1))
    assert repo.get_candidate(untouched['id'])['status']=='EXPIRED'
    assert repo.get_candidate(chatting['id'])['status']=='CHATTING'


def test_failed_classic_slot_does_not_block_original_and_retries_are_bounded(tmp_path, stores):
    repo=EncounterRepository(stores(tmp_path/'partial.db'))
    settings=SimpleNamespace(api_key='test',encounter_enabled=True,encounter_daily_enabled=True,encounter_max_pending=3)
    scheduler=EncounterScheduler(SimpleNamespace(settings=settings),repo)
    def create(**kwargs):
        if kwargs['encounter_kind']=='CLASSIC':raise RuntimeError('provider failed')
        return {'candidate':repo.create_candidate(source_type='GENERATED',draft={'name':'original'},encounter_hook='hi',opening_message='hi',
            now=kwargs['now'],daily_key=kwargs['daily_key'],encounter_kind='ORIGINAL')}
    scheduler.service.create_candidate=create
    now=datetime(2026,10,16,12,tzinfo=timezone.utc)
    assert [x['status'] for x in scheduler.run_once(now)]==['FAILED','CREATED']
    assert scheduler.run_once(now+timedelta(minutes=1))==[]
    assert [x['status'] for x in scheduler.run_once(now+timedelta(minutes=11))]==['FAILED']


def test_cross_connection_daily_claim_is_serialized(tmp_path, stores):
    import concurrent.futures
    path=tmp_path/'concurrent.db'
    a,b=EncounterRepository(stores(path)),EncounterRepository(stores(path))
    now=datetime(2026,10,16,12,tzinfo=timezone.utc)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        claimed=list(executor.map(lambda repo:repo.claim_daily_slot(now,'CLASSIC'),[a,b]))
    assert sum(key is not None for key in claimed)==1


def test_coalesced_burst_reply_is_newer_than_both_user_messages(tmp_path, stores):
    repo=EncounterRepository(stores(tmp_path/'burst.db'))
    now=datetime.now(timezone.utc)-timedelta(seconds=10)
    c=repo.create_candidate(source_type='GENERATED',draft={'name':'test'},encounter_hook='hi',opening_message='hi',now=now)
    scheduler=EncounterScheduler(SimpleNamespace(require_bundle=lambda:SimpleNamespace(model=SimpleNamespace(
        structured_for_session=lambda *args:SimpleNamespace(message='回答两句话')))),repo)
    repo.append_open_message(c['id'],'USER','one',now)
    scheduler.enqueue_reply(c['id'],now=now)
    repo.append_open_message(c['id'],'USER','two',now+timedelta(seconds=2))
    scheduler.enqueue_reply(c['id'],now=now+timedelta(seconds=2))
    scheduler.drain_pending_replies()
    assert [m['role'] for m in repo.list_messages(c['id'])]==['USER','USER','CHARACTER']
    assert repo.candidates_waiting_for_reply()==[]


def test_active_conversations_are_filtered_before_history_limit(tmp_path, stores):
    repo=EncounterRepository(stores(tmp_path/'discoverable.db'))
    now=datetime.now(timezone.utc)
    old=repo.create_candidate(source_type='GENERATED',draft={'name':'old friend'},encounter_hook='hi',opening_message='hi',now=now-timedelta(days=40))
    repo.append_open_message(old['id'],'USER','still here',now-timedelta(days=40))
    for n in range(60):
        c=repo.create_candidate(source_type='GENERATED',draft={'name':str(n)},encounter_hook='hi',opening_message='hi',now=now+timedelta(seconds=n))
        repo.set_status(c['id'],'DISMISSED',now)
    assert old['id'] not in [c['id'] for c in repo.list_candidates(limit=50)]
    assert [c['id'] for c in repo.list_candidates(limit=50,view='ongoing')]==[old['id']]
    assert len(repo.list_candidates(limit=50,offset=50))==11
