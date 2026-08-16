"""The restore engine.

Design notes
------------
*Streaming, not batching.* Objects are classified as they stream out of the
paginator. Work that needs an API call is handed to a bounded thread pool;
everything else (filtered, wrong storage class, already restored) is settled
inline without touching the network.

*Bounded in-flight work.* We never build a list of futures the size of the
bucket. At most ``concurrency * 4`` requests are outstanding, so memory stays
flat whether the prefix holds a thousand objects or fifty million.

*Cooperative cancellation.* Ctrl-C sets an event; the producer stops feeding
the pool and in-flight requests are allowed to land, so the summary and the
state file reflect what actually happened.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .aws import describe_error
from .config import RestoreConfig
from .filters import KeyFilter
from .lister import format_bytes, head_object, iter_objects
from .manifest import NullManifest
from .models import (
    ARCHIVE_STORAGE_CLASSES,
    DEEP_ARCHIVE_TIERS,
    GLACIER_IR,
    INTELLIGENT_TIERING,
    Outcome,
    S3Object,
    Stats,
)
from .state import NullState

log = logging.getLogger(__name__)


def still_available(expiry: datetime | None) -> bool:
    """True when a restored copy exists *and* has not expired yet.

    S3 is not guaranteed to drop ``RestoreExpiryDate`` the instant a temporary
    copy lapses, so the date is compared against the clock rather than merely
    checked for presence.
    """
    if expiry is None:
        return False
    if expiry.tzinfo is None:  # defensive: boto3 returns aware datetimes
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > datetime.now(timezone.utc)


# Error codes that mean "this object is fine, just not actionable".
_BENIGN_ERRORS = {
    "RestoreAlreadyInProgress": Outcome.ALREADY_IN_PROGRESS,
    "NoSuchKey": Outcome.VANISHED,
    "NoSuchVersion": Outcome.VANISHED,
}


class RestoreEngine:
    """Runs one restore pass over a prefix."""

    def __init__(
        self,
        client,
        cfg: RestoreConfig,
        key_filter: KeyFilter | None = None,
        state=None,
        manifest=None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        # Explicit None checks, not `or`: an empty KeyFilter and a fresh
        # StateFile are both falsy by design, and `or` would silently discard
        # a real state file, defeating resume.
        self.filter = KeyFilter() if key_filter is None else key_filter
        self.state = NullState() if state is None else state
        self.manifest = NullManifest() if manifest is None else manifest
        self.stop = threading.Event() if stop_event is None else stop_event
        self.stats = Stats()
        self._started = 0.0
        self._deep_archive_tier_warned = False

    # ---------------------------------------------------------------- run --

    def run(self) -> Stats:
        """Execute the pass and return the collected statistics."""
        self._started = time.monotonic()
        in_flight = max(self.cfg.concurrency * 4, self.cfg.concurrency + 1)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.cfg.concurrency,
            thread_name_prefix="restore",
        ) as pool:
            pending: set = set()
            try:
                for obj in self._scan():
                    if self.stop.is_set():
                        break
                    if len(pending) >= in_flight:
                        done, pending = concurrent.futures.wait(
                            pending, return_when=concurrent.futures.FIRST_COMPLETED
                        )
                        self._harvest(done)
                    pending.add(pool.submit(self._process, obj))
            finally:
                # Drain whatever is still outstanding, including after a stop.
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    self._harvest(done)

        return self.stats

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    # ------------------------------------------------------------- scanning --

    def _scan(self) -> Iterator[S3Object]:
        """Yield only the objects that require a network call."""
        considered = 0
        for obj in iter_objects(self.client, self.cfg):
            if self.stop.is_set():
                return
            self.stats.record_scanned(obj)
            self._maybe_log_progress()

            outcome, reason = self._classify(obj)
            if outcome is not None:
                self.stats.record(outcome, obj, reason)
                if outcome is not Outcome.FILTERED:
                    log.debug(
                        "%s: %s%s", obj.label, outcome.value, f" ({reason})" if reason else ""
                    )
                continue

            considered += 1
            yield obj

            if self.cfg.max_objects is not None and considered >= self.cfg.max_objects:
                log.info("Reached --max-objects limit of %d.", self.cfg.max_objects)
                return

    def _classify(self, obj: S3Object) -> tuple[Outcome | None, str]:
        """Settle an object without I/O where possible.

        Returns ``(None, "")`` when the object needs a network call.
        """
        if self.filter:
            keep, reason = self.filter.match(obj.key)
            if not keep:
                return Outcome.FILTERED, reason

        if self.state.contains(obj.state_id):
            return Outcome.RESUMED, ""

        storage_class = obj.storage_class

        if storage_class == GLACIER_IR:
            # Instant Retrieval is readable without a restore; RestoreObject
            # would fail with InvalidObjectState.
            return Outcome.NOT_RESTORABLE, "GLACIER_IR is readable without a restore"

        if storage_class == INTELLIGENT_TIERING:
            if not self.cfg.include_intelligent_tiering:
                return (
                    Outcome.NOT_ARCHIVED,
                    "INTELLIGENT_TIERING (pass --include-intelligent-tiering to check)",
                )
            # Needs a HeadObject to learn whether it is in an archive tier.
            return None, ""

        if storage_class not in ARCHIVE_STORAGE_CLASSES:
            return Outcome.NOT_ARCHIVED, storage_class

        if obj.restore_in_progress:
            return Outcome.ALREADY_IN_PROGRESS, ""

        if still_available(obj.restore_expiry):
            # A restored copy is available right now; re-requesting it would
            # only extend the expiry, which is rarely what a bulk run intends.
            return (
                Outcome.ALREADY_RESTORED,
                f"available until {obj.restore_expiry:%Y-%m-%d %H:%M} UTC",
            )
        # An expiry in the past means the temporary copy has already lapsed and
        # the object is archived again. Treating that as "already restored"
        # would silently refuse to restore an object the caller cannot read --
        # the exact failure this tool exists to prevent. Fall through instead.

        if storage_class == "DEEP_ARCHIVE" and self.cfg.tier not in DEEP_ARCHIVE_TIERS:
            if not self._deep_archive_tier_warned:
                self._deep_archive_tier_warned = True
                log.warning(
                    "Tier '%s' is not supported for DEEP_ARCHIVE objects; those "
                    "objects are being skipped. Use --tier Bulk or --tier Standard.",
                    self.cfg.tier,
                )
            return (
                Outcome.NOT_RESTORABLE,
                f"DEEP_ARCHIVE does not support the {self.cfg.tier} tier",
            )

        return None, ""

    # ------------------------------------------------------------- workers --

    def _process(self, obj: S3Object) -> tuple[S3Object, Outcome, str]:
        """Run in a worker thread. Never raises."""
        try:
            return self._process_inner(obj)
        except ClientError as exc:
            code, message = describe_error(exc)
            benign = _BENIGN_ERRORS.get(code)
            if benign is not None:
                return obj, benign, code
            if code == "InvalidObjectState":
                return obj, Outcome.NOT_RESTORABLE, message
            if code in ("AccessDenied", "403"):
                return obj, Outcome.ACCESS_DENIED, message
            return obj, Outcome.ERROR, f"{code}: {message}"
        except BotoCoreError as exc:
            return obj, Outcome.ERROR, str(exc)
        except Exception as exc:  # noqa: BLE001 - a worker must never kill the run
            log.debug("Unexpected error for %s", obj.label, exc_info=True)
            return obj, Outcome.ERROR, f"{type(exc).__name__}: {exc}"

    def _process_inner(self, obj: S3Object) -> tuple[S3Object, Outcome, str]:
        intelligent_tiering = obj.storage_class == INTELLIGENT_TIERING

        if intelligent_tiering:
            head = head_object(self.client, self.cfg, obj)
            archive_status = head.get("ArchiveStatus")
            if not archive_status:
                return (
                    obj,
                    Outcome.NOT_ARCHIVED,
                    "INTELLIGENT_TIERING, not in an archive tier",
                )
            if head.get("Restore"):
                if 'ongoing-request="true"' in head["Restore"]:
                    return obj, Outcome.ALREADY_IN_PROGRESS, ""
                return obj, Outcome.ALREADY_RESTORED, head["Restore"]

        self.manifest.write(obj)

        if self.cfg.dry_run:
            return obj, Outcome.WOULD_RESTORE, ""

        self.client.restore_object(**self._restore_params(obj, intelligent_tiering))
        return obj, Outcome.INITIATED, ""

    def _restore_params(self, obj: S3Object, intelligent_tiering: bool) -> dict[str, Any]:
        """Build the RestoreObject request.

        Intelligent-Tiering archive tiers take neither ``Days`` nor a retrieval
        tier: the object is moved back to the Frequent Access tier and stays
        there. Sending ``Days`` for one of those is an InvalidRequest.
        """
        params: dict[str, Any] = {"Bucket": self.cfg.bucket, "Key": obj.key}
        if obj.version_id:
            params["VersionId"] = obj.version_id
        if self.cfg.expected_bucket_owner:
            params["ExpectedBucketOwner"] = self.cfg.expected_bucket_owner
        if self.cfg.requester_pays:
            params["RequestPayer"] = "requester"

        if intelligent_tiering:
            params["RestoreRequest"] = {}
        else:
            params["RestoreRequest"] = {
                "Days": self.cfg.days,
                "GlacierJobParameters": {"Tier": self.cfg.tier},
            }
        return params

    # ------------------------------------------------------------ harvesting --

    def _harvest(self, done: Iterable[concurrent.futures.Future]) -> None:
        for future in done:
            try:
                obj, outcome, detail = future.result()
            except concurrent.futures.CancelledError:
                continue
            except Exception as exc:  # noqa: BLE001 - defensive; _process catches
                log.error("Worker failed unexpectedly: %s", exc)
                self.stats.record(Outcome.ERROR, None, str(exc))
                continue

            self.stats.record(outcome, obj, detail if outcome.is_failure else "")

            if outcome is Outcome.INITIATED:
                self.state.record(obj.state_id)
                log.info("Restore requested: %s", obj.label)
            elif outcome is Outcome.WOULD_RESTORE:
                log.info("Would restore: %s (%s)", obj.label, format_bytes(obj.size))
            elif outcome.is_failure:
                log.error("Failed: %s -- %s", obj.label, detail)
            else:
                log.debug("%s: %s %s", obj.label, outcome.value, detail)

    # -------------------------------------------------------------- progress --

    def _maybe_log_progress(self) -> None:
        every = self.cfg.progress_every
        if every <= 0:
            return
        scanned = self.stats.scanned
        if scanned % every:
            return
        elapsed = max(self.elapsed, 1e-6)
        log.info(
            "Progress: scanned %s objects (%s), %s restore actions, %s failures -- %.0f objects/s",
            f"{scanned:,}",
            format_bytes(self.stats.scanned_bytes),
            f"{self.stats.actions:,}",
            f"{self.stats.failures:,}",
            scanned / elapsed,
        )


def summarize(stats: Stats, cfg: RestoreConfig, elapsed: float) -> list[str]:
    """Render the end-of-run summary block."""
    verb = "Would be restored" if cfg.dry_run else "Restore requests issued"
    reported = [o for o in Outcome if not o.is_action and stats.get(o)]

    # Size the label column to the labels actually being printed, so a long
    # outcome name does not knock the colons out of alignment.
    width = max(
        [len(verb), len("Objects scanned"), len("Throughput")] + [len(o.value) for o in reported]
    )

    lines = [
        "",
        "--- Summary ---",
        f"{'Objects scanned':<{width}} : {stats.scanned:,} ({format_bytes(stats.scanned_bytes)})",
        f"{verb:<{width}} : {stats.actions:,} ({format_bytes(stats.action_bytes)})",
    ]

    for outcome in reported:
        lines.append(f"{outcome.value:<{width}} : {stats.get(outcome):,}")

    lines.append(f"{'Elapsed':<{width}} : {elapsed:,.1f}s")
    if elapsed > 0 and stats.scanned:
        lines.append(f"{'Throughput':<{width}} : {stats.scanned / elapsed:,.0f} objects/s")

    if stats.failures:
        lines.append("")
        lines.append("Most common failure reasons:")
        ranked = sorted(stats.reasons.items(), key=lambda kv: -kv[1])[:5]
        for reason, count in ranked:
            lines.append(f"  {count:>8,}  {reason}")

    if not cfg.dry_run and stats.actions:
        lines.append("")
        lines.append(
            f"Restores were requested at the {cfg.tier} tier. Bulk retrievals "
            "typically complete within 5-12 hours (up to 48 for Deep Archive)."
        )
        lines.append(
            f"Restored copies expire after {cfg.days} day(s). To keep the data, "
            "copy it elsewhere or apply a lifecycle rule before then."
        )

    return lines
