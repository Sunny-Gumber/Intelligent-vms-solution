"""Security defaults must stay strong even though tests lower them.

The test suite deliberately reduces Argon2 cost for speed. These tests assert
that the SHIPPED defaults are strong and that production mode refuses weak or
unsafe configuration, so the convenience of fast tests can never silently
become the production posture.
"""

from __future__ import annotations

import pytest
from app.config import Settings


def _production(**overrides):
    base = {"environment": "production",
            "secret_key": "a-sufficiently-long-production-secret-key-value",
            "database_url": "postgresql+psycopg://vms:pw@db/vms",
            "trusted_hosts": ["vms.example.com"]}
    base.update(overrides)
    return Settings(**base)


def test_shipped_argon2_defaults_follow_owasp():
    defaults = Settings(_env_file=None)
    assert defaults.password_hash_memory_kib >= 19456
    assert defaults.password_hash_time_cost >= 2
    assert defaults.password_min_length >= 12


def test_production_requires_strong_secret():
    with pytest.raises(ValueError, match="VMS_SECRET_KEY"):
        _production(secret_key="short")


def test_production_rejects_sqlite():
    with pytest.raises(ValueError, match="SQLite is not supported in production"):
        _production(database_url="sqlite+pysqlite:///./vms.db")


def test_production_rejects_wildcard_trusted_hosts():
    with pytest.raises(ValueError, match="VMS_TRUSTED_HOSTS"):
        _production(trusted_hosts=["*"])


def test_production_rejects_weak_password_hashing():
    with pytest.raises(ValueError, match="OWASP minimum"):
        _production(password_hash_memory_kib=8192)


def test_production_rejects_unimplemented_ldap():
    with pytest.raises(ValueError, match="LDAP"):
        _production(ldap_enabled=True)


def test_development_defaults_are_permissive_but_sqlite_only_locally():
    development = Settings(_env_file=None)
    assert development.environment == "development"
    assert development.is_sqlite
