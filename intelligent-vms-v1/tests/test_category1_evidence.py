import asyncio
import socket
from unittest.mock import AsyncMock, MagicMock

import pytest
from defusedxml import ElementTree as DET

from app.services import onvif_client, onvif_discovery
from app.services.network_policy import TargetNotAllowed


def test_onvif_probe_carries_firmware_and_sanitized_stream_metadata(monkeypatch):
    device_info = DET.fromstring("<Device><Manufacturer>Example</Manufacturer>"
                                    "<Model>Camera</Model><FirmwareVersion>1.2.3</FirmwareVersion>"
                                    "<SerialNumber>SN001</SerialNumber></Device>")
    services = DET.fromstring("<Services><Service><Namespace>http://www.onvif.org/ver10/media/wsdl</Namespace>"
                                  "<XAddr>http://192.168.1.20/onvif/media_service</XAddr></Service></Services>")
    profiles = DET.fromstring("<Profiles token='main'><Name>Main</Name>"
                                  "<VideoEncoderConfiguration><Encoding>H264</Encoding>"
                                  "<Resolution><Width>1920</Width><Height>1080</Height></Resolution>"
                                  "</VideoEncoderConfiguration></Profiles>")
    stream = DET.fromstring("<MediaUri><Uri>rtsp://admin:password@192.168.1.20:554/main?token=private</Uri>"
                                "</MediaUri>")
    soap = AsyncMock(side_effect=[device_info, services, DET.fromstring("<Capabilities/>"), profiles, stream])
    monkeypatch.setattr(onvif_client, "_soap", soap)

    result = asyncio.run(onvif_client.probe_xaddr("http://192.168.1.20/onvif/device_service", "admin", "password"))
    public = onvif_client.public_probe(result)

    assert result["device_info"]["FirmwareVersion"] == "1.2.3"
    assert result["recommended_main_profile_token"] == "main"
    assert public["profiles"][0]["stream_uri"] == "rtsp://192.168.1.20:554/main"
    assert "password" not in str(public)
    assert "token=private" not in str(public)
    assert soap.await_count == 5


def test_onvif_probe_rejects_public_advertised_media_endpoint(monkeypatch):
    services = DET.fromstring("<Services><Service><Namespace>http://www.onvif.org/ver10/media/wsdl</Namespace>"
                                  "<XAddr>http://8.8.8.8/onvif/media_service</XAddr></Service></Services>")
    soap = AsyncMock(side_effect=[DET.fromstring("<Device/>"), services, DET.fromstring("<Capabilities/>")])
    monkeypatch.setattr(onvif_client, "_soap", soap)

    with pytest.raises(TargetNotAllowed):
        asyncio.run(onvif_client.probe_xaddr("http://192.168.1.20/onvif/device_service", None, None))
    assert soap.await_count == 3


def test_discovery_deduplicates_responses_and_closes_socket(monkeypatch):
    payload = b"<Envelope><ProbeMatch><Address>urn:uuid:camera-a</Address>"
    payload += b"<XAddrs>http://192.168.1.20/onvif/device_service</XAddrs></ProbeMatch></Envelope>"
    sock = MagicMock()
    sock.recvfrom.side_effect = [(payload, ("192.168.1.20", 3702))] * 2 + [socket.timeout()]
    monkeypatch.setattr(onvif_discovery.socket, "socket", lambda *_args: sock)
    ticks = iter([0.0, 0.0, 0.1, 0.2, 0.6])
    monkeypatch.setattr(onvif_discovery.time, "monotonic", lambda: next(ticks))

    result = onvif_discovery.discover(0.5)

    assert len(result) == 1
    assert result[0]["endpoint_reference"] == "urn:uuid:camera-a"
    assert sock.sendto.call_count == 1
    sock.close.assert_called_once_with()
