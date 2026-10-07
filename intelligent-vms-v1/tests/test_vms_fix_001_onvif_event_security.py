"""VMS-FIX-001 ONVIF event confinement and bounded-response regressions."""

import asyncio
import socket

import httpx
import pytest
from defusedxml import ElementTree as DET

from app.core.config import settings
from app.services import network_policy, onvif_client, onvif_events
from app.services.network_policy import TargetNotAllowed
from app.services.onvif_client import OnvifError


EVENT_SERVICES = [
    {
        "namespace": "http://www.onvif.org/ver10/events/wsdl",
        "xaddr": "http://10.1.0.8/onvif/events",
    }
]


@pytest.fixture(autouse=True)
def _event_policy(monkeypatch):
    """Configure deterministic synthetic global and tenant/site camera networks."""
    monkeypatch.setattr(
        settings,
        "onvif_allowed_cidrs",
        "10.0.0.0/8,127.0.0.0/8,169.254.0.0/16",
    )
    monkeypatch.setattr(
        settings,
        "onvif_site_allowed_cidrs_json",
        (
            '{"tenant-a/site-a":["10.1.0.0/16"],'
            '"tenant-b/site-b":["10.2.0.0/16"]}'
        ),
    )
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 128)
    monkeypatch.setattr(settings, "onvif_connect_timeout_seconds", 1.0)
    monkeypatch.setattr(settings, "onvif_operation_timeout_seconds", 2.0)


def _subscription_root(address: str):
    return DET.fromstring(
        (
            '<Envelope xmlns:wsa="http://www.w3.org/2005/08/addressing">'
            "<SubscriptionReference>"
            f"<wsa:Address>{address}</wsa:Address>"
            "</SubscriptionReference>"
            "</Envelope>"
        ).encode()
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        chunks=(b"<Envelope/>",),
        headers=None,
        stream_error=None,
        block=False,
    ):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.stream_error = stream_error
        self.block = block
        self.closed = False
        self.yielded = 0
        self.started = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def aclose(self):
        self.closed = True

    async def aiter_bytes(self, chunk_size=None):
        del chunk_size
        self.started.set()
        if self.block:
            while True:
                await asyncio.sleep(3600)
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


class _FakeClient:
    def __init__(self, response, client_options, requests, **kwargs):
        client_options.append(kwargs)
        self.response = response
        self.requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, **_kwargs):
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "content": request.content,
                "headers": request.headers,
                "extensions": request.extensions,
            }
        )
        return self.response


def _install_client(monkeypatch, response):
    client_options = []
    requests = []

    def factory(**kwargs):
        return _FakeClient(response, client_options, requests, **kwargs)

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", factory)
    return client_options, requests


def _soap_call(**kwargs):
    return onvif_client._soap(
        "http://10.1.0.8/onvif/events",
        "urn:synthetic-action",
        "<tds:GetDeviceInformation/>",
        "synthetic-user",
        "synthetic-password",
        tenant_id="tenant-a",
        site_id="site-a",
        **kwargs,
    )


