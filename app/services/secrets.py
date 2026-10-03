"""Encryption for infrastructure secrets — S3 credentials held in
`s3_presets`/`drive_storage_targets.custom_config` (see owload-docs/decisions/0011).

This is a backend-only secret, unrelated to the per-drive encryption key the
client derives from its password: the backend never sees that one, but it
does need to actually connect to S3 on the drive owner's behalf, so S3
credentials necessarily pass through here in a form the backend can decrypt.
"""

from cryptography.fernet import Fernet

from app.core.config import get_settings


def encrypt_secret(plaintext: str) -> str:
    return Fernet(get_settings().backend_secret_key.encode()).encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    return Fernet(get_settings().backend_secret_key.encode()).decrypt(token.encode()).decode()
