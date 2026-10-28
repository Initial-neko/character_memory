from datetime import datetime, timedelta, timezone
import pytest
from character_memory.domain.facts import FactRef
from character_memory.domain.models import Event, EventType
from character_memory.storage.sqlite import SQLiteStore
from character_memory.storage.reaction_delivery import ReactionDeliveryRepository, DeliveryUnavailable


def repository(tmp_path):
    store = SQLiteStore(tmp_path / 'delivery.db')
    now = [100.0]
    return store, ReactionDeliveryRepository(store, clock=lambda: now[0]), now


def add(repo, store, number, *, media=None, mentions=None):
    ref = FactRef(kind='EVENT', id=number)
    with store.transaction():
        repo.record(ref, 'direct:momo:room', character_id='momo', conversation_id='room',
            media_ids=media or [], mentions=mentions or [])
    return ref


def test_record_requires_source_transaction_and_rolls_back(tmp_path):
    store, repo, _ = repository(tmp_path)
    with pytest.raises(RuntimeError, match='transaction'):
        repo.record(FactRef(kind='EVENT', id=1), 'channel', character_id='momo', conversation_id='room')
    with pytest.raises(ValueError):
        with store.transaction():
            repo.record(FactRef(kind='EVENT', id=1), 'channel', character_id='momo', conversation_id='room')
            raise ValueError('source transaction rolled back')
    assert repo.pending() == []
    store.close()


def test_frozen_batch_preserves_sources_media_and_plan_after_restart(tmp_path):
    store, repo, now = repository(tmp_path)
    one = add(repo, store, 1, media=['image-1'], mentions=['rin'])
    two = add(repo, store, 2, mentions=['momo', 'rin'])
    batch = repo.claim(two, lease_seconds=10, plan_factory=lambda mentions: {'speaker_order': mentions, 'max_speakers': 2})
    assert batch.snapshot['media_ids'] == ['image-1']
    assert batch.snapshot['mentions'] == ['rin', 'momo']
    assert batch.snapshot['source_refs'] == [one.model_dump(mode='json'), two.model_dump(mode='json')]
    store.close()
    store = SQLiteStore(tmp_path / 'delivery.db')
    repo = ReactionDeliveryRepository(store, clock=lambda: now[0])
    with pytest.raises(DeliveryUnavailable, match='RUNNING'):
        repo.claim(two, lease_seconds=10)
    now[0] = 110.0
    replay = repo.claim(two, lease_seconds=10, plan_factory=lambda _: pytest.fail('must reuse frozen plan'))
    assert replay.snapshot == batch.snapshot
    assert replay.token != batch.token
    assert not repo.owns(batch)
    assert repo.owns(replay)
    assert not repo.finish(batch, 'COGNITION_COMMITTED')
    assert repo.finish(replay, 'COGNITION_COMMITTED')
    assert repo.status(one) == 'COGNITION_COMMITTED'
    assert repo.pending() == []
    store.close()


def test_newer_watermark_inherits_unconsumed_images_and_supersedes_old_owner(tmp_path):
    store, repo, _ = repository(tmp_path)
    one = add(repo, store, 1, media=['first-image'])
    old = repo.claim(one, lease_seconds=10)
    two = add(repo, store, 2)
    current = repo.claim(two, lease_seconds=10)
    assert not repo.owns(old)
    assert current.snapshot['media_ids'] == ['first-image']
    assert not repo.finish(old, 'UNKNOWN')
    assert repo.finish(current, 'COGNITION_COMMITTED')
    assert repo.status(one) == 'COGNITION_COMMITTED'
    store.close()


@pytest.mark.parametrize('state', ['COGNITION_COMMITTED', 'UNKNOWN', 'BLOCKED'])
def test_terminal_delivery_is_not_automatically_reclaimed(tmp_path, state):
    store, repo, now = repository(tmp_path)
    ref = add(repo, store, 1)
    batch = repo.claim(ref, lease_seconds=10)
    repo.finish(batch, state)
    now[0] = 10000.0
    assert repo.pending() == []
    with pytest.raises(DeliveryUnavailable, match=state):
        repo.claim(ref, lease_seconds=10)
    store.close()


def wait_state(repo, ref, expected):
    import time
    until = time.monotonic() + 5
    while time.monotonic() < until:
        if repo.status(ref) == expected:
            return
        time.sleep(.02)
    pytest.fail(f'expected {expected}, got {repo.status(ref)}')