def test_cross_site_subscription_is_rejected_before_follow_up_credentials(monkeypatch):
    """Reject a globally allowed site-B PullPoint before follow-up credentials leave site A."""
    calls = []

    async def fake_soap(xaddr, action, body, username, password, **kwargs):
        calls.append(
            {
                "xaddr": xaddr,
                "action": action,
                "body": body,
                "username": username,
                "password": password,
                **kwargs,
            }
        )
        return _subscription_root("http://10.2.0.5/onvif/subscription")

    monkeypatch.setattr(onvif_events, "_soap", fake_soap)
    with pytest.raises(TargetNotAllowed):
        asyncio.run(
            onvif_events.create_pullpoint(
                "http://10.1.0.8/onvif/events",
                "synthetic-user",
                "synthetic-password",
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )

    assert len(calls) == 1
    assert calls[0]["xaddr"] == "http://10.1.0.8/onvif/events"
    assert all("10.2.0.5" not in call["xaddr"] for call in calls)


def test_same_site_event_lifecycle_preserves_scope_and_poll_headroom(monkeypatch):
    """Carry exact scope through create, sync, pull, retry-by-recreate and unsubscribe."""
    calls = []

    async def fake_soap(xaddr, action, body, username, password, **kwargs):
        calls.append((xaddr, action, username, password, kwargs))
        if action == onvif_events.CREATE_ACTION:
            return _subscription_root(
                "http://pullpoint.example/onvif/subscription"
            )
        return DET.fromstring(b"<Envelope/>")

    monkeypatch.setattr(
        network_policy.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.1.0.9", 0),
            )
        ],
    )
    monkeypatch.setattr(onvif_events, "_soap", fake_soap)
    event_xaddr = onvif_events.event_service_xaddr(
        EVENT_SERVICES,
        tenant_id="tenant-a",
        site_id="site-a",
    )
    first = asyncio.run(
        onvif_events.create_pullpoint(
            event_xaddr,
            "synthetic-user",
            "synthetic-password",
            tenant_id="tenant-a",
            site_id="site-a",
        )
    )
    asyncio.run(
        onvif_events.set_synchronization_point(
            first,
            "synthetic-user",
            "synthetic-password",
        )
    )
    asyncio.run(
        onvif_events.pull_messages(
            first,
            "synthetic-user",
            "synthetic-password",
            timeout="PT30S",
        )
    )
    retry = asyncio.run(
        onvif_events.create_pullpoint(
            event_xaddr,
            "synthetic-user",
            "synthetic-password",
            tenant_id="tenant-a",
            site_id="site-a",
        )
    )
    asyncio.run(
        onvif_events.unsubscribe(
            retry,
            "synthetic-user",
            "synthetic-password",
        )
    )

    assert first.address == "http://10.1.0.9/onvif/subscription"
    assert first.logical_address == "http://pullpoint.example/onvif/subscription"
    assert first.request_host_header == "pullpoint.example"
    assert first.tls_server_name == "pullpoint.example"
    assert retry.tenant_id == "tenant-a" and retry.site_id == "site-a"
    assert all(call[4]["tenant_id"] == "tenant-a" for call in calls)
    assert all(call[4]["site_id"] == "site-a" for call in calls)
    subscription_calls = [
        call for call in calls if call[1] != onvif_events.CREATE_ACTION
    ]
    assert all(
        call[4]["request_host_header"] == "pullpoint.example"
        for call in subscription_calls
    )
    assert all(
        call[4]["tls_server_name"] == "pullpoint.example"
        for call in subscription_calls
    )
    pull_call = next(call for call in calls if call[1] == onvif_events.PULL_ACTION)
    assert pull_call[4]["operation_timeout_seconds"] == 35.0
    assert "pullpoint.example" in pull_call[4]["extra_header_xml"]


@pytest.mark.parametrize(
    ("tenant_id", "site_id", "xaddr"),
    [
        ("tenant-a", "site-a", "http://10.2.0.5/onvif/events"),
        ("tenant-b", "site-b", "http://10.1.0.8/onvif/events"),
        ("tenant-a", "site-a", "http://127.0.0.1/onvif/events"),
        ("tenant-a", "site-a", "http://169.254.10.20/onvif/events"),
    ],
)
def test_event_service_rejects_cross_scope_loopback_and_link_local(
    tenant_id,
    site_id,
    xaddr,
):
    """Reject globally allowed endpoints that are outside the exact camera site."""
    services = [{"namespace": EVENT_SERVICES[0]["namespace"], "xaddr": xaddr}]
    with pytest.raises(TargetNotAllowed):
        onvif_events.event_service_xaddr(
            services,
            tenant_id=tenant_id,
            site_id=site_id,
        )


def test_event_apis_fail_closed_without_required_scope(monkeypatch):
    """Prevent event operations from silently dropping to global-only validation."""
    created = []

    def client_factory(**_kwargs):
        created.append(True)
        raise AssertionError("network client must not be created")

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", client_factory)
    with pytest.raises(TypeError):
        onvif_events.event_service_xaddr(EVENT_SERVICES)
    with pytest.raises(TypeError):
        onvif_events.create_pullpoint(
            "http://10.1.0.8/onvif/events",
            "synthetic-user",
            "synthetic-password",
        )
    with pytest.raises(TargetNotAllowed):
        asyncio.run(
            onvif_client._soap(
                "http://10.1.0.8/onvif/events",
                "urn:synthetic-action",
                "<tds:GetDeviceInformation/>",
                "synthetic-user",
                "synthetic-password",
                tenant_id="tenant-a",
            )
        )
    assert created == []


