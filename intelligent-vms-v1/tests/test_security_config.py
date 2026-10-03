import pytest
from cryptography.fernet import InvalidToken

from app.core.config import Settings, settings
from app.core.security import decrypt_secret, encrypt_secret


EXPLICIT_TEST_SECRET = "0123456789abcdef0123456789abcdef"


def test_settings_has_no_usable_secret_default():
    """Verify the settings model does not embed a credential-encryption key."""
    assert Settings.model_fields["vms_secret_key"].default == ""


def test_secret_round_trip_uses_explicit_configured_key(monkeypatch):
    """Encrypt and decrypt credentials only after an explicit key is supplied."""
    monkeypatch.setattr(settings, "vms_secret_key", EXPLICIT_TEST_SECRET)

    encrypted = encrypt_secret("camera-password")

    assert encrypted is not None
    assert encrypted != "camera-password"
    assert decrypt_secret(encrypted) == "camera-password"


def test_empty_secret_values_do_not_require_encryption_key(monkeypatch):
    """Treat absent credentials as absent without requiring encryption setup."""
    monkeypatch.setattr(settings, "vms_secret_key", "")

    assert encrypt_secret(None) is None
    assert encrypt_secret("") is None
    assert decrypt_secret(None) is None
    assert decrypt_secret("") is None


def test_encrypt_secret_fails_closed_without_configured_key(monkeypatch):
    """Reject credential encryption when VMS_SECRET_KEY is missing."""
    monkeypatch.setattr(settings, "vms_secret_key", "")

    with pytest.raises(RuntimeError, match="VMS_SECRET_KEY must be configured"):
        encrypt_secret("camera-password")


def test_decrypt_secret_fails_closed_without_configured_key(monkeypatch):
    """Reject credential decryption when VMS_SECRET_KEY is missing."""
    monkeypatch.setattr(settings, "vms_secret_key", "")

    with pytest.raises(RuntimeError, match="VMS_SECRET_KEY must be configured"):
        decrypt_secret("not-a-real-token")


def test_decrypt_secret_rejects_token_from_different_key(monkeypatch):
    """Reject ciphertext that was created with a different configured key."""
    monkeypatch.setattr(settings, "vms_secret_key", EXPLICIT_TEST_SECRET)
    encrypted = encrypt_secret("camera-password")
    monkeypatch.setattr(settings, "vms_secret_key", "abcdef0123456789abcdef0123456789")

    with pytest.raises(InvalidToken):
        decrypt_secret(encrypted)
