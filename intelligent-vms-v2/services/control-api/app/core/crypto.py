"""Credential vault and signing primitives.

Camera passwords are the crown jewels of a VMS: they grant direct access to
the customer's devices. They are encrypted at rest with AES-256-GCM using a
master key stored outside the database.

Key management
--------------
* Master key: 32 random bytes in <keys_dir>/vms-master.key, mode 0600
  (Windows: ACL restricted to SYSTEM + Administrators by the installer).
* Never written to the database, logs, API responses, or to backups taken by
  tools/backup_restore.py unless --include-keys is explicitly passed.
* Rotation supported: ciphertext carries a key id, so old secrets stay
  readable while new writes use the current key.

Ciphertext format, urlsafe-base64 of:
    b"v1" | key_id(4) | nonce(12) | ciphertext+tag
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"v1"
NONCE_LEN = 12
KEY_ID_LEN = 4


class CryptoError(RuntimeError):
    pass


class DecryptionError(CryptoError):
    pass


@dataclass(frozen=True)
class MasterKey:
    key_id: bytes      # 4 bytes
    key: bytes         # 32 bytes

    @staticmethod
    def generate() -> MasterKey:
        key = AESGCM.generate_key(bit_length=256)
        return MasterKey(key_id=hashlib.sha256(key).digest()[:KEY_ID_LEN], key=key)


def _secure_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if not sys.platform.startswith("win"):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def load_or_create_master_key(keys_dir: Path, filename: str = "vms-master.key") -> MasterKey:
    """Load the master key, creating it on first run.

    If the file is lost, every stored camera credential becomes unreadable.
    That is by design and is documented in docs/operations/disaster-recovery.md.
    """
    path = keys_dir / filename
    if path.is_file():
        raw = base64.urlsafe_b64decode(path.read_bytes().strip())
        if len(raw) != 32:
            raise CryptoError(f"master key at {path} is {len(raw)} bytes, expected 32")
        return MasterKey(key_id=hashlib.sha256(raw).digest()[:KEY_ID_LEN], key=raw)
    mk = MasterKey.generate()
    _secure_write(path, base64.urlsafe_b64encode(mk.key))
    return mk


class CredentialVault:
    """Encrypts with the active key; decrypts with any known key."""

    def __init__(self, active: MasterKey, retired: list[MasterKey] | None = None) -> None:
        self._active = active
        self._keys: dict[bytes, bytes] = {active.key_id: active.key}
        for key in retired or []:
            self._keys[key.key_id] = key.key

    @property
    def active_key_id(self) -> str:
        return self._active.key_id.hex()

    def encrypt(self, plaintext: str, aad: str | None = None) -> str:
        if plaintext is None:
            raise CryptoError("refusing to encrypt None")
        nonce = os.urandom(NONCE_LEN)
        blob = AESGCM(self._active.key).encrypt(
            nonce, plaintext.encode("utf-8"), aad.encode("utf-8") if aad else None)
        return base64.urlsafe_b64encode(
            MAGIC + self._active.key_id + nonce + blob).decode("ascii")

    def decrypt(self, token: str, aad: str | None = None) -> str:
        try:
            raw = base64.urlsafe_b64decode(token.encode("ascii"))
        except Exception as exc:
            raise DecryptionError("credential blob is not valid base64") from exc
        if not raw.startswith(MAGIC):
            raise DecryptionError("unsupported credential blob version")
        off = len(MAGIC)
        key_id = raw[off:off + KEY_ID_LEN]
        nonce = raw[off + KEY_ID_LEN:off + KEY_ID_LEN + NONCE_LEN]
        body = raw[off + KEY_ID_LEN + NONCE_LEN:]
        key = self._keys.get(key_id)
        if key is None:
            raise DecryptionError(
                f"credential encrypted with unknown key id {key_id.hex()}; "
                "the master key file may have been replaced")
        try:
            return AESGCM(key).decrypt(
                nonce, body, aad.encode("utf-8") if aad else None).decode("utf-8")
        except InvalidTag as exc:
            raise DecryptionError(
                "credential authentication failed (wrong key or tampering)") from exc

    def rotate(self, new_key: MasterKey, keys_dir: Path) -> None:
        self._keys[new_key.key_id] = new_key.key
        self._active = new_key
        _secure_write(keys_dir / "vms-master.key", base64.urlsafe_b64encode(new_key.key))


# -- opaque token hashing (refresh tokens, API keys, download tokens) -------
def new_opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str, pepper: str) -> str:
    """Fast keyed hash for high-entropy tokens.

    Argon2 is for human passwords; for 256-bit random tokens a keyed SHA-256 is
    sufficient and keeps per-request verification cheap.
    """
    return hmac.new(pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# -- Ed25519 signing (offline licence files, federation tokens) ------------
def generate_signing_keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    priv_bytes = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption())
    pub_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return priv_bytes, pub_bytes


def sign(private_key_raw: bytes, payload: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(private_key_raw).sign(payload)


def verify_signature(public_key_raw: bytes, payload: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(signature, payload)
        return True
    except Exception:
        return False


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Return (hex digest, size). Used for export evidence hashing."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
