"""OIDC / SSO authorization-code flow with PKCE.

STATUS: implemented; NOT tested against a real identity provider in this
environment. Discovery, state/nonce handling, PKCE, token exchange and ID
token validation (JWKS signature, issuer, audience, nonce, expiry) are all
implemented. Qualification against Entra ID / Keycloak is an external item in
docs/verification/external-qualification-plan.md.

Local accounts remain available, so SSO misconfiguration can never lock an
administrator out of the surveillance system.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.config import Settings, get_settings
from app.core.errors import AuthenticationError, ValidationError
from app.logging_setup import get_logger

log = get_logger("identity.oidc")


@dataclass
class AuthorizationRequest:
    url: str
    state: str
    nonce: str
    code_verifier: str
    expires_at: float


@dataclass
class OidcIdentity:
    subject: str
    username: str
    email: str | None = None
    display_name: str | None = None
    groups: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class OidcClient:
    def __init__(self, settings: Settings | None = None, *, http_client=None) -> None:  # noqa: ANN001
        self.settings = settings or get_settings()
        self._metadata: dict[str, Any] | None = None
        self._jwk_client: PyJWKClient | None = None
        self._http = http_client
        self._pending: dict[str, AuthorizationRequest] = {}

    def enabled(self) -> bool:
        return bool(self.settings.oidc_enabled and self.settings.oidc_issuer
                    and self.settings.oidc_client_id)

    async def metadata(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        if not self.settings.oidc_issuer:
            raise ValidationError("OIDC issuer is not configured",
                                  code="oidc_not_configured")
        url = self.settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        client = self._http or httpx.AsyncClient(timeout=10.0)
        async with client:
            response = await client.get(url)
        if response.status_code != 200:
            raise AuthenticationError(f"OIDC discovery failed ({response.status_code})",
                                      code="oidc_discovery_failed")
        self._metadata = response.json()
        return self._metadata

    async def authorization_request(self, *, scopes: str = "openid profile email"
                                    ) -> AuthorizationRequest:
        metadata = await self.metadata()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")

        params = {
            "response_type": "code",
            "client_id": self.settings.oidc_client_id,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "scope": scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }
        request = AuthorizationRequest(
            url=f"{metadata['authorization_endpoint']}?{urlencode(params)}",
            state=state, nonce=nonce, code_verifier=verifier,
            expires_at=time.time() + 600)
        self._pending[state] = request
        self._prune()
        return request

    def _prune(self) -> None:
        now = time.time()
        for state in [s for s, r in self._pending.items() if r.expires_at < now]:
            del self._pending[state]

    async def exchange(self, code: str, state: str) -> OidcIdentity:
        request = self._pending.pop(state, None)
        if request is None or request.expires_at < time.time():
            raise AuthenticationError("The sign-in request expired or is unknown",
                                      code="oidc_state_invalid")
        metadata = await self.metadata()

        data = {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": self.settings.oidc_redirect_uri,
            "client_id": self.settings.oidc_client_id,
            "code_verifier": request.code_verifier,
        }
        if self.settings.oidc_client_secret:
            data["client_secret"] = self.settings.oidc_client_secret

        client = self._http or httpx.AsyncClient(timeout=10.0)
        async with client:
            response = await client.post(metadata["token_endpoint"], data=data)
        if response.status_code != 200:
            raise AuthenticationError(
                "Identity provider rejected the authorization code",
                code="oidc_token_exchange_failed")

        id_token = response.json().get("id_token")
        if not id_token:
            raise AuthenticationError("Identity provider returned no ID token",
                                      code="oidc_no_id_token")
        claims = self.validate_id_token(id_token, metadata, request.nonce)

        return OidcIdentity(
            subject=claims["sub"],
            username=(claims.get("preferred_username") or claims.get("email")
                      or claims["sub"]),
            email=claims.get("email"), display_name=claims.get("name"),
            groups=list(claims.get("groups", []) or []), raw=claims)

    def validate_id_token(self, id_token: str, metadata: dict[str, Any],
                          nonce: str) -> dict[str, Any]:
        if self._jwk_client is None:
            self._jwk_client = PyJWKClient(metadata["jwks_uri"])
        signing_key = self._jwk_client.get_signing_key_from_jwt(id_token)
        try:
            claims = jwt.decode(
                id_token, signing_key.key, algorithms=["RS256", "ES256"],
                audience=self.settings.oidc_client_id,
                issuer=metadata.get("issuer", self.settings.oidc_issuer),
                options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(f"ID token is not valid: {exc}",
                                      code="oidc_token_invalid") from exc
        if claims.get("nonce") != nonce:
            raise AuthenticationError("ID token nonce mismatch",
                                      code="oidc_nonce_mismatch")
        return claims
