"""Smoke test for the Rock RMS browser client.

Runs the four create_* methods plus the three link_* methods against a
single hard-coded test date, printing each step and saving a screenshot
on failure.

Usage:
    python scripts/rock_smoke.py [--skip-spotlight] [--skip-image]

Flags exist so you can stage the test piece by piece. `--skip-spotlight`
skips the Wistia cascade (the riskiest interaction); `--skip-image`
skips the Background Image upload (Vista will be created without one).

Required for a full run:
    --image PATH    Path to a JPG/PNG to upload as the Vista background.
                    Any small image works — we delete it from Rock later
                    by hand if needed.
    --wistia-ref    Wistia media reference label, e.g. "app 260512".
                    Must match an option in the Spotlight Media dropdown
                    exactly. Skip the spotlight create with --skip-spotlight
                    if you don't have one ready.

Tomorrow's checklist:
    1. Open a terminal in the project root.
    2. `pip install playwright` (one time).
    3. Have an image file handy at e.g. C:\\temp\\test.jpg.
    4. Pick a Wistia ref from any existing Spotlight (open one and
       glance at its Media dropdown).
    5. Run e.g.:
       python scripts/rock_smoke.py --image C:\\temp\\test.jpg --wistia-ref "app 260512"
    6. First run will pop a Chrome window for login — sign in within
       5 minutes. The session is saved to rock_session.json.
    7. After the run, copy any rock_failure_*.png screenshots and the
       full console log so they can be reviewed.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

# Make the project root importable
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from uploaders.rock import (  # noqa: E402
    ItemRef,
    ParentFields,
    ReflectionFields,
    RockBrowserClient,
    SpotlightFields,
    VistaFields,
    parent_title,
    reflection_title,
)


def _step(label: str) -> None:
    print(f"\n=== {label} ===", flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="Smoke test for Rock browser client")
    p.add_argument(
        "--date", default="2026-12-31",
        help="ISO publish date for the test Daily Experience (YYYY-MM-DD). "
             "Default: 2026-12-31 (chosen far from any real production date).",
    )
    p.add_argument("--image", type=Path, help="Path to a JPG/PNG for the Vista background image")
    p.add_argument("--wistia-ref", default="", help="Exact Wistia media reference label, e.g. 'app 260512'")
    p.add_argument("--skip-spotlight", action="store_true", help="Skip the Spotlight create step")
    p.add_argument("--skip-vista", action="store_true", help="Skip the Vista create step")
    p.add_argument("--skip-reflection", action="store_true", help="Skip the Reflection create step")
    p.add_argument("--skip-parent", action="store_true", help="Skip the Parent create step")
    p.add_argument("--skip-link", action="store_true", help="Skip linking children to parent")
    p.add_argument("--skip-image", action="store_true", help="Create Vista without uploading an image")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    publish_date = date.fromisoformat(args.date)
    print(f"Test publish date: {publish_date}")
    print(f"  Parent title:     {parent_title(publish_date)!r}")
    print(f"  Reflection title: {reflection_title(publish_date)!r}")

    if args.image and not args.image.exists() and not args.skip_image and not args.skip_vista:
        print(f"!! Image path does not exist: {args.image}", file=sys.stderr)
        return 2

    spot_ref: ItemRef | None = None
    vista_ref: ItemRef | None = None
    refl_ref: ItemRef | None = None
    parent_ref: ItemRef | None = None

    with RockBrowserClient() as rock:
        # 1) Reflection — simplest, no media, no images. If this fails we
        #    have a fundamental problem with auth or the create flow.
        if not args.skip_reflection:
            _step("create_reflection")
            refl_ref = rock.create_reflection(ReflectionFields(
                title=reflection_title(publish_date),
                content=(
                    "Father, this is a smoke-test reflection. Thank You for "
                    "Your patience as we iron out the automation. Amen."
                ),
            ))
            print(f"  -> {refl_ref}")

        # 2) Vista — exercises EditorJS Content + image upload.
        if not args.skip_vista:
            _step("create_vista")
            vista_ref = rock.create_vista(VistaFields(
                title="Smoke Test 1:1",
                content=(
                    "This is a smoke-test scripture body. — Smoke Test 1:1"
                ),
                background_image_path=None if args.skip_image else args.image,
            ))
            print(f"  -> {vista_ref}")

        # 3) Spotlight — riskiest, exercises the Wistia cascade.
        if not args.skip_spotlight:
            _step("create_spotlight")
            if not args.wistia_ref:
                print(
                    "!! --wistia-ref is required for spotlight; "
                    "rerun with --skip-spotlight if you don't have one.",
                    file=sys.stderr,
                )
                return 2
            spot_ref = rock.create_spotlight(SpotlightFields(
                title="Smoke Test Spotlight",
                media_reference=args.wistia_ref,
            ))
            print(f"  -> {spot_ref}")

        # 4) Parent.
        if not args.skip_parent:
            _step("create_parent")
            parent_ref = rock.create_parent(ParentFields(
                title=parent_title(publish_date),
                active_date=publish_date,
            ))
            print(f"  -> {parent_ref}")

        # 5) Link children. Only attempt for ones we actually created.
        if not args.skip_link and parent_ref is not None:
            if spot_ref is not None:
                _step("link_spotlight_to_parent")
                rock.link_spotlight_to_parent(parent_ref, spot_ref)
            if vista_ref is not None:
                _step("link_vista_to_parent")
                rock.link_vista_to_parent(parent_ref, vista_ref)
            if refl_ref is not None:
                _step("link_reflection_to_parent")
                rock.link_reflection_to_parent(parent_ref, refl_ref)

        _step("done — capturing final parent screenshot")
        if parent_ref is not None:
            shot = _PROJECT_ROOT / f"rock_smoke_parent_{parent_ref.id}.png"
            rock.screenshot(str(shot))
            print(f"  saved {shot}")

    print("\nSmoke test complete.")
    print("Created:")
    for label, ref in [
        ("Reflection", refl_ref),
        ("Vista", vista_ref),
        ("Spotlight", spot_ref),
        ("Parent", parent_ref),
    ]:
        if ref is not None:
            print(f"  {label:<11} {ref.edit_url}")
    print(
        "\nIf you want to clean up the test items, delete them from the "
        "channel listings in Rock — there's no automatic cleanup."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
