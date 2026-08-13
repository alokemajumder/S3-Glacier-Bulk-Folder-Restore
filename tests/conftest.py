"""Shared fixtures: an in-memory S3 stand-in with realistic pagination."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError

from s3_glacier_restore.config import RestoreConfig


def client_error(code: str, operation: str = "RestoreObject", message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


class FakePaginator:
    """Mimics botocore's paginator closely enough for the lister."""

    def __init__(self, fake: FakeS3, operation: str) -> None:
        self.fake = fake
        self.operation = operation

    def paginate(self, **kwargs) -> list[dict[str, Any]]:
        self.fake.list_calls.append((self.operation, kwargs))
        prefix = kwargs.get("Prefix", "") or ""
        page_size = (kwargs.get("PaginationConfig") or {}).get("PageSize", 1000)
        entries = [e for e in self.fake.entries if e["Key"].startswith(prefix)]

        if self.operation == "list_object_versions":
            container = "Versions"
        else:
            container = "Contents"
            entries = [e for e in entries if e.get("IsLatest", True)]

        pages = []
        for start in range(0, max(len(entries), 1), page_size):
            chunk = entries[start : start + page_size]
            if not chunk and pages:
                break
            page = {"IsTruncated": start + page_size < len(entries)}
            if chunk:
                page[container] = [dict(entry) for entry in chunk]
            pages.append(page)
        return pages


class FakeS3:
    """Records calls and returns scripted responses. Thread-safe."""

    def __init__(self, entries: list[dict[str, Any]] | None = None) -> None:
        self.entries = entries or []
        self.restore_calls: list[dict[str, Any]] = []
        self.head_calls: list[dict[str, Any]] = []
        self.list_calls: list[Any] = []
        self.restore_errors: dict[str, ClientError] = {}
        self.head_responses: dict[str, dict[str, Any]] = {}
        self.head_errors: dict[str, ClientError] = {}
        self._lock = threading.Lock()

    def get_paginator(self, operation: str) -> FakePaginator:
        return FakePaginator(self, operation)

    def restore_object(self, **kwargs):
        with self._lock:
            self.restore_calls.append(kwargs)
        error = self.restore_errors.get(kwargs["Key"])
        if error:
            raise error
        return {"ResponseMetadata": {"HTTPStatusCode": 202}}

    def head_object(self, **kwargs):
        with self._lock:
            self.head_calls.append(kwargs)
        error = self.head_errors.get(kwargs["Key"])
        if error:
            raise error
        return self.head_responses.get(kwargs["Key"], {})

    def head_bucket(self, **kwargs):
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_bucket_location(self, **kwargs):
        return {"LocationConstraint": "eu-central-1"}

    @property
    def restored_keys(self) -> list[str]:
        return [call["Key"] for call in self.restore_calls]


def obj(
    key: str,
    storage_class: str = "GLACIER",
    size: int = 1024,
    restore_in_progress: bool | None = None,
    restore_expiry: datetime | None = None,
    version_id: str | None = None,
    is_latest: bool = True,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "Key": key,
        "Size": size,
        "StorageClass": storage_class,
        "IsLatest": is_latest,
    }
    if version_id:
        entry["VersionId"] = version_id
    if restore_in_progress is not None or restore_expiry is not None:
        entry["RestoreStatus"] = {}
        if restore_in_progress is not None:
            entry["RestoreStatus"]["IsRestoreInProgress"] = restore_in_progress
        if restore_expiry is not None:
            entry["RestoreStatus"]["RestoreExpiryDate"] = restore_expiry
    return entry


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def cfg() -> RestoreConfig:
    config = RestoreConfig(bucket="test-bucket", prefix="data/", concurrency=4)
    config.validate()
    return config


@pytest.fixture
def fake() -> FakeS3:
    return FakeS3()
