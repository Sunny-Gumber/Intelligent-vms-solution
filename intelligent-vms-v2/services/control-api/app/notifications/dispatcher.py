"""Notification routing with persistent, retried deliveries."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import CredentialVault
from app.core.timeutil import utcnow
from app.db.models import NotificationChannel, NotificationDelivery
from app.logging_setup import get_logger
from app.notifications.email import EmailSender, alarm_body, alarm_subject
from app.notifications.webhook import WebhookSender

log = get_logger("notifications.dispatcher")

MAX_ATTEMPTS = 6


class NotificationDispatcher:
    def __init__(self, vault: CredentialVault, *, webhook: WebhookSender | None = None,
                 email: EmailSender | None = None) -> None:
        self.vault = vault
        self.webhook = webhook or WebhookSender()
        self.email = email or EmailSender()

    def enqueue(self, session: Session, channel_names: list[str],
                payload: dict[str, Any]) -> list[NotificationDelivery]:
        channels = session.execute(
            select(NotificationChannel)
            .where(NotificationChannel.name.in_(channel_names),
                   NotificationChannel.enabled.is_(True))).scalars().all()
        deliveries: list[NotificationDelivery] = []
        for channel in channels:
            delivery = NotificationDelivery(
                channel_id=channel.id, alarm_id=payload.get("alarm_id"),
                event_id=payload.get("id"), payload=payload, next_attempt_at=utcnow())
            session.add(delivery)
            deliveries.append(delivery)
        return deliveries

    async def process_pending(self, session: Session, limit: int = 50) -> int:
        pending = session.execute(
            select(NotificationDelivery)
            .where(NotificationDelivery.state == "pending",
                   NotificationDelivery.next_attempt_at <= utcnow())
            .limit(limit)).scalars().all()

        processed = 0
        for delivery in pending:
            channel = session.get(NotificationChannel, delivery.channel_id)
            if channel is None or not channel.enabled:
                delivery.state = "cancelled"
                continue

            delivery.attempts += 1
            ok, error = await self._deliver(channel, delivery.payload)
            if ok:
                delivery.state = "sent"
                channel.last_success_at = utcnow()
                channel.failure_count = 0
                channel.last_error = None
            else:
                channel.failure_count += 1
                channel.last_error = (error or "")[:500]
                delivery.last_error = (error or "")[:500]
                if delivery.attempts >= MAX_ATTEMPTS:
                    delivery.state = "failed"
                else:
                    delivery.next_attempt_at = utcnow() + dt.timedelta(
                        seconds=min(600, 5 * 2 ** delivery.attempts))
            processed += 1
        return processed

    async def _deliver(self, channel: NotificationChannel,
                       payload: dict[str, Any]) -> tuple[bool, str | None]:
        config = channel.config or {}
        if channel.kind == "webhook":
            secret = None
            if channel.secret_blob:
                secret = self.vault.decrypt(channel.secret_blob,
                                            aad=f"channel:{channel.id}")
            result = await self.webhook.send(config.get("url", ""), payload,
                                             secret=secret)
            return result.ok, result.error
        if channel.kind == "email":
            result = self.email.send(config.get("recipients", []),
                                     alarm_subject(payload),
                                     alarm_body(payload, config.get("base_url")))
            return result.ok, result.error
        return False, f"unsupported channel kind {channel.kind!r}"
