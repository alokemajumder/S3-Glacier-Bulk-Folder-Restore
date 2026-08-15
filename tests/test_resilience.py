"""Failure-path tests.

A run can last hours over millions of objects. The invariant these tests
protect is that nothing short of an explicit interrupt ends it early: a worker
must always come back with an outcome, however it failed.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError

from s3_glacier_restore import cli
from s3_glacier_restore.aws import AwsError
from s3_glacier_restore.config import RestoreConfig
from s3_glacier_restore.engine import RestoreEngine
from s3_glacier_restore.models import Outcome

from .conftest import FakeS3, obj


def run_with_restore_failure(exc, count=1):
    fake = FakeS3([obj(f"k{i}.bin") for i in range(count)])
    for i in range(count):
        fake.restore_errors[f"k{i}.bin"] = exc
    cfg = RestoreConfig(bucket="b", concurrency=2)
    cfg.validate()
    return fake, RestoreEngine(fake, cfg).run()


# ------------------------------------------------- workers must never raise --


def test_network_error_is_recorded_not_raised():
    _, stats = run_with_restore_failure(
        EndpointConnectionError(endpoint_url="https://s3.amazonaws.com")
    )
    assert stats.get(Outcome.ERROR) == 1


def test_connection_closed_is_recorded_not_raised():
    _, stats = run_with_restore_failure(
        ConnectionClosedError(endpoint_url="https://s3.amazonaws.com")
    )
    assert stats.get(Outcome.ERROR) == 1


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad"), MemoryError()])
def test_unexpected_exceptions_are_contained(exc):
    """Anything a worker can throw must become an outcome, not a crash."""
    _, stats = run_with_restore_failure(exc)
    assert stats.get(Outcome.ERROR) == 1


def test_the_run_completes_when_every_object_fails():
    fake, stats = run_with_restore_failure(RuntimeError("boom"), count=25)
    assert stats.scanned == 25
    assert stats.failures == 25
    assert stats.get(Outcome.INITIATED) == 0


def test_failure_detail_reaches_the_summary():
    fake = FakeS3([obj("a.bin")])
    fake.restore_errors["a.bin"] = RuntimeError("disk on fire")
    cfg = RestoreConfig(bucket="b")
    cfg.validate()
    stats = RestoreEngine(fake, cfg).run()
    assert any("disk on fire" in reason for reason in stats.reasons)


def test_head_failure_during_intelligent_tiering_is_contained():
    from .conftest import client_error

    cfg = RestoreConfig(bucket="b", include_intelligent_tiering=True)
    cfg.validate()
    fake = FakeS3([obj("a.bin", "INTELLIGENT_TIERING")])
    fake.head_errors["a.bin"] = client_error("AccessDenied", "HeadObject")
    stats = RestoreEngine(fake, cfg).run()
    assert stats.get(Outcome.ACCESS_DENIED) == 1
    assert fake.restored_keys == []


def test_a_listing_failure_propagates():
    """Listing is not per-object: if it breaks, the run cannot be trusted."""

    class BrokenLister(FakeS3):
        def get_paginator(self, operation):
            raise RuntimeError("listing exploded")

    cfg = RestoreConfig(bucket="b")
    cfg.validate()
    with pytest.raises(RuntimeError, match="listing exploded"):
        RestoreEngine(BrokenLister(), cfg).run()


# ---------------------------------------------------------- CLI error paths --


@pytest.fixture
def patched_aws(monkeypatch):
    fake = FakeS3([obj("data/a.bin", "GLACIER")])
    monkeypatch.setattr(cli, "build_session", lambda cfg: object())
    monkeypatch.setattr(cli, "resolve_bucket_region", lambda session, cfg: "us-east-1")
    monkeypatch.setattr(cli, "build_client", lambda session, cfg, region: fake)
    monkeypatch.setattr(cli, "verify_access", lambda client, cfg: None)
    monkeypatch.setattr(cli, "caller_identity", lambda session, cfg: "arn:test")
    return fake


def test_aws_error_exits_with_the_config_code(monkeypatch, patched_aws):
    monkeypatch.setattr(
        cli, "verify_access", lambda client, cfg: (_ for _ in ()).throw(AwsError("no bucket"))
    )
    assert cli.main(["--bucket", "b", "--prefix", "", "--yes"]) == cli.EXIT_CONFIG


def test_missing_credentials_message_is_actionable(monkeypatch, patched_aws, capsys):
    monkeypatch.setattr(
        cli,
        "build_session",
        lambda cfg: (_ for _ in ()).throw(
            AwsError("No AWS credentials found. Configure them with 'aws configure'.")
        ),
    )
    assert cli.main(["--bucket", "b", "--prefix", "", "--yes"]) == cli.EXIT_CONFIG
    # The package logger does not propagate, so read the handler's stream.
    assert "No AWS credentials" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(monkeypatch, patched_aws):
    monkeypatch.setattr(
        cli, "verify_access", lambda client, cfg: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    assert cli.main(["--bucket", "b", "--prefix", "", "--yes"]) == cli.EXIT_INTERRUPTED


def test_config_error_exits_2(patched_aws):
    assert cli.main(["--bucket", "b", "--prefix", "", "--days", "0"]) == cli.EXIT_CONFIG


# ------------------------------------------------------------- interactive --


def test_interactive_prompts_fill_missing_values(monkeypatch, patched_aws):
    answers = iter(["my-bucket", "photos/", "14", "", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    args = cli.build_parser().parse_args([])
    cli.fill_interactively(args)

    assert args.bucket == "my-bucket"
    assert args.prefix == "photos/"
    assert args.days == 14
    assert args.dry_run is True


def test_interactive_reprompts_after_a_bad_number(monkeypatch, capsys):
    answers = iter(["bkt", "", "thirty", "-1x", "9", "", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    args = cli.build_parser().parse_args([])
    cli.fill_interactively(args)
    assert args.days == 9
    assert "not a whole number" in capsys.readouterr().out


def test_interactive_eof_becomes_keyboard_interrupt(monkeypatch):
    def raise_eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    with pytest.raises(KeyboardInterrupt):
        cli.fill_interactively(cli.build_parser().parse_args([]))


def test_secret_is_read_without_echo(monkeypatch):
    """v1 read the secret with input(), printing it to the terminal."""
    captured = {}

    def fake_getpass(prompt):
        captured["prompt"] = prompt
        return "s3cr3t"

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    args = cli.build_parser().parse_args(
        ["--bucket", "b", "--access-key-id", "AKIA", "--secret-access-key", "-"]
    )
    cli.resolve_secret(args)
    assert args.secret_access_key == "s3cr3t"
    assert "hidden" in captured["prompt"]


def test_secret_prompted_when_only_the_key_id_is_given(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "s3cr3t")
    args = cli.build_parser().parse_args(["--bucket", "b", "--access-key-id", "AKIA"])
    cli.resolve_secret(args)
    assert args.secret_access_key == "s3cr3t"


def test_secret_is_never_printed_in_the_preflight(patched_aws, capsys):
    cli.main(
        [
            "--bucket",
            "b",
            "--prefix",
            "",
            "--dry-run",
            "--access-key-id",
            "AKIAIOSFODNN7EXAMPLE",
            "--secret-access-key",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        ]
    )
    out = capsys.readouterr().out
    assert "wJalrXUtnFEMI" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "MPLE" in out  # masked form still identifies the key
