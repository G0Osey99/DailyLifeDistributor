"""Generate the agent's release-signing keypair.

Run ONCE:
    python scripts/generate_release_keypair.py

Writes:
- agent/release_pubkey.pem      <- commit this to the repo.
- ~/.dld-keys/release_private.pem  <- DO NOT commit. Upload its bytes verbatim
                                      to the GHA secret AGENT_RELEASE_PRIVATE_KEY.

The private key goes to a file (not stdout) on purpose: terminal copy/paste
on Windows tends to introduce CRLF line endings, which `cryptography` rejects
with "MalformedFraming" when GHA tries to load the secret. Always upload from
the file so the bytes survive end-to-end.
"""
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _private_key_path() -> Path:
    return Path(os.path.expanduser("~")) / ".dld-keys" / "release_private.pem"


def main() -> None:
    priv = Ed25519PrivateKey.generate()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    pub_path = Path("agent") / "release_pubkey.pem"
    pub_path.write_bytes(pub_pem)

    priv_path = _private_key_path()
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    priv_path.write_bytes(priv_pem)
    # Best-effort POSIX permissions (no-op on Windows; that's fine).
    try:
        os.chmod(priv_path, 0o600)
    except OSError:
        pass

    print(f"Wrote {pub_path} - commit this.")
    print(f"Wrote {priv_path} - DO NOT commit.")
    print()
    print("Upload the PRIVATE key to GHA secret AGENT_RELEASE_PRIVATE_KEY:")
    print()
    print("  Newer gh (>=2.40):")
    print(f"    gh secret set AGENT_RELEASE_PRIVATE_KEY --body-file \"{priv_path}\"")
    print()
    print("  Older gh (PowerShell, normalizes line endings to LF):")
    print("    $k = (Get-Content -Raw \"" + str(priv_path).replace("\\", "\\\\")
          + "\") -replace \"`r`n\",\"`n\" -replace \"`r\",\"`n\"")
    print("    gh secret set AGENT_RELEASE_PRIVATE_KEY --body $k")


if __name__ == "__main__":
    main()
