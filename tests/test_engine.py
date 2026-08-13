from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from s3_glacier_restore.config import RestoreConfig
from s3_glacier_restore.engine import RestoreEngine, summarize
from s3_glacier_restore.filters import KeyFilter
from s3_glacier_restore.manifest import ManifestWriter
from s3_glacier_restore.models import Outcome
from s3_glacier_restore.state import StateFile

from .conftest import FakeS3, client_error, obj


def run(entries, cfg=None, **engine_kwargs):
    fake = FakeS3(entries)
    cfg = cfg or RestoreConfig(bucket="b", prefix="", concurrency=4)
    cfg.validate()
    engine = RestoreEngine(fake, cfg, **engine_kwargs)
    stats = engine.run()
    return fake, stats


# --------------------------------------------------------- storage classes --


def test_restores_glacier_and_deep_archive():
    fake, stats = run(
        [
            obj("a.bin", "GLACIER"),
            obj("b.bin", "DEEP_ARCHIVE"),
        ]
    )
    assert sorted(fake.restored_keys) == ["a.bin", "b.bin"]
    assert stats.get(Outcome.INITIATED) == 2
    assert stats.failures == 0


def test_glacier_ir_is_not_restorable():
    """Regression: v1 sent RestoreObject for GLACIER_IR and got InvalidObjectState."""
    fake, stats = run([obj("instant.bin", "GLACIER_IR")])
    assert fake.restored_keys == []
    assert stats.get(Outcome.NOT_RESTORABLE) == 1


@pytest.mark.parametrize("storage_class", ["STANDARD", "STANDARD_IA", "ONEZONE_IA"])
def test_non_archive_classes_skipped(storage_class):
    fake, stats = run([obj("x.bin", storage_class)])
    assert fake.restored_keys == []
    assert stats.get(Outcome.NOT_ARCHIVED) == 1


def test_missing_storage_class_defaults_to_standard():
    fake, stats = run([{"Key": "x.bin", "Size": 10, "IsLatest": True}])
    assert fake.restored_keys == []
    assert stats.get(Outcome.NOT_ARCHIVED) == 1


def test_folder_markers_are_ignored():
    fake, stats = run([obj("dir/", "GLACIER", size=0), obj("dir/f.bin", "GLACIER")])
    assert fake.restored_keys == ["dir/f.bin"]
    assert stats.scanned == 1


# ------------------------------------------------------------ restore state --


def test_in_progress_restore_is_not_reissued():
    """Regression: v1 read obj['Restore'], which lists never return, so this
    check never fired and every re-run re-requested in-flight objects."""
    fake, stats = run([obj("a.bin", "GLACIER", restore_in_progress=True)])
    assert fake.restored_keys == []
    assert stats.get(Outcome.ALREADY_IN_PROGRESS) == 1


def test_already_restored_object_is_left_alone():
    expiry = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake, stats = run([obj("a.bin", "GLACIER", restore_expiry=expiry)])
    assert fake.restored_keys == []
    assert stats.get(Outcome.ALREADY_RESTORED) == 1


def test_lister_requests_restore_status_attribute():
    fake, _ = run([obj("a.bin")])
    _, kwargs = fake.list_calls[0]
    assert kwargs["OptionalObjectAttributes"] == ["RestoreStatus"]


# ------------------------------------------------------------------- tiers --


def test_deep_archive_rejects_expedited_before_calling_aws():
    cfg = RestoreConfig(bucket="b", tier="Expedited")
    cfg.validate()
    fake, stats = run([obj("a.bin", "DEEP_ARCHIVE"), obj("b.bin", "GLACIER")], cfg=cfg)
    assert fake.restored_keys == ["b.bin"]
    assert stats.get(Outcome.NOT_RESTORABLE) == 1


