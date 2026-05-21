"""Unit tests for the Fernet master-key crypto core."""
import pytest

from core import crypto


def test_round_trip():
    token = crypto.encrypt(b"super secret")
    assert token != b"super secret"
    assert crypto.decrypt(token) == b"super secret"


def test_wrong_key_fails(monkeypatch):
    token = crypto.encrypt(b"data")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SECRET_ENC_KEY", Fernet.generate_key().decode())
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(token)


def test_tampered_token_fails():
    token = bytearray(crypto.encrypt(b"data"))
    token[-1] ^= 0x01  # flip a bit
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(bytes(token))


def test_missing_key_is_fatal(monkeypatch):
    monkeypatch.delenv("SECRET_ENC_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyError):
        crypto.validate_master_key()


def test_invalid_key_is_fatal(monkeypatch):
    monkeypatch.setenv("SECRET_ENC_KEY", "not-a-valid-fernet-key")
    with pytest.raises(crypto.MasterKeyError):
        crypto.validate_master_key()


def test_encrypt_without_key_is_fatal(monkeypatch):
    monkeypatch.delenv("SECRET_ENC_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyError):
        crypto.encrypt(b"x")


def test_decrypt_without_key_is_fatal(monkeypatch):
    monkeypatch.delenv("SECRET_ENC_KEY", raising=False)
    with pytest.raises(crypto.MasterKeyError):
        crypto.decrypt(b"sometoken")
