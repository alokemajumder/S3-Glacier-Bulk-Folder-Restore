"""Checkpoint file so an interrupted run can be resumed.

A bulk restore over millions of objects can run for hours. Without a
checkpoint, a crash at 90% means re-issuing 900k restore requests -- which is
not just slow, it is billable. The state file is append-only and flushed as it
goes, so a `kill -9` loses at most the last buffered line.

Format: one record per line, ``key`` or ``key<TAB>versionId``. Plain text
keeps it greppable and lets an operator hand-edit it.

The file records which **bucket** it belongs to. Entries are bare keys, so
pointing the same state file at a second bucket with a similar layout would
silently mark never-restored objects as done; the recorded scope turns that
into an explicit error instead.
"""

from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger(__name__)

_HEADER = "# s3-glacier-restore state file: one restored object per line\n"
_SCOPE = "# bucket: "


class StateScopeError(RuntimeError):
    """The state file belongs to a different bucket than the current run."""


class StateFile:
    """Records objects whose restore has been successfully initiated."""

    def __init__(self, path: str, bucket: str | None = None, flush_every: int = 100) -> None:
        self.path = os.path.expanduser(path)
        self.bucket = bucket
        self.flush_every = max(1, flush_every)
        self._seen: set[str] = set()
        self._handle = None
        self._lock = threading.Lock()
        self._since_flush = 0

    def load(self) -> int:
        """Read any pre-existing entries. Returns how many were found.

        Raises :class:`StateScopeError` if the file was written for a
        different bucket.
        """
        if not os.path.exists(self.path):
            return 0
        recorded = None
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                entry = line.rstrip("\n")
                if entry.startswith(_SCOPE):
                    recorded = entry[len(_SCOPE) :].strip()
                    continue
                if not entry or entry.startswith("#"):
                    continue
                self._seen.add(entry)

        if recorded and self.bucket and recorded != self.bucket:
            raise StateScopeError(
                f"State file '{self.path}' belongs to bucket '{recorded}', but this "
                f"run targets '{self.bucket}'. Its entries are bare keys, so reusing "
                "it here would mark objects as restored that never were. Use a "
                "separate state file per bucket."
            )
        if self._seen and not recorded:
            # Written by 2.0.0, which did not record a scope.
            log.warning(
                "State file '%s' predates bucket scoping; assuming it belongs to "
                "'%s'. Delete it if it was written for a different bucket.",
                self.path,
                self.bucket or "this bucket",
            )
        return len(self._seen)

    def open(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._handle = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - closed by close()/__exit__
        if is_new:
            self._handle.write(_HEADER)
            if self.bucket:
                self._handle.write(f"{_SCOPE}{self.bucket}\n")

    @staticmethod
    def _encode(state_id: str) -> str:
        """Make one entry safe for a line-oriented file.

        S3 keys may contain newlines and backslashes. Both the in-memory set
        and the file hold the *encoded* form, so a key written in one run is
        still recognised when the next run reloads it.
        """
        return state_id.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")

    def contains(self, state_id: str) -> bool:
        return self._encode(state_id) in self._seen

    def record(self, state_id: str) -> None:
        entry = self._encode(state_id)
        with self._lock:
            if entry in self._seen:
                return
            self._seen.add(entry)
            if self._handle is None:
                return
            self._handle.write(entry + "\n")
            self._since_flush += 1
            if self._since_flush >= self.flush_every:
                self._handle.flush()
                self._since_flush = 0

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None

    def __enter__(self) -> StateFile:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __len__(self) -> int:
        return len(self._seen)


class NullState:
    """No-op stand-in so the engine never branches on ``state is None``."""

    path: str | None = None

    def load(self) -> int:
        return 0

    def open(self) -> None:
        return None

    def contains(self, state_id: str) -> bool:
        return False

    def record(self, state_id: str) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> NullState:
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def __len__(self) -> int:
        return 0
