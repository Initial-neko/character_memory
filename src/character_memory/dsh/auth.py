"""Ed25519 request-signature primitives for the DSH surface.

The signature binds method, path+query, body and grant. It is verified against
the public key registered for ``(subject_id, key_id)``; nothing here trusts a
caller-supplied identity.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from character_memory.dsh.constants import DshError

SUBJECT_PATTERN = re.compile(r"^dsh_[0-9a-f]{32}$")
KEY_PATTERN = re.compile(r"^key_[0-9a-f]{16}$")
NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


@dataclass(frozen=True)
class AuthHeaders:
    subject_id: str
    key_id: str
    timestamp: datetime
    timestamp_raw: str
    nonce: str
    signature: bytes


def decode_base64url(value: str, *, expected_bytes: int | None = None) -> bytes:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty base64url value")
    padding = "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(raw + padding)
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise ValueError("unexpected decoded length")
    return decoded


def parse_auth_headers(headers, *, grant_id: str) -> AuthHeaders:
    """Parse and shape-check the five authentication headers.

    A malformed header is always ``DSH_AUTHENTICATION_FAILED``; the route has
    not authenticated anything yet, so no finer distinction is exposed.
    """

    subject_id = str(headers.get("X-DSH-Subject") or "").strip()
    key_id = str(headers.get("X-DSH-Key") or "").strip()
    timestamp_raw = str(headers.get("X-DSH-Timestamp") or "").strip()
    nonce = str(headers.get("X-DSH-Nonce") or "").strip()
    signature_raw = str(headers.get("X-DSH-Signature") or "").strip()
    if not (subject_id and key_id and timestamp_raw and nonce and signature_raw):
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "missing authentication header")
    if not SUBJECT_PATTERN.match(subject_id):
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid subject header")
    if not KEY_PATTERN.match(key_id):
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid key header")
    if not NONCE_PATTERN.match(nonce):
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid nonce")
    if not SIGNATURE_PATTERN.match(signature_raw):
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid signature encoding")
    try:
        timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "timestamp must be UTC")
    if timestamp.utcoffset().total_seconds() != 0:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "timestamp must be UTC")
    try:
        signature = decode_base64url(signature_raw)
    except (ValueError, TypeError) as exc:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid signature encoding") from exc
    if len(signature) != 64:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid signature length")
    return AuthHeaders(
        subject_id=subject_id,
        key_id=key_id,
        timestamp=timestamp.astimezone(timezone.utc),
        timestamp_raw=timestamp_raw,
        nonce=nonce,
        signature=signature,
    )


def signature_message(
    *, method: str, path_with_query: str, body: bytes,
    timestamp: str, nonce: str, grant_id: str,
) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    lines = [
        "DSH1",
        str(method).upper(),
        str(path_with_query),
        body_hash,
        str(timestamp),
        str(nonce),
        str(grant_id or ""),
    ]
    return "\n".join(lines).encode("utf-8")


def verify_signature(public_key_b64: str, signature: bytes, message: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(public_key_b64, expected_bytes=32))
    except (ValueError, TypeError) as exc:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "invalid stored public key") from exc
    try:
        public_key.verify(signature, message)
    except InvalidSignature as exc:
        raise DshError(401, "DSH_AUTHENTICATION_FAILED", "signature mismatch") from exc


def validate_public_key(value: str) -> str:
    """Return the canonical base64url form, or raise ``ValueError``."""
    raw = str(value or "").strip()
    decoded = decode_base64url(raw, expected_bytes=32)
    if not any(decoded):
        raise ValueError("all-zero public key is not allowed")
    # Re-encode without padding so the stored form is canonical.
    return base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")


def encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
