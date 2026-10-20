"""Outbound webhooks with HMAC signing and bounded retry.

Every delivery carries:
    X-VMS-Event        event id
    X-VMS-Timestamp    unix seconds
    X-VMS-Signature    sha256=<hex hmac of "timestamp." + body>

The timestamp is inside the signed material, so a captured request cannot be
replayed later. Receivers should reject deliveries older than ~5 minutes.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.core.backoff import BackoffPolicy
from app.core.netguard import GuardPolicy, NetworkGuard, NetworkGuardError
from app.logging_setup import get_logger

log = get_logger("notifications.webhook")


@dataclass
class DeliveryResult:
    ok: bool
    status: int | None = None
    error: str | None = None
    attempts: int = 0


def sign_payload(secret: str, body: bytes, timestamp: int) -> str:
    material = f"{timestamp}.".encode("utf-8") + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), material,
                                hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, timestamp: int, signature: str,
                     tolerance_s: int = 300) -> bool:
    if abs(time.time() - timestamp) > tolerance_s:
        return False
    return hmac.compare_digest(sign_payload(secret, body, timestamp), signature)


class WebhookSender:
    def __init__(self, settings: Settings | None = None,
                 guard: NetworkGuard | None = None) -> None:
        self.settings = settings or get_settings()
        # Webhooks target customer infrastructure, so private ranges are
        # permitted, but the same SSRF guard still applies.
        self.guard = guard or NetworkGuard(GuardPolicy(
            allow_private=True, allowed_ports=(80, 443, 8080, 8443, 9000)))
        self.policy = BackoffPolicy(initial_s=1.0, max_s=60.0,
                                    max_attempts=self.settings.webhook_max_retries)

    async def send(self, url: str, payload: dict[str, Any], *, secret: str | None = None,
                   headers: dict[str, str] | None = None) -> DeliveryResult:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        timestamp = int(time.time())
        request_headers = {
            "Content-Type": "application/json",
            "User-Agent": "IntelligentVMS-Webhook/1.0",
            "X-VMS-Event": str(payload.get("id", "")),
            "X-VMS-Timestamp": str(timestamp),
        }
        if secret:
            request_headers["X-VMS-Signature"] = sign_payload(secret, body, timestamp)
        request_headers.update(headers or {})

        try:
            target, _ = self.guard.pin_url(url)
        except NetworkGuardError as exc:
            return DeliveryResult(False, error=f"destination rejected: {exc}")

        attempts = 0
        last_error: str | None = None
        while attempts < self.policy.max_attempts:
            attempts += 1
            try:
                async with httpx.AsyncClient(
                        timeout=httpx.Timeout(self.settings.webhook_timeout_s),
                        follow_redirects=False) as client:
                    response = await client.post(target, content=body,
                                                 headers=request_headers)
                if 200 <= response.status_code < 300:
                    return DeliveryResult(True, response.status_code, attempts=attempts)
                last_error = f"HTTP {response.status_code}"
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    # Client errors are not retryable: a 404 stays a 404.
                    return DeliveryResult(False, response.status_code, last_error,
                                          attempts)
            except httpx.HTTPError as exc:
                last_error = str(exc)[:200]

            if attempts < self.policy.max_attempts:
                await asyncio.sleep(self.policy.delay_for(attempts))

        log.warning("webhook.failed", url=target.split("?")[0], attempts=attempts,
                    error=last_error)
        return DeliveryResult(False, error=last_error, attempts=attempts)
