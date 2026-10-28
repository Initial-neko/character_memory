#!/usr/bin/env python
"""Minimal external DSH client (P13).

It signs every business request with the Ed25519 key registered by the operator.
The private key is read from ``DSH_PRIVATE_KEY`` (base64url raw 32-byte seed) or
``--private-key``; it is never sent over the wire.

Examples::

    python scripts/dsh_client.py keygen
    DSH_PRIVATE_KEY=... python scripts/dsh_client.py self \
        --origin http://127.0.0.1:8010 --subject dsh_... --key-id key_...
    DSH_PRIVATE_KEY=... python scripts/dsh_client.py submit-turn \
        --origin http://127.0.0.1:8010 --subject dsh_... --key-id key_... \
        --grant grant_... --content "大家好"
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def signature_message(method: str, path_with_query: str, body: bytes,
                      timestamp: str, nonce: str, grant_id: str) -> bytes:
    return "\n".join([
        "DSH1",
        method.upper(),
        path_with_query,
        hashlib.sha256(body).hexdigest(),
        timestamp,
        nonce,
        grant_id,
    ]).encode("utf-8")


def signed_request(client: httpx.Client, *, method: str, path: str,
                   subject_id: str, key_id: str, private_key_b64: str,
                   grant_id: str = "", params: dict | None = None,
                   payload: dict | None = None) -> httpx.Response:
    query = urlencode(params or {})
    path_with_query = path + (f"?{query}" if query else "")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") \
        if payload is not None else b""
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    nonce = b64url_encode(os.urandom(18))
    private = Ed25519PrivateKey.from_private_bytes(b64url_decode(private_key_b64))
    signature = private.sign(signature_message(
        method, path_with_query, body, timestamp, nonce, grant_id))
    headers = {
        "X-DSH-Subject": subject_id,
        "X-DSH-Key": key_id,
        "X-DSH-Timestamp": timestamp,
        "X-DSH-Nonce": nonce,
        "X-DSH-Signature": b64url_encode(signature),
        "Content-Type": "application/json",
    }
    return client.request(method, path_with_query, content=body or None,
                          headers=headers)


def _client(origin: str) -> httpx.Client:
    return httpx.Client(base_url=origin.rstrip("/"), timeout=20)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signed P13 DSH client")
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="generate an Ed25519 key pair")

    def add_common(p):
        p.add_argument("--origin", required=True)
        p.add_argument("--subject", required=True)
        p.add_argument("--key-id", required=True)
        p.add_argument("--private-key", default=os.getenv("DSH_PRIVATE_KEY", ""))
        p.add_argument("--request-id", default="")
        p.add_argument("--grant", default="")

    self_cmd = sub.add_parser("self", help="GET /v2/dsh/self")
    add_common(self_cmd)
    activity = sub.add_parser("activity", help="read the grant's activity")
    add_common(activity)
    transcript = sub.add_parser("transcript", help="read granted activity facts")
    add_common(transcript)
    transcript.add_argument("--after-seq", type=int, default=0)
    transcript.add_argument("--limit", type=int, default=50)
    space_post = sub.add_parser("space-post", help="read one public Space post")
    add_common(space_post)
    space_post.add_argument("--post-id", type=int, required=True)
    turn = sub.add_parser("submit-turn", help="ACTIVITY_SUBMIT_TURN")
    add_common(turn)
    turn.add_argument("--content", required=True)
    turn.add_argument("--reply-to", type=int, default=None)
    leave = sub.add_parser("leave", help="ACTIVITY_LEAVE")
    add_common(leave)

    args = parser.parse_args(argv)

    if args.command == "keygen":
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes_raw()
        public = private.public_key().public_bytes_raw()
        print(json.dumps({"private_key": b64url_encode(seed),
                          "public_key": b64url_encode(public)}, indent=2))
        return 0

    if not args.private_key:
        print("missing Ed25519 private key (--private-key or DSH_PRIVATE_KEY)",
              file=sys.stderr)
        return 2
    request_id = args.request_id or ("req-" + b64url_encode(os.urandom(9)))

    with _client(args.origin) as client:
        common = dict(subject_id=args.subject, key_id=args.key_id,
                      private_key_b64=args.private_key)
        if args.command == "self":
            response = signed_request(
                client, method="GET", path="/v2/dsh/self", params={"request_id": request_id},
                grant_id="", **common)
        elif args.command == "activity":
            response = signed_request(
                client, method="GET", path=f"/v2/dsh/grants/{args.grant}/activity",
                params={"request_id": request_id}, grant_id=args.grant, **common)
        elif args.command == "transcript":
            response = signed_request(
                client, method="GET", path=f"/v2/dsh/grants/{args.grant}/transcript",
                params={"request_id": request_id, "after_seq": args.after_seq,
                        "limit": args.limit},
                grant_id=args.grant, **common)
        elif args.command == "space-post":
            response = signed_request(
                client, method="GET",
                path=f"/v2/dsh/grants/{args.grant}/facts/space_post/{args.post_id}",
                params={"request_id": request_id}, grant_id=args.grant, **common)
        elif args.command == "submit-turn":
            payload = {"type": "ACTIVITY_SUBMIT_TURN", "request_id": request_id,
                       "content": args.content}
            if args.reply_to is not None:
                payload["reply_to_fact_id"] = args.reply_to
            response = signed_request(
                client, method="POST", path=f"/v2/dsh/grants/{args.grant}/actions",
                payload=payload, grant_id=args.grant, **common)
        else:
            response = signed_request(
                client, method="POST", path=f"/v2/dsh/grants/{args.grant}/actions",
                payload={"type": "ACTIVITY_LEAVE", "request_id": request_id},
                grant_id=args.grant, **common)

    print(json.dumps({"status": response.status_code,
                      "body": response.json()}, ensure_ascii=False, indent=2))
    return 0 if response.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
