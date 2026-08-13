from __future__ import annotations

import pytest

from s3_glacier_restore.config import ConfigError, RestoreConfig


def make(**kwargs) -> RestoreConfig:
    params = {"bucket": "b"}
    params.update(kwargs)
    return RestoreConfig(**params)


def test_defaults_are_valid():
    cfg = make()
    cfg.validate()
    assert cfg.tier == "Bulk"
    assert cfg.days == 30


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"bucket": ""}, "bucket name is required"),
        ({"bucket": "my-bucket/path"}, "--prefix"),
        ({"tier": "Cheap"}, "Unknown retrieval tier"),
        ({"days": 0}, "--days"),
        ({"days": -1}, "--days"),
        ({"days": 100000}, "--days"),
        ({"concurrency": 0}, "--concurrency"),
        ({"concurrency": 1000}, "throttled"),
        ({"page_size": 0}, "--page-size"),
        ({"page_size": 5000}, "--page-size"),
        ({"max_objects": 0}, "--max-objects"),
        ({"max_attempts": 0}, "--max-attempts"),
        ({"access_key_id": "AKIA"}, "or neither"),
        ({"secret_access_key": "s"}, "or neither"),
        ({"session_token": "t"}, "--session-token"),
        (
            {"profile": "p", "access_key_id": "AKIA", "secret_access_key": "s"},
            "not both",
        ),
    ],
)
def test_validation_rejects(kwargs, fragment):
    with pytest.raises(ConfigError) as exc:
        make(**kwargs).validate()
    assert fragment in str(exc.value)


def test_expedited_not_supported_for_deep_archive():
    assert make(tier="Expedited").tier_supported_for_deep_archive is False
    assert make(tier="Bulk").tier_supported_for_deep_archive is True
    assert make(tier="Standard").tier_supported_for_deep_archive is True


def test_describe_mentions_dry_run_and_prefix():
    lines = "\n".join(make(dry_run=True, prefix="").describe())
    assert "DRY RUN" in lines
    assert "entire bucket" in lines
