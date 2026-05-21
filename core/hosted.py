"""Single source of truth for 'are we the headless hosted VPS?'."""
import os


def is_hosted() -> bool:
    return (os.environ.get("HOSTED") or "").strip().lower() in ("1", "true", "yes")
