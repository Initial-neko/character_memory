"""Trusted production composition for World continuation extensions.

The model selects only current argument references. Identity, grants, scopes,
channel IDs and credentials are resolved again on the server before dispatch.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import yaml

from character_memory.character_capabilities import read_role_capabilities
from character_memory.runtime.capability_discovery import CapabilityDescription
from character_memory.runtime.capability_execution import CapabilityAdapter, CapabilityResult


def _permissions(access, character_id):
    profile = next((p for p in access.character_profiles() if p.get('id') == character_id and 'archived_at' not in p), None)
    try:
        permissions = read_role_capabilities(profile)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return frozenset()
    return frozenset(permissions.capabilities) if permissions else frozenset()


def _schema(properties):
    return {'type':'object', 'additionalProperties':False, 'properties':properties, 'required':list(properties)}


def build_world_continuation_tools(access, character_id, *, collaboration=None):
    """No network calls. Optional extensions are backed by existing services."""
    permissions = _permissions(access, character_id)
    descriptions, adapters = [], {}
    if 'role_contact' in permissions and collaboration is not None:
        from character_memory.application.role_collaboration import CollaborationContext
        def mapping():
            result = {}
            for person in collaboration.eligible_recipients(character_id)[:32]:
                for channel in person['channels'][:8]:
                    target = {'recipient_id':person['recipient_id'], **channel}
                    ref = hashlib.sha256(json.dumps(target, sort_keys=True).encode('utf-8')).hexdigest()[:24]
                    result[ref] = target
            return result
        current = mapping()
        if current:
            def validate_contact(request):
                if request.character_id != character_id or 'role_contact' not in _permissions(access, character_id):
                    return 'ROLE_CAPABILITY_REVOKED'
                args = request.arguments
                if set(args) != {'recipient_id', 'channel_ref', 'topic'} or any(not isinstance(v, str) for v in args.values()):
                    return 'INVALID_ARGUMENTS'
                target = mapping().get(args['channel_ref'])
                if target is None or target['recipient_id'] != args['recipient_id'] or not args['topic'].strip() or len(args['topic']) > 240:
                    return 'CHANNEL_OR_TOPIC_DENIED'
                return ''
            def contact(request):
                denial = validate_contact(request)
                if denial:
                    return CapabilityResult(request.request_id, 'DENIED', reason=denial)
                target = mapping()[request.arguments['channel_ref']]
                receipt = collaboration.contact(target['recipient_id'], request.arguments['topic'], context=CollaborationContext(
                    sender_id=character_id, channel=target['channel'], channel_id=target['channel_id'],
                    opportunity_id=request.opportunity_id, source_event_id=None, now=datetime.now().astimezone()))
                # Return only actual shared reply facts visible to the sender.
                if receipt.get('reply_event_id') and request.arguments['channel_ref'] in mapping():
                    if target['channel'] == 'GROUP':
                        facts = collaboration.groups.list_events(target['channel_id'], limit=16, character_id=character_id)
                    else:
                        facts = collaboration.space.list_comments(int(target['channel_id']))[-16:]
                    reply = next((fact for fact in facts if fact.id == receipt['reply_event_id']), None)
                    if reply is not None:
                        receipt['shared_reply'] = {'id':reply.id, 'content':reply.content[:6000]}
                data = {'contact':receipt, 'content_scope':'SHARED_CHANNEL_CONTACT',
                        'nested_call_bounds':{'recipient_decisions':2,'recipient_research':1,'separate_from_outer_budget':True}}
                status = 'SUCCESS' if receipt['status'] in ('COMPLETED', 'SILENT') else ('SKIPPED' if receipt['status'] == 'UNKNOWN' else 'FAILED')
                return CapabilityResult(request.request_id, status, data, receipt.get('reason', ''))
            descriptions.append(CapabilityDescription('ROLE_CONTACT', 'Optionally contact another role in a current shared channel; reply or silence is their own choice. channel_ref mapping: '+json.dumps(current, ensure_ascii=False),
                _schema({'recipient_id':{'type':'string','enum':sorted({t['recipient_id'] for t in current.values()})}, 'channel_ref':{'type':'string','enum':list(current)}, 'topic':{'type':'string','minLength':1,'maxLength':240}}), 'WORLD', 'shared_channel', required_grant='role_contact'))
            adapters['ROLE_CONTACT'] = CapabilityAdapter(frozenset({'WORLD'}), validate_contact, contact)
    if 'github_issue_write' in permissions:
        arguments = _schema({'repository':{'type':'string','maxLength':200}, 'title':{'type':'string','minLength':1,'maxLength':240}, 'body':{'type':'string','maxLength':20000}})
        def validate_issue(request):
            if request.character_id != character_id or 'github_issue_write' not in _permissions(access, character_id):
                return 'ROLE_CAPABILITY_REVOKED'
            args = request.arguments
            if set(args) != {'repository','title','body'} or any(not isinstance(v, str) for v in args.values()):
                return 'INVALID_ARGUMENTS'
            if (re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args['repository']) is None
                    or len(args['repository']) > 200 or not args['title'].strip() or len(args['title']) > 240 or len(args['body']) > 20000):
                return 'INVALID_ARGUMENTS'
            return ''
        def draft(request):
            denial = validate_issue(request)
            return CapabilityResult(request.request_id, 'DENIED', reason=denial) if denial else CapabilityResult(request.request_id, 'SUCCESS', {'status':'DRAFT', **request.arguments, 'external_write':False})
        descriptions.append(CapabilityDescription('GITHUB_ISSUE_DRAFT', 'Prepare a local issue draft. This creates no remote issue and performs no external write.', arguments, 'WORLD', 'local', required_grant='github_issue_write'))
        adapters['GITHUB_ISSUE_DRAFT'] = CapabilityAdapter(frozenset({'WORLD'}), validate_issue, draft)
        def policy():
            settings = access.settings
            repositories = tuple(getattr(settings, 'github_issue_write_repositories', ()))
            operator = getattr(settings, 'github_issue_write_operator_id', '')
            maximum = getattr(settings, 'github_issue_write_max_issues', 0)
            token = getattr(settings, 'github_issue_token', '')
            if (not repositories or any(not isinstance(r, str) or re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', r) is None for r in repositories)
                    or not isinstance(operator, str) or not operator.strip() or not isinstance(token, str) or not token.strip()
                    or type(maximum) is not int or maximum <= 0):
                return None
            return repositories, operator, maximum, token
        if policy() is not None:
            def validate_create(request):
                denial = validate_issue(request)
                current = policy()
                if denial:
                    return denial
                return '' if current is not None and request.arguments['repository'] in current[0] else 'ISSUE_OPERATOR_SCOPE_DENIED'
            def create(request):
                denial = validate_create(request)
                if denial:
                    return CapabilityResult(request.request_id, 'DENIED', reason=denial)
                from character_memory.application.collaboration_runtime import GitHubIssueHTTPTransport
                from character_memory.application.role_collaboration import GitHubIssueAdapter, IssueWriteGrant
                repositories, operator, maximum, token = policy()
                adapter = GitHubIssueAdapter(access.store(), GitHubIssueHTTPTransport(token))
                receipt = adapter.create(character_id=character_id, request_id=request.request_id, **request.arguments,
                    grant=IssueWriteGrant(operator, repositories, maximum, character_id))
                status = 'SUCCESS' if receipt['status'] == 'CREATED' else ('SKIPPED' if receipt['status'] in ('RUNNING','UNKNOWN') else 'DENIED')
                return CapabilityResult(request.request_id, status, receipt, receipt.get('reason',''))
            descriptions.append(CapabilityDescription('GITHUB_ISSUE_CREATE', 'Create one real remote GitHub issue only within the explicit operator scope. Optional; drafts or silence remain valid.', arguments, 'WORLD', 'github', required_grant='github_issue_write'))
            adapters['GITHUB_ISSUE_CREATE'] = CapabilityAdapter(frozenset({'WORLD'}), validate_create, create)
    return tuple(descriptions), adapters



def prepare_world_continuation_tools(access):
    """Composition-time schema preparation; returned discovery remains read-only."""
    from character_memory.application.collaboration_runtime import build_role_collaboration
    from character_memory.application.role_collaboration import GitHubIssueAdapter
    collaboration = build_role_collaboration(access)
    GitHubIssueAdapter(access.store(), None)  # Prepare its durable ledger only.
    return lambda character_id: build_world_continuation_tools(access, character_id, collaboration=collaboration)
