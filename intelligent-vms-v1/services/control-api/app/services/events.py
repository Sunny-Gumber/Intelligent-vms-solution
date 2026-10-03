import asyncio
import json
import logging

from aiokafka import AIOKafkaProducer

from app.core.config import settings

log = logging.getLogger(__name__)


class EventPublisherUnavailable(RuntimeError):
    """Raised when a required event-broker publish cannot be completed."""


class EventPublisherPayloadError(RuntimeError):
    """Raised when an event payload cannot be serialized safely for publishing."""


class EventPublisher:
    """Lazily manage the Kafka producer used by direct/outbox event publishing."""

    def __init__(self):
        self._producer: AIOKafkaProducer | None = None
        self._lock = asyncio.Lock()

    async def start(self):
        """Initialize publisher lifecycle without requiring broker connectivity.

        Returns:
            None. Broker connection remains lazy so an outage cannot block API startup.
        """
        # Connectivity is intentionally lazy. Kafka outage must not prevent the
        # control plane from starting; direct callers fail/retry and the outbox
        # persists DB-backed events until the broker is available.
        return

    async def stop(self):
        """Stop and release the active Kafka producer, if one exists.

        Returns:
            None after producer ownership is cleared.

        Raises:
            Exception: Producer shutdown failures propagate to application shutdown.
        """
        async with self._lock:
            producer = self._producer
            self._producer = None
        if producer:
            await producer.stop()

    async def _ensure_started(self) -> AIOKafkaProducer:
        if self._producer is not None:
            return self._producer
        if not settings.kafka_bootstrap_servers:
            raise EventPublisherUnavailable("Kafka is not configured")

        async with self._lock:
            if self._producer is not None:
                return self._producer
            producer = AIOKafkaProducer(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                request_timeout_ms=max(1000, settings.kafka_request_timeout_ms),
                value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            )
            try:
                await producer.start()
            except Exception as exc:
                try:
                    await producer.stop()
                except Exception as cleanup_exc:
                    log.warning(
                        "event_publisher_start_cleanup_failed error_type=%s",
                        cleanup_exc.__class__.__name__,
                    )
                raise EventPublisherUnavailable("Kafka producer unavailable") from exc
            self._producer = producer
            return producer

    async def _reset_after_failure(self, producer: AIOKafkaProducer) -> None:
        async with self._lock:
            if self._producer is producer:
                self._producer = None
        try:
            await producer.stop()
        except Exception as exc:
            log.warning(
                "event_publisher_reset_cleanup_failed error_type=%s",
                exc.__class__.__name__,
            )

    async def publish_to(
        self,
        topic: str,
        payload: dict,
        key: bytes,
        *,
        require_available: bool = False,
    ) -> None:
        """Publish one serialized payload to a Kafka topic.

        Args:
            topic: Kafka topic name.
            payload: Event/message dictionary.
            key: Kafka partitioning key.
            require_available: Whether missing broker configuration is an error.

        Returns:
            None after publish, or immediately when optional publishing is disabled.

        Raises:
            EventPublisherPayloadError: If the payload cannot be serialized.
            EventPublisherUnavailable: If a required broker is unavailable or
                publishing fails.
        """
        if not settings.kafka_bootstrap_servers:
            if require_available:
                raise EventPublisherUnavailable("Kafka is not configured")
            return

        try:
            # Validate serialization before touching broker state so poison
            # payloads are distinguishable from transient broker outages.
            json.dumps(payload, default=str)
        except Exception as exc:
            raise EventPublisherPayloadError("Event payload is not serializable") from exc

        producer = await self._ensure_started()
        try:
            await producer.send_and_wait(topic, payload, key=key)
        except Exception as exc:
            await self._reset_after_failure(producer)
            raise EventPublisherUnavailable("Kafka publish failed") from exc

    async def publish(self, event: dict):
        """Publish one normalized VMS event to the configured event topic.

        Args:
            event: Normalized VMS event dictionary containing camera identity.

        Returns:
            None after publish or optional no-op when Kafka is not configured.

        Raises:
            KeyError: If camera_id is absent from the event.
            EventPublisherPayloadError: If the payload is not serializable.
            EventPublisherUnavailable: If broker publishing fails.
        """
        key = f"{event.get('tenant_id','default')}:{event['camera_id']}".encode("utf-8")
        await self.publish_to(settings.kafka_topic_events, event, key)


event_publisher = EventPublisher()
