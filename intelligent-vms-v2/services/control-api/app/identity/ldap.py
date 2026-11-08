"""LDAP / Active Directory integration — DESIGNED, NOT IMPLEMENTED.

This module exists to make the gap explicit and to fix the interface a future
implementation must satisfy. Nothing here pretends to work: every method
raises NotImplementedFeatureError, and app/config.py refuses to start in
production with `ldap_enabled=true`.

Implementation plan (docs/decisions/ADR-0011-identity-providers.md):
  * dependency: `ldap3` (LGPL, redistributable)
  * bind strategy: service-account search, then user bind
  * group mapping: AD group DN -> Intelligent VMS role
  * TLS: StartTLS required, certificate validation mandatory
  * failure mode: an LDAP outage must NOT lock out local administrators
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.errors import NotImplementedFeatureError


@dataclass
class LdapConfig:
    url: str
    base_dn: str
    bind_dn: str | None = None
    bind_password: str | None = None
    user_filter: str = "(sAMAccountName={username})"
    group_attribute: str = "memberOf"
    group_role_map: dict[str, str] = field(default_factory=dict)
    start_tls: bool = True
    timeout_s: float = 10.0


@dataclass
class LdapIdentity:
    distinguished_name: str
    username: str
    display_name: str | None
    email: str | None
    groups: list[str]


class LdapProvider:
    """Interface placeholder. Methods raise rather than return fake data."""

    name = "ldap"

    def __init__(self, config: LdapConfig) -> None:
        self.config = config

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        raise NotImplementedFeatureError(
            "LDAP/Active Directory authentication is designed but not implemented "
            "in this release. Use local accounts or OIDC. See docs/LIMITATIONS.md.",
            code="ldap_not_implemented")

    def search_user(self, username: str) -> LdapIdentity:
        raise NotImplementedFeatureError(
            "LDAP directory search is not implemented in this release.",
            code="ldap_not_implemented")

    def map_roles(self, identity: LdapIdentity) -> list[str]:
        raise NotImplementedFeatureError(
            "LDAP group-to-role mapping is not implemented in this release.",
            code="ldap_not_implemented")
