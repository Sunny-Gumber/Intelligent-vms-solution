"""Unit tests for security, crypto, netguard, paths, backoff, circuit, MFA."""

from __future__ import annotations

import datetime as dt

import pytest
from app.core import mfa
from app.core.backoff import BackoffPolicy, RetryExhausted, retry_async
from app.core.circuit import BreakerState, CircuitBreaker
from app.core.crypto import (
    CredentialVault,
    DecryptionError,
    MasterKey,
    generate_signing_keypair,
    sign,
    verify_signature,
)
from app.core.netguard import GuardPolicy, NetworkGuard, NetworkGuardError
from app.core.paths import PathConfinementError, resolve_within, safe_name
from app.core.ratelimit import TokenBucketLimiter
from app.core.timeutil import clamp_range, ensure_utc, utcnow
from app.media.url_builder import (
    build_rtsp_url,
    contains_credentials,
    sanitize_for_display,
    with_credentials,
)


# -- crypto ----------------------------------------------------------------
def test_credential_vault_round_trip():
    vault = CredentialVault(MasterKey.generate())
    token = vault.encrypt("P@ssw0rd!", aad="camera:abc")
    assert "P@ssw0rd" not in token
    assert vault.decrypt(token, aad="camera:abc") == "P@ssw0rd!"


def test_credential_vault_rejects_wrong_aad():
    """A ciphertext copied to another camera row must not decrypt."""
    vault = CredentialVault(MasterKey.generate())
    token = vault.encrypt("secret", aad="camera:abc")
    with pytest.raises(DecryptionError):
        vault.decrypt(token, aad="camera:other")


def test_credential_vault_rejects_unknown_key():
    token = CredentialVault(MasterKey.generate()).encrypt("secret")
    with pytest.raises(DecryptionError):
        CredentialVault(MasterKey.generate()).decrypt(token)


def test_ed25519_signature_detects_tampering():
    private, public = generate_signing_keypair()
    payload = b'{"cameras":64}'
    signature = sign(private, payload)
    assert verify_signature(public, payload, signature)
    assert not verify_signature(public, b'{"cameras":640}', signature)


# -- password / token security --------------------------------------------
def test_password_hash_and_verify():
    from app.core.security import hash_password, verify_password

    stored = hash_password("CorrectHorse!42")
    assert stored.startswith("$argon2id$")
    assert verify_password("CorrectHorse!42", stored)
    assert not verify_password("wrong", stored)


def test_password_strength_rules(settings):
    from app.core.errors import ValidationError
    from app.core.security import validate_password_strength

    for weak in ("short", "password", "aaaaaaaaaaaaaaa"):
        with pytest.raises(ValidationError):
            validate_password_strength(weak, settings)
    with pytest.raises(ValidationError):
        validate_password_strength("Operator-Operator1", settings, username="operator")
    validate_password_strength("Str0ng-Passphrase!", settings, username="operator")


def test_access_token_round_trip(settings):
    from app.core.security import create_access_token, decode_access_token

    class FakeRole:
        name = "operator"

    class FakeLink:
        role = FakeRole()

    class FakeUser:
        id = "u1"
        username = "op"
        roles = (FakeLink(),)

    token, claims = create_access_token(FakeUser(), ["live.view"], settings)
    decoded = decode_access_token(token, settings)
    assert decoded.sub == "u1"
    assert "live.view" in decoded.permissions
    assert decoded.jti == claims.jti


def test_access_token_rejects_tampering(settings):
    from app.core.errors import AuthenticationError
    from app.core.security import create_access_token, decode_access_token

    class FakeUser:
        id = "u1"
        username = "op"
        roles = ()

    token, _ = create_access_token(FakeUser(), [], settings)
    head, body, signature = token.split(".")
    with pytest.raises(AuthenticationError):
        decode_access_token(f"{head}.{body}.{signature[:-2]}xx", settings)


# -- netguard (SSRF) -------------------------------------------------------
@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1"])
def test_netguard_blocks_dangerous_addresses(address):
    with pytest.raises(NetworkGuardError):
        NetworkGuard(GuardPolicy()).check_ip(address)


def test_netguard_allows_private_camera_network():
    assert str(NetworkGuard(GuardPolicy()).check_ip("192.168.1.50")) == "192.168.1.50"


def test_netguard_allow_list_excludes_other_ranges():
    guard = NetworkGuard(GuardPolicy(allow_cidrs=("10.20.0.0/16",)))
    guard.check_ip("10.20.5.5")
    with pytest.raises(NetworkGuardError):
        guard.check_ip("192.168.1.5")


def test_netguard_blocks_unlisted_ports():
    with pytest.raises(NetworkGuardError):
        NetworkGuard(GuardPolicy(allowed_ports=(80, 554))).check_port(22)


