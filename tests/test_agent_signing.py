from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from agent import signing


def _fresh_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, pub_pem


def test_sha256_hex_of_bytes():
    assert signing.sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert signing.sha256(b"hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_verify_signature_accepts_real_signature():
    priv, pub_pem = _fresh_keypair()
    data = b"release-payload"
    sig = priv.sign(data)
    assert signing.verify_signature(data, sig, pub_pem) is True


def test_verify_signature_rejects_tampered_payload():
    priv, pub_pem = _fresh_keypair()
    sig = priv.sign(b"original")
    assert signing.verify_signature(b"tampered", sig, pub_pem) is False


def test_verify_signature_rejects_wrong_key():
    _, pub_pem = _fresh_keypair()
    other_priv, _ = _fresh_keypair()
    sig = other_priv.sign(b"x")
    assert signing.verify_signature(b"x", sig, pub_pem) is False
