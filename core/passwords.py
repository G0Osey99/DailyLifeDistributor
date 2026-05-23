"""Password policy: Argon2id (delegated to user_store for hashing) + pwned check.

`user_store` is the canonical place for hashing/verifying — this module keeps
the *policy* (min length + pwned-top-10k membership) so blueprints can validate
a password before forwarding to user_store.create_user / update_password.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

MIN_LENGTH = 12


@lru_cache(maxsize=1)
def _pwned_set() -> frozenset[str]:
    """Load the lowercased pwned-top-10k list, cached for the process lifetime."""
    p = (
        Path(__file__).resolve().parent.parent / "data" / "pwned_top_10k.txt"
    )
    if not p.exists():
        return frozenset()
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        return frozenset(
            line.strip().lower() for line in fh if line.strip()
        )


def is_pwned(plain: str) -> bool:
    """True iff *plain* appears in our local compromised-password list."""
    if not isinstance(plain, str):
        return False
    return plain.strip().lower() in _pwned_set()


def validate_password(plain: str) -> Optional[str]:
    """Return an error string if *plain* fails policy, else None.

    Policy:
      * length >= MIN_LENGTH (12)
      * not in the pwned-top-10k list
    """
    if not isinstance(plain, str):
        return "Password is required."
    if len(plain) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters."
    if is_pwned(plain):
        return (
            "This password appears in a list of common compromised "
            "passwords; pick another."
        )
    return None
