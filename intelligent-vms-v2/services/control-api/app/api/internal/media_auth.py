"""POST /internal/media/auth -- MediaMTX external authentication hook.

Registered outside /api/v1 and excluded from the OpenAPI document. Exclusion
is documentation hygiene only, NOT access control: protection comes from
caller authentication (HTTP Basic hook secret + allowed peer network) and from
the deployment keeping this listener on an internal network (ADR-0015 s8).

Response contract: 204 admits, 401 denies, and nothing else. Every failure,
including missing application state, a database outage or a bug, resolves to
401 with an empty body. Logs carry the deny reason and an exception TYPE only,
never exception text, tokens, passwords or the query string.
"""

from __future__ import annotations

import base64
import binascii
import json

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from app.db.session import session_scope
from app.logging_setup import get_logger
from app.media.media_auth import (
    AuthPayload,
    Decision,
    DenyReason,
    caller_is_trusted,
    decide,
)

router = APIRouter(prefix="/internal/media", tags=["internal"], include_in_schema=False)
log = get_logger("media.auth_hook")

MAX_BODY_BYTES = 8 * 1024
_ALLOW = 204
_DENY = 401


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic" or not value:
        return None
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return user, password


def _log(decision: Decision, payload: AuthPayload | None) -> None:
    fields = {
        "action": payload.action if payload else None,
        "protocol": payload.protocol if payload else None,
        "path": payload.path if payload else None,
        "connection_id": payload.id if payload else None,
        "viewer_ip": payload.ip if payload else None,
        "live_session_id": decision.live_session_id,
    }
    if decision.allowed:
        log.info("media_auth.allowed", bound=decision.bound, **fields)
    elif decision.reason is DenyReason.NO_CREDENTIALS:
        log.debug("media_auth.challenge", **fields)   # routine RTSP 401 challenge
    else:
        log.warning("media_auth.denied", reason=decision.reason.value if decision.reason
                    else None, **fields)


@router.post("/auth", status_code=_ALLOW)
async def media_auth(request: Request) -> Response:
    payload: AuthPayload | None = None
    try:
        container = getattr(request.app.state, "container", None)
        if container is None:
            decision = Decision.deny(DenyReason.CONFIG_INVALID)
            _log(decision, None)
            return Response(status_code=_DENY)
        settings = container.settings
        broker = container.sessions
        registry = broker.connections

        if settings.media_auth_problems():
            decision = Decision.deny(DenyReason.CONFIG_INVALID)
            _log(decision, None)
            return Response(status_code=_DENY)

        peer = request.client.host if request.client else None
        if not caller_is_trusted(settings, peer, _basic_credentials(request)):
            decision = Decision.deny(DenyReason.UNTRUSTED_CALLER)
            _log(decision, None)
            return Response(status_code=_DENY)

        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            _log(Decision.deny(DenyReason.MALFORMED), None)
            return Response(status_code=_DENY)
        try:
            raw = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raw = None
        payload = AuthPayload.parse(raw)
        if payload is None:
            _log(Decision.deny(DenyReason.MALFORMED), None)
            return Response(status_code=_DENY)

        def run() -> Decision:
            with session_scope() as db:
                result = decide(db, settings=settings, broker=broker,
                                registry=registry, payload=payload)
                if not result.allowed:
                    # Never persist a partial binding for a denied request.
                    db.rollback()
                return result

        decision = await run_in_threadpool(run)
        _log(decision, payload)
        return Response(status_code=_ALLOW if decision.allowed else _DENY)

    except Exception as exc:  # noqa: BLE001 - fail closed; never 2xx on error
        # Type only: exception text can contain tokens or connection strings.
        log.error("media_auth.failed_closed", error_type=type(exc).__name__)
        return Response(status_code=_DENY)
