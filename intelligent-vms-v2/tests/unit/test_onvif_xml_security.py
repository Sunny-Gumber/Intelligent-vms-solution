"""Reject hostile device XML at the actual SOAP and discovery parsing boundaries."""

import httpx
import pytest
from app.devices.discovery import parse_probe_match
from app.devices.onvif_soap import SoapClient, SoapFault

ENTITY_XML = """<!DOCTYPE Envelope [<!ENTITY injected "expanded">]>
<Envelope>&injected;</Envelope>"""


def test_discovery_rejects_entity_declarations():
    assert parse_probe_match(ENTITY_XML, "192.168.10.20") is None


@pytest.mark.asyncio
async def test_soap_rejects_entity_declarations(monkeypatch):
    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=ENTITY_XML))
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    with pytest.raises(SoapFault) as failure:
        await SoapClient("http://192.168.10.20/onvif/device_service").call("<Probe />")
    assert failure.value.code == "parse_error"
