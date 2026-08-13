#!/usr/bin/env python3
"""Compatibility entry point for `python glacier_restore.py`.

The implementation moved into the ``s3_glacier_restore`` package in v2.0.
This shim keeps the original invocation working, including for anyone with an
existing cron entry or runbook pointing at this filename.

Prefer either of:

    s3-glacier-restore --help          # after `pip install .`
    python -m s3_glacier_restore --help
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from s3_glacier_restore.cli import main
except ImportError as exc:  # pragma: no cover - dependency guidance
    sys.stderr.write(
        f"Could not load s3_glacier_restore: {exc}\n"
        "Install the dependencies with:  pip install -r requirements.txt\n"
    )
    raise SystemExit(2) from exc


if __name__ == "__main__":
    sys.exit(main())