def test_restore_request_shape():
    cfg = RestoreConfig(bucket="b", days=7, tier="Standard")
    cfg.validate()
    fake, _ = run([obj("a.bin")], cfg=cfg)
    assert fake.restore_calls[0]["RestoreRequest"] == {
        "Days": 7,
        "GlacierJobParameters": {"Tier": "Standard"},
    }


def test_expected_owner_and_requester_pays_propagate():
    cfg = RestoreConfig(bucket="b", expected_bucket_owner="123456789012", requester_pays=True)
    cfg.validate()
    fake, _ = run([obj("a.bin")], cfg=cfg)
    call = fake.restore_calls[0]
    assert call["ExpectedBucketOwner"] == "123456789012"
    assert call["RequestPayer"] == "requester"


# ------------------------------------------------------ intelligent tiering --


def test_intelligent_tiering_skipped_by_default():
    fake, stats = run([obj("a.bin", "INTELLIGENT_TIERING")])
    assert fake.head_calls == []
    assert stats.get(Outcome.NOT_ARCHIVED) == 1


def test_intelligent_tiering_archived_is_restored_without_days():
    cfg = RestoreConfig(bucket="b", include_intelligent_tiering=True)
    cfg.validate()
    fake = FakeS3([obj("a.bin", "INTELLIGENT_TIERING")])
    fake.head_responses["a.bin"] = {"ArchiveStatus": "DEEP_ARCHIVE_ACCESS"}
    stats = RestoreEngine(fake, cfg).run()
    assert stats.get(Outcome.INITIATED) == 1
    # Days/Tier are invalid for Intelligent-Tiering archive tiers.
    assert fake.restore_calls[0]["RestoreRequest"] == {}


def test_intelligent_tiering_not_archived_is_skipped():
    cfg = RestoreConfig(bucket="b", include_intelligent_tiering=True)
    cfg.validate()
    fake = FakeS3([obj("a.bin", "INTELLIGENT_TIERING")])
    fake.head_responses["a.bin"] = {}
    stats = RestoreEngine(fake, cfg).run()
    assert fake.restored_keys == []
    assert stats.get(Outcome.NOT_ARCHIVED) == 1


def test_intelligent_tiering_in_progress_detected_from_head():
    cfg = RestoreConfig(bucket="b", include_intelligent_tiering=True)
    cfg.validate()
    fake = FakeS3([obj("a.bin", "INTELLIGENT_TIERING")])
    fake.head_responses["a.bin"] = {
        "ArchiveStatus": "ARCHIVE_ACCESS",
        "Restore": 'ongoing-request="true"',
    }
    stats = RestoreEngine(fake, cfg).run()
    assert fake.restored_keys == []
    assert stats.get(Outcome.ALREADY_IN_PROGRESS) == 1


# ------------------------------------------------------------ error mapping --


@pytest.mark.parametrize(
    "code,outcome",
    [
        ("RestoreAlreadyInProgress", Outcome.ALREADY_IN_PROGRESS),
        ("NoSuchKey", Outcome.VANISHED),
        ("NoSuchVersion", Outcome.VANISHED),
        ("InvalidObjectState", Outcome.NOT_RESTORABLE),
        ("AccessDenied", Outcome.ACCESS_DENIED),
        ("InternalError", Outcome.ERROR),
    ],
)
def test_error_codes_map_to_outcomes(code, outcome):
    fake = FakeS3([obj("a.bin")])
    fake.restore_errors["a.bin"] = client_error(code)
    cfg = RestoreConfig(bucket="b")
    cfg.validate()
    stats = RestoreEngine(fake, cfg).run()
    assert stats.get(outcome) == 1


def test_one_failure_does_not_abort_the_run():
    fake = FakeS3([obj(f"k{i}.bin") for i in range(10)])
    fake.restore_errors["k3.bin"] = client_error("InternalError")
    cfg = RestoreConfig(bucket="b", concurrency=4)
    cfg.validate()
    stats = RestoreEngine(fake, cfg).run()
    assert stats.get(Outcome.INITIATED) == 9
    assert stats.failures == 1


