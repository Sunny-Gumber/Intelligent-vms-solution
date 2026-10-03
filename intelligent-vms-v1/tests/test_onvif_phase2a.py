import importlib.util
import logging
import sys
from pathlib import Path

CONTROL = Path(__file__).parents[1] / "services" / "control-api"
sys.path.insert(0, str(CONTROL))

from app.services.network_policy import address_allowed
from app.services.onvif_client import (
    inject_rtsp_credentials,
    parse_profiles,
    public_probe,
    sanitize_rtsp_uri,
    select_main_sub,
)
from app.services.onvif_discovery import _parse_probe_response, parse_probe_match
from defusedxml import ElementTree as DET


def test_network_policy_private_and_public_defaults():
    assert address_allowed("10.10.10.10")
    assert address_allowed("192.168.1.20")
    assert address_allowed("100.64.1.5")
    assert not address_allowed("8.8.8.8")


def test_probe_match_parser():
    xml = b"""<?xml version="1.0"?>
    <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
      xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
      xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
      <s:Body><d:ProbeMatches><d:ProbeMatch>
        <a:EndpointReference><a:Address>urn:uuid:camera-1</a:Address></a:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/Gate</d:Scopes>
        <d:XAddrs>http://192.168.1.20/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></s:Body>
    </s:Envelope>"""
    item = parse_probe_match(xml)
    assert item["endpoint_reference"] == "urn:uuid:camera-1"
    assert item["xaddrs"] == ["http://192.168.1.20/onvif/device_service"]
    assert len(item["scopes"]) == 2


def test_profile_parser_and_selection_do_not_assume_order():
    xml = DET.fromstring(b"""<Envelope xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
      xmlns:tt="http://www.onvif.org/ver10/schema">
      <trt:Profiles token="sub"><tt:Name>Sub</tt:Name>
        <tt:VideoEncoderConfiguration>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
          <tt:RateControl><tt:FrameRateLimit>10</tt:FrameRateLimit><tt:BitrateLimit>256</tt:BitrateLimit></tt:RateControl>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
      <trt:Profiles token="main"><tt:Name>Main</tt:Name>
        <tt:VideoEncoderConfiguration>
          <tt:Encoding>H265</tt:Encoding>
          <tt:Resolution><tt:Width>3840</tt:Width><tt:Height>2160</tt:Height></tt:Resolution>
          <tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
        </tt:VideoEncoderConfiguration>
      </trt:Profiles>
    </Envelope>""")
    profiles = parse_profiles(xml)
    main, sub = select_main_sub(profiles)
    assert main == "main"
    assert sub == "sub"


def test_uri_sanitization_and_credential_injection():
    raw = "rtsp://camera-user:secret@192.168.1.20:554/cam/stream?token=private"
    assert sanitize_rtsp_uri(raw) == "rtsp://192.168.1.20:554/cam/stream"
    source = inject_rtsp_credentials(
        "rtsp://192.168.1.20:554/cam/stream?channel=1",
        "admin@example",
        "p@ss word",
    )
    assert source == "rtsp://admin%40example:p%40ss%20word@192.168.1.20:554/cam/stream?channel=1"


def test_public_probe_removes_internal_raw_uri():
    probe = {
        "xaddr": "http://192.168.1.20/onvif/device_service",
        "device_info": {},
        "services": [],
        "features": {},
        "profiles": [
            {
                "token": "p1",
                "stream_uri": "rtsp://192.168.1.20/stream",
                "_raw_stream_uri": "rtsp://u:p@192.168.1.20/stream?secret=x",
            }
        ],
        "recommended_main_profile_token": "p1",
        "recommended_sub_profile_token": None,
    }
    public = public_probe(probe)
    assert "_raw_stream_uri" not in public["profiles"][0]
    assert "secret=x" not in str(public)


def test_invalid_discovery_response_is_ignored_with_observability(caplog):
    """Ignore malformed multicast noise while recording why it was rejected."""
    with caplog.at_level(logging.DEBUG, logger="app.services.onvif_discovery"):
        result = _parse_probe_response(b"<broken", ("192.168.1.50", 3702))

    assert result is None
    assert "onvif_discovery_invalid_response" in caplog.text
    assert "192.168.1.50" in caplog.text
