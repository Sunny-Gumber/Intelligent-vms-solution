import base64
import hashlib

from cryptography.fernet import Fernet

from .config import settings


def _fernet() -> Fernet:
    secret_key = settings.vms_secret_key
    if not secret_key:
        raise RuntimeError(
            "VMS_SECRET_KEY must be configured before encrypting or decrypting credentials"
        )
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str | None) -> str | None:
    """Encrypt a non-empty credential using the configured VMS secret key.

    Args:
        value: Plaintext credential. Empty values are stored as None.

    Returns:
        The encrypted ASCII token, or None for an empty input.

    Raises:
        RuntimeError: If credential encryption is requested without
            VMS_SECRET_KEY configured.
    """
    if not value:
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a non-empty stored credential using the configured VMS secret key.

    Args:
        value: Encrypted ASCII token. Empty values are treated as absent.

    Returns:
        The decrypted plaintext credential, or None for an empty input.

    Raises:
        RuntimeError: If credential decryption is requested without
            VMS_SECRET_KEY configured.
        cryptography.fernet.InvalidToken: If the token cannot be decrypted by
            the configured key.
    """
    if not value:
        return None
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