def test_dns_pin_preserves_http_host_and_https_sni(monkeypatch):
    """Connect to one approved IP while retaining the camera hostname authority."""
    resolutions = []

    def resolver(host, *_args, **_kwargs):
        resolutions.append(host)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.1.0.8", 0),
            )
        ]

    monkeypatch.setattr(network_policy.socket, "getaddrinfo", resolver)
    services = [
        {
            "namespace": EVENT_SERVICES[0]["namespace"],
            "xaddr": "https://camera-a.example:8443/onvif/events",
        }
    ]
    logical = onvif_events.event_service_xaddr(
        services,
        tenant_id="tenant-a",
        site_id="site-a",
    )
    assert logical == "https://camera-a.example:8443/onvif/events"

    response = _FakeResponse()
    client_options, requests = _install_client(monkeypatch, response)
    asyncio.run(
        onvif_client._soap(
            logical,
            "urn:synthetic-action",
            "<tds:GetDeviceInformation/>",
            "synthetic-user",
            "synthetic-password",
            tenant_id="tenant-a",
            site_id="site-a",
        )
    )

    assert resolutions == ["camera-a.example", "camera-a.example"]
    assert [request["url"] for request in requests] == [
        "https://10.1.0.8:8443/onvif/events"
    ]
    assert requests[0]["headers"]["Host"] == "camera-a.example:8443"
    assert requests[0]["extensions"]["sni_hostname"] == "camera-a.example"
    assert client_options[0]["follow_redirects"] is False
    assert "verify" not in client_options[0]
    assert b"synthetic-password" not in requests[0]["content"]


