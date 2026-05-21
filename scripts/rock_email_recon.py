"""Read-only recon of the Daily Life *email* content channel form.

Opens the channel listing (page 343, ContentChannelGuid for the Daily Life
email channel), clicks Add to reach the new-item form, and dumps every
label / input id / attribute control we'll need to drive it. Saves a
screenshot + an HTML excerpt next to the project root. Creates nothing.

Usage:
    python scripts/rock_email_recon.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.playwright_session import PlaywrightSession  # noqa: E402
from uploaders.rock.client import _ROCK_SESSION_CONFIG  # noqa: E402

_EMAIL_CHANNEL_GUID = "2182c1f3-8f8c-44f3-987f-75a698fe44a7"
_LIST_URL = (
    "https://rock.lcbcchurch.com/page/343"
    f"?ContentChannelGuid={_EMAIL_CHANNEL_GUID}"
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with PlaywrightSession(_ROCK_SESSION_CONFIG) as sess:
        page = sess.page
        page.goto(_LIST_URL, wait_until="domcontentloaded")
        print("Listing URL:", page.url, flush=True)

        # Click the channel's Add button (header or footer action row).
        add = page.locator(
            'a[href*="gContentChannelItems$actionFooterRow$footerGridActions$lbAdd"]'
        ).first
        add.click()
        page.wait_for_selector('input[placeholder="Enter a title..."]', timeout=30000)
        print("New-item form URL:", page.url, flush=True)

        # Dump every label + the control it points at.
        fields = page.evaluate(
            r"""() => {
                const out = [];
                // form-groups carry the label + control in Rock
                document.querySelectorAll('.form-group').forEach(g => {
                    const lbl = g.querySelector(':scope > label, .control-label');
                    const label = lbl ? (lbl.textContent || '').trim() : '';
                    const inputs = [];
                    g.querySelectorAll('input,select,textarea,[contenteditable="true"],a.btn').forEach(el => {
                        inputs.push({
                            tag: el.tagName.toLowerCase(),
                            type: el.getAttribute('type') || '',
                            id: el.id || '',
                            name: el.getAttribute('name') || '',
                            cls: el.className || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            text: (el.tagName === 'A' ? (el.textContent||'').trim() : ''),
                        });
                    });
                    if (label || inputs.length) out.push({label, inputs});
                });
                return out;
            }"""
        )
        dump_path = _PROJECT_ROOT / "rock_email_form_fields.json"
        dump_path.write_text(json.dumps(fields, indent=2), encoding="utf-8")
        print(f"\nWrote {dump_path} ({len(fields)} form-groups)\n", flush=True)
        for f in fields:
            ids = ", ".join(
                f"{i['tag']}#{i['id']}({i['type'] or i['cls'][:20]})"
                for i in f["inputs"]
            )
            print(f"  [{f['label']}] -> {ids}", flush=True)

        shot = _PROJECT_ROOT / "rock_email_form.png"
        page.screenshot(path=str(shot), full_page=True)
        print(f"\nSaved screenshot {shot}", flush=True)

        html_path = _PROJECT_ROOT / "rock_email_form.html"
        html_path.write_text(page.content(), encoding="utf-8")
        print(f"Saved HTML {html_path}", flush=True)

    print("\nRecon complete. Nothing was saved to Rock.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
