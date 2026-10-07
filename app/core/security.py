import time
from dataclasses import dataclass

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import jwt
from jose.exceptions import JOSEError

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class CurrentUser:
    sub: str
    username: str | None
    email: str | None
    # The display name from the identity provider (Google, SSO); absent after a plain registration.
    name: str | None = None


class KeycloakJWKSCache:
    """Fetches and caches the realm's JWKS so every request doesn't hit Keycloak."""

    def __init__(self, issuer: str, ttl_seconds: int) -> None:
        self._issuer = issuer.rstrip("/")
        self._ttl_seconds = ttl_seconds
        self._keys: dict | None = None
        self._fetched_at: float = 0.0

    async def get_keys(self) -> dict:
        now = time.monotonic()
        if self._keys is None or (now - self._fetched_at) > self._ttl_seconds:
            jwks_url = f"{self._issuer}/protocol/openid-connect/certs"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(jwks_url)
                resp.raise_for_status()
                self._keys = resp.json()
                self._fetched_at = now
        return self._keys

    def invalidate(self) -> None:
        self._keys = None


_jwks_cache: KeycloakJWKSCache | None = None


def _get_jwks_cache(settings: Settings) -> KeycloakJWKSCache:
    global _jwks_cache
    if _jwks_cache is None:
        _jwks_cache = KeycloakJWKSCache(settings.keycloak_issuer, settings.keycloak_jwks_cache_seconds)
    return _jwks_cache


async def get_current_user(
    x_access_token: str | None = Header(None, alias="x-access-token"),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    if not x_access_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing x-access-token header")

    token = x_access_token
    cache = _get_jwks_cache(settings)

    try:
        jwks = await cache.get_keys()
        unverified_header = jwt.get_unverified_header(token)
        key = next((k for k in jwks["keys"] if k["kid"] == unverified_header.get("kid")), None)
        if key is None:
            # kid rotated on Keycloak's side — refresh once and retry.
            cache.invalidate()
            jwks = await cache.get_keys()
            key = next((k for k in jwks["keys"] if k["kid"] == unverified_header.get("kid")), None)
        if key is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown signing key")

        claims = jwt.decode(
            token,
            key,
            algorithms=[key.get("alg", "RS256")],
            audience=settings.keycloak_audience,
            issuer=settings.keycloak_issuer.rstrip("/"),
            options={"require_exp": True, "require_sub": True},
        )
    except JOSEError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Unable to reach identity provider") from exc

    return CurrentUser(
        sub=claims["sub"],
        username=claims.get("preferred_username"),
        email=claims.get("email"),
        name=claims["name"] if isinstance(claims.get("name"), str) else None,
    )