def test_dns_change_between_validation_and_request_is_refused(monkeypatch):
    """Reject a DNS change to another site before the HTTP client sees credentials."""
    resolutions = []
    created = []

    def resolver(host, *_args, **_kwargs):
        resolutions.append(host)
        address = "10.1.0.8" if len(resolutions) == 1 else "10.2.0.5"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 0),
            )
        ]

    def client_factory(**_kwargs):
        created.append(True)
        raise AssertionError("network client must not be created")

    monkeypatch.setattr(network_policy.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", client_factory)
    services = [
        {
            "namespace": EVENT_SERVICES[0]["namespace"],
            "xaddr": "http://camera-a.example/onvif/events",
        }
    ]
    logical = onvif_events.event_service_xaddr(
        services,
        tenant_id="tenant-a",
        site_id="site-a",
    )
    assert logical == "http://camera-a.example/onvif/events"

    with pytest.raises(TargetNotAllowed):
        asyncio.run(
            onvif_client._soap(
                logical,
                "urn:synthetic-action",
                "<tds:GetDeviceInformation/>",
                "synthetic-user",
                "synthetic-password",
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )

    assert resolutions == ["camera-a.example", "camera-a.example"]
    assert created == []


def test_cross_site_soap_target_is_refused_before_network_client(monkeypatch):
    """Prove the final credential boundary rejects another site's globally allowed IP."""
    created = []

    def client_factory(**_kwargs):
        created.append(True)
        raise AssertionError("network client must not be created")

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", client_factory)
    with pytest.raises(TargetNotAllowed) as error:
        asyncio.run(
            onvif_client._soap(
                "http://10.2.0.5/onvif/subscription",
                "urn:synthetic-action",
                "<tev:PullMessages/>",
                "synthetic-user",
                "synthetic-password",
                tenant_id="tenant-a",
                site_id="site-a",
            )
        )
    assert "synthetic-password" not in str(error.value)
    assert created == []


def test_redirect_is_not_followed_or_credentialed(monkeypatch):
    """Preserve the no-redirect ONVIF client contract for credential-bearing requests."""
    response = _FakeResponse(
        status_code=302,
        chunks=(),
        headers={"location": "http://10.2.0.5/onvif/escape"},
    )
    client_options, requests = _install_client(monkeypatch, response)
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert client_options[0]["follow_redirects"] is False
    assert len(requests) == 1
    assert requests[0]["url"] == "http://10.1.0.8/onvif/events"
    assert response.closed is True


def test_chunked_response_stops_during_stream_and_closes(monkeypatch):
    """Stop an oversized response without Content-Length before all chunks are read."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 8)
    response = _FakeResponse(chunks=(b"12345", b"67890", b"must-not-be-read"))
    _client_options, _requests = _install_client(monkeypatch, response)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert response.yielded == 2
    assert response.closed is True


def test_overall_deadline_stops_continuous_response_and_closes(monkeypatch):
    """Enforce a whole-operation deadline even while bytes keep arriving."""
    response = _FakeResponse(block=True)
    _client_options, _requests = _install_client(monkeypatch, response)
    observed_timeouts = []
    real_sleep = asyncio.sleep

    async def expire(awaitable, timeout):
        observed_timeouts.append(timeout)
        task = asyncio.create_task(awaitable)
        for _ in range(3):
            await real_sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        raise TimeoutError

    monkeypatch.setattr(onvif_client.asyncio, "wait_for", expire)
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call(operation_timeout_seconds=35.0))
    assert error.value.code == "NETWORK_UNREACHABLE"
    assert observed_timeouts == [35.0]
    assert response.closed is True


def test_external_cancellation_releases_stream_resources(monkeypatch):
    """Propagate cancellation while closing an in-flight streamed response."""
    response = _FakeResponse(block=True)
    _client_options, _requests = _install_client(monkeypatch, response)

    async def scenario():
        task = asyncio.create_task(_soap_call(operation_timeout_seconds=60.0))
        await response.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert response.closed is True


def test_stream_error_releases_resources_and_maps_network_failure(monkeypatch):
    """Close response resources when a streaming read fails."""
    response = _FakeResponse(
        chunks=(b"<Envelope",),
        stream_error=httpx.ReadError("synthetic read failure"),
    )
    _client_options, _requests = _install_client(monkeypatch, response)
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "NETWORK_UNREACHABLE"
    assert "synthetic read failure" not in str(error.value)
    assert response.closed is True


def test_small_response_and_auth_failure_remain_usable(monkeypatch):
    """Keep normal bounded SOAP parsing and approved authentication errors intact."""
    success = _FakeResponse(chunks=(b"<Envelope/>",))
    _client_options, requests = _install_client(monkeypatch, success)
    root = asyncio.run(_soap_call())
    assert root.tag == "Envelope"
    assert len(requests) == 1
    assert success.closed is True

    denied = _FakeResponse(status_code=401, chunks=(b"ignored",))
    _client_options, _requests = _install_client(monkeypatch, denied)
    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "AUTH_FAILED"
    assert denied.yielded == 0
    assert denied.closed is True


_REAL_HTTPX_ASYNC_CLIENT = httpx.AsyncClient
_DIGEST_CHALLENGE = 'Digest realm="synthetic", nonce="nonce-1", algorithm=MD5, qop="auth"'


class _TrackingAsyncByteStream(httpx.AsyncByteStream):
    """Track deterministic response-stream consumption and cleanup."""

    def __init__(self, chunks=(), *, stream_error=None, block=False):
        self.chunks = list(chunks)
        self.stream_error = stream_error
        self.block = block
        self.closed = False
        self.yielded = 0
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error
        if self.block:
            while True:
                await asyncio.sleep(3600)

    async def aclose(self):
        self.closed = True


def _install_real_httpx_mock_transport(monkeypatch, handler):
    """Inject only MockTransport while retaining real AsyncClient and DigestAuth."""
    transport = httpx.MockTransport(handler)
    clients = []

    def factory(**kwargs):
        kwargs["transport"] = transport
        client = _REAL_HTTPX_ASYNC_CLIENT(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(onvif_client.httpx, "AsyncClient", factory)
    return clients


def test_digest_chunked_challenge_is_bounded_before_authenticated_retry(monkeypatch):
    """Reject an oversized chunked Digest challenge before full body consumption."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 16)
    challenge = _TrackingAsyncByteStream([b"x" * 32] * 6)
    success = _TrackingAsyncByteStream([b"<Envelope/>"])
    requests = []

    def handler(request):
        requests.append(request)
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
                stream=challenge,
            )
        return httpx.Response(200, stream=success)

    _install_real_httpx_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert challenge.yielded < 6
    assert challenge.closed is True
    assert success.yielded == 0
    assert len(requests) == 1


def test_digest_declared_oversized_challenge_is_rejected_without_body_read(monkeypatch):
    """Reject an oversized declared Digest challenge before consuming its stream."""
    monkeypatch.setattr(settings, "onvif_max_response_bytes", 16)
    challenge = _TrackingAsyncByteStream([b"x" * 32] * 6)
    success = _TrackingAsyncByteStream([b"<Envelope/>"])
    requests = []

    def handler(request):
        requests.append(request)
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": _DIGEST_CHALLENGE,
                    "Content-Length": "192",
                },
                stream=challenge,
            )
        return httpx.Response(200, stream=success)

    _install_real_httpx_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert challenge.yielded == 0
    assert challenge.closed is True
    assert success.yielded == 0
    assert len(requests) == 1


