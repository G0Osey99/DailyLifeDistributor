#!/usr/bin/env python3
"""Check the /goal prompts in a build guide.

Every fenced ```text block whose first line starts with /goal is a prompt. Each one must:
  - be 4000 characters or fewer,
  - not contain the banned keyword (it would switch Claude Code into multi-agent mode),
  - name its step id, and write the build log that matches the step heading above it.

Usage:  python3 scripts/check_prompts.py [guide.md ...]   (default: docs/rebuild/build-guide.md)
Exit 1 on any failure. Standard library only, so it runs before the project is installed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LIMIT = 4000
BANNED = "ultra" + "code"  # split so this file doesn't trip its own check
# "Step 3.0", or a step inserted after one with a letter: "Step 3.0a" (Phase 3G)
HEADING = re.compile(r"^#{2,6}\s+Step\s+(\d+\.\d+[a-z]?)\b")
FENCE_OPEN = re.compile(r"^```text\s*$")


def prompts(text: str):
    """Yield (line_no, step_from_heading, prompt_text)."""
    lines = text.splitlines()
    step = None
    i = 0
    while i < len(lines):
        m = HEADING.match(lines[i])
        if m:
            step = m.group(1)
        if FENCE_OPEN.match(lines[i]):
            start = i + 1
            j = start
            while j < len(lines) and lines[j].strip() != "```":
                j += 1
            body = "\n".join(lines[start:j])
            if body.lstrip().startswith("/goal"):
                yield start, step, body.strip("\n")
            i = j
        i += 1


def check(path: Path) -> list[str]:
    errors: list[str] = []
    rows = []
    for line_no, step, body in prompts(path.read_text(encoding="utf-8")):
        n = len(body)
        problems = []
        if n > LIMIT:
            problems.append(f"{n} chars > {LIMIT}")
        if BANNED in body.lower():
            problems.append("contains the banned keyword")
        if step is None:
            problems.append("no 'Step X.Y' heading above it")
        else:
            if not re.search(rf"/goal Step {re.escape(step)}\b", body):
                problems.append(f"doesn't start with '/goal Step {step}'")
            if f"build-log/step-{step}.md" not in body:
                problems.append(f"doesn't write build-log/step-{step}.md")
        rows.append((step or "?", n, line_no, problems))
        for p in problems:
            errors.append(f"{path}:{line_no} step {step}: {p}")

    width = max((len(r[0]) for r in rows), default=4)
    print(f"{path}  ({len(rows)} prompts)")
    for step, n, line_no, problems in rows:
        status = "FAIL" if problems else "ok"
        print(f"  {step:<{width}}  {n:>5} chars  line {line_no:<5} {status}")
    if rows:
        print(f"  longest: {max(r[1] for r in rows)} chars (limit {LIMIT})")
    return errors


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or [Path("docs/rebuild/build-guide.md")]
    errors: list[str] = []
    for p in paths:
        if not p.exists():
            errors.append(f"{p}: not found")
            continue
        errors += check(p)
    if errors:
        print("\n".join(["", "Problems:"] + [f"  {e}" for e in errors]))
        return 1
    print("All prompts pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
