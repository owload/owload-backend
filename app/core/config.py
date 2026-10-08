from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Postgres
    database_url: str = "postgresql+asyncpg://owl:owl@localhost:5432/owl"

    # S3 / MinIO
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "owl-files"
    s3_use_ssl: bool = True

    # Keycloak (OIDC)
    keycloak_issuer: str = "https://keycloak.example.com/realms/owl"
    keycloak_audience: str = "account"
    keycloak_jwks_cache_seconds: int = 3600

    # CORS
    cors_allow_origins: list[str] = ["http://localhost:5173"]
    # Answer Chrome's private-network preflight (a page from one local address calling the API on another).
    # Off by default; only the listed origins are ever allowed. Turn it on for local development.
    cors_allow_private_network: bool = False

    # Account activity (sign-ins, sessions): where the client's IP address is read from, and how long events are kept.
    # With 0 the address is the one of the connection. Behind N reverse proxies that each append to X-Forwarded-For, set N:
    # the address N entries from the right of that header is used (the ones before it can be forged by the client).
    trusted_proxy_hops: int = 0
    activity_retention_days: int = 365

    # Infra-secret encryption (S3 credentials in s3_presets/drive_storage_targets.custom_config)
    # — a backend-only Fernet key, unrelated to the client's data-encryption key.
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    backend_secret_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
