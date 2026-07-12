#!/usr/bin/env python3
"""Set the local CardPulse Web password without exposing plaintext."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

from web_auth import write_password_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the CardPulse Web password hash.")
    default_path = Path(os.environ.get("CARDPULSE_CONFIG_DIR", str(ROOT_DIR / "config"))) / "web-auth.json"
    parser.add_argument("--auth-file", default=str(default_path))
    args = parser.parse_args()

    password = getpass.getpass("CardPulse Web password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 2
    if password == "henry":
        print("Warning: 'henry' is a weak temporary password. Replace it before LAN use.", file=sys.stderr)
    try:
        write_password_config(Path(args.auth_file), password)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Password hash written to {Path(args.auth_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
