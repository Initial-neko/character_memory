"""Operator-only DSH admin surface.

This module is attached to the operator/Main listener, never to the independent
DSH listener. Authentication is a dedicated operator token; loopback is not
trusted because Tailscale Serve presents remote requests as loopback.
"""
from __future__ import annotations

import hmac
import json
import os
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from character_memory.dsh.constants import DshError
from character_memory.dsh.service import DshService

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
SUBJECT_ID = re.compile(r"^dsh_[0-9a-f]{32}$")
TOKEN_ENV = "CHARACTER_MEMORY_DSH_OPERATOR_TOKEN"


def _http_error(exc: DshError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={"code": exc.code, "message": exc.message},
    )


def _operator_token() -> str:
    return str(os.getenv(TOKEN_ENV) or "")


def _require_operator(request: Request, service: DshService, *, action: str) -> None:
    expected = _operator_token()
    provided = str(request.headers.get("X-DSH-Operator-Token") or "")
    allowed = bool(expected) and hmac.compare_digest(provided, expected)
    if allowed:
        return
    try:
        service.dsh.audit_in_new_transaction(
            event_type="ADMIN_DENY", outcome="DENY",
            detail={"reason": "operator token missing or invalid",
                    "action": action})
    except DshError:
        raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed")
    raise DshError(401, "DSH_OPERATOR_AUTH_REQUIRED",
                   "operator token required")


