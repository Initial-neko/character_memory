"""Independent DSH listener routes: business surface only.

Only the explicitly granted Activity reads/actions and ``/health`` are mounted
here. Admin routes, ``/v1/*``, ``/v2/activities``, static assets, ``/docs`` and
``/openapi.json`` are absent by construction, so a DSH client cannot reach the
operator or Main API surface from this listener.
"""
from __future__ import annotations

import json
import re

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from character_memory.dsh.constants import (
    ACTION_LEAVE,
    ALLOWED_ACTIONS,
    FORBIDDEN_ACTION_FIELDS,
    DshError,
)
from character_memory.dsh.service import DshService

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
ACTION_KEYS = frozenset({"type", "request_id", "content", "reply_to_fact_id"})


def dsh_http_error(exc: DshError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={"code": exc.code, "message": exc.message},
    )


def _bad_request(message: str) -> DshError:
    return DshError(422, "DSH_SCHEMA_INVALID", message)


def _read_failure(service: DshService, ctx, grant_id: str, request: Request,
                  exc: DshError) -> None:
    if exc.audited or exc.status >= 500:
        return
    raw = request.query_params.get("request_id", "")
    request_id = raw if REQUEST_ID.match(raw) else ""
    service._deny_read(ctx, grant_id, request_id, exc)


def _request_id(value) -> str:
    request_id = str(value or "")
    if not REQUEST_ID.match(request_id):
        raise _bad_request("request_id must be 8-80 characters of [A-Za-z0-9_-]")
    return request_id


async def _authenticate(request: Request, service: DshService, *, grant_id: str):
    body = await request.body()
    path_with_query = request.url.path
    if request.url.query:
        path_with_query += "?" + request.url.query
    context = service.dsh.authenticate(
        request.headers,
        method=request.method,
        path_with_query=path_with_query,
        body=body,
        grant_id=grant_id,
    )
    return context, body


def _parse_action(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise _bad_request("request body must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise _bad_request("request body must be a JSON object")
    forbidden = sorted(set(payload) & FORBIDDEN_ACTION_FIELDS)
    if forbidden:
        raise DshError(422, "DSH_IDENTITY_FIELD_FORBIDDEN",
                       "identity or authorization fields are server-derived")
    unknown = sorted(set(payload) - ACTION_KEYS)
    if unknown:
        raise _bad_request("unknown action field")
    action_type = payload.get("type")
    if action_type not in ALLOWED_ACTIONS:
        raise DshError(403, "DSH_ACTION_NOT_ALLOWED", "action is not in the closed set")
    request_id = _request_id(payload.get("request_id"))
    action = {
        "type": action_type,
        "request_id": request_id,
        "content": payload.get("content"),
        "reply_to_fact_id": payload.get("reply_to_fact_id"),
    }
    if action_type == ACTION_LEAVE:
        if "content" in payload or "reply_to_fact_id" in payload:
            raise _bad_request("ACTIVITY_LEAVE takes no content or reply target")
    else:
        content = action.get("content")
        if not isinstance(content, str) or len(content) > 4000 or not content.strip():
            raise _bad_request("content must be 1-4000 characters and not blank")
        reply = action.get("reply_to_fact_id")
        if reply is not None and (type(reply) is not int or reply < 1):
            raise _bad_request("reply_to_fact_id must be a positive integer")
        action["content"] = content.strip()
    return action


def attach_dsh_business_routes(app, service: DshService) -> None:
    @app.exception_handler(DshError)
    async def _dsh_error_handler(_request: Request, exc: DshError):
        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    def health():
        # This listener must never initialize the model bundle or Persona
        # runtime. The health payload deliberately contains no Main API state.
        return {"ok": True, "service": "dsh", "ready": True}

    @app.get("/v2/dsh/self")
    async def read_self(request: Request):
        ctx, _body = await _authenticate(request, service, grant_id="")
        try:
            request_id = _request_id(request.query_params.get("request_id"))
            return service.read_self(ctx, request_id)
        except DshError as exc:
            _read_failure(service, ctx, "", request, exc)
            raise dsh_http_error(exc) from exc

    @app.get("/v2/dsh/grants/{grant_id}/activity")
    async def read_activity(grant_id: str, request: Request):
        ctx, _body = await _authenticate(request, service, grant_id=grant_id)
        try:
            request_id = _request_id(request.query_params.get("request_id"))
            return service.read_activity(
                ctx, grant_id, request_id,
                requested_activity_id=request.query_params.get("activity_id"))
        except DshError as exc:
            _read_failure(service, ctx, grant_id, request, exc)
            raise dsh_http_error(exc) from exc

    @app.get("/v2/dsh/grants/{grant_id}/transcript")
    async def read_transcript(grant_id: str, request: Request):
        ctx, _body = await _authenticate(request, service, grant_id=grant_id)
        try:
            request_id = _request_id(request.query_params.get("request_id"))
            after_seq_raw = request.query_params.get("after_seq", "0")
            limit_raw = request.query_params.get("limit", "50")
            try:
                after_seq = int(after_seq_raw)
                limit = int(limit_raw)
            except (TypeError, ValueError) as exc:
                raise _bad_request("after_seq and limit must be integers") from exc
            return service.read_transcript(
                ctx, grant_id, request_id, after_seq=after_seq, limit=limit,
                requested_activity_id=request.query_params.get("activity_id"))
        except DshError as exc:
            _read_failure(service, ctx, grant_id, request, exc)
            raise dsh_http_error(exc) from exc

    @app.get("/v2/dsh/grants/{grant_id}/facts/space_post/{post_id}")
    async def read_space_post(grant_id: str, post_id: str, request: Request):
        ctx, _body = await _authenticate(request, service, grant_id=grant_id)
        try:
            request_id = _request_id(request.query_params.get("request_id"))
            try:
                parsed_post_id = int(post_id)
            except (TypeError, ValueError) as exc:
                raise _bad_request("post_id must be an integer") from exc
            return service.read_space_post(
                ctx, grant_id, parsed_post_id, request_id,
                requested_activity_id=request.query_params.get("activity_id"))
        except DshError as exc:
            _read_failure(service, ctx, grant_id, request, exc)
            raise dsh_http_error(exc) from exc

    @app.post("/v2/dsh/grants/{grant_id}/actions")
    async def run_action(grant_id: str, request: Request):
        ctx, body = await _authenticate(request, service, grant_id=grant_id)
        try:
            action = _parse_action(body)
        except DshError as exc:
            # Schema/identity rejection happens before the service, so this is
            # the one place the route itself records the terminal deny.
            service._deny_action(ctx, grant_id, _safe_request_id(body), "", exc)
            raise dsh_http_error(exc) from exc
        try:
            return service.execute_action(
                ctx, grant_id, action,
                requested_activity_id=request.query_params.get("activity_id"))
        except DshError as exc:
            raise dsh_http_error(exc) from exc


def _safe_request_id(body: bytes) -> str:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict):
        return ""
    raw = value.get("request_id")
    return raw if isinstance(raw, str) and REQUEST_ID.match(raw) else ""
