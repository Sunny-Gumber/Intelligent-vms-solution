import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import events as event_services
from app.services import health_monitor


def test_event_publisher_start_cleanup_log_omits_exception_text(monkeypatch, caplog):
    """Log Kafka start cleanup failure without exposing exception text."""
    producer = MagicMock()
    producer.start = AsyncMock(side_effect=RuntimeError("broker-start-secret"))
    producer.stop = AsyncMock(
        side_effect=RuntimeError("broker-stop-secret-must-not-be-logged")
    )
    monkeypatch.setattr(
        event_services,
        "AIOKafkaProducer",
        lambda **_kwargs: producer,
    )
    monkeypatch.setattr(
        event_services.settings,
        "kafka_bootstrap_servers",
        "broker:9092",
    )
    publisher = event_services.EventPublisher()

    with caplog.at_level(logging.WARNING, logger="app.services.events"):
        with pytest.raises(event_services.EventPublisherUnavailable):
            asyncio.run(publisher._ensure_started())

    producer.start.assert_awaited_once_with()
    producer.stop.assert_awaited_once_with()
    assert "event_publisher_start_cleanup_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "broker-stop-secret-must-not-be-logged" not in caplog.text


def test_event_publisher_reset_cleanup_log_omits_exception_text(caplog):
    """Log Kafka reset cleanup failure while clearing stale producer state."""
    producer = MagicMock()
    producer.stop = AsyncMock(
        side_effect=RuntimeError("reset-secret-must-not-be-logged")
    )
    publisher = event_services.EventPublisher()
    publisher._producer = producer

    with caplog.at_level(logging.WARNING, logger="app.services.events"):
        asyncio.run(publisher._reset_after_failure(producer))

    producer.stop.assert_awaited_once_with()
    assert publisher._producer is None
    assert "event_publisher_reset_cleanup_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "reset-secret-must-not-be-logged" not in caplog.text


def test_health_probe_close_log_omits_exception_text(monkeypatch, caplog):
    """Keep a successful RTSP probe while exposing socket cleanup failure safely."""
    writer = MagicMock()
    writer.wait_closed = AsyncMock(
        side_effect=RuntimeError("health-secret-must-not-be-logged")
    )

    async def fake_open_connection(*, host, port):
        assert host == "192.168.1.20"
        assert port == 554
        return object(), writer

    monkeypatch.setattr(
        health_monitor.asyncio,
        "open_connection",
        fake_open_connection,
    )

    with caplog.at_level(logging.WARNING, logger="app.services.health_monitor"):
        ready = asyncio.run(
            health_monitor.probe_rtsp_transport("192.168.1.20", 554, 1.0)
        )

    assert ready is True
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()
    assert "health_probe_writer_close_failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "health-secret-must-not-be-logged" not in caplog.text
