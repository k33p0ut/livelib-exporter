#!/usr/bin/env python3
"""Repository-local launcher; installation provides the `livelib-export` command."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from livelib_exporter.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
