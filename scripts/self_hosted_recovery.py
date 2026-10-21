#!/usr/bin/env python3
"""Run the shared recovery command from a source checkout."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.scripts.self_hosted_recovery import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