def test_digest_small_challenge_then_authenticated_soap_succeeds(monkeypatch):
    """Preserve real HTTPX Digest challenge/retry behavior for bounded responses."""
    challenge = _TrackingAsyncByteStream([b"challenge"])
    success = _TrackingAsyncByteStream([b"<Envelope/>"])
    requests = []

    def handler(request):
        requests.append(request.headers.get("authorization"))
        if request.headers.get("authorization") is None:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
                stream=challenge,
            )
        return httpx.Response(200, stream=success)

    _install_real_httpx_mock_transport(monkeypatch, handler)

    root = asyncio.run(_soap_call())
    assert root.tag == "Envelope"
    assert len(requests) == 2
    assert requests[0] is None
    assert requests[1].startswith("Digest ")
    assert challenge.yielded == 1 and challenge.closed is True
    assert success.yielded == 1 and success.closed is True


def test_digest_rejected_credentials_map_auth_failure_and_close(monkeypatch):
    """Keep rejected real Digest credentials safe and close both response streams."""
    challenge = _TrackingAsyncByteStream([b"challenge"])
    rejected = _TrackingAsyncByteStream([b"denied"])
    requests = []

    def handler(request):
        requests.append(request)
        if "authorization" not in request.headers:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
                stream=challenge,
            )
        return httpx.Response(401, stream=rejected)

    _install_real_httpx_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "AUTH_FAILED"
    assert len(requests) == 2
    assert challenge.closed is True
    assert rejected.closed is True
    assert "synthetic-password" not in str(error.value)


def test_digest_challenge_read_failure_closes_and_maps_safely(monkeypatch):
    """Close an intermediate Digest challenge when its streamed body read fails."""
    challenge = _TrackingAsyncByteStream(
        [b"x"],
        stream_error=httpx.ReadError("synthetic challenge read failure"),
    )

    def handler(_request):
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
            stream=challenge,
        )

    _install_real_httpx_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "NETWORK_UNREACHABLE"
    assert "synthetic challenge read failure" not in str(error.value)
    assert challenge.closed is True


def test_digest_challenge_cancellation_closes_real_httpx_stream(monkeypatch):
    """Propagate cancellation while closing a real HTTPX Digest challenge stream."""
    challenge = _TrackingAsyncByteStream(block=True)

    def handler(_request):
        return httpx.Response(
            401,
            headers={"WWW-Authenticate": _DIGEST_CHALLENGE},
            stream=challenge,
        )

    _install_real_httpx_mock_transport(monkeypatch, handler)

    async def scenario():
        task = asyncio.create_task(_soap_call(operation_timeout_seconds=60.0))
        await challenge.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert challenge.closed is True


def test_soap_requests_identity_content_coding_and_rejects_encoded_body(monkeypatch):
    """Prevent hidden content-decoder expansion outside the configured body cap."""
    encoded = _TrackingAsyncByteStream([b"synthetic-compressed-body"])
    observed_accept_encoding = []

    def handler(request):
        observed_accept_encoding.append(request.headers.get("accept-encoding"))
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=encoded,
        )

    _install_real_httpx_mock_transport(monkeypatch, handler)

    with pytest.raises(OnvifError) as error:
        asyncio.run(_soap_call())
    assert error.value.code == "DEVICE_SERVICE_INVALID"
    assert observed_accept_encoding == ["identity"]
    assert encoded.yielded == 0
    assert encoded.closed is True
