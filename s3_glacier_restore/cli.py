"""Command-line interface.

Two ways in, sharing one code path:

* fully flagged, for cron/CI (``--bucket ... --yes``);
* interactive prompts when required arguments are missing and a TTY is
  attached, preserving the workflow this tool has always had.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import signal
import sys
import textwrap
import threading
from collections.abc import Sequence

from botocore.exceptions import BotoCoreError, ClientError

from . import __version__
from .aws import (
    AwsError,
    build_client,
    build_session,
    caller_identity,
    resolve_bucket_region,
    verify_access,
)
from .config import ConfigError, RestoreConfig
from .engine import RestoreEngine, summarize
from .filters import KeyFilter, load_pattern_file
from .lister import format_bytes, sample_objects
from .logsetup import configure, emit
from .manifest import ManifestWriter, NullManifest
from .models import RETRIEVAL_TIERS
from .state import NullState, StateFile

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_CONFIG = 2
EXIT_ABORTED = 3
EXIT_INTERRUPTED = 130

BANNER = "S3 Glacier Bulk Folder Restore v" + __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s3-glacier-restore",
        description=(
            "Recursively restore archived S3 objects (Glacier Flexible "
            "Retrieval, Glacier Deep Archive, and optionally Intelligent-"
            "Tiering archive tiers) under a prefix."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              # See what would happen, without issuing any restore
              s3-glacier-restore --bucket my-archive --prefix backups/2019/ --dry-run

              # Restore for 7 days, unattended, resumable, with an audit log
              s3-glacier-restore --bucket my-archive --prefix backups/ --days 7 \\
                  --concurrency 32 --state-file restore.state --log-file restore.log --yes

              # Skip junk files, restore every object version
              s3-glacier-restore --bucket my-archive --prefix photos/ \\
                  --skip-file skiplist.txt --versions --yes

              # Produce a manifest for S3 Batch Operations instead of calling
              # RestoreObject directly (recommended past a few million objects)
              s3-glacier-restore --bucket my-archive --prefix backups/ \\
                  --dry-run --manifest-out manifest.csv

            Exit codes: 0 success, 1 some objects failed, 2 bad configuration,
            3 aborted at the confirmation prompt, 130 interrupted.
            """
        ),
    )

    target = parser.add_argument_group("target")
    target.add_argument("--bucket", "-b", help="S3 bucket name.")
    target.add_argument(
        "--prefix",
        "-p",
        default=None,
        help="Key prefix to restore recursively, e.g. 'backups/'. Omit to cover the whole bucket.",
    )
    target.add_argument(
        "--days",
        "-d",
        type=int,
        default=None,
        help="Days to keep the restored copy available (default: 30).",
    )
    target.add_argument(
        "--tier",
        "-t",
        choices=RETRIEVAL_TIERS,
        default="Bulk",
        help="Retrieval tier (default: Bulk, the cheapest). Expedited is not "
        "supported for Deep Archive.",
    )

    creds = parser.add_argument_group("credentials and endpoint")
    creds.add_argument("--profile", help="Named AWS profile to use.")
    creds.add_argument("--region", help="Bucket region. Auto-detected when not supplied.")
    creds.add_argument("--endpoint-url", help="Custom S3 endpoint (MinIO, Ceph, VPC endpoint).")
    creds.add_argument(
        "--access-key-id",
        help="Explicit access key ID. Prefer --profile or the standard "
        "environment variables; keys on the command line leak into shell "
        "history and process listings.",
    )
    creds.add_argument(
        "--secret-access-key",
        help="Explicit secret access key. Pass '-' to be prompted without echo.",
    )
    creds.add_argument("--session-token", help="Session token for temporary credentials.")
    creds.add_argument(
        "--expected-bucket-owner",
        help="Fail if the bucket is not owned by this account ID.",
    )
    creds.add_argument(
        "--requester-pays",
        action="store_true",
        help="Send the requester-pays header.",
    )

    selection = parser.add_argument_group("selection")
    selection.add_argument(
        "--skip-file",
        "--skiplist",
        dest="skip_file",
        help="File of exclude patterns, one glob per line, '#' for comments.",
    )
    selection.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude keys matching this glob. Repeatable.",
    )
    selection.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Restore only keys matching this glob. Repeatable. Excludes still win.",
    )
    selection.add_argument(
        "--ignore-case",
        action="store_true",
        help="Match include/exclude patterns case-insensitively.",
    )
    selection.add_argument(
        "--versions",
        action="store_true",
        help="Restore every object version, not just the current one.",
    )
    selection.add_argument(
        "--include-intelligent-tiering",
        action="store_true",
        help="Also restore Intelligent-Tiering objects sitting in an archive "
        "access tier. Costs one HeadObject per such object.",
    )
    selection.add_argument(
        "--max-objects",
        type=int,
        help="Stop after considering this many restorable objects. Useful for a bounded first run.",
    )

    execution = parser.add_argument_group("execution")
    execution.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=16,
        help="Parallel restore requests (default: 16).",
    )
    execution.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="List and classify everything, but issue no restore calls.",
    )
    execution.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt. Required for non-interactive runs.",
    )
    execution.add_argument(
        "--state-file",
        help="Append every initiated restore here and skip those keys on a "
        "later run. Makes a long run resumable.",
    )
    execution.add_argument(
        "--manifest-out",
        help="Write a CSV manifest of eligible objects for S3 Batch Operations.",
    )
    execution.add_argument(
        "--max-attempts",
        type=int,
        default=10,
        help="Retry attempts per AWS request (default: 10, adaptive mode).",
    )
    execution.add_argument(
        "--page-size",
        type=int,
        default=1000,
        help="Objects per list request (default: 1000, the S3 maximum).",
    )
    execution.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Log a progress line every N objects scanned. 0 disables.",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--log-file", help="Write a full debug log here.")
    output.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="-v for debug output, -vv to include botocore wire logging.",
    )
    output.add_argument("--quiet", "-q", action="store_true", help="Warnings and errors only.")
    output.add_argument("--version", action="version", version=BANNER)
    output.add_argument(
        "--gui",
        action="store_true",
        help="Launch the desktop window instead of the command line.",
    )

    return parser


