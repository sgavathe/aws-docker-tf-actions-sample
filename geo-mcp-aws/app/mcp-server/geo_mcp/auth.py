"""Token verification for the MCP server (Keycloak in the enclave, Cognito in AWS).

The server is an OAuth resource server only: the identity provider issues the tokens, we
verify signature, issuer, audience/client and expiry against its JWKS, then map roles
(Keycloak realm roles or Cognito groups) to MCP scopes. AUTH_PROVIDER picks the provider.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("geo_mcp.auth")

READER = "geo-reader"
WRITER = "geo-writer"
KNOWN_ROLES = {READER, WRITER}


@dataclass
class KeycloakConfig:
    issuer: str
    jwks_url: str
    audience: str

    @classmethod
    def from_env(cls) -> "KeycloakConfig":
        return cls(
            issuer=os.environ["KEYCLOAK_ISSUER"],
            jwks_url=os.environ["KEYCLOAK_JWKS_URL"],
            audience=os.environ.get("MCP_AUDIENCE", "geo-mcp"),
        )


def claims_to_access_token(token: str, claims: dict, audience: str) -> AccessToken:
    roles = set(claims.get("realm_access", {}).get("roles", [])) & KNOWN_ROLES
    return AccessToken(
        token=token,
        client_id=claims.get("azp") or claims.get("client_id") or "unknown",
        scopes=sorted(roles),
        expires_at=claims.get("exp"),
        resource=audience,
        subject=claims.get("sub"),
        claims={"iss": claims.get("iss"), "preferred_username": claims.get("preferred_username")},
    )


class KeycloakTokenVerifier:
    """Implements mcp.server.auth.provider.TokenVerifier."""

    def __init__(self, cfg: KeycloakConfig, jwks_client: PyJWKClient | None = None):
        self.cfg = cfg
        self.jwks = jwks_client or PyJWKClient(cfg.jwks_url, cache_keys=True, lifespan=300)

    def decode(self, token: str) -> dict:
        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.cfg.audience,
            issuer=self.cfg.issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=30,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = self.decode(token)
        except Exception as exc:  # any failure = unauthenticated (401)
            log.warning("token rejected: %s", type(exc).__name__)
            return None
        return claims_to_access_token(token, claims, self.cfg.audience)


def current_user() -> tuple[str, set[str]]:
    tok = get_access_token()
    if tok is None:
        return "anonymous", set()
    user = (tok.claims or {}).get("preferred_username") or tok.subject or tok.client_id
    return user, set(tok.scopes)


def require(role: str) -> str:
    """Raise unless the caller holds `role`. Returns the username for audit logging."""
    user, roles = current_user()
    if role not in roles:
        raise PermissionError(f"user '{user}' lacks role '{role}' required for this tool")
    return user


# ---------------------------------------------------------------- Cognito (AWS)

@dataclass
class CognitoConfig:
    issuer: str        # https://cognito-idp.<region>.amazonaws.com/<user pool id>
    client_id: str     # the chat app client; Cognito access tokens carry it as client_id (no aud)
    jwks_json: str     # the pool's public keys, passed in at deploy time: the MCP Lambda has no internet

    @classmethod
    def from_env(cls) -> "CognitoConfig":
        return cls(issuer=os.environ["TOKEN_ISSUER"], client_id=os.environ["COGNITO_CLIENT_ID"],
                   jwks_json=os.environ["COGNITO_JWKS"])


class CognitoTokenVerifier:
    """Verifies Cognito ACCESS tokens: RS256 signature against the pool's JWKS, issuer, expiry,
    token_use=access and client_id. Groups geo-reader / geo-writer become MCP scopes."""

    def __init__(self, cfg: CognitoConfig):
        self.cfg = cfg
        self.keys = {k["kid"]: jwt.PyJWK(k).key for k in json.loads(cfg.jwks_json)["keys"]}

    def decode(self, token: str) -> dict:
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.keys:
            raise jwt.InvalidKeyError("unknown signing key")
        claims = jwt.decode(token, self.keys[kid], algorithms=["RS256"], issuer=self.cfg.issuer,
                            options={"require": ["exp", "iat", "iss", "sub", "token_use", "client_id"],
                                     "verify_aud": False},  # access tokens have no aud; client_id below
                            leeway=30)
        if claims.get("token_use") != "access" or claims.get("client_id") != self.cfg.client_id:
            raise jwt.InvalidTokenError("not an access token for this client")
        return claims

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            c = self.decode(token)
        except Exception as exc:
            log.warning("token rejected: %s", type(exc).__name__)
            return None
        return AccessToken(
            token=token, client_id=c["client_id"], scopes=sorted(set(c.get("cognito:groups", [])) & KNOWN_ROLES),
            expires_at=c.get("exp"), resource=c["client_id"], subject=c.get("sub"),
            claims={"iss": c.get("iss"), "preferred_username": c.get("username")})


def make_verifier():
    if os.environ.get("AUTH_PROVIDER", "keycloak") == "cognito":
        return CognitoTokenVerifier(CognitoConfig.from_env())
    return KeycloakTokenVerifier(KeycloakConfig.from_env())