# -------------------------------------------------------------- dry run etc --


def test_dry_run_issues_no_calls():
    cfg = RestoreConfig(bucket="b", dry_run=True)
    cfg.validate()
    fake, stats = run([obj("a.bin"), obj("b.bin")], cfg=cfg)
    assert fake.restore_calls == []
    assert stats.get(Outcome.WOULD_RESTORE) == 2
    assert stats.action_bytes == 2048


def test_filters_applied_before_any_api_call():
    fake, stats = run(
        [obj("keep.bin"), obj("junk/.DS_Store")],
        key_filter=KeyFilter(excludes=[".DS_Store"]),
    )
    assert fake.restored_keys == ["keep.bin"]
    assert stats.get(Outcome.FILTERED) == 1


def test_max_objects_limits_work():
    cfg = RestoreConfig(bucket="b", max_objects=3, concurrency=2)
    cfg.validate()
    fake, stats = run([obj(f"k{i:03d}.bin") for i in range(50)], cfg=cfg)
    assert len(fake.restored_keys) == 3


def test_prefix_is_passed_through():
    cfg = RestoreConfig(bucket="b", prefix="data/")
    cfg.validate()
    fake, _ = run([obj("data/a.bin"), obj("other/b.bin")], cfg=cfg)
    assert fake.restored_keys == ["data/a.bin"]


# ------------------------------------------------------------- concurrency --


def test_large_run_restores_every_object_exactly_once():
    keys = [obj(f"k{i:05d}.bin") for i in range(1500)]
    cfg = RestoreConfig(bucket="b", concurrency=16, page_size=100)
    cfg.validate()
    fake, stats = run(keys, cfg=cfg)
    assert len(fake.restored_keys) == 1500
    assert len(set(fake.restored_keys)) == 1500
    assert stats.scanned == 1500


def test_stop_event_halts_the_run_and_still_summarizes():
    stop = threading.Event()
    fake = FakeS3([obj(f"k{i:05d}.bin") for i in range(500)])
    cfg = RestoreConfig(bucket="b", concurrency=2)
    cfg.validate()

    original = fake.restore_object

    def restore_then_stop(**kwargs):
        result = original(**kwargs)
        if len(fake.restore_calls) >= 5:
            stop.set()
        return result

    fake.restore_object = restore_then_stop
    stats = RestoreEngine(fake, cfg, stop_event=stop).run()
    assert stats.scanned < 500
    assert stats.get(Outcome.INITIATED) >= 5


# ------------------------------------------------------------------- state --


def test_state_file_makes_a_rerun_a_no_op(tmp_path):
    path = str(tmp_path / "run.state")
    entries = [obj("a.bin"), obj("b.bin")]
    cfg = RestoreConfig(bucket="b", state_file=path)
    cfg.validate()

    fake = FakeS3(entries)
    with StateFile(path) as state:
        RestoreEngine(fake, cfg, state=state).run()
    assert sorted(fake.restored_keys) == ["a.bin", "b.bin"]

    fake2 = FakeS3(entries)
    state2 = StateFile(path)
    assert state2.load() == 2
    with state2:
        stats = RestoreEngine(fake2, cfg, state=state2).run()
    assert fake2.restored_keys == []
    assert stats.get(Outcome.RESUMED) == 2


def test_failed_objects_are_not_recorded_in_state(tmp_path):
    path = str(tmp_path / "run.state")
    fake = FakeS3([obj("ok.bin"), obj("bad.bin")])
    fake.restore_errors["bad.bin"] = client_error("InternalError")
    cfg = RestoreConfig(bucket="b", state_file=path)
    cfg.validate()
    with StateFile(path) as state:
        RestoreEngine(fake, cfg, state=state).run()

    reloaded = StateFile(path)
    reloaded.load()
    assert reloaded.contains("ok.bin")
    assert not reloaded.contains("bad.bin")


# ---------------------------------------------------------------- versions --