async def _json_object(request: Request) -> dict:
    try:
        payload = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise DshError(422, "DSH_SCHEMA_INVALID", "body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise DshError(422, "DSH_SCHEMA_INVALID", "body must be a JSON object")
    return payload


def _fields(payload: dict, *, required: set[str], optional: set[str] = frozenset()) -> None:
    unknown = sorted(set(payload) - required - set(optional))
    if unknown:
        raise DshError(422, "DSH_SCHEMA_INVALID", "unknown field")
    missing = sorted(required - set(payload))
    if missing:
        raise DshError(422, "DSH_SCHEMA_INVALID", "missing required field")


def _request_id(payload: dict) -> str:
    value = str(payload.get("request_id") or "")
    if not REQUEST_ID.match(value):
        raise DshError(422, "DSH_SCHEMA_INVALID",
                       "request_id must be 8-80 characters of [A-Za-z0-9_-]")
    return value


def _subject_id(value) -> str:
    subject_id = str(value or "")
    if not SUBJECT_ID.match(subject_id):
        raise DshError(422, "DSH_SCHEMA_INVALID", "invalid subject_id")
    return subject_id


def _audit_admin_read(service: DshService, *, subject_id: str | None,
                      grant_id: str | None, resource: str) -> None:
    try:
        service.dsh.audit_in_new_transaction(
            event_type="READ", outcome="OK", subject_id=subject_id,
            grant_id=grant_id, resource_type=resource.upper(),
            resource_id=str(grant_id or subject_id or ""),
            detail={"admin": True, "resource": resource})
    except DshError:
        raise DshError(503, "DSH_AUDIT_UNAVAILABLE", "audit append failed")


def attach_dsh_admin_routes(app, service: DshService) -> None:
    @app.exception_handler(DshError)
    async def _dsh_error_handler(_request: Request, exc: DshError):
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.post("/v2/dsh/admin/subjects", status_code=201)
    async def register_subject(request: Request):
        _require_operator(request, service, action="SUBJECT_REGISTER")
        try:
            payload = await _json_object(request)
            _fields(payload, required={"request_id", "display_name", "public_key"})
            return service.dsh.register_subject(
                request_id=_request_id(payload),
                display_name=payload.get("display_name"),
                public_key=payload.get("public_key"),
            )
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.post("/v2/dsh/admin/subjects/{subject_id}/keys", status_code=201)
    async def add_key(subject_id: str, request: Request):
        _require_operator(request, service, action="KEY_ADD")
        try:
            payload = await _json_object(request)
            _fields(payload, required={"request_id", "public_key"})
            return service.dsh.add_key(
                subject_id=_subject_id(subject_id),
                request_id=_request_id(payload),
                public_key=payload.get("public_key"),
            )
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.post("/v2/dsh/admin/subjects/{subject_id}/keys/{key_id}/revoke")
    async def revoke_key(subject_id: str, key_id: str, request: Request):
        _require_operator(request, service, action="KEY_REVOKE")
        try:
            payload = await _json_object(request)
            _fields(payload, required={"request_id"})
            return service.dsh.revoke_key(
                subject_id=_subject_id(subject_id),
                key_id=key_id,
                request_id=_request_id(payload),
            )
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.post("/v2/dsh/admin/subjects/{subject_id}/revoke")
    async def revoke_subject(subject_id: str, request: Request):
        _require_operator(request, service, action="SUBJECT_REVOKE")
        try:
            payload = await _json_object(request)
            _fields(payload, required={"request_id"})
            return service.dsh.revoke_subject(
                subject_id=_subject_id(subject_id),
                request_id=_request_id(payload),
            )
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.post("/v2/dsh/admin/grants", status_code=201)
    async def create_grant(request: Request):
        _require_operator(request, service, action="GRANT_CREATE")
        try:
            payload = await _json_object(request)
            _fields(
                payload,
                required={"request_id", "subject_id", "activity_id", "scopes",
                          "expires_at"},
                optional={"actions", "public_facts", "max_reads", "max_actions"},
            )
            spec = {
                key: payload[key]
                for key in ("scopes", "actions", "public_facts", "expires_at",
                            "max_reads", "max_actions")
                if key in payload
            }
            return service.dsh.create_grant(
                request_id=_request_id(payload),
                subject_id=_subject_id(payload.get("subject_id")),
                activity_id=str(payload.get("activity_id") or ""),
                spec=spec,
            )
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.post("/v2/dsh/admin/grants/{grant_id}/revoke")
    async def revoke_grant(grant_id: str, request: Request):
        _require_operator(request, service, action="GRANT_REVOKE")
        try:
            payload = await _json_object(request)
            _fields(payload, required={"request_id"})
            return service.dsh.revoke_grant(
                grant_id=grant_id, request_id=_request_id(payload))
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.get("/v2/dsh/admin/audit")
    def query_audit(request: Request):
        _require_operator(request, service, action="AUDIT_READ")
        try:
            params = request.query_params
            try:
                after_id = int(params.get("after_id", "0"))
                limit = int(params.get("limit", "50"))
            except (TypeError, ValueError) as exc:
                raise DshError(422, "DSH_SCHEMA_INVALID",
                               "after_id and limit must be integers") from exc
            subject_id = params.get("subject_id") or None
            if subject_id is not None:
                subject_id = _subject_id(subject_id)
            events = service.dsh.query_audit(
                subject_id=subject_id, grant_id=params.get("grant_id") or None,
                after_id=after_id, limit=limit)
            _audit_admin_read(service, subject_id=subject_id,
                              grant_id=params.get("grant_id") or None,
                              resource="AUDIT")
            return {"events": events}
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.get("/v2/dsh/admin/audit/verify")
    def verify_audit(request: Request):
        _require_operator(request, service, action="AUDIT_VERIFY")
        try:
            result = service.dsh.verify_audit()
            _audit_admin_read(service, subject_id=None, grant_id=None,
                              resource="AUDIT_VERIFY")
            return result
        except DshError as exc:
            raise _http_error(exc) from exc

    @app.get("/v2/dsh/admin/grants/{grant_id}/facts")
    def grant_facts(grant_id: str, request: Request):
        _require_operator(request, service, action="GRANT_FACTS")
        try:
            params = request.query_params
            try:
                after_seq = int(params.get("after_seq", "0"))
                limit = int(params.get("limit", "50"))
            except (TypeError, ValueError) as exc:
                raise DshError(422, "DSH_SCHEMA_INVALID",
                               "after_seq and limit must be integers") from exc
            result = service.admin_facts(
                grant_id, after_seq=after_seq, limit=limit)
            _audit_admin_read(service, subject_id=None, grant_id=grant_id,
                              resource="GRANT_FACTS")
            return result
        except DshError as exc:
            raise _http_error(exc) from exc
