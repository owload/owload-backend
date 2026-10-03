import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt
from jose.utils import long_to_base64

from app.core import security
from app.core.config import Settings

pytestmark = pytest.mark.asyncio

ISSUER = "https://kc.example.com/realms/test"
AUDIENCE = "owl-backend"
KID = "test-key-1"


def _b64(n: int) -> str:
    return long_to_base64(n).decode()


@pytest.fixture
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwk_dict = {
        "kty": "RSA",
        "kid": KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(public_numbers.n),
        "e": _b64(public_numbers.e),
    }
    return private_key, jwk_dict


def _make_token(private_key, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KID})


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    settings = Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    security._jwks_cache = None
    yield
    security._jwks_cache = None


async def _patch_jwks(monkeypatch, jwk_dict):
    cache = security._get_jwks_cache(Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE))

    async def fake_get_keys():
        return {"keys": [jwk_dict]}

    monkeypatch.setattr(cache, "get_keys", fake_get_keys)


async def test_valid_token_returns_current_user(monkeypatch, rsa_keypair):
    private_key, jwk_dict = rsa_keypair
    await _patch_jwks(monkeypatch, jwk_dict)

    token = _make_token(
        private_key,
        {
            "sub": "user-123",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": 9999999999,
            "preferred_username": "alice",
            "email": "alice@example.com",
        },
    )
    user = await security.get_current_user(token, Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE))

    assert user.sub == "user-123"
    assert user.username == "alice"
    assert user.email == "alice@example.com"


async def test_wrong_audience_is_rejected(monkeypatch, rsa_keypair):
    private_key, jwk_dict = rsa_keypair
    await _patch_jwks(monkeypatch, jwk_dict)

    token = _make_token(
        private_key,
        {"sub": "user-123", "iss": ISSUER, "aud": "some-other-client", "exp": 9999999999},
    )
    with pytest.raises(HTTPException) as exc_info:
        await security.get_current_user(token, Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE))
    assert exc_info.value.status_code == 401


async def test_missing_header_is_rejected():
    with pytest.raises(HTTPException) as exc_info:
        await security.get_current_user(None, Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE))
    assert exc_info.value.status_code == 401


async def test_expired_token_is_rejected(monkeypatch, rsa_keypair):
    private_key, jwk_dict = rsa_keypair
    await _patch_jwks(monkeypatch, jwk_dict)

    token = _make_token(
        private_key,
        {"sub": "user-123", "iss": ISSUER, "aud": AUDIENCE, "exp": 1},
    )

    with pytest.raises(HTTPException) as exc_info:
        await security.get_current_user(token, Settings(keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE))
    assert exc_info.value.status_code == 401
