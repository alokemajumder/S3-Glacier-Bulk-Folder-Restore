"""Tests for the GUI's logic layer (GitHub issue #1).

No display is opened. :class:`GuiRunner` and :func:`config_from_fields` are
deliberately free of Tk imports so the parts that can actually be wrong --
validation, threading, event delivery -- are testable in CI.
"""

from __future__ import annotations

import logging
import queue

import pytest

from s3_glacier_restore.config import ConfigError
from s3_glacier_restore.gui import (
    Fields,
    GuiRunner,
    QueueLogHandler,
    config_from_fields,
    main,
)

from .conftest import FakeS3, client_error, obj


def drain(runner: GuiRunner) -> dict:
    """Collect every event the worker produced, keyed by kind."""
    events: dict = {}
    while True:
        try:
            kind, payload = runner.events.get_nowait()
        except queue.Empty:
            return events
        events.setdefault(kind, []).append(payload)


# ------------------------------------------------------------ form to config --


def test_minimal_fields_produce_a_valid_config():
    cfg = config_from_fields(Fields(bucket="my-bucket", prefix="photos/"))
    assert cfg.bucket == "my-bucket"
    assert cfg.prefix == "photos/"
    assert cfg.days == 30
    assert cfg.dry_run is True  # the GUI defaults to the safe option


def test_whitespace_is_trimmed():
    cfg = config_from_fields(Fields(bucket="  my-bucket  ", prefix="  photos/  "))
    assert cfg.bucket == "my-bucket"
    assert cfg.prefix == "photos/"


def test_blank_bucket_is_rejected():
    with pytest.raises(ConfigError, match="bucket name"):
        config_from_fields(Fields(bucket="   "))


@pytest.mark.parametrize("value", ["thirty", "7.5", "1e3", "-"])
def test_non_numeric_days_gets_a_readable_message(value):
    with pytest.raises(ConfigError, match="whole number"):
        config_from_fields(Fields(bucket="b", days=value))


def test_blank_numbers_fall_back_to_defaults():
    cfg = config_from_fields(Fields(bucket="b", days="", concurrency=""))
    assert (cfg.days, cfg.concurrency) == (30, 16)


def test_out_of_range_days_is_rejected():
    with pytest.raises(ConfigError, match="--days"):
        config_from_fields(Fields(bucket="b", days="0"))


def test_exclude_field_splits_on_commas():
    cfg = config_from_fields(Fields(bucket="b", exclude=" *.tmp , .DS_Store ,, "))
    assert cfg.excludes == ["*.tmp", ".DS_Store"]


def test_skip_file_is_merged(tmp_path):
    skip = tmp_path / "skip.txt"
    skip.write_text("# junk\nThumbs.db\n", encoding="utf-8")
    cfg = config_from_fields(Fields(bucket="b", exclude="*.tmp", skip_file=str(skip)))
    assert cfg.excludes == ["*.tmp", "Thumbs.db"]


def test_unreadable_skip_file_gets_a_readable_message():
    with pytest.raises(ConfigError, match="skip-list file"):
        config_from_fields(Fields(bucket="b", skip_file="/nonexistent/skip.txt"))


def test_blank_optional_fields_become_none():
    cfg = config_from_fields(Fields(bucket="b"))
    assert cfg.region is None
    assert cfg.profile is None
    assert cfg.access_key_id is None
    assert cfg.state_file is None


def test_checkboxes_map_through():
    cfg = config_from_fields(
        Fields(bucket="b", dry_run=False, versions=True, include_intelligent_tiering=True)
    )
    assert cfg.dry_run is False
    assert cfg.versions is True
    assert cfg.include_intelligent_tiering is True


# ------------------------------------------------------------------- runner --


@pytest.fixture
def fake_runner():
    fake = FakeS3([obj("data/a.bin", "GLACIER"), obj("data/b.txt", "STANDARD")])
    return GuiRunner(client_factory=lambda cfg: fake), fake


def test_check_reports_samples_and_finishes(fake_runner):
    runner, _ = fake_runner
    runner.check(config_from_fields(Fields(bucket="b", prefix="data/")))
    runner.join(5)
    events = drain(runner)

    samples, has_more = events["samples"][0]
    assert [o.key for o in samples] == ["data/a.bin", "data/b.txt"]
    assert has_more is False
    assert "checked" in events
    assert len(events["finished"]) == 1


def test_dry_run_restores_nothing(fake_runner):
    runner, fake = fake_runner
    runner.restore(config_from_fields(Fields(bucket="b", prefix="data/", dry_run=True)))
    runner.join(5)
    events = drain(runner)

    assert fake.restore_calls == []
    stats = events["finished"][0]
    assert stats.actions == 1
    assert any("Dry run complete" in s for s in events["status"])


