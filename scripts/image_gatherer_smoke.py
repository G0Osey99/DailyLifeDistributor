"""Smoke test for the Vista image gatherer.

Hits llamafile + Unsplash (or Pexels fallback) end-to-end and saves the
chosen image next to this script for visual inspection. Does NOT record
to image_history — that only happens once Rock has confirmed the upload.

Usage:
    python scripts/image_gatherer_smoke.py
    python scripts/image_gatherer_smoke.py --verse "Be still, and know that I am God." --date 2026-05-10

Requires:
    - llamafile running on :8081 (start the app once or run launch_mac.command)
    - UNSPLASH_ACCESS_KEY in .env
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env")

from core import db as _db  # noqa: E402
from core.image_gatherer import gather_image_for_verse  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--verse",
        default="Be still, and know that I am God. - Psalm 46:10",
        help="Verse text to feed the gatherer",
    )
    p.add_argument("--date", default=date.today().isoformat(), help="Publish date (YYYY-MM-DD)")
    p.add_argument("--topic", default="", help="Optional Excel topic hint")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    _db.init_db()  # ensure image_history table exists

    publish_date = date.fromisoformat(args.date)
    print(f"Verse: {args.verse}")
    print(f"Date:  {publish_date}")
    if args.topic:
        print(f"Topic hint: {args.topic}")

    img = gather_image_for_verse(args.verse, publish_date, topic_hint=args.topic)
    if img is None:
        print("\n!! No image was returned. Check logs above for the reason.", file=sys.stderr)
        return 1

    dest = _PROJECT_ROOT / f"image_smoke_{img.source}_{img.photo_id}.jpg"
    shutil.copyfile(img.file_path, dest)

    print("\nGot image:")
    print(f"  source:       {img.source}")
    print(f"  photo_id:     {img.photo_id}")
    print(f"  topic:        {img.topic}")
    print(f"  photographer: {img.photographer}")
    print(f"  photo_url:    {img.photo_url}")
    print(f"  saved copy:   {dest}")
    print(f"  temp file:    {img.file_path}  (delete this when done)")
    print(
        "\nIf the image looks good, the orchestrator will call "
        "core.db.record_image_use(...) once Rock accepts the upload."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
