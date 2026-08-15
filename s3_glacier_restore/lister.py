"""Streaming enumeration of objects under a prefix.

Everything is a generator: a bucket with 50 million keys is walked page by
page and never materialised in memory.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .config import RestoreConfig
from .models import S3Object

log = logging.getLogger(__name__)


def _restore_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Extract restore state, present only when RestoreStatus was requested.

    Note this is ``RestoreStatus``, not ``Restore``. ``Restore`` is a
    HeadObject/GetObject response header and never appears in a list response
    -- reading it here is why versions before 2.0 never detected in-flight
    restores.
    """
    status = entry.get("RestoreStatus") or {}
    return {
        "restore_in_progress": bool(status.get("IsRestoreInProgress", False)),
        "restore_expiry": status.get("RestoreExpiryDate"),
    }


def iter_objects(client, cfg: RestoreConfig) -> Iterator[S3Object]:
    """Yield every object (or object version) under the configured prefix."""
    if cfg.versions:
        yield from _iter_versions(client, cfg)
    else:
        yield from _iter_current(client, cfg)


def _common_kwargs(cfg: RestoreConfig) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "Bucket": cfg.bucket,
        "Prefix": cfg.prefix,
        # Ask S3 to tell us which objects already have a restore in flight or
        # a restored copy available, so we do not pay to re-request them.
        "OptionalObjectAttributes": ["RestoreStatus"],
    }
    if cfg.expected_bucket_owner:
        kwargs["ExpectedBucketOwner"] = cfg.expected_bucket_owner
    if cfg.requester_pays:
        kwargs["RequestPayer"] = "requester"
    return kwargs


def _iter_current(client, cfg: RestoreConfig) -> Iterator[S3Object]:
    paginator = client.get_paginator("list_objects_v2")
    pages = paginator.paginate(PaginationConfig={"PageSize": cfg.page_size}, **_common_kwargs(cfg))
    for page in pages:
        for entry in page.get("Contents", ()):
            key = entry["Key"]
            # A zero-byte key ending in '/' is a console-created folder marker,
            # not data. Restoring it is a wasted request.
            if key.endswith("/") and entry.get("Size", 0) == 0:
                continue
            yield S3Object(
                key=key,
                size=entry.get("Size", 0) or 0,
                storage_class=entry.get("StorageClass", "STANDARD"),
                **_restore_fields(entry),
            )


def _iter_versions(client, cfg: RestoreConfig) -> Iterator[S3Object]:
    paginator = client.get_paginator("list_object_versions")
    pages = paginator.paginate(PaginationConfig={"PageSize": cfg.page_size}, **_common_kwargs(cfg))
    for page in pages:
        # Delete markers carry no data and cannot be restored.
        for entry in page.get("Versions", ()):
            key = entry["Key"]
            if key.endswith("/") and entry.get("Size", 0) == 0:
                continue
            yield S3Object(
                key=key,
                size=entry.get("Size", 0) or 0,
                storage_class=entry.get("StorageClass", "STANDARD"),
                version_id=entry.get("VersionId"),
                is_latest=bool(entry.get("IsLatest", False)),
                **_restore_fields(entry),
            )


def sample_objects(client, cfg: RestoreConfig, limit: int = 5):
    """Return ``(sample_keys, has_more)`` for the confirmation screen.

    Deliberately stops after ``limit`` objects: counting an entire archive
    bucket just to print a preview can take minutes and costs LIST requests.
    """
    sample = []
    has_more = False
    for obj in iter_objects(client, cfg):
        if len(sample) >= limit:
            has_more = True
            break
        sample.append(obj)
    return sample, has_more


def head_object(client, cfg: RestoreConfig, obj: S3Object) -> dict[str, Any]:
    """HeadObject for a single object, used to inspect Intelligent-Tiering."""
    kwargs: dict[str, Any] = {"Bucket": cfg.bucket, "Key": obj.key}
    if obj.version_id:
        kwargs["VersionId"] = obj.version_id
    if cfg.expected_bucket_owner:
        kwargs["ExpectedBucketOwner"] = cfg.expected_bucket_owner
    if cfg.requester_pays:
        kwargs["RequestPayer"] = "requester"
    return client.head_object(**kwargs)


def format_bytes(num: float | None) -> str:
    """Human-readable size using binary units."""
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} PiB"  # pragma: no cover - unreachable
