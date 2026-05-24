"""Print the status of every circuit breaker in the running process.

When to use:
    Triage a slow / hanging upload run. If an upstream is flapping (image
    provider, LLM, Resend, an uploader Playwright session), its breaker
    will be OPEN — visible here as state=open + seconds-since-open.

Caveat:
    Breakers live in process memory. This script reflects only the
    process you exec into. For the production app, run inside its
    container:

        docker exec dld python scripts/breaker_status.py

    For a one-off check from outside the container, prefer
    ``GET /health/details`` (same data, no exec dance).

Usage:
    python scripts/breaker_status.py --help
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Repo root on sys.path so `from core import ...` works when invoked directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    parser = argparse.ArgumentParser(
        prog="breaker_status",
        description="Print every circuit breaker in the process-wide registry.",
    )
    parser.parse_args()

    # Trigger imports of the modules that own the breakers so the registry
    # is populated. Without this, a freshly-launched process has an empty
    # registry and the output is a misleading "no breakers".
    try:
        import core.image_gatherer  # noqa: F401
    except Exception:
        pass
    try:
        import core.llm_title_gen  # noqa: F401
    except Exception:
        pass
    try:
        import core.email  # noqa: F401
    except Exception:
        pass

    from core import circuit_breaker as _cb

    rows = []
    now = time.monotonic()
    for name, br in sorted(_cb._registry.items()):  # type: ignore[attr-defined]
        opened_at = br._opened_at  # type: ignore[attr-defined]
        if opened_at is None:
            since = "-"
        else:
            since = f"{now - opened_at:.0f}s"
        rows.append((
            name,
            br.state.value,
            str(br._consecutive_failures),  # type: ignore[attr-defined]
            since,
        ))

    if not rows:
        print("No circuit breakers registered in this process yet.")
        print("(Breakers materialize lazily on first use; the registry is "
              "empty until image_gatherer / email / a platform uploader "
              "runs at least once.)")
        return

    name_w = max(len(r[0]) for r in rows + [("name",)])
    print(f"{'name'.ljust(name_w)}  state       failures  since_open")
    print(f"{'-' * name_w}  ----------  --------  ----------")
    for name, state, fails, since in rows:
        print(f"{name.ljust(name_w)}  {state.ljust(10)}  {fails.rjust(8)}  {since.rjust(10)}")


if __name__ == "__main__":
    main()
