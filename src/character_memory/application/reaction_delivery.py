"""Connect durable message delivery to the existing reaction scheduler."""
import logging
import threading
from character_memory.domain.facts import FactRef
from character_memory.group_store import GroupRepository

logger = logging.getLogger(__name__)


class DeliveryStopping(RuntimeError):
    """Stop admission without turning safe preparation into an unknown call."""


class DurableReactionDelivery:
    def __init__(self, repository, media_storage, scheduler, profiles):
        self.repository = repository
        self.media_storage = media_storage
        self.scheduler = scheduler
        self.profiles = profiles
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='reaction-delivery', daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.recover_once()
            except Exception:
                logger.exception('reaction delivery scan failed')
            self._stop.wait(.5)

    def recover_once(self):
        from character_memory.application.async_conversation import direct_channel, group_channel
        store = self.repository.store
        for item in self.repository.pending():
            if self._stop.is_set():
                return
            ref = FactRef.model_validate(item['source'])
            profiles = {profile['id']: profile for profile in self.profiles()}
            if self._stop.is_set():
                return
            missing_person = ref.kind == 'EVENT' and (item['character_id'] not in profiles
                or 'archived_at' in profiles[item['character_id']])
            group = GroupRepository(store).get_group(ref.conversation_id) if ref.kind == 'CONVERSATION_EVENT' else None
            missing_group = ref.kind == 'CONVERSATION_EVENT' and (group is None or len(group.member_ids) < 2)
            if missing_person or missing_group:
                self.repository.block(ref, reason='SCOPE_UNAVAILABLE')
                continue
            if ref.kind == 'EVENT':
                event = store.get_event(ref.id)
                channel = direct_channel(item['character_id'], item['conversation_id'])
                latest = self.scheduler._latest_direct_user_id(store, item['character_id'], item['conversation_id'])
            else:
                repo = GroupRepository(store)
                event = repo.get_event(ref.conversation_id, ref.id)
                channel = group_channel(ref.conversation_id)
                latest = repo.latest_user_event_id_by_insertion(ref.conversation_id)
            if latest is not None and latest > ref.id:
                self.repository.discard_stale(ref)
                continue
            if event is None:
                batch = self.repository.claim(ref, lease_seconds=120)
                if batch is not None:
                    self.repository.finish(batch, 'BLOCKED', error_kind='SOURCE_MISSING')
                continue
            if not self.scheduler.prepare_recovery(channel, event.id):
                continue
            if ref.kind == 'EVENT':
                self.scheduler.enqueue_direct(item['character_id'], item['conversation_id'], event, only_if_idle=True)
            else:
                self.scheduler.enqueue_group(ref.conversation_id, event, only_if_idle=True)

    def begin(self, event, *, kind, bundle, service=None, max_speakers=None):
        ref = FactRef(kind=kind, id=event.id,
            conversation_id=event.conversation_id if kind == 'CONVERSATION_EVENT' else None)
        if ((service is None and event.character_id not in bundle.runtimes)
                or (service is not None and service.repo.get_group(ref.conversation_id) is None)):
            self.repository.block(ref, reason='SCOPE_UNAVAILABLE')
            raise PermissionError('delivery scope is unavailable')
        try:
            batch = self.repository.claim(ref,
                lease_seconds=getattr(bundle.settings, 'reaction_preparation_lease_seconds', 120.0),
                plan_factory=(lambda mentions: service.plan_reaction(event, mention_order=mentions,
                    max_speakers=max_speakers)) if service else None)
        except KeyError:
            self.repository.block(ref, reason='SCOPE_UNAVAILABLE')
            raise
        if batch is None:
            return None, None
        try:
            expected_owner = ('group:' + ref.conversation_id) if service else event.character_id
            urls = []
            for media_id in batch.snapshot['media_ids']:
                asset = bundle.store.get_media_asset(media_id)
                if asset is None or asset.character_id != expected_owner:
                    raise FileNotFoundError('delivery media is missing or outside its source scope')
                urls.append(self.media_storage.data_url(asset))
            return batch, urls
        except Exception as exc:
            self.repository.finish(batch, 'BLOCKED', error_kind=type(exc).__name__)
            raise

    def guard(self, batch, character_id=None, *, conversation_id=None):
        if batch is None:
            return True
        if self._stop.is_set():
            raise DeliveryStopping('reaction delivery is stopping')
        if not self.repository.owns(batch):
            return False
        if character_id is not None:
            profile = next((p for p in self.profiles() if p['id'] == character_id), None)
            if profile is None or 'archived_at' in profile:
                raise PermissionError('delivery character is unavailable')
            if conversation_id is not None:
                store = self.repository.store
                with store._lock:
                    allowed = store.conn.execute("SELECT 1 FROM conversations g JOIN conversation_members m "
                        "ON m.conversation_id=g.id WHERE g.id=? AND g.archived_at IS NULL "
                        "AND m.actor_type='CHARACTER' AND m.actor_id=?", (conversation_id, character_id)).fetchone()
                if allowed is None:
                    raise PermissionError('delivery group membership changed')
        return True

    def finish(self, batch, *, superseded=False, error=None):
        if batch is None:
            return
        if superseded and not self._stop.is_set():
            self.repository.finish(batch, 'SUPERSEDED')
            return
        try:
            expected = (batch.snapshot['plan']['speaker_order'] if batch.snapshot['plan'] is not None
                else [batch.snapshot['character_id']])
            if not isinstance(expected, list) or not all(isinstance(p, str) and p for p in expected):
                raise ValueError("invalid delivery member plan")
        except (KeyError, TypeError, ValueError):
            self.repository.finish(batch, 'BLOCKED', error_kind='INVALID_SNAPSHOT')
            return
        store = self.repository.store
        with store._lock:
            exists = store.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_reaction_receipts'").fetchone()
            rows = store.conn.execute(
                'SELECT character_id,state,preparation_deadline FROM conversation_reaction_receipts WHERE source_ref_json=? AND reaction_version=?',
                (batch.ref.model_dump_json(), '1')).fetchall() if exists else []
            states = {row['character_id']: row['state'] for row in rows}
            preparation = [row['preparation_deadline'] for row in rows
                if row['character_id'] in expected and row['state'] == 'PREPARING']
        if any(states.get(person) in {'CALLING', 'UNKNOWN'} for person in expected):
            state = 'UNKNOWN'
        elif error is None and expected and all(states.get(person) == 'COMMITTED' for person in expected):
            state = 'COGNITION_COMMITTED'
        elif preparation and all(value is not None for value in preparation):
            self.repository.defer(batch, max(preparation))
            return
        elif self._stop.is_set() and all(value in {'COMMITTED', 'FAILED_BEFORE_CALL'} for value in states.values()):
            self.repository.defer(batch, self.repository._now())
            return
        else:
            state = 'BLOCKED'
        self.repository.finish(batch, state, error_kind=type(error).__name__ if error else None)
