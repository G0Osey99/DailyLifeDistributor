"""Read-only recon of the Vista Social Schedule step.

Reuses the stored Vista session (so we're logged in) and drives the composer
up to the Schedule controls, dumping the live DOM at each stage so we can see
why ``.react-datepicker__input-container input`` never becomes visible after
clicking Next. Does NOT click Schedule/Publish — at worst it leaves an
autosaved draft, which the real uploader already dismisses.

Run on the VPS where the Vista session lives, e.g.:
    docker cp scripts/vista_schedule_recon.py dld:/tmp/r.py
    docker exec dld python /tmp/r.py

Prints compact triage JSON for each stage to stdout.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Headless on the VPS (no display); the session is already valid.
os.environ.setdefault("VISTA_SOCIAL_HEADLESS", "true")

from core.playwright_session import PlaywrightSession, _load_session_blob_to  # noqa: E402
from uploaders import vista_social_uploader as V  # noqa: E402

# Hosted blobs are org-scoped; this account lives under org_id=1. A bare
# script has no Flask/org context, so materialize the Vista session file
# explicitly before opening the browser.
_ORG_ID = int(os.environ.get("RECON_ORG_ID", "1"))


def _triage(page, label: str) -> None:
    """Print a compact snapshot of the controls that matter for scheduling."""
    try:
        info = page.evaluate(
            """() => {
                const vis = (el) => !!el.offsetParent;
                const text = (el) => (el.innerText || el.textContent || '').trim();
                const labels = Array.from(document.querySelectorAll('label'))
                    .filter(vis)
                    .map(l => text(l)).filter(t => t && t.length < 40);
                const buttons = Array.from(document.querySelectorAll('button'))
                    .filter(vis)
                    .map(b => ({t: text(b), disabled: b.disabled}))
                    .filter(b => b.t);
                const radios = Array.from(document.querySelectorAll(
                    'input[type=radio]')).map(r => ({
                        name: r.name, value: r.value, checked: r.checked,
                        aria: r.getAttribute('aria-label'), vis: vis(r)}));
                const pickerInputs = document.querySelectorAll(
                    '.react-datepicker__input-container input');
                const dateLike = Array.from(document.querySelectorAll('input'))
                    .filter(i => /date|schedule|datepicker|time/i.test(
                        (i.className||'')+' '+(i.name||'')+' '+(i.placeholder||'')))
                    .map(i => ({name: i.name, cls: i.className,
                                ph: i.placeholder, type: i.type, vis: vis(i)}));
                const selects = Array.from(document.querySelectorAll('select'))
                    .map(s => ({name: s.name, vis: vis(s)}));
                const allInputs = Array.from(document.querySelectorAll('input'))
                    .filter(i => vis(i) && i.type !== 'radio' && i.type !== 'checkbox')
                    .map(i => ({id: i.id, name: i.name, cls: i.className,
                                ph: i.placeholder, type: i.type,
                                val: (i.value||'').slice(0, 24)}));
                return {url: location.href,
                        labels_unique: Array.from(new Set(labels)),
                        buttons, radios,
                        react_datepicker_count: pickerInputs.length,
                        date_like_inputs: dateLike,
                        visible_inputs: allInputs,
                        selects};
            }"""
        )
    except Exception as e:  # noqa: BLE001
        info = {"error": f"{type(e).__name__}: {e}"}
    print(f"\n===== STAGE: {label} =====", flush=True)
    print(json.dumps(info, indent=2), flush=True)
    # Self-contained artifact save (the deployed uploader may not have the
    # new _capture_debug helper, so don't depend on it).
    try:
        out = "/data/vista-debug" if os.path.isdir("/data") else "/tmp/vista-debug"
        os.makedirs(out, exist_ok=True)
        page.screenshot(path=os.path.join(out, f"recon-{label}.png"), full_page=True)
        with open(os.path.join(out, f"recon-{label}.html"), "w", encoding="utf-8") as fh:
            fh.write(page.content())
        print(f"(artifacts: {out}/recon-{label}.png|.html)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"(artifact save failed: {e})", flush=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = _load_session_blob_to(V._VS_SESSION_CONFIG.session_file, org_id=_ORG_ID)
    print(f"materialized vista session (org={_ORG_ID}): {ok}", flush=True)
    with PlaywrightSession(V._VS_SESSION_CONFIG) as sess:
        page = sess.page
        print("Landing URL:", page.url, flush=True)

        V._open_new_post(page)
        V._dismiss_autosave_prompt(page)
        _triage(page, "composer-opened")

        try:
            # Production keeps Facebook + Instagram (only YouTube unchecked).
            V._set_profile_selection(page, [V._NETWORK_YOUTUBE])
        except Exception as e:  # noqa: BLE001
            print("profile selection raised:", e, flush=True)
        try:
            V._fill_caption(page, "recon — ignore")
        except Exception as e:  # noqa: BLE001
            print("fill caption raised:", e, flush=True)

        # Attach a valid 1080x1080 JPEG so Instagram's content check passes —
        # this reproduces the real flow (FB+IG+media), unlike a no-media run.
        try:
            from PIL import Image
            img_path = "/tmp/recon_media.jpg"
            _dims = os.environ.get("RECON_IMG_DIMS", "1080x1080")
            _w, _h = (int(x) for x in _dims.split("x"))
            Image.new("RGB", (_w, _h), (40, 90, 160)).save(img_path, "JPEG")
            print(f"using image {_w}x{_h}", flush=True)
            V._attach_media(page, img_path)
            V._wait_for_media_upload(page, 120_000)
            print("media attached + upload-wait returned", flush=True)
        except Exception as e:  # noqa: BLE001
            print("media attach raised:", e, flush=True)
        _triage(page, "after-media")

        # The crux: does selecting Schedule toggle a radio, and does a picker
        # appear inline (no Next needed) or only after Next?
        try:
            V._select_schedule_radio(page)
        except Exception as e:  # noqa: BLE001
            print("select schedule radio raised:", e, flush=True)
        page.wait_for_timeout(800)
        _triage(page, "after-schedule-radio")

        # Click Next if present, then re-dump — this is the exact transition
        # that fails in production.
        try:
            nxt = page.locator("button", has_text="Next").first
            if nxt.count() and nxt.is_visible():
                nxt.click()
                page.wait_for_timeout(1500)
                _triage(page, "after-next")
            else:
                print("No visible Next button after schedule radio.", flush=True)
        except Exception as e:  # noqa: BLE001
            print("Next click raised:", e, flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
