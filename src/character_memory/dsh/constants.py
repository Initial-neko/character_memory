"""Frozen P13 vocabulary: scopes, actions, error codes and identity fields."""

from __future__ import annotations

# Read scopes (closed set). ``ACTIVITY_PARTICIPATE`` is write-only.
READ_SCOPES = frozenset({
    "ACTIVITY_READ_SELF",
    "ACTIVITY_READ_TRANSCRIPT",
    "SPACE_POST_READ",
})
PARTICIPATE_SCOPE = "ACTIVITY_PARTICIPATE"
ALLOWED_SCOPES = READ_SCOPES | {PARTICIPATE_SCOPE}

# Action names are a closed set and must be derived from the grant's scopes.
ACTION_SUBMIT_TURN = "ACTIVITY_SUBMIT_TURN"
ACTION_LEAVE = "ACTIVITY_LEAVE"
ALLOWED_ACTIONS = frozenset({ACTION_SUBMIT_TURN, ACTION_LEAVE})
ACTION_SCOPE = {ACTION_SUBMIT_TURN: PARTICIPATE_SCOPE, ACTION_LEAVE: PARTICIPATE_SCOPE}

# Explicitly non-grantable names from contract section 2.3. Anything else that
# is merely unknown gets DSH_SCOPE_UNKNOWN instead.
NON_GRANTABLE_SCOPES = frozenset({
    "DIRECT_READ",
    "DIRECT_WRITE",
    "GROUP_READ",
    "GROUP_WRITE",
    "CONVERSATION_EVENTS_READ",
    "MEMORY_READ",
    "MEMORY_WRITE",
    "MENTAL_STATE_READ",
    "MENTAL_STATE_WRITE",
    "PERSONA_READ",
    "PERSONA_WRITE",
    "INTENT_READ",
    "INTENT_WRITE",
    "GOAL_READ",
    "GOAL_WRITE",
    "PROFILE_READ",
    "PROFILE_WRITE",
    "SPACE_WRITE",
    "SPACE_COMMENT_READ",
    "SPACE_MEDIA_READ",
    "NOTIFICATION_READ",
    "AUDIT_READ",
    "CHARACTER_IMPERSONATE",
    "MODEL_INVOKE",
    "SQL",
    "FILESYSTEM",
    "SHELL",
    "ENV_READ",
    "PROVIDER_KEY_READ",
    "BROWSER",
    "NETWORK",
})

# Request-body fields a DSH action may never supply.
FORBIDDEN_ACTION_FIELDS = frozenset({
    "character_id",
    "user_id",
    "actor_id",
    "actor_type",
    "subject_id",
    "grant_id",
    "experience_scope",
    "reality",
    "scope",
    "expires_at",
    "memory_id",
    "persona",
    "sql",
})

PUBLIC_FACT_KINDS = frozenset({"SPACE_POST"})
MAX_PUBLIC_FACTS = 20
MAX_GRANT_TTL_SECONDS = 7 * 24 * 60 * 60
CLOCK_SKEW_SECONDS = 300
NONCE_TTL_SECONDS = 600
MAX_ACTIVE_KEYS = 2
MAX_READS_LIMIT = 1000
MAX_ACTIONS_LIMIT = 100

AUDIT_GENESIS_HASH = "0" * 64


class DshError(Exception):
    """One frozen error code; the web layer turns this into ``detail``."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or code
        # Set once a terminal audit row has been written for this failure, so
        # a route or nested layer cannot accidentally record it twice.
        self.audited = False
