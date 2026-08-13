from __future__ import annotations

import pytest

from s3_glacier_restore import cli
from s3_glacier_restore.config import ConfigError

from .conftest import FakeS3, obj


def parse(*argv):
    return cli.build_parser().parse_args(list(argv))


# ------------------------------------------------------------ arg parsing --


def test_minimal_args():
    args = parse("--bucket", "my-bucket")
    cfg = cli.config_from_args(args)
    assert cfg.bucket == "my-bucket"
    assert cfg.days == 30
    assert cfg.tier == "Bulk"
    assert cfg.concurrency == 16


def test_repeatable_include_exclude():
    args = parse(
        "--bucket", "b", "--exclude", "*.tmp", "--exclude", ".DS_Store", "--include", "*.tif"
    )
    cfg = cli.config_from_args(args)
    assert cfg.excludes == ["*.tmp", ".DS_Store"]
    assert cfg.includes == ["*.tif"]


def test_skip_file_merges_with_exclude_flags(tmp_path):
    skip = tmp_path / "skip.txt"
    skip.write_text("# junk\nThumbs.db\n", encoding="utf-8")
    args = parse("--bucket", "b", "--exclude", "*.tmp", "--skip-file", str(skip))
    cfg = cli.config_from_args(args)
    assert cfg.excludes == ["*.tmp", "Thumbs.db"]


def test_legacy_skiplist_alias(tmp_path):
    skip = tmp_path / "skip.txt"
    skip.write_text("Thumbs.db\n", encoding="utf-8")
    args = parse("--bucket", "b", "--skiplist", str(skip))
    assert cli.config_from_args(args).excludes == ["Thumbs.db"]


def test_missing_skip_file_is_a_config_error():
    args = parse("--bucket", "b", "--skip-file", "/nonexistent/skip.txt")
    with pytest.raises(ConfigError):
        cli.config_from_args(args)


def test_bad_tier_rejected_by_argparse():
    with pytest.raises(SystemExit):
        parse("--bucket", "b", "--tier", "Cheap")


def test_bad_days_rejected_by_validation():
    with pytest.raises(ConfigError):
        cli.config_from_args(parse("--bucket", "b", "--days", "0"))


def test_non_numeric_days_rejected_by_argparse():
    # v1 crashed with an unhandled ValueError here.
    with pytest.raises(SystemExit):
        parse("--bucket", "b", "--days", "seven")


def test_bucket_with_path_is_rejected():
    with pytest.raises(ConfigError) as exc:
        cli.config_from_args(parse("--bucket", "my-bucket/data"))
    assert "--prefix" in str(exc.value)


# ------------------------------------------------------------------ masking --


@pytest.mark.parametrize(
    "value,expected",
    [(None, "(none)"), ("", "(none)"), ("abc", "***"), ("AKIAIOSFODNN7EXAMPLE", "*" * 16 + "MPLE")],
)
def test_mask(value, expected):
    assert cli.mask(value) == expected


# --------------------------------------------------------------- full runs --


@pytest.fixture
def patched_aws(monkeypatch):
    """Route the CLI at a FakeS3 instead of AWS."""
    fake = FakeS3([obj("data/a.bin", "GLACIER"), obj("data/b.txt", "STANDARD")])
    monkeypatch.setattr(cli, "build_session", lambda cfg: object())
    monkeypatch.setattr(cli, "resolve_bucket_region", lambda session, cfg: "us-east-1")
    monkeypatch.setattr(cli, "build_client", lambda session, cfg, region: fake)
    monkeypatch.setattr(cli, "verify_access", lambda client, cfg: None)
    monkeypatch.setattr(cli, "caller_identity", lambda session, cfg: "arn:aws:iam::1:user/t")
    return fake


def test_dry_run_needs_no_confirmation(patched_aws, capsys):
    code = cli.main(["--bucket", "b", "--prefix", "data/", "--dry-run"])
    assert code == cli.EXIT_OK
    assert patched_aws.restore_calls == []
    assert "DRY RUN" in capsys.readouterr().out


def test_live_run_with_yes(patched_aws):
    code = cli.main(["--bucket", "b", "--prefix", "data/", "--yes", "--quiet"])
    assert code == cli.EXIT_OK
    assert patched_aws.restored_keys == ["data/a.bin"]


def test_live_run_refuses_without_confirmation_when_not_a_tty(patched_aws, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    code = cli.main(["--bucket", "b", "--prefix", "data/"])
    assert code == cli.EXIT_ABORTED
    assert patched_aws.restore_calls == []


def test_declining_the_prompt_aborts(patched_aws, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    code = cli.main(["--bucket", "b", "--prefix", "data/"])
    assert code == cli.EXIT_ABORTED
    assert patched_aws.restore_calls == []


def test_failures_produce_a_nonzero_exit(patched_aws):
    from .conftest import client_error

    patched_aws.restore_errors["data/a.bin"] = client_error("InternalError")
    code = cli.main(["--bucket", "b", "--prefix", "data/", "--yes", "--quiet"])
    assert code == cli.EXIT_FAILURES


def test_bucket_required_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2
    assert "--bucket is required" in capsys.readouterr().err


def test_state_file_round_trip_through_cli(patched_aws, tmp_path):
    state = str(tmp_path / "run.state")
    argv = ["--bucket", "b", "--prefix", "data/", "--yes", "--quiet", "--state-file", state]
    assert cli.main(argv) == cli.EXIT_OK
    assert patched_aws.restored_keys == ["data/a.bin"]

    patched_aws.restore_calls.clear()
    assert cli.main(argv) == cli.EXIT_OK
    assert patched_aws.restore_calls == []


def test_manifest_written_through_cli(patched_aws, tmp_path, capsys):
    manifest = tmp_path / "m.csv"
    code = cli.main(
        ["--bucket", "b", "--prefix", "data/", "--dry-run", "--manifest-out", str(manifest)]
    )
    assert code == cli.EXIT_OK
    assert manifest.read_text(encoding="utf-8").strip() == "b,data/a.bin"
    assert "S3 Batch Operations" in capsys.readouterr().out


def test_version_flag():
    with pytest.raises(SystemExit) as exc:
        parse("--version")
    assert exc.value.code == 0


def test_help_lists_key_flags(capsys):
    with pytest.raises(SystemExit):
        parse("--help")
    out = capsys.readouterr().out
    for flag in ("--dry-run", "--state-file", "--concurrency", "--versions", "--tier"):
        assert flag in out
