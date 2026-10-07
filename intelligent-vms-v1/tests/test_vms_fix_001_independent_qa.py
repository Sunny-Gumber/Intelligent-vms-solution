"""Independent QA-only regressions for VMS-FIX-001.

These tests intentionally contain no production changes. They exercise missing
security/resource boundaries against the reviewed product head.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest

from app.core.config import settings
from app.services import network_policy, onvif_client, onvif_events
from app.services.network_policy import TargetNotAllowed
from app.services.onvif_client import OnvifError


_REAL_ASYNC_CLIENT = httpx.AsyncClient
_DIGEST_CHALLENGE = (
    'Digest realm="synthetic", nonce="nonce-qa", algorithm=MD5, qop="auth"'
)


class _TrackingStream(httpx.AsyncByteStream):
    """Track transport chunks and stream cleanup for bounded-response QA."""

    def __init__(self, chunks=(), *, active=False):
        self.chunks = list(chunks)
        self.active = active
        self.closed = False
        self.yielded = 0
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        while self.active:
            self.yielded += 1
            yield b"x"
            await asyncio.sleep(0)

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _qa_policy(monkeypatch):
    """Use deterministic synthetic global and exact-site camera policies."""
    monkeypatch.setattr(
        settings,
        "onvif_allowed_cidrs",
        "10.0.0.0/8,127.0.0.0/8,169.254.0.0/16",
    )
    monkeypatch.setattr(
        settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["10.1.0.0/16"],"tenant-b/site-b":["10.2.0.0/16"]}',
    )
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 128)
    monkeypatch.setattr(settings, "onvif_connect_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "onvif_operation_timeout_seconds", 2.0)


def _install_mock_transport(monkeypatch, handler):
    """Use real HTTPX AsyncClient/DigestAuth with only the transport mocked."""
    transport = httpx.MockTransport(handler)
    clients = []

    def factory(**kwargs):
        kwargs["transport"] = transport
        client = _REAL_ASYNC_CLIENT(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", factory)
    return clients


def _soap_call(**kwargs):
    """Execute one synthetic scoped SOAP operation."""
    return onvif_client._soap(
        "http://10.1.0.8/onvif/events",
        "urn:qa-action",
        "<tds:GetDeviceInformation/>",
        "qa-user",
        "qa-password",
        tenant_id="tenant-a",
        site_id="site-a",
        **kwargs,
    )


def test_qa_malformed_digest_challenge_maps_to_safe_onvif_error(monkeypatch):
    """Malformed camera Digest syntax must not escape as raw parser exceptions."""
    challenge = _TrackingStream([b"ignored"])

    def handler(_request):
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": 'Digest realm="synthetic", nonce'},
            stream=challenge,
        )

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())

    assert "qa-password" not in str(error.value)
    assert challenge.yielded == 0
    assert challenge.closed is True


def test_qa_digest_final_overflow_stops_at_first_oversized_chunk(monkeypatch):
    """Bound the authenticated terminal response, not only the 401 challenge."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 16)
    challenge = _TrackingStream([b"ok"])
    final = _TrackingStream([b"z" * 32, b"must-not-be-read"])
    requests = []

    def handler(request):
        requests.append(request.headers.get("authorization"))
        if request.headers.get("authorization") is None:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
                stream=challenge,
            )
        return httpx.Response(200, stream=final)

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())

    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert len(requests) == 2
    assert challenge.yielded == 1 and challenge.closed is True
    assert final.yielded == 1 and final.closed is True


def test_qa_exact_cap_succeeds_and_empty_body_fails_safely(monkeypatch):
    """Allow an exact-cap XML payload while treating an empty body as invalid XML."""
    payload = b"<Envelope/>"
    monkeypatch.setattr(settings, "onvif_max_response_bytes", len(payload))
    exact = _TrackingStream([payload])

    _install_mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=exact),
    )
    root = asyncio.run(_soap_call())
    assert root.tag == "Envelope"
    assert exact.yielded == 1 and exact.closed is True

    empty = _TrackingStream([])
    _install_mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=empty),
    )
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert empty.yielded == 0 and empty.closed is True


def test_qa_several_small_chunks_cross_cap_and_terminal_403_is_unread(monkeypatch):
    """Reject cumulative overflow and never consume a terminal forbidden body."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 8)
    overflow = _TrackingStream([b"123", b"456", b"789", b"later"])
    _install_mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=overflow),
    )
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert overflow.yielded == 3
    assert overflow.closed is True

    challenge = _TrackingStream([b"ok"])
    forbidden = _TrackingStream([b"do-not-consume"])
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if request.headers.get("authorization") is None:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
                stream=challenge,
            )
        return httpx.Response(403, stream=forbidden)

    _install_mock_transport(monkeypatch, handler)
    with pytest.raises(OnvifError) as denied:
        asyncio.run(_soap_call())
    assert denied.value.code == "AUTH_FAILED"
    assert count == 2
    assert forbidden.yielded == 0 and forbidden.closed is True


def test_qa_real_wait_for_bounds_continuously_active_final_stream(monkeypatch):
    """Prove the real scheduler deadline cancels an active stream and closes it."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 1_000_000)
    active = _TrackingStream(active=True)
    _install_mock_transport(
        monkeypatch,
        lambda _request: httpx.Response(200, stream=active),
    )

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call(operation_timeout_seconds=0.05))

    assert error.value.code == "NETWORK_UNREACHABLE"
    assert active.yielded > 1
    assert active.closed is True