def delivery_runtime(tmp_path, *, bad=False):
    from types import SimpleNamespace
    from test_reaction_receipts import CountedModel, runtime, NOW
    from character_memory.application.async_conversation import ReactionScheduler, ConversationEventHub
    from character_memory.application.chat_service import ChatService
    from character_memory.application.clock import FixedClock
    from character_memory.application.reaction_delivery import DurableReactionDelivery
    from character_memory.config import Settings
    from character_memory.media import MediaStorage
    store = SQLiteStore(tmp_path / 'live.db')
    models = {'momo': CountedModel(fail=bad), 'rin': CountedModel()}
    runtimes = {key: runtime(store, model) for key, model in models.items()}
    clock = FixedClock(NOW)
    bundle = SimpleNamespace(settings=Settings(), store=store, runtimes=runtimes, clock=clock,
        chat=ChatService(store, runtimes, clock))
    profiles = [{'id': key, 'name': key} for key in models]
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: bundle, lambda: profiles, hub, quiet_seconds=.01, max_burst_seconds=.02)
    repo = ReactionDeliveryRepository(store)
    delivery = DurableReactionDelivery(repo, MediaStorage(tmp_path / 'media'), scheduler, lambda: profiles)
    scheduler.delivery = delivery
    return store, repo, delivery, scheduler, hub, models, bundle, profiles


@pytest.mark.parametrize('grouped', [False, True])
def test_saved_before_enqueue_is_recovered_without_request_key(tmp_path, grouped):
    from test_reaction_receipts import source, group_service, NOW
    from character_memory.application.async_conversation import direct_channel, group_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    if grouped:
        service = group_service(store, models)
        group = service.create_group('recover', list(models), at=NOW)
        event = service.persist_user_event(group.id, 'saved before enqueue', at=NOW)
        ref = FactRef(kind='CONVERSATION_EVENT', id=event.id, conversation_id=group.id)
        channel, character, room = group_channel(group.id), None, group.id
    else:
        event = source(store)
        ref = FactRef(kind='EVENT', id=event.id)
        channel, character, room = direct_channel('momo', 'once'), 'momo', 'once'
    with store.transaction():
        repo.record(ref, channel, character_id=character, conversation_id=room)
    delivery.recover_once()
    wait_state(repo, ref, 'COGNITION_COMMITTED')
    assert models['momo'].calls == 1
    if grouped:
        assert models['rin'].calls == 1
    delivery.recover_once()
    assert models['momo'].calls == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_missing_media_blocks_recovery_before_any_provider_call(tmp_path):
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, *_ = delivery_runtime(tmp_path)
    event = source(store)
    ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once', media_ids=['missing'])
    delivery.recover_once()
    wait_state(repo, ref, 'BLOCKED')
    assert models['momo'].calls == 0
    assert repo.pending() == []
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_partial_group_recovery_keeps_original_plan_and_does_not_repeat_committed_member(tmp_path):
    from test_reaction_receipts import group_service, NOW
    from character_memory.application.async_conversation import group_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    service = group_service(store, models)
    group = service.create_group('partial', list(models), at=NOW)
    event = service.persist_user_event(group.id, 'partly done', at=NOW)
    ref = FactRef(kind='CONVERSATION_EVENT', id=event.id, conversation_id=group.id)
    with store.transaction():
        repo.record(ref, group_channel(group.id), conversation_id=group.id)
    old = ReactionDeliveryRepository(store, clock=lambda: 100)
    batch = old.claim(ref, lease_seconds=1, plan_factory=lambda mentions: service.plan_reaction(event, mention_order=mentions))
    first = batch.snapshot['plan']['speaker_order'][0]
    service._react_member(group=group, source_event=event, character_id=first)
    # Changed runtime limit must not truncate the original persisted plan.
    bundle.settings.group_max_speakers_per_turn = 1
    delivery.recover_once()
    wait_state(repo, ref, 'COGNITION_COMMITTED')
    assert [model.calls for model in models.values()] == [1, 1]
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_unknown_member_is_not_recalled_and_aggregate_is_unknown(tmp_path):
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, *_ = delivery_runtime(tmp_path, bad=True)
    event = source(store); ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
    delivery.recover_once()
    wait_state(repo, ref, 'UNKNOWN')
    delivery.recover_once()
    assert models['momo'].calls == 1
    assert repo.pending() == []
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_recovery_of_image_then_text_preserves_actual_asset_bytes(tmp_path):
    from test_reaction_receipts import source, NOW
    from character_memory.application.async_conversation import direct_channel
    from io import BytesIO
    import base64
    from PIL import Image
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    image = BytesIO(); Image.new("RGB", (2, 2), "red").save(image, format="PNG")
    expected = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
    asset, normalized = delivery.media_storage.save_data_url(character_id="momo", original_name="image.png", data_url=expected, created_at=NOW)
    with store.transaction():
        store.add_media_asset(asset)
        first = source(store)
        repo.record(FactRef(kind="EVENT", id=first.id), direct_channel("momo", "once"),
            character_id="momo", conversation_id="once", media_ids=[asset.id])
        last = source(store)
        ref = FactRef(kind="EVENT", id=last.id)
        repo.record(ref, direct_channel("momo", "once"), character_id="momo", conversation_id="once")
    seen = []
    person = bundle.runtimes["momo"]
    handle = person.handle
    def observe(*args, **kwargs):
        seen.extend(kwargs.get("image_data_urls") or [])
        return handle(*args, **kwargs)
    person.handle = observe
    delivery.recover_once()
    wait_state(repo, ref, "COGNITION_COMMITTED")
    assert seen == [normalized]
    assert models["momo"].calls == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_archived_character_blocks_without_loading_a_model_call(tmp_path):
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    event = source(store); ref = FactRef(kind="EVENT", id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel("momo", "once"), character_id="momo", conversation_id="once")
    profiles[0]["archived_at"] = None
    delivery.recover_once()
    wait_state(repo, ref, "BLOCKED")
    assert models["momo"].calls == 0
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_empty_delivery_scan_never_initializes_bundle(tmp_path):
    from character_memory.application.reaction_delivery import DurableReactionDelivery
    from character_memory.application.async_conversation import ReactionScheduler, ConversationEventHub
    store, repo, _ = repository(tmp_path)
    hub = ConversationEventHub()
    scheduler = ReactionScheduler(lambda: pytest.fail("empty scan initialized model"), lambda: [], hub)
    delivery = DurableReactionDelivery(repo, None, scheduler, lambda: [])
    delivery.recover_once()
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_batch_expiring_before_person_preparation_remains_recoverable(tmp_path):
    from character_memory.storage.message_receipts import ReactionExecutionRepository
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    now = [100.0]; repo.clock = lambda: now[0]
    event = source(store); ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
    batch = repo.claim(ref, lease_seconds=120)
    now[0] = 110
    receipt = ReactionExecutionRepository(store, clock=lambda: now[0])
    receipt.claim(ref, 'momo')
    now[0] = 221
    resumed = repo.claim(ref, lease_seconds=120)
    delivery.finish(resumed)
    assert repo.status(ref) == 'RUNNING'
    assert repo.pending() == []
    now[0] = 231
    assert len(repo.pending()) == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()


