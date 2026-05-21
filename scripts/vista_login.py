"""Open a headed Chrome window so the user can log in to Vista Social.

Reuses the uploader's SessionConfig (no_login_recovery=False), so
PlaywrightSession will pop a window, wait up to login_timeout, and save
storage_state on exit.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force headed regardless of any env override.
os.environ["VISTA_SOCIAL_HEADLESS"] = "false"
# Generous deadline so the user has time to type creds + 2FA.
os.environ.setdefault("VISTA_SOCIAL_LOGIN_TIMEOUT", "600")

from core.playwright_session import PlaywrightSession  # noqa: E402
from uploaders.vista_social_uploader import _VS_SESSION_CONFIG  # noqa: E402


def main() -> None:
    print("Opening Vista Social login window. Log in, then leave the window;")
    print("session will save automatically once you reach the dashboard.")
    with PlaywrightSession(_VS_SESSION_CONFIG):
        pass
    print("Login flow complete; session saved.")


if __name__ == "__main__":
    main()
