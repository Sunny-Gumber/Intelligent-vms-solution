import asyncio
import hashlib
import json
import logging
import os
import random
import uuid
from dataclasses import dataclass

import httpx

from sqlalchemy import select

from app.core.security import decrypt_secret
from app.db.session import SessionLocal
from app.models.entities import CameraCapabilityEntity, CameraEntity
from app.services.outbox import enqueue_event_once
from app.services.onvif_events import (
    create_pullpoint,
    event_service_xaddr,
    parse_notifications,
    pull_messages,
    set_synchronization_point,
    unsubscribe,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("onvif-event-worker")

REFRESH_SECONDS = max(5.0, float(os.getenv("ONVIF_EVENT_REFRESH_SECONDS", "30")))
MAX_CAMERAS = max(1, int(os.getenv("ONVIF_EVENT_MAX_CAMERAS", "200")))
SCAN_LIMIT = max(MAX_CAMERAS, int(os.getenv("ONVIF_EVENT_SCAN_LIMIT", "5000")))
SHARD_COUNT = max(1, int(os.getenv("ONVIF_EVENT_SHARD_COUNT", "1")))
SHARD_INDEX = int(os.getenv("ONVIF_EVENT_SHARD_INDEX", "0"))
if not 0 <= SHARD_INDEX < SHARD_COUNT:
    raise RuntimeError("ONVIF_EVENT_SHARD_INDEX must be within ONVIF_EVENT_SHARD_COUNT")
PULL_MESSAGE_LIMIT = max(1, min(256, int(os.getenv("ONVIF_EVENT_MESSAGE_LIMIT", "32"))))
MEDIA_NODE_ID = os.getenv("ONVIF_EVENT_MEDIA_NODE_ID", "").strip()
BACKOFF_MAX = max(10.0, float(os.getenv("ONVIF_EVENT_BACKOFF_MAX_SECONDS", "120")))
REGIONAL_SPOOL_URL = os.getenv("REGIONAL_SPOOL_URL", "").strip().rstrip("/")
REGIONAL_SPOOL_TOKEN = os.getenv("REGIONAL_SPOOL_TOKEN", "")
_spool_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class Target:
    """Immutable ONVIF event-subscription target for one camera.

    Attributes:
        camera_id: Camera identifier.
        tenant_id: Owning tenant.
        site_id: Owning site.
        media_node_id: Assigned media node.
        username: Optional ONVIF username.
        password: Optional ONVIF password.
        event_xaddr: Validated ONVIF event-service URL.
    """

    camera_id: str
    tenant_id: str
    site_id: str
    media_node_id: str
    username: str | None
    password: str | None
    event_xaddr: str


async def load_targets() -> list[Target]:
    """Load the bounded shard-local set of ONVIF event-capable cameras.

    Returns:
        Target objects for enabled cameras assigned to this worker shard.

    Raises:
        Exception: Database or credential-decryption failures propagate.
    """
    async with SessionLocal() as session:
        q = (
            select(CameraEntity, CameraCapabilityEntity)
            .join(CameraCapabilityEntity, CameraCapabilityEntity.camera_id == CameraEntity.id)
            .where(CameraEntity.enabled.is_(True))
            .order_by(CameraEntity.id)
            .limit(SCAN_LIMIT)
        )
        if MEDIA_NODE_ID:
            q = q.where(CameraEntity.media_node_id == MEDIA_NODE_ID)
        rows = (await session.execute(q)).all()

    targets: list[Target] = []
    for camera, capability in rows:
        shard = int.from_bytes(
            hashlib.sha256(camera.id.encode("utf-8")).digest()[:8], "big"
        ) % SHARD_COUNT
        if shard != SHARD_INDEX:
            continue
        features = capability.features_json or {}
        if not features.get("events"):
            continue
        try:
            xaddr = event_service_xaddr(
                capability.services_json or [],
                tenant_id=camera.tenant_id,
                site_id=camera.site_id,
            )
        except Exception:
            log.exception("invalid_event_service camera_id=%s", camera.id)
            continue
        if not xaddr:
            continue
        targets.append(
            Target(
                camera_id=camera.id,
                tenant_id=camera.tenant_id,
                site_id=camera.site_id,
                media_node_id=camera.media_node_id,
                username=decrypt_secret(camera.username_enc),
                password=decrypt_secret(camera.password_enc),
                event_xaddr=xaddr,
            )
        )
        if len(targets) >= MAX_CAMERAS:
            break
    return targets


def normalized_event(target: Target, item: dict) -> dict:
    """Normalize one parsed ONVIF notification into the VMS event contract.

    Args:
        target: Camera subscription target providing tenant and site identity.
        item: Parsed ONVIF notification dictionary.

    Returns:
        Normalized deterministic VMS event dictionary.

    Raises:
        KeyError: If required notification fields are missing.
    """
    event_type = str(item["event_type"])[:128]
    if item.get("active") is False and event_type in {
        "motion", "tamper", "tripwire", "intrusion", "digital_input"
    }:
        event_type = f"{event_type}_cleared"

    raw_items = item.get("items", {})
    if not isinstance(raw_items, dict):
        raw_items = {}
    bounded_items = {}
    for key, value in list(raw_items.items())[:64]:
        safe_key = str(key)[:128]
        if value is None or isinstance(value, (bool, int, float)):
            bounded_items[safe_key] = value
        else:
            bounded_items[safe_key] = str(value)[:2048]

    fingerprint = json.dumps(
        {
            "camera_id": target.camera_id,
            "timestamp": item["timestamp"].isoformat(),
            "topic": str(item.get("topic") or "")[:512],
            "operation": str(item.get("property_operation") or "")[:128],
            "items": bounded_items,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))

    return {
        "event_id": event_id,
        "tenant_id": target.tenant_id,
        "site_id": target.site_id,
        "camera_id": target.camera_id,
        "timestamp": item["timestamp"].isoformat(),
        "event_type": event_type,
        "object_type": None,
        "source": "camera",
        "confidence": None,
        "zone_id": str(bounded_items.get("Rule") or bounded_items.get("Region") or "")[:128] or None,
        "severity": "medium" if not event_type.endswith("_cleared") else "info",
        "snapshot_uri": None,
        "recording_start": None,
        "recording_end": None,
        "attributes": {
            "protocol": "onvif_pullpoint",
            "topic": str(item.get("topic") or "")[:512],
            "active": item.get("active"),
            "property_operation": str(item.get("property_operation") or "")[:128],
            "items": bounded_items,
            "media_node_id": target.media_node_id,
        },
    }


async def publish_event(event: dict):
    """Durably publish one ONVIF event through regional spool or central outbox.

    Args:
        event: Normalized VMS event.

    Returns:
        None after durable acceptance.

    Raises:
        RuntimeError: If regional spooling is configured without a client.
        httpx.HTTPError: If regional spool submission fails.
        Exception: Central database or outbox failures propagate.
    """
    if REGIONAL_SPOOL_URL:
        if _spool_client is None:
            raise RuntimeError("regional spool client is not initialized")
        response = await _spool_client.post(
            f"{REGIONAL_SPOOL_URL}/v1/events",
            json=event,
            headers={"X-Regional-Spool-Token": REGIONAL_SPOOL_TOKEN},
        )
        response.raise_for_status()
        return

    # Central-mode ONVIF events are accepted durably by PostgreSQL first.
    # Kafka delivery is asynchronous through the shared outbox worker.
    async with SessionLocal() as session:
        async with session.begin():
            await enqueue_event_once(session, event)


async def camera_loop(target: Target, stop: asyncio.Event):
    """Maintain one camera PullPoint subscription with bounded reconnect backoff.

    Args:
        target: Camera event target.
        stop: Shared shutdown event.

    Returns:
        None after shutdown or cancellation.

    Raises:
        asyncio.CancelledError: Cancellation is propagated immediately.
    """
    failures = 0
    subscription = None
    while not stop.is_set():
        try:
            subscription = await create_pullpoint(
                target.event_xaddr,
                target.username,
                target.password,
                tenant_id=target.tenant_id,
                site_id=target.site_id,
            )
            try:
                await set_synchronization_point(
                    subscription, target.username, target.password
                )
            except Exception:
                # Some interoperable devices do not implement this robustly.
                log.info("sync_point_unavailable camera_id=%s", target.camera_id)

            failures = 0
            log.info("subscription_ready camera_id=%s", target.camera_id)
            while not stop.is_set():
                root = await pull_messages(
                    subscription,
                    target.username,
                    target.password,
                    timeout="PT30S",
                    message_limit=PULL_MESSAGE_LIMIT,
                )
                for item in parse_notifications(root):
                    await publish_event(normalized_event(target, item))
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            delay = min(BACKOFF_MAX, (2 ** min(failures, 6)) + random.random())
            log.exception(
                "subscription_failed camera_id=%s retry_seconds=%.1f",
                target.camera_id,
                delay,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        finally:
            if subscription:
                await unsubscribe(subscription, target.username, target.password)
                subscription = None


async def supervisor():
    """Supervise shard-local ONVIF camera subscription tasks.

    Returns:
        None under normal operation; runs until cancelled.

    Raises:
        RuntimeError: If regional spool configuration is incomplete.
        Exception: Target loading or task-management failures propagate.
    """
    global _spool_client
    if REGIONAL_SPOOL_URL:
        if not REGIONAL_SPOOL_TOKEN:
            raise RuntimeError("REGIONAL_SPOOL_TOKEN is required when REGIONAL_SPOOL_URL is set")
        _spool_client = httpx.AsyncClient(timeout=5.0)
    stop = asyncio.Event()
    tasks: dict[str, tuple[Target, asyncio.Task]] = {}
    try:
        while True:
            targets = {target.camera_id: target for target in await load_targets()}

            for camera_id, (old_target, task) in list(tasks.items()):
                new_target = targets.get(camera_id)
                if new_target != old_target:
                    task.cancel()
                    tasks.pop(camera_id, None)

            for camera_id, target in targets.items():
                if camera_id not in tasks:
                    task = asyncio.create_task(
                        camera_loop(target, stop),
                        name=f"onvif-event-{camera_id}",
                    )
                    tasks[camera_id] = (target, task)

            await asyncio.sleep(REFRESH_SECONDS)
    finally:
        stop.set()
        for _, task in tasks.values():
            task.cancel()
        await asyncio.gather(*(task for _, task in tasks.values()), return_exceptions=True)
        if _spool_client is not None:
            await _spool_client.aclose()
            _spool_client = None


if __name__ == "__main__":
    asyncio.run(supervisor())