def test_live_run_issues_restores(fake_runner):
    runner, fake = fake_runner
    runner.restore(config_from_fields(Fields(bucket="b", prefix="data/", dry_run=False)))
    runner.join(5)
    events = drain(runner)

    assert fake.restored_keys == ["data/a.bin"]
    assert events["finished"][0].actions == 1
    assert any(k == "summary" for k in events)


def test_errors_are_delivered_not_raised():
    def boom(cfg):
        raise RuntimeError("connection refused")

    runner = GuiRunner(client_factory=boom)
    runner.check(config_from_fields(Fields(bucket="b")))
    runner.join(5)
    events = drain(runner)

    assert "connection refused" in events["error"][0]
    assert len(events["finished"]) == 1  # the UI is always re-enabled


def test_aws_errors_are_delivered_verbatim():
    from s3_glacier_restore.aws import AwsError

    runner = GuiRunner(
        client_factory=lambda cfg: (_ for _ in ()).throw(AwsError("Bucket 'x' does not exist."))
    )
    runner.restore(config_from_fields(Fields(bucket="x")))
    runner.join(5)
    assert drain(runner)["error"] == ["Bucket 'x' does not exist."]


def test_engine_failures_do_not_become_gui_errors(fake_runner):
    runner, fake = fake_runner
    fake.restore_errors["data/a.bin"] = client_error("InternalError")
    runner.restore(config_from_fields(Fields(bucket="b", prefix="data/", dry_run=False)))
    runner.join(5)
    events = drain(runner)

    assert "error" not in events  # per-object failures belong in the summary
    assert events["finished"][0].failures == 1


def test_cancel_stops_the_run():
    fake = FakeS3([obj(f"k{i:05d}.bin") for i in range(2000)])
    runner = GuiRunner(client_factory=lambda cfg: fake)
    cfg = config_from_fields(Fields(bucket="b", dry_run=False, concurrency="2"))

    original = fake.restore_object

    def restore_then_cancel(**kwargs):
        result = original(**kwargs)
        if len(fake.restore_calls) >= 5:
            runner.cancel()
        return result

    fake.restore_object = restore_then_cancel
    runner.restore(cfg)
    runner.join(15)

    events = drain(runner)
    assert events["finished"][0].scanned < 2000
    assert any("Stopped" in s for s in events["status"])


def test_busy_runner_refuses_a_second_operation(fake_runner):
    runner, fake = fake_runner
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def slow_factory(cfg):
        started.set()
        release.wait(5)
        return fake

    runner._client_factory = slow_factory
    runner.check(config_from_fields(Fields(bucket="b")))
    started.wait(5)
    try:
        assert runner.busy is True
        with pytest.raises(RuntimeError, match="already running"):
            runner.restore(config_from_fields(Fields(bucket="b")))
    finally:
        release.set()
        runner.join(5)


def test_state_file_resumes_between_gui_runs(fake_runner, tmp_path):
    runner, fake = fake_runner
    state = str(tmp_path / "gui.state")
    fields = Fields(bucket="b", prefix="data/", dry_run=False, state_file=state)

    runner.restore(config_from_fields(fields))
    runner.join(5)
    drain(runner)
    assert fake.restored_keys == ["data/a.bin"]

    fake.restore_calls.clear()
    runner.restore(config_from_fields(fields))
    runner.join(5)
    events = drain(runner)
    assert fake.restore_calls == []
    assert any("Resuming" in s for s in events["status"])


# ------------------------------------------------------------- log plumbing --


def test_queue_log_handler_forwards_records():
    sink: queue.Queue = queue.Queue()
    handler = QueueLogHandler(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test.gui.handler")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info("restoring %s", "a.bin")
        logger.error("nope")
    finally:
        logger.removeHandler(handler)

    assert sink.get_nowait() == ("log", (logging.INFO, "restoring a.bin"))
    assert sink.get_nowait() == ("log", (logging.ERROR, "nope"))


def test_engine_logs_reach_the_queue(fake_runner):
    """The GUI shows engine progress, so its logger must feed the queue."""
    runner, _ = fake_runner
    handler = QueueLogHandler(runner.events)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("s3_glacier_restore")
    previous = logger.level
    logger.setLevel(logging.INFO)  # RestoreApp._attach_logging does the same
    logger.addHandler(handler)
    try:
        runner.restore(config_from_fields(Fields(bucket="b", prefix="data/", dry_run=False)))
        runner.join(5)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    logs = [text for level, text in drain(runner).get("log", [])]
    assert any("data/a.bin" in text for text in logs)


# ------------------------------------------------------------- availability --


def test_main_explains_how_to_install_tkinter(monkeypatch, capsys):
    import s3_glacier_restore.gui as gui

    monkeypatch.setattr(gui, "TK_AVAILABLE", False)
    assert main() == 2
    err = capsys.readouterr().err
    assert "python3-tk" in err
    assert "s3-glacier-restore --help" in err
