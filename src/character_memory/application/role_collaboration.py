"""Role contact over existing shared facts; no Direct transport or private messenger."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging

from character_memory.domain.models import Event, EventType
from character_memory.group_store import GroupEvent
from character_memory.runtime.capability_execution import CapabilityRequest


@dataclass(frozen=True)
class CollaborationContext:
    sender_id: str
    channel: str
    channel_id: str
    opportunity_id: str
    source_event_id: int | None
    now: datetime


class RoleCollaborationService:
    """Composition supplies trusted identity, policy and the recipient's normal decision.

    research executes an existing CapabilityRequest through the shared executor;
    reply_decider may decline (None), disagree or interpret returned research.
    No model-generated argument can change execution grants or server identity.
    """
    def __init__(self, store, groups, space, *, profiles, reply_decider,
                 research=None, research_policy=None, publish_group_event=None):
        self.store, self.groups, self.space = store, groups, space
        self.profiles, self.reply_decider = profiles, reply_decider
        self.research, self.research_policy = research, research_policy
        self.publish_group_event = publish_group_event
        store.apply_schema_migration('collaboration/001-contact-receipts', self._create_schema, immediate=True)

    def _create_schema(self):
        self.store.conn.execute('CREATE TABLE IF NOT EXISTS role_collaboration_receipts ('
            'request_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, receipt_json TEXT NOT NULL)')

    def available_channels(self, sender_id, recipient_id):
        ids = {p['id'] for p in self.profiles() if 'archived_at' not in p}
        if sender_id == recipient_id or not {sender_id, recipient_id} <= ids:
            return []
        channels = [{'channel': 'GROUP', 'channel_id': g.id} for g in self.groups.list_groups()
                if {sender_id, recipient_id} <= set(g.member_ids)]
        if self.space:
            channels.extend({'channel':'SPACE', 'channel_id':str(p.id)}
                for p in self.space.list_posts(limit=30)[0] if p.visibility == 'ACTIVE_CHARACTERS')
        return channels

    def eligible_recipients(self, sender_id):
        return [{'recipient_id':p['id'], 'channels':self.available_channels(sender_id,p['id'])}
                for p in self.profiles() if p['id'] != sender_id and 'archived_at' not in p]

    def _validate(self, recipient, topic, c):
        ids = {p['id'] for p in self.profiles() if 'archived_at' not in p}
        if not {c.sender_id, recipient} <= ids or recipient == c.sender_id:
            return 'INVALID_PARTICIPANTS'
        if not isinstance(topic, str) or not topic.strip() or len(topic) > 240:
            return 'INVALID_TOPIC'
        if not c.opportunity_id or len(c.opportunity_id) > 128:
            return 'INVALID_OPPORTUNITY'
        if c.channel == 'GROUP':
            group = self.groups.get_group(c.channel_id)
            if group is None or not {c.sender_id, recipient} <= set(group.member_ids):
                return 'CHANNEL_MEMBERSHIP_DENIED'
            if c.source_event_id is not None:
                row = self.store.conn.execute('SELECT 1 FROM conversation_events WHERE id=? AND conversation_id=?',
                    (c.source_event_id, c.channel_id)).fetchone()
                if row is None:
                    return 'SOURCE_DENIED'
        elif c.channel == 'SPACE' and self.space:
            try:
                post = self.space.get_post(int(c.channel_id))
            except (ValueError, TypeError):
                return 'CHANNEL_DENIED'
            if post is None or post.visibility != 'ACTIVE_CHARACTERS':
                return 'CHANNEL_DENIED'
            # A referenced source is a real comment in this post. Never reply to
            # User: that would intentionally create a human inbox notification.
            if c.source_event_id is not None:
                source = self.space.get_comment(c.source_event_id)
                if source is None or source.post_id != post.id or source.actor_type != 'CHARACTER':
                    return 'SOURCE_DENIED'
        else:
            return 'CHANNEL_DENIED'
        return ''

    def _save(self, key, payload, receipt):
        self.store.conn.execute('INSERT OR REPLACE INTO role_collaboration_receipts VALUES(?,?,?)',
            (key, payload, json.dumps(receipt, ensure_ascii=False)))

    def _emit(self, actor, recipient, content, c, key, reply_to=None):
        # Caller holds the write transaction: validate immediately before fact insertion.
        active = {p['id'] for p in self.profiles() if 'archived_at' not in p}
        if not {actor, recipient} <= active:
            raise PermissionError('PERMISSION_CHANGED')
        if c.channel == 'SPACE':
            post = self.space.get_post(int(c.channel_id))
            if post is None or post.visibility != 'ACTIVE_CHARACTERS':
                raise PermissionError('PERMISSION_CHANGED')
        if c.channel == 'GROUP':
            # Membership is rechecked inside the same transaction as the fact.
            group = self.groups.get_group(c.channel_id)
            if group is None or not {actor, recipient} <= set(group.member_ids):
                raise PermissionError('PERMISSION_CHANGED')
            fact = self.groups.append_event(GroupEvent(conversation_id=c.channel_id,
                turn_id=key, actor_type='CHARACTER', actor_id=actor,
                event_type='CHARACTER_MESSAGE', event_time=c.now, content=content,
                metadata={'action':'MESSAGE', 'collaboration_request_id':key,
                    'recipient_id':recipient, 'source_event_id':c.source_event_id,
                    'opportunity_id':c.opportunity_id, 'reply_to_event_id':reply_to}))
        else:
            fact = self.space.add_comment(int(c.channel_id), actor, content, c.now,
                reply_to_comment_id=reply_to, client_request_id=hashlib.sha256((key+actor).encode()).hexdigest())
        self._project_participants(actor, recipient, content, c, key, fact.id, reply_to)
        return fact.id

    def _project_participants(self, actor, recipient, content, c, key, fact_id, reply_to):
        # A channel fact is stored once by its owner. These local ACTION facts
        # record only each participant's real sending/receiving experience; they
        # are neither Direct chat messages nor inferred Memory/Web observations.
        metadata = {'channel':c.channel, 'channel_id':c.channel_id,
            'fact_kind':'ROLE_COLLABORATION_MESSAGE', 'shared_fact_id':fact_id,
            'actor_id':actor, 'recipient_id':recipient,
            'participants':sorted([actor, recipient]), 'collaboration_request_id':key,
            'opportunity_id':c.opportunity_id, 'source_event_id':c.source_event_id,
            'reply_to_shared_fact_id':reply_to}
        if c.channel == 'GROUP':
            metadata.update(conversation_id=c.channel_id, group_event_id=fact_id,
                            source_conversation_event_id=fact_id)
        else:
            metadata.update(conversation_id=f'space:{c.channel_id}',
                            post_id=int(c.channel_id), comment_id=fact_id, commenter_id=actor)
        for person_id, direction in ((actor,'SENT'), (recipient,'RECEIVED')):
            self.store.append_event(Event(character_id=person_id, event_type=EventType.ACTION,
                event_time=c.now, content=f'{actor} → {recipient}: {content}',
                metadata={**metadata, 'direction':direction}))

    def _publish_group(self, c, key, fact_id):
        # Invoke only after the fact/projections/claim transaction has committed.
        # SSE is ephemeral; failed delivery is reconciled by shared history, never
        # by replaying a durable send or a completed contact on cache hits.
        if c.channel != 'GROUP' or self.publish_group_event is None:
            return
        try:
            fact = next(f for f in self.groups.list_turn_events(c.channel_id, key) if f.id == fact_id)
            self.publish_group_event(fact.model_dump(mode='json'))
        except Exception:
            logging.getLogger(__name__).warning('collaboration.group_publication_failed request=%s event=%s', key, fact_id)

    def contact(self, recipient_id, topic, *, context):
        c = context
        denial = self._validate(recipient_id, topic, c)
        if denial:
            return {'status':'DENIED', 'reason':denial}
        key = hashlib.sha256(json.dumps([c.sender_id,c.opportunity_id,c.channel,c.channel_id,recipient_id]).encode()).hexdigest()
        payload = json.dumps([topic,c.source_event_id], ensure_ascii=False)
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute('SELECT payload_json,receipt_json FROM role_collaboration_receipts WHERE request_id=?', (key,)).fetchone()
            if row:
                if row['payload_json'] != payload:
                    return {'status':'DENIED', 'reason':'IDEMPOTENCY_CONFLICT'}
                existing = json.loads(row['receipt_json'])
                if existing['status'] == 'RUNNING':
                    return {**existing, 'status':'UNKNOWN', 'reason':'IN_FLIGHT_OR_INTERRUPTED_NO_REPLAY'}
                return existing
            receipt = dict(request_id=key, status='RUNNING', sender_id=c.sender_id,
                recipient_id=recipient_id, channel=c.channel, channel_id=c.channel_id,
                source_event_id=c.source_event_id, opportunity_id=c.opportunity_id,
                sent_event_id=self._emit(c.sender_id,recipient_id,topic,c,key,c.source_event_id))
            self._save(key,payload,receipt)
        self._publish_group(c, key, receipt['sent_event_id'])
        try:
            result = None
            choice = self.reply_decider(recipient_id, topic, None, c)
            query = choice.research_query if isinstance(choice, CollaborationReply) else None
            bounds = self.research_policy(recipient_id) if self.research_policy else None
            if query and bounds and self.research:
                if not isinstance(query, str) or not query.strip() or len(query)>240:
                    raise ValueError("INVALID_RESEARCH_QUERY")
                request = CapabilityRequest(key+':research', recipient_id, c.opportunity_id,
                    receipt['sent_event_id'], 'WORLD', 'WEB_SEARCH', {'query':query}, dict(bounds))
                result = self.research(request)
                receipt['research'] = {'request_id':result.request_id, 'status':result.status,
                    'data':result.data, 'reason':result.reason}
            if result is not None:
                choice = self.reply_decider(recipient_id, topic, result, c)
            reply = choice.message if isinstance(choice, CollaborationReply) else choice
            if reply is not None and (not isinstance(reply,str) or len(reply)>6000):
                raise ValueError('INVALID_REPLY')
            with self.store.transaction(immediate=True):
                if reply and reply.strip():
                    receipt['reply_event_id'] = self._emit(recipient_id,c.sender_id,reply,c,key,receipt['sent_event_id'])
                receipt['status'] = 'COMPLETED' if reply and reply.strip() else 'SILENT'
                self._save(key,payload,receipt)
            if receipt.get('reply_event_id') is not None:
                self._publish_group(c, key, receipt['reply_event_id'])
        except Exception as exc:
            receipt.update(status='FAILED',reason='PERMISSION_CHANGED' if isinstance(exc, PermissionError) else type(exc).__name__)
            with self.store.transaction(immediate=True):
                self._save(key,payload,receipt)
        return receipt

@dataclass(frozen=True)
class CollaborationReply:
    message: str = ''
    research_query: str | None = None


@dataclass(frozen=True)
class IssueWriteGrant:
    operator_id: str
    repositories: tuple[str, ...]
    max_issues: int
    character_id: str


class GitHubIssueAdapter:
    """A separately granted writer. transport is an operator-owned bounded HTTP adapter.

    No shell is executed. Unknown external outcomes remain unknown forever until
    an operator reconciles them; repeated calls cannot blindly write again.
    """
    def __init__(self, store, transport, *, timeout_seconds=20):
        self.store, self.transport = store, transport
        if not 1 <= timeout_seconds <= 60:
            raise ValueError('INVALID_TIMEOUT')
        self.timeout_seconds = timeout_seconds
        store.apply_schema_migration('collaboration/002-issue-write-receipts', self._create_schema, immediate=True)

    def _create_schema(self):
        self.store.conn.execute('CREATE TABLE IF NOT EXISTS collaboration_issue_writes ('
            'request_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, payload_json TEXT NOT NULL, receipt_json TEXT NOT NULL)')

    def create(self, *, character_id, request_id, repository, title, body, grant=None):
        import re
        from urllib.parse import urlparse
        draft = {'status':'DRAFT', 'repository':repository, 'title':title, 'body':body}
        if grant is None:
            return draft
        if (not isinstance(grant, IssueWriteGrant) or not grant.operator_id.strip()
                or grant.character_id != character_id or repository not in grant.repositories
                or type(grant.max_issues) is not int or grant.max_issues < 1
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)):
            return {'status':'DENIED', 'reason':'GRANT_DENIED'}
        if not request_id or not title.strip() or len(title)>240 or len(body)>20000:
            return {'status':'DENIED', 'reason':'INVALID_REQUEST'}
        payload = json.dumps([character_id,repository,title,body],ensure_ascii=False)
        with self.store.transaction(immediate=True):
            row = self.store.conn.execute('SELECT * FROM collaboration_issue_writes WHERE request_id=?',(request_id,)).fetchone()
            if row:
                if row['payload_json'] != payload:
                    return {'status':'DENIED','reason':'IDEMPOTENCY_CONFLICT'}
                existing = json.loads(row['receipt_json'])
                return {**existing,'status':'UNKNOWN'} if existing['status']=='RUNNING' else existing
            count = self.store.conn.execute('SELECT COUNT(*) FROM collaboration_issue_writes WHERE character_id=?',(character_id,)).fetchone()[0]
            if count >= grant.max_issues:
                return {'status':'DENIED','reason':'QUOTA_EXHAUSTED'}
            self.store.conn.execute('INSERT INTO collaboration_issue_writes VALUES(?,?,?,?)',
                (request_id,character_id,payload,json.dumps({'status':'RUNNING','request_id':request_id})))
        try:
            result = self.transport(repository=repository,title=title,body=body,
                timeout_seconds=self.timeout_seconds,idempotency_key=request_id)
            url = result.get('html_url','')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment or not re.fullmatch('/'+re.escape(repository)+r'/issues/[1-9][0-9]*',parsed.path):
                raise ValueError('UNVERIFIED_ISSUE_URL')
            receipt = {'status':'CREATED','request_id':request_id,'url':url,
                'operator_id':grant.operator_id,'character_id':character_id,'repository':repository}
        except Exception as exc:
            receipt = {'status':'UNKNOWN','request_id':request_id,'reason':type(exc).__name__,
                'operator_id':grant.operator_id,'character_id':character_id,'repository':repository}
        with self.store.transaction(immediate=True):
            self.store.conn.execute('UPDATE collaboration_issue_writes SET receipt_json=? WHERE request_id=?',
                (json.dumps(receipt),request_id))
        return receipt

