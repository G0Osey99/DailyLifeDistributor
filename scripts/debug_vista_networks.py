"""Dump every raw Vista calendar event in a window without PLATFORMS filtering.

Used to diagnose why Facebook entries are not showing up on the refresh —
prints each (network, isoDate, isStory, postId, text) so we can see what
network string Vista is actually emitting for FB posts.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.playwright_session import PlaywrightSession  # noqa: E402
from core.refresh.vista_source import (  # noqa: E402
    _SESSION_CFG,
    _capture_events_in_window,
)


def main() -> None:
    today = date.today()
    start = date(today.year, today.month, 1)
    end = (start.replace(day=28) + timedelta(days=10)).replace(day=1) + timedelta(days=60)

    print(f"Scanning window {start} .. {end}")

    with PlaywrightSession(_SESSION_CFG) as sess:
        page = sess.page
        try:
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as e:  # noqa: BLE001 — networkidle may never fire
            print(f"(networkidle wait timed out, continuing: {e})")
        page.wait_for_selector("[data-date]", timeout=20_000)
        events = _capture_events_in_window(page, start, end)

    print(f"\nTotal raw events captured: {len(events)}\n")

    networks = Counter(e.get("network") or "<empty>" for e in events)
    print("Network counts (raw, pre-filter):")
    for net, n in networks.most_common():
        print(f"  {net!r:25s} {n}")

    print("\nFirst 15 events:")
    for e in events[:15]:
        head = (e.get("text") or "").splitlines()[0][:60]
        print(
            f"  {e.get('isoDate'):10s}  net={e.get('network')!r:18s}  "
            f"story={e.get('isStory')}  id={e.get('postId')}  text={head!r}"
        )

    # Spot check: any event whose network is NOT instagram/facebook — show
    # the full text so we can see what we'd be dropping.
    odd = [e for e in events if (e.get("network") or "").lower() not in ("instagram", "facebook")]
    if odd:
        print(f"\nNon-IG/FB events ({len(odd)}):")
        for e in odd[:10]:
            print(f"  {e.get('isoDate')}  net={e.get('network')!r}  text={(e.get('text') or '')[:120]!r}")


if __name__ == "__main__":
    main()
