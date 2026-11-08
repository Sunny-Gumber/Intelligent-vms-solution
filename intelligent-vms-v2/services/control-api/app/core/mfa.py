"""TOTP multi-factor authentication (RFC 6238).

Implemented on hmac/hashlib so the product carries no extra dependency.
Secrets are stored encrypted with the credential vault. Replay inside the
validity window is rejected by remembering the last accepted counter.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote

DIGITS = 6
PERIOD = 30
ALGORITHM = "SHA1"   # RFC 6238 default; what authenticator apps expect


def generate_secret(length: int = 20) -> str:
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = DIGITS) -> str:
    padding = "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(secret_b32.upper() + padding, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_now(secret_b32: str, at: float | None = None, period: int = PERIOD) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // period))


def verify_totp(secret_b32: str, code: str, *, at: float | None = None,
                window: int = 1, last_counter: int | None = None) -> tuple[bool, int | None]:
    """Verify allowing +/- `window` steps for clock drift.

    Returns (ok, accepted_counter). The caller must persist accepted_counter and
    pass it back as `last_counter` to prevent replay within the window.
    """
    if not code or not code.isdigit() or len(code) != DIGITS:
        return False, None
    current = int((at if at is not None else time.time()) // PERIOD)
    for drift in range(-window, window + 1):
        counter = current + drift
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(_hotp(secret_b32, counter), code):
            return True, counter
    return False, None


@dataclass(frozen=True)
class MfaEnrollment:
    secret: str
    uri: str
    digits: int = DIGITS
    period: int = PERIOD


def enrollment(username: str, issuer: str = "Intelligent VMS") -> MfaEnrollment:
    secret = generate_secret()
    label = quote(f"{issuer}:{username}")
    uri = (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
           f"&algorithm={ALGORITHM}&digits={DIGITS}&period={PERIOD}")
    return MfaEnrollment(secret=secret, uri=uri)


def backup_codes(count: int = 10) -> list[str]:
    return [base64.b32encode(os.urandom(5)).decode("ascii").rstrip("=") for _ in range(count)]
