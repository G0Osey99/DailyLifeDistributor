"""Hash + ed25519 signature verification used by the agent's auto-update.

The matching private key lives ONLY in the GHA release secret. The public
key is committed at agent/release_pubkey.pem and bundled by PyInstaller.
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_signature(data: bytes, sig: bytes, pubkey_pem: bytes) -> bool:
    """True iff `sig` is a valid ed25519 signature of `data` under `pubkey_pem`.

    Any structural failure (wrong key type, malformed PEM, bad signature)
    returns False — callers should treat False as "do not apply this update".
    """
    try:
        key = serialization.load_pem_public_key(pubkey_pem)
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(sig, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
