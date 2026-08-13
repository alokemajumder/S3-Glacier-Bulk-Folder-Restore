"""CSV manifest writer for S3 Batch Operations.

Direct ``RestoreObject`` calls are the right tool up to a few million objects.
Past that, S3 Batch Operations is what AWS builds for the job: it runs the
restores server-side, retries on your behalf, and writes a completion report.
It is driven by a CSV manifest, which is what this module produces.

Format (no header row, as S3 Batch Operations requires):

* ``bucket,key``                -- manifest format ``S3BatchOperations_CSV_20180820``
* ``bucket,key,versionId``      -- same format, with the optional version column

Keys are written raw and quoted by :mod:`csv`, which is what the manifest
format expects for keys containing commas or quotes.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from typing import TextIO

from .models import S3Object

log = logging.getLogger(__name__)


class ManifestWriter:
    """Thread-safe CSV manifest writer."""

    def __init__(self, path: str, bucket: str, include_versions: bool = False) -> None:
        self.path = os.path.expanduser(path)
        self.bucket = bucket
        self.include_versions = include_versions
        self.rows = 0
        self._handle: TextIO | None = None
        self._writer = None
        self._lock = threading.Lock()

    def open(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # newline="" is required by csv to avoid stray \r on Windows.
        self._handle = open(self.path, "w", encoding="utf-8", newline="")  # noqa: SIM115 - closed by close()/__exit__
        self._writer = csv.writer(self._handle, quoting=csv.QUOTE_MINIMAL)

    def write(self, obj: S3Object) -> None:
        if self._writer is None:
            return
        row = [self.bucket, obj.key]
        if self.include_versions:
            row.append(obj.version_id or "")
        with self._lock:
            self._writer.writerow(row)
            self.rows += 1

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None
                self._writer = None

    def __enter__(self) -> ManifestWriter:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class NullManifest:
    """No-op stand-in used when ``--manifest-out`` was not given."""

    path: str | None = None
    rows = 0

    def open(self) -> None:
        return None

    def write(self, obj: S3Object) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> NullManifest:
        return self

    def __exit__(self, *exc_info) -> None:
        return None