def test_versions_mode_restores_noncurrent_versions():
    entries = [
        obj("a.bin", version_id="v2", is_latest=True),
        obj("a.bin", version_id="v1", is_latest=False),
    ]
    cfg = RestoreConfig(bucket="b", versions=True)
    cfg.validate()
    fake, stats = run(entries, cfg=cfg)
    assert {c["VersionId"] for c in fake.restore_calls} == {"v1", "v2"}
    assert stats.get(Outcome.INITIATED) == 2


def test_current_only_mode_ignores_noncurrent_versions():
    entries = [
        obj("a.bin", version_id="v2", is_latest=True),
        obj("a.bin", version_id="v1", is_latest=False),
    ]
    fake, stats = run(entries)
    assert len(fake.restore_calls) == 1


def test_state_ids_distinguish_versions(tmp_path):
    path = str(tmp_path / "v.state")
    entries = [
        obj("a.bin", version_id="v2", is_latest=True),
        obj("a.bin", version_id="v1", is_latest=False),
    ]
    cfg = RestoreConfig(bucket="b", versions=True, state_file=path)
    cfg.validate()
    with StateFile(path) as state:
        RestoreEngine(FakeS3(entries), cfg, state=state).run()
    reloaded = StateFile(path)
    assert reloaded.load() == 2


# ---------------------------------------------------------------- manifest --


def test_manifest_lists_eligible_objects_only(tmp_path):
    path = str(tmp_path / "m.csv")
    cfg = RestoreConfig(bucket="my-bucket", dry_run=True)
    cfg.validate()
    fake = FakeS3([obj("a.bin", "GLACIER"), obj("plain.txt", "STANDARD")])
    with ManifestWriter(path, "my-bucket") as manifest:
        RestoreEngine(fake, cfg, manifest=manifest).run()
    with open(path, encoding="utf-8", newline="") as fh:
        assert fh.read() == "my-bucket,a.bin\r\n"


def test_manifest_quotes_awkward_keys(tmp_path):
    path = str(tmp_path / "m.csv")
    cfg = RestoreConfig(bucket="b", dry_run=True)
    cfg.validate()
    with ManifestWriter(path, "b") as manifest:
        RestoreEngine(FakeS3([obj("odd,key.bin")]), cfg, manifest=manifest).run()
    with open(path, encoding="utf-8", newline="") as fh:
        assert fh.read() == 'b,"odd,key.bin"\r\n'


# ----------------------------------------------------------------- summary --


def test_summary_reports_actions_and_failures():
    fake = FakeS3([obj("a.bin"), obj("b.bin"), obj("c.txt", "STANDARD")])
    fake.restore_errors["b.bin"] = client_error("InternalError", message="boom")
    cfg = RestoreConfig(bucket="b")
    cfg.validate()
    engine = RestoreEngine(fake, cfg)
    stats = engine.run()
    text = "\n".join(summarize(stats, cfg, 12.5))
    assert "Objects scanned" in text
    assert "Restore requests issued" in text
    assert "Failed" in text
    assert "boom" in text
    assert "expire after 30 day(s)" in text


def test_summary_lists_only_real_failure_reasons():
    """Benign skips must not be reported under 'failure reasons'."""
    fake = FakeS3(
        [obj("a.bin", "GLACIER"), obj("plain.txt", "STANDARD"), obj("ir.bin", "GLACIER_IR")]
    )
    fake.restore_errors["a.bin"] = client_error("InternalError", message="real failure")
    cfg = RestoreConfig(bucket="b")
    cfg.validate()
    stats = RestoreEngine(fake, cfg).run()

    assert list(stats.reasons) == ["InternalError: real failure"]

    block = "\n".join(summarize(stats, cfg, 1.0)).split("Most common failure reasons:")[1]
    listed = [line.strip() for line in block.splitlines() if line.strip()]
    assert listed == ["1  InternalError: real failure"]