def test_netguard_dns_rebinding_pin():
    """Resolution happens once; the validated IP is what callers connect to."""
    guard = NetworkGuard(GuardPolicy())
    dest = guard.resolve_pinned("camera.local", 554,
                                resolver=lambda host, port: ["192.168.9.9"])
    assert dest.ip == "192.168.9.9"
    with pytest.raises(NetworkGuardError):
        guard.resolve_pinned("evil.example", 554,
                             resolver=lambda host, port: ["127.0.0.1"])


def test_netguard_url_validation_and_pinning():
    guard = NetworkGuard(GuardPolicy())
    pinned, dest = guard.pin_url("rtsp://user:pw@cam.local:554/stream1",
                                 resolver=lambda host, port: ["10.0.0.7"])
    assert "10.0.0.7" in pinned and dest.ip == "10.0.0.7"
    with pytest.raises(NetworkGuardError):
        guard.validate_url("file:///etc/passwd")


def test_discovery_range_is_capped():
    guard = NetworkGuard(GuardPolicy(max_discovery_hosts=256))
    assert len(guard.expand_discovery_range("192.168.1.0/24")) == 254
    with pytest.raises(NetworkGuardError):
        guard.expand_discovery_range("10.0.0.0/8")


# -- path confinement ------------------------------------------------------
def test_resolve_within_blocks_traversal(tmp_path):
    for bad in ("../../etc/passwd", "/etc/passwd"):
        with pytest.raises(PathConfinementError):
            resolve_within(tmp_path, bad)
    assert resolve_within(tmp_path, "a/b/c.mp4").is_relative_to(tmp_path.resolve())


def test_safe_name_rejects_separators():
    safe_name("segment-01.mp4")
    for bad in ("../x", "a/b", "", "."):
        with pytest.raises(PathConfinementError):
            safe_name(bad)


# -- url builder -----------------------------------------------------------
def test_rtsp_url_encodes_special_characters():
    url = build_rtsp_url(host="10.0.0.5", port=554, path="/stream1",
                         username="admin", password="p@ss:w/rd")
    assert "p%40ss%3Aw%2Frd" in url
    assert url.startswith("rtsp://admin:")


def test_sanitize_removes_credentials():
    url = "rtsp://admin:secret@10.0.0.5:554/s1"
    assert contains_credentials(url)
    clean = sanitize_for_display(url)
    assert "secret" not in clean and "admin" not in clean
    assert clean == "rtsp://10.0.0.5:554/s1"


def test_with_credentials_does_not_double_inject():
    url = with_credentials("rtsp://10.0.0.5:554/s1", "admin", "pw")
    assert with_credentials(url, "other", "x") == url


# -- backoff / circuit / limiter ------------------------------------------
def test_backoff_is_bounded_and_monotonic_without_jitter():
    policy = BackoffPolicy(initial_s=1, max_s=10, multiplier=2, jitter=False)
    assert policy.delays(6) == [1, 2, 4, 8, 10, 10]


def test_backoff_jitter_never_exceeds_cap():
    assert all(0 <= d <= 5
               for d in BackoffPolicy(initial_s=1, max_s=5, multiplier=2).delays(20))


async def test_retry_async_gives_up():
    attempts = {"n": 0}

    async def failing():
        attempts["n"] += 1
        raise RuntimeError("boom")

    async def no_sleep(_):
        return None

    with pytest.raises(RetryExhausted):
        await retry_async(failing, policy=BackoffPolicy(max_attempts=3), sleep=no_sleep)
    assert attempts["n"] == 3


def test_circuit_breaker_opens_and_recovers():
    clock = {"t": 0.0}
    breaker = CircuitBreaker("camera", failure_threshold=2, reset_timeout_s=10,
                             clock=lambda: clock["t"])
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allows()

    clock["t"] = 11
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_token_bucket_limits_then_refills():
    clock = {"t": 0.0}
    limiter = TokenBucketLimiter(60, burst=2, clock=lambda: clock["t"])
    assert limiter.check("k")[0]
    assert limiter.check("k")[0]
    allowed, retry_after = limiter.check("k")
    assert not allowed and retry_after > 0
    clock["t"] = 2.0
    assert limiter.check("k")[0]


# -- MFA -------------------------------------------------------------------
def test_totp_accepts_current_code_and_rejects_replay():
    secret = mfa.generate_secret()
    ok, counter = mfa.verify_totp(secret, mfa.totp_now(secret))
    assert ok
    assert not mfa.verify_totp(secret, mfa.totp_now(secret), last_counter=counter)[0]


def test_totp_rejects_malformed_code():
    secret = mfa.generate_secret()
    assert not mfa.verify_totp(secret, "abc")[0]
    assert not mfa.verify_totp(secret, "12345")[0]


# -- time ------------------------------------------------------------------
def test_ensure_utc_and_clamp():
    assert ensure_utc(dt.datetime(2026, 1, 1, 12, 0)).tzinfo is dt.UTC
    start = utcnow()
    _, end, clamped = clamp_range(start, start + dt.timedelta(hours=48), 3600)
    assert clamped and (end - start).total_seconds() == 3600