def test_qa_policy_change_after_subscription_creation_fails_before_client(monkeypatch):
    """Re-approve a stored pinned subscription against the current site policy."""
    subscription = onvif_events.PullPointSubscription(
        address="http://10.1.0.9/onvif/subscription",
        tenant_id="tenant-a",
        site_id="site-a",
        logical_address="http://pullpoint.example/onvif/subscription",
        request_host_header="pullpoint.example",
        tls_server_name="pullpoint.example",
    )
    monkeypatch.setattr(
        settings,
        "onvif_site_allowed_cidrs_json",
        '{"tenant-a/site-a":["10.3.0.0/16"]}',
    )
    created = []

    def factory(**_kwargs):
        created.append(True)
        raise AssertionError("network client must not be created")

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", factory)
    with pytest.raises(TargetNotAllowed):
        asyncio.run(onvif_events.pull_messages(subscription, "qa-user", "qa-password"))
    assert created == []


def test_qa_dns_change_cannot_move_existing_pinned_subscription(monkeypatch):
    """Keep an existing subscription on its stored approved IP despite DNS movement."""
    subscription = onvif_events.PullPointSubscription(
        address="http://10.1.0.9/onvif/subscription",
        tenant_id="tenant-a",
        site_id="site-a",
        logical_address="http://pullpoint.example/onvif/subscription",
        request_host_header="pullpoint.example",
        tls_server_name="pullpoint.example",
    )
    response = _TrackingStream([b"<Envelope/>"])
    observed = []

    def handler(request):
        observed.append((str(request.url), request.headers.get("host")))
        return httpx.Response(200, stream=response)

    def unexpected_dns(*_args, **_kwargs):
        raise AssertionError("stored logical subscription hostname must not be re-resolved")

    monkeypatch.setattr(network_policy.socket, "getaddrinfo", unexpected_dns)
    _install_mock_transport(monkeypatch, handler)
    root = asyncio.run(
        onvif_events.pull_messages(subscription, "qa-user", "qa-password")
    )
    assert root.tag == "Envelope"
    assert observed == [
        ("http://10.1.0.9/onvif/subscription", "pullpoint.example")
    ]


def test_qa_unscoped_shared_soap_compatibility_remains_global(monkeypatch):
    """Preserve deliberate non-event global-policy compatibility for shared SOAP."""
    body = _TrackingStream([b"<Envelope/>"])
    observed = []

    def handler(request):
        observed.append(str(request.url))
        return httpx.Response(200, stream=body)

    _install_mock_transport(monkeypatch, handler)
    root = asyncio.run(
        onvif_client._soap(
            "http://10.9.0.8/onvif/device_service",
            "urn:qa-unscoped",
            "<tds:GetDeviceInformation/>",
            None,
            None,
        )
    )
    assert root.tag == "Envelope"
    assert observed == ["http://10.9.0.8/onvif/device_service"]


def _write_tls_material(tmp_path):
    """Create an ephemeral trusted loopback certificate for camera-a.example."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "camera-a.example")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("camera-a.example")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "qa-cert.pem"
    key_path = tmp_path / "qa-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_qa_real_tls_handshake_uses_logical_sni_and_host(monkeypatch, tmp_path):
    """Verify pinned-IP HTTPS keeps normal verification against the logical hostname."""
    cert_path, key_path = _write_tls_material(tmp_path)
    seen_hosts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen_hosts.append(self.headers.get("Host"))
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = b"<Envelope/>"
            self.send_response(200)
            self.send_header("Content-Type", "application/soap+xml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client_context = ssl.create_default_context(cafile=str(cert_path))
    original_client = _REAL_ASYNC_CLIENT

    def client_factory(**kwargs):
        kwargs["verify"] = client_context
        return original_client(**kwargs)

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(
        network_policy,
        "resolve_site_pinned_host",
        lambda host, tenant_id, site_id: (
            "127.0.0.1"
            if (host, tenant_id, site_id) == ("camera-a.example", "tenant-a", "site-a")
            else (_ for _ in ()).throw(AssertionError("unexpected pin request"))
        ),
    )

    port = server.server_address[1]
    try:
        root = asyncio.run(
            onvif_client._soap(
                f"https://camera-a.example:{port}/onvif/events",
                "urn:qa-tls",
                "<tds:GetDeviceInformation/>",
                "qa-user",
                "qa-password",
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert root.tag == "Envelope"
    assert seen_hosts == [f"camera-a.example:{port}"]


def test_qa_absent_site_policy_fails_closed_for_event_service(monkeypatch):
    """Reject an event service when its exact tenant/site CIDR policy is absent."""
    monkeypatch.setattr(settings, "onvif_site_allowed_cidrs_json", "{}")
    services = [
        {
            "namespace": "http://www.onvif.org/ver10/events/wsdl",
            "xaddr": "http://10.1.0.8/onvif/events",
        }
    ]
    with pytest.raises(TargetNotAllowed):
        onvif_events.event_service_xaddr(
            services,
            tenant_id="tenant-a",
            site_id="site-a",
        )


def test_qa_real_wait_for_bounds_active_digest_challenge(monkeypatch):
    """Prove the real scheduler deadline also bounds challenge-body processing."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 1_000_000)
    challenge = _TrackingStream(active=True)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
            stream=challenge,
        )

    _install_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call(operation_timeout_seconds=0.05))

    assert error.value.code == "NETWORK_UNREACHABLE"
    assert challenge.yielded > 1
    assert challenge.closed is True
    assert len(requests) == 1
