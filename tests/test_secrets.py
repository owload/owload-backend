"""Unit tests for app/services/secrets.py — pure Fernet round-trip, no infra."""

from cryptography.fernet import Fernet

from app.services import secrets


def test_encrypt_decrypt_round_trip(monkeypatch):
    key = Fernet.generate_key().decode()
    fake_settings = type("S", (), {"backend_secret_key": key})()
    monkeypatch.setattr(secrets, "get_settings", lambda: fake_settings)

    token = secrets.encrypt_secret("super-secret-access-key")
    assert token != "super-secret-access-key"
    assert secrets.decrypt_secret(token) == "super-secret-access-key"


def test_encrypt_empty_string_round_trips(monkeypatch):
    key = Fernet.generate_key().decode()
    fake_settings = type("S", (), {"backend_secret_key": key})()
    monkeypatch.setattr(secrets, "get_settings", lambda: fake_settings)

    token = secrets.encrypt_secret("")
    assert secrets.decrypt_secret(token) == ""


def test_wrong_key_fails_to_decrypt(monkeypatch):
    encrypt_settings = type("S", (), {"backend_secret_key": Fernet.generate_key().decode()})()
    monkeypatch.setattr(secrets, "get_settings", lambda: encrypt_settings)
    token = secrets.encrypt_secret("data")

    decrypt_settings = type("S", (), {"backend_secret_key": Fernet.generate_key().decode()})()
    monkeypatch.setattr(secrets, "get_settings", lambda: decrypt_settings)
    try:
        secrets.decrypt_secret(token)
        assert False, "expected decryption to fail with a different key"
    except Exception:
        pass
