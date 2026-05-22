"""Generate the agent's release-signing keypair.

Run ONCE:
    python scripts/generate_release_keypair.py

Writes the public key to agent/release_pubkey.pem (commit it).
Prints the private key to stdout — copy it into the GitHub Actions repository
secret named AGENT_RELEASE_PRIVATE_KEY. DO NOT commit the private key.
"""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


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
    with open("agent/release_pubkey.pem", "wb") as f:
        f.write(pub_pem)
    print("Wrote agent/release_pubkey.pem - commit it.")
    print()
    print("Add this PRIVATE key to GHA secret AGENT_RELEASE_PRIVATE_KEY:")
    print("-" * 60)
    print(priv_pem.decode("ascii"))


if __name__ == "__main__":
    main()
