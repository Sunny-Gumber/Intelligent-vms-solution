"""Camera credential storage and retrieval.

The ONLY module permitted to decrypt camera secrets. Callers receive a
short-lived Credentials object; plaintext never reaches a DTO, log line, audit
record or API response.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.crypto import CredentialVault, DecryptionError
from app.core.timeutil import utcnow
from app.db.models import Camera, CameraCredential
from app.devices.base import Credentials
from app.logging_setup import get_logger

log = get_logger("devices.credentials")


class CredentialStore:
    def __init__(self, vault: CredentialVault) -> None:
        self._vault = vault

    def set(self, session: Session, camera: Camera, username: str | None,
            password: str | None) -> CameraCredential:
        record = camera.credential
        if record is None:
            record = CameraCredential(camera_id=camera.id)
            session.add(record)
            camera.credential = record
        record.username = username
        if password is not None:
            # AAD binds ciphertext to this camera: a blob copied to another row
            # fails authentication instead of silently decrypting.
            record.secret_blob = self._vault.encrypt(password, aad=f"camera:{camera.id}")
            record.key_id = self._vault.active_key_id
            record.rotated_at = utcnow()
        return record

    def get(self, camera: Camera) -> Credentials:
        record = camera.credential
        if record is None or not record.secret_blob:
            return Credentials(username=record.username if record else None, password=None)
        try:
            secret = self._vault.decrypt(record.secret_blob, aad=f"camera:{camera.id}")
        except DecryptionError as exc:
            log.error("camera.credential_undecryptable", camera_id=camera.id,
                      error=str(exc))
            raise
        return Credentials(username=record.username, password=secret)

    def has_secret(self, camera: Camera) -> bool:
        return bool(camera.credential and camera.credential.secret_blob)

    def clear(self, session: Session, camera: Camera) -> None:
        if camera.credential is not None:
            session.delete(camera.credential)
            camera.credential = None

    def encrypt_stream_url(self, camera_id: str, url: str) -> str:
        return self._vault.encrypt(url, aad=f"stream:{camera_id}")

    def decrypt_stream_url(self, camera_id: str, blob: str) -> str:
        return self._vault.decrypt(blob, aad=f"stream:{camera_id}")