@pytest.mark.parametrize('kind', ['missing_person', 'archived_group'])
def test_unavailable_scope_becomes_blocked_without_endless_pending_scan(tmp_path, kind):
    from test_reaction_receipts import source, group_service, NOW
    from character_memory.application.async_conversation import direct_channel, group_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    if kind == 'archived_group':
        service = group_service(store, models)
        group = service.create_group('gone', list(models), at=NOW)
        event = service.persist_user_event(group.id, 'pending', at=NOW)
        ref = FactRef(kind='CONVERSATION_EVENT', id=event.id, conversation_id=group.id)
        with store.transaction():
            repo.record(ref, group_channel(group.id), conversation_id=group.id)
        service.repo.archive_group(group.id, NOW)
    else:
        event = source(store); ref = FactRef(kind='EVENT', id=event.id)
        with store.transaction():
            repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
        del bundle.runtimes['momo']
        profiles[:] = [p for p in profiles if p['id'] != 'momo']
    delivery.recover_once()
    wait_state(repo, ref, 'BLOCKED')
    assert repo.pending() == []
    assert all(model.calls == 0 for model in models.values())
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_shutdown_before_any_person_call_preserves_safe_pending_work(tmp_path):
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, *_ = delivery_runtime(tmp_path)
    event = source(store); ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
    batch = repo.claim(ref, lease_seconds=120)
    delivery.stop()
    delivery.finish(batch, superseded=True)
    assert repo.status(ref) == 'RUNNING'
    assert len(repo.pending()) == 1
    assert models['momo'].calls == 0
    scheduler.close(); hub.close(); store.close()


