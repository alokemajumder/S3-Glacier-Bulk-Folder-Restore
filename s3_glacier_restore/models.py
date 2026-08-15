"""Core value types shared across the package."""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, field
from datetime import datetime

# Storage classes whose objects are archived and must be restored before they
# can be read.
ARCHIVE_STORAGE_CLASSES = frozenset({"GLACIER", "DEEP_ARCHIVE"})

# Intelligent-Tiering objects *may* be archived; only a HeadObject call reveals
# whether a given object currently sits in an archive access tier.
INTELLIGENT_TIERING = "INTELLIGENT_TIERING"

# Immediately readable despite the "GLACIER" in the name. Calling RestoreObject
# on one of these returns InvalidObjectState.
GLACIER_IR = "GLACIER_IR"

RETRIEVAL_TIERS = ("Bulk", "Standard", "Expedited")

# Deep Archive supports only Bulk and Standard retrievals.
DEEP_ARCHIVE_TIERS = frozenset({"Bulk", "Standard"})


@dataclass(frozen=True)
class S3Object:
    """One object (or object version) as returned by a list operation."""

    key: str
    size: int = 0
    storage_class: str = "STANDARD"
    version_id: str | None = None
    is_latest: bool = True
    restore_in_progress: bool = False
    restore_expiry: datetime | None = None

    @property
    def label(self) -> str:
        """Human-readable identifier used in log lines."""
        if self.version_id:
            return f"{self.key} (version {self.version_id})"
        return self.key

    @property
    def state_id(self) -> str:
        """Stable identity used for checkpoint/resume bookkeeping."""
        if self.version_id:
            return f"{self.key}\t{self.version_id}"
        return self.key


class Outcome(enum.Enum):
    """What happened to a single object during a run.

    Ordered roughly by how much the operator cares about it; the summary is
    printed in declaration order.
    """

    INITIATED = "Restore initiated"
    WOULD_RESTORE = "Would restore (dry run)"
    ALREADY_IN_PROGRESS = "Restore already in progress"
    ALREADY_RESTORED = "Already restored and still available"
    RESUMED = "Skipped, recorded in state file"
    FILTERED = "Skipped by include/exclude filters"
    NOT_ARCHIVED = "Skipped, not an archived storage class"
    NOT_RESTORABLE = "Skipped, cannot be restored"
    VANISHED = "Skipped, object disappeared mid-run"
    ACCESS_DENIED = "Failed, access denied"
    ERROR = "Failed"

    @property
    def is_failure(self) -> bool:
        return self in (Outcome.ACCESS_DENIED, Outcome.ERROR)

    @property
    def is_action(self) -> bool:
        """True when the run actually did (or would do) work for this object."""
        return self in (Outcome.INITIATED, Outcome.WOULD_RESTORE)


@dataclass
class Stats:
    """Thread-safe counters for a run.

    Every mutation goes through :meth:`record`, so worker threads and the main
    thread can both report outcomes without racing.
    """

    scanned: int = 0
    scanned_bytes: int = 0
    counts: dict[Outcome, int] = field(default_factory=dict)
    action_bytes: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_scanned(self, obj: S3Object) -> None:
        with self._lock:
            self.scanned += 1
            self.scanned_bytes += obj.size

    def record(self, outcome: Outcome, obj: S3Object | None = None, reason: str = "") -> None:
        with self._lock:
            self.counts[outcome] = self.counts.get(outcome, 0) + 1
            # Only failures: the summary presents `reasons` as failure causes,
            # so mixing in benign skip reasons ("STANDARD") would misreport
            # them as things that went wrong.
            if reason and outcome.is_failure:
                self.reasons[reason] = self.reasons.get(reason, 0) + 1
            if obj is not None and outcome.is_action:
                self.action_bytes += obj.size

    def get(self, outcome: Outcome) -> int:
        with self._lock:
            return self.counts.get(outcome, 0)

    @property
    def actions(self) -> int:
        return self.get(Outcome.INITIATED) + self.get(Outcome.WOULD_RESTORE)

    @property
    def failures(self) -> int:
        return sum(n for o, n in self.counts.items() if o.is_failure)
