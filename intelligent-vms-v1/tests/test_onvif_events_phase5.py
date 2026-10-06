import asyncio
import logging

import pytest
from defusedxml import ElementTree as DET

import app.services.onvif_events as onvif_events
from app.services.onvif_client import OnvifError
from app.services.onvif_events import PullPointSubscription, parse_notifications, unsubscribe


def test_parse_motion_notification():
    root = DET.fromstring(
        b"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
          xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
          xmlns:tt="http://www.onvif.org/ver10/schema">
          <s:Body><PullMessagesResponse>
            <wsnt:NotificationMessage>
              <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
              <wsnt:Message>
                <tt:Message UtcTime="2026-09-24T05:00:00Z" PropertyOperation="Changed">
                  <tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="v1"/></tt:Source>
                  <tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data>
                </tt:Message>
              </wsnt:Message>
            </wsnt:NotificationMessage>
          </PullMessagesResponse></s:Body>
        </s:Envelope>"""
    )
    events = parse_notifications(root)
    assert len(events) == 1
    assert events[0]["event_type"] == "motion"
    assert events[0]["active"] is True
    assert events[0]["items"]["IsMotion"] == "true"
    assert events[0]["property_operation"] == "Changed"


def test_unknown_topic_remains_vendor_neutral():
    root = DET.fromstring(
        b"""<Envelope><NotificationMessage><Topic>vendor:Something/New</Topic>
        <Message UtcTime="2026-09-24T05:00:00Z"><Data>
        <SimpleItem Name="Value" Value="42"/></Data></Message>
        </NotificationMessage></Envelope>"""
    )
    events = parse_notifications(root)
    assert events[0]["event_type"] == "onvif_event"
    assert events[0]["topic"] == "vendor:Something/New"


def test_event_soap_envelope_declares_required_namespaces():
    from defusedxml import ElementTree as SafeET
    from app.services.onvif_client import _envelope
    from app.services.onvif_events import CREATE_ACTION, _addressing_header

    payload = _envelope(
        "<tev:CreatePullPointSubscription/>",
        "user",
        "password",
        _addressing_header(CREATE_ACTION, "http://192.168.1.20/onvif/events"),
    )
    root = SafeET.fromstring(payload)
    assert root.tag.endswith("Envelope")


def test_unsubscribe_body_namespace_is_valid_xml():
    from defusedxml import ElementTree as SafeET
    from app.services.onvif_client import _envelope
    from app.services.onvif_events import UNSUBSCRIBE_ACTION, _addressing_header

    payload = _envelope(
        "<wsnt:Unsubscribe/>",
        None,
        None,
        _addressing_header(UNSUBSCRIBE_ACTION, "http://192.168.1.20/onvif/subscription"),
    )
    root = SafeET.fromstring(payload)
    assert root.tag.endswith("Envelope")


def test_unsubscribe_logs_expected_onvif_cleanup_failure(monkeypatch, caplog):
    """Keep known ONVIF cleanup failure best-effort but observable."""
    async def fail_soap(*_args, **_kwargs):
        raise OnvifError("NETWORK_UNREACHABLE", "camera unavailable")

    monkeypatch.setattr(onvif_events, "_soap", fail_soap)
    subscription = PullPointSubscription(
        address="http://192.168.1.20/onvif/subscription",
        tenant_id="tenant-a",
        site_id="site-a",
    )

    with caplog.at_level(logging.WARNING, logger=onvif_events.__name__):
        asyncio.run(unsubscribe(subscription, None, None))

    assert "onvif_unsubscribe_failed" in caplog.text
    assert "NETWORK_UNREACHABLE" in caplog.text


def test_unsubscribe_propagates_unexpected_programming_failure(monkeypatch):
    """Do not hide unexpected non-ONVIF failures during unsubscribe."""
    async def fail_soap(*_args, **_kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(onvif_events, "_soap", fail_soap)
    subscription = PullPointSubscription(
        address="http://192.168.1.20/onvif/subscription",
        tenant_id="tenant-a",
        site_id="site-a",
    )

    with pytest.raises(RuntimeError, match="unexpected bug"):
        asyncio.run(unsubscribe(subscription, None, None))