# --------------------------------------------------------------- prompting --


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{text}{suffix}: ").strip()
    except EOFError:
        raise KeyboardInterrupt from None
    return answer or (default or "")


def _prompt_int(text: str, default: int) -> int:
    while True:
        answer = _prompt(text, str(default))
        try:
            return int(answer)
        except ValueError:
            emit(f"  '{answer}' is not a whole number. Try again.")


def _prompt_yes_no(text: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = _prompt(f"{text} ({hint})").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def fill_interactively(args: argparse.Namespace) -> None:
    """Ask for anything still missing. Only reached on a TTY."""
    emit(BANNER)
    emit("=" * len(BANNER))
    emit("")

    if not args.bucket:
        while not args.bucket:
            args.bucket = _prompt("S3 bucket name")
    if args.prefix is None:
        args.prefix = _prompt("Key prefix (blank for the whole bucket)")
    if args.days is None:
        args.days = _prompt_int("Days to keep the restored copy", 30)
    if not args.skip_file and not args.exclude:
        skip_file = _prompt("Path to a skip-list file (blank for none)")
        if skip_file:
            args.skip_file = skip_file
    if not args.dry_run:
        args.dry_run = _prompt_yes_no(
            "Dry run first (recommended: classifies everything, restores nothing)",
            default=True,
        )


def resolve_secret(args: argparse.Namespace) -> None:
    """Read a secret key without echoing it.

    The pre-2.0 script read the secret with ``input()``, which printed it to
    the terminal and left it in scrollback.
    """
    if args.secret_access_key == "-":
        args.secret_access_key = getpass.getpass("AWS secret access key (hidden): ")
    if args.access_key_id and not args.secret_access_key:
        args.secret_access_key = getpass.getpass("AWS secret access key (hidden): ")


def mask(value: str | None) -> str:
    if not value:
        return "(none)"
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


# ------------------------------------------------------------------- config --


def config_from_args(args: argparse.Namespace) -> RestoreConfig:
    excludes: list[str] = list(args.exclude)
    if args.skip_file:
        try:
            excludes.extend(load_pattern_file(args.skip_file))
        except OSError as exc:
            raise ConfigError(f"Could not read skip file: {exc}") from exc

    cfg = RestoreConfig(
        bucket=(args.bucket or "").strip(),
        prefix=(args.prefix or "").strip(),
        days=30 if args.days is None else args.days,
        tier=args.tier,
        profile=args.profile,
        region=args.region,
        endpoint_url=args.endpoint_url,
        access_key_id=args.access_key_id,
        secret_access_key=args.secret_access_key,
        session_token=args.session_token,
        expected_bucket_owner=args.expected_bucket_owner,
        requester_pays=args.requester_pays,
        excludes=excludes,
        includes=list(args.include),
        ignore_case=args.ignore_case,
        versions=args.versions,
        include_intelligent_tiering=args.include_intelligent_tiering,
        max_objects=args.max_objects,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        state_file=args.state_file,
        manifest_out=args.manifest_out,
        page_size=args.page_size,
        max_attempts=args.max_attempts,
        progress_every=args.progress_every,
    )
    cfg.validate()
    return cfg


def preflight(client, session, cfg: RestoreConfig, key_filter: KeyFilter) -> None:
    """Print what is about to happen, with a real sample from the bucket."""
    emit("")
    emit("Checking the prefix...")
    sample, has_more = sample_objects(client, cfg, limit=5)

    if not sample:
        emit(f"  No objects found under prefix '{cfg.prefix}'.")
    else:
        count = f"{len(sample)}+" if has_more else str(len(sample))
        emit(f"  Found {count} object(s). First {len(sample)}:")
        for index, obj in enumerate(sample, start=1):
            kept = "" if key_filter.keeps(obj.key) else "  <- excluded by filters"
            emit(f"    {index}. {obj.key}  [{obj.storage_class}, {format_bytes(obj.size)}]{kept}")

    emit("")
    emit("--- Configuration ---")
    for line in cfg.describe():
        emit(line)
    emit(f"Filters               : {key_filter.describe()}")
    emit(f"Identity              : {caller_identity(session, cfg)}")
    if cfg.access_key_id:
        emit(f"Access key ID         : {mask(cfg.access_key_id)}")
    else:
        emit("Credentials           : default AWS credential chain")
    emit("")


def confirm(cfg: RestoreConfig, assume_yes: bool) -> bool:
    if cfg.dry_run or assume_yes:
        return True
    if not _is_interactive():
        log.error(
            "Refusing to start a live restore without confirmation. Re-run with "
            "--yes for non-interactive use, or --dry-run to preview."
        )
        return False
    emit(
        "This issues billable retrieval requests and creates temporary copies "
        f"charged at S3 Standard rates for {cfg.days} day(s)."
    )
    return _prompt_yes_no("Proceed", default=False)


# ---------------------------------------------------------------------- run --


def _install_signal_handler(stop: threading.Event) -> None:
    def handler(signum, frame):  # noqa: ARG001
        if stop.is_set():
            # Second Ctrl-C: the operator means it.
            raise KeyboardInterrupt
        stop.set()
        log.warning(
            "Interrupt received. Finishing in-flight requests; press Ctrl-C "
            "again to exit immediately."
        )

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not on the main thread, or the platform lacks the signal: the run
        # still works, it just cannot be stopped gracefully.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def run(cfg: RestoreConfig, assume_yes: bool = False) -> int:
    key_filter = KeyFilter(cfg.excludes, cfg.includes, cfg.ignore_case)

    session = build_session(cfg)
    region = resolve_bucket_region(session, cfg)
    if region and not cfg.region:
        log.debug("Resolved bucket region: %s", region)
        cfg.region = region
    client = build_client(session, cfg, region)
    verify_access(client, cfg)
    log.info("Bucket '%s' is accessible in %s.", cfg.bucket, region or "the default region")

    preflight(client, session, cfg, key_filter)

    if not confirm(cfg, assume_yes):
        emit("Aborted. Nothing was restored.")
        return EXIT_ABORTED

    state = StateFile(cfg.state_file) if cfg.state_file else NullState()
    resumed = state.load()
    if resumed:
        log.info(
            "Loaded %s previously restored object(s) from %s; they will be skipped.",
            f"{resumed:,}",
            cfg.state_file,
        )

    manifest = (
        ManifestWriter(cfg.manifest_out, cfg.bucket, include_versions=cfg.versions)
        if cfg.manifest_out
        else NullManifest()
    )

    stop = threading.Event()
    _install_signal_handler(stop)

    emit("")
    emit("Starting..." if not cfg.dry_run else "Starting dry run...")
    emit("")

    with state, manifest:
        engine = RestoreEngine(
            client,
            cfg,
            key_filter=key_filter,
            state=state,
            manifest=manifest,
            stop_event=stop,
        )
        stats = engine.run()
        elapsed = engine.elapsed

    for line in summarize(stats, cfg, elapsed):
        emit(line)

    if cfg.manifest_out and manifest.rows:
        emit("")
        emit(f"Wrote {manifest.rows:,} row(s) to {cfg.manifest_out}.")
        emit(
            "Use it with S3 Batch Operations (manifest format "
            "S3BatchOperations_CSV_20180820) for very large restores."
        )

    if stop.is_set():
        emit("")
        emit("Run was interrupted before completion.")
        if cfg.state_file:
            emit(f"Re-run the same command to resume from {cfg.state_file}.")
        return EXIT_INTERRUPTED

    return EXIT_FAILURES if stats.failures else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure(verbose=args.verbose, quiet=args.quiet, log_file=args.log_file)

    if args.gui:
        from .gui import main as gui_main

        return gui_main()

    try:
        if not args.bucket or args.prefix is None:
            if _is_interactive():
                fill_interactively(args)
            elif not args.bucket:
                parser.error("--bucket is required in non-interactive mode")

        resolve_secret(args)
        cfg = config_from_args(args)
        return run(cfg, assume_yes=args.yes)

    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    except AwsError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    except ClientError as exc:
        log.error("AWS request failed: %s", exc)
        return EXIT_FAILURES
    except BotoCoreError as exc:
        log.error("AWS client error: %s", exc)
        return EXIT_FAILURES
    except KeyboardInterrupt:
        emit("")
        log.warning("Interrupted.")
        return EXIT_INTERRUPTED
    finally:
        logging.shutdown()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