def test_scheduler_shutdown_waits_for_inflight_provider_before_store_can_close(tmp_path):
    import threading
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, models, bundle, _ = delivery_runtime(tmp_path)
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = models['momo'].react_call_for_session
    def hold(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    models['momo'].react_call_for_session = hold
    event = source(store); ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
    delivery.recover_once()
    assert started.wait(2)
    def shutdown():
        delivery.stop(); scheduler.close(); stopped.set()
    closer = threading.Thread(target=shutdown)
    closer.start()
    try:
        assert not stopped.wait(.05)
    finally:
        release.set(); closer.join(timeout=5)
    assert stopped.is_set()
    assert not scheduler._workers
    assert repo.status(ref) == 'UNKNOWN'
    hub.close(); store.close()


@pytest.mark.parametrize('grouped', [False, True])
def test_derived_commit_reserves_writer_before_scope_reads(tmp_path, grouped):
    import threading
    from test_reaction_receipts import source, group_service, NOW
    store, repo, delivery, scheduler, hub, models, bundle, _ = delivery_runtime(tmp_path)
    other = SQLiteStore(tmp_path / 'live.db')
    read_started, writer_done = threading.Event(), threading.Event()
    errors = []
    def external_write():
        try:
            assert read_started.wait(3)
            source(other, character_id='unrelated')
        except BaseException as exc:
            errors.append(exc)
        finally:
            writer_done.set()
    writer = threading.Thread(target=external_write)
    writer.start()
    def guard():
        if store._tx_depth and not read_started.is_set():
            store.conn.execute('SELECT COUNT(*) FROM events').fetchone()
            read_started.set()
            assert not writer_done.wait(.05), 'external writer invalidated derived commit snapshot'
        return True
    try:
        if grouped:
            service = group_service(store, models)
            group = service.create_group('writer', list(models), at=NOW)
            event = service.persist_user_event(group.id, 'one', at=NOW)
            result = service.react_from_event(event, commit_guard=guard)
            assert not any(row.get('error') for row in result['decisions'])
        else:
            event = source(store)
            bundle.runtimes['momo'].handle(event, persist_event=False, commit_guard=guard)
    finally:
        read_started.set(); writer.join(timeout=5)
        delivery.stop(); scheduler.close(); hub.close(); other.close(); store.close()
    assert writer_done.is_set() and not errors


def test_old_worker_cleanup_cannot_clear_a_new_generation_active_flag():
    from types import SimpleNamespace
    import threading
    from character_memory.application.async_conversation import ReactionScheduler, ConversationEventHub
    hub = ConversationEventHub(); scheduler = ReactionScheduler(lambda: None, lambda: [], hub)
    old_idle, release_old, new_started, release_new = [threading.Event() for _ in range(4)]
    def old_target(channel, state):
        assert scheduler._finish_cycle(state, 1)
        old_idle.set(); assert release_old.wait(3)
    def new_target(channel, state):
        new_started.set(); assert release_new.wait(3)
    scheduler._enqueue('race', SimpleNamespace(id=1, metadata={}), None, old_target)
    assert old_idle.wait(2)
    with scheduler._guard:
        old_worker = next(iter(scheduler._workers))
    scheduler._enqueue('race', SimpleNamespace(id=2, metadata={}), None, new_target)
    assert new_started.wait(2)
    try:
        release_old.set(); old_worker.join(timeout=2)
        assert not old_worker.is_alive()
        assert scheduler._state('race').active, 'old cleanup cleared the new worker ownership'
    finally:
        release_old.set(); release_new.set(); scheduler.close(); hub.close()


def test_delivery_stop_waits_for_a_blocked_scanner_before_database_close(tmp_path):
    import threading
    from test_reaction_receipts import source
    from character_memory.application.async_conversation import direct_channel
    store, repo, delivery, scheduler, hub, _, _, profiles = delivery_runtime(tmp_path)
    event = source(store); ref = FactRef(kind='EVENT', id=event.id)
    with store.transaction():
        repo.record(ref, direct_channel('momo', 'once'), character_id='momo', conversation_id='once')
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def delayed_profiles():
        started.set(); assert release.wait(10)
        return profiles
    delivery.profiles = delayed_profiles
    delivery.start()
    assert started.wait(2)
    stopper = threading.Thread(target=lambda: (delivery.stop(), stopped.set()))
    stopper.start()
    try:
        assert not stopped.wait(5.1), 'scanner stop returned while it could still access SQLite'
    finally:
        release.set(); stopper.join(timeout=3)
        delivery.stop(); scheduler.close(); hub.close(); store.close()
    assert not delivery._thread.is_alive()


@pytest.mark.parametrize('grouped', [False, True])
def test_corrupt_committed_cache_reports_blocked_not_cognition_success(tmp_path, grouped):
    from test_reaction_receipts import source, group_service, NOW
    from character_memory.application.async_conversation import direct_channel, group_channel
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    if grouped:
        service = group_service(store, models)
        group = service.create_group('cache', list(models), at=NOW)
        event = service.persist_user_event(group.id, 'cache', at=NOW)
        service.react_from_event(event)
        ref = FactRef(kind='CONVERSATION_EVENT', id=event.id, conversation_id=group.id)
        channel, person, room = group_channel(group.id), None, group.id
    else:
        event = source(store)
        bundle.runtimes['momo'].handle(event, persist_event=False)
        ref = FactRef(kind='EVENT', id=event.id)
        channel, person, room = direct_channel('momo', 'once'), 'momo', 'once'
    with store.transaction():
        store.conn.execute("UPDATE conversation_reaction_receipts SET result_json='{}' WHERE source_ref_json=? AND character_id='momo'", (ref.model_dump_json(),))
        repo.record(ref, channel, character_id=person, conversation_id=room)
    calls = [model.calls for model in models.values()]
    delivery.recover_once()
    wait_state(repo, ref, 'BLOCKED')
    assert [model.calls for model in models.values()] == calls
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def _direct_event(store, at, content):
    return store.append_message(Event(character_id='momo', event_type=EventType.USER_MESSAGE,
        event_time=at, content=content, metadata={'conversation_id': 'once'}))


def _record(repo, store, event):
    with store.transaction():
        repo.record(FactRef(kind='EVENT', id=event.id), 'direct:momo:once',
            character_id='momo', conversation_id='once')


def test_future_dated_fact_does_not_stall_present_delivery(tmp_path):
    """P15-F1: a future event_time must not supersede a present generation."""
    from test_reaction_receipts import NOW
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    future = _direct_event(store, NOW + timedelta(days=1), '未来的一天')
    present = _direct_event(store, NOW, '现在这一句')
    _record(repo, store, future)
    _record(repo, store, present)

    scheduler.enqueue_direct('momo', 'once', present)
    wait_state(repo, FactRef(kind='EVENT', id=present.id), 'COGNITION_COMMITTED')
    assert repo.pending() == []
    assert models['momo'].calls == 1

    # Repeated scans after the commit are idempotent: no retry storm, no replay.
    for _ in range(3):
        delivery.recover_once()
    assert repo.status(FactRef(kind='EVENT', id=present.id)) == 'COGNITION_COMMITTED'
    assert models['momo'].calls == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_future_dated_fact_is_still_delivered_on_its_own(tmp_path):
    """A future-dated input is not blocked merely because it is ahead of the clock."""
    from test_reaction_receipts import NOW
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    future = _direct_event(store, NOW + timedelta(days=1), '明天再来')
    _record(repo, store, future)

    scheduler.enqueue_direct('momo', 'once', future)
    wait_state(repo, FactRef(kind='EVENT', id=future.id), 'COGNITION_COMMITTED')
    assert repo.pending() == []
    assert models['momo'].calls == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()


def test_superseded_batch_with_pending_sources_is_reclaimed_after_restart(tmp_path):
    """A SUPERSEDED batch must never strand its still-PENDING sources (P15-F1)."""
    store, repo, now = repository(tmp_path)
    one = add(repo, store, 1)
    two = add(repo, store, 2)
    batch = repo.claim(two, lease_seconds=10)
    repo.finish(batch, 'SUPERSEDED')
    assert repo.pending(), 'superseded generation should stay recoverable'
    store.close()

    store = SQLiteStore(tmp_path / 'delivery.db')
    repo = ReactionDeliveryRepository(store, clock=lambda: now[0])
    again = repo.claim(two, lease_seconds=10)
    assert again is not None and again.ref == two
    assert repo.finish(again, 'COGNITION_COMMITTED')
    assert repo.status(one) == 'COGNITION_COMMITTED'
    assert repo.pending() == []
    store.close()


def test_future_dated_fact_delivers_once_when_its_time_has_arrived(tmp_path):
    """After the trusted clock reaches event_time the fact replies normally."""
    from test_reaction_receipts import NOW
    store, repo, delivery, scheduler, hub, models, bundle, profiles = delivery_runtime(tmp_path)
    event = _direct_event(store, NOW + timedelta(days=1), '到了明天')
    _record(repo, store, event)

    bundle.clock.set(NOW + timedelta(days=2))
    delivery.recover_once()
    wait_state(repo, FactRef(kind='EVENT', id=event.id), 'COGNITION_COMMITTED')
    assert models['momo'].calls == 1

    delivery.recover_once()
    delivery.recover_once()
    assert repo.pending() == []
    assert models['momo'].calls == 1
    delivery.stop(); scheduler.close(); hub.close(); store.close()
