#!/usr/bin/env python3
"""Entry point for CardPulse Web."""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "lib"))

from cardpulse_web import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
