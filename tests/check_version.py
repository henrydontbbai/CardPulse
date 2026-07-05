#!/usr/bin/env python3
"""Verify that the CLI version is documented in CHANGELOG."""

from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cli = (ROOT / "bin" / "cardpulse").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    match = re.search(r'^VERSION="([^"]+)"$', cli, re.MULTILINE)
    if not match:
        print("VERSION not found in bin/cardpulse", file=sys.stderr)
        return 1

    version = match.group(1)
    if f"## [{version}]" not in changelog:
        print(f"CHANGELOG.md does not contain ## [{version}]", file=sys.stderr)
        return 1

    print(f"version ok: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
