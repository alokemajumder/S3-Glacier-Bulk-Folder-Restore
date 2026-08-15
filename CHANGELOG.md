# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0]

A rewrite of the single-file script into a tested package. The command-line
entry point `python glacier_restore.py` still works.

### Fixed

- **In-flight restores were never detected.** The script read `obj["Restore"]`
  from `ListObjectsV2` results, but that field only exists on `HeadObject` /
  `GetObject` responses — it was always `None`. Every re-run therefore
  re-requested objects whose restore was already in progress, at full cost.
  The listing now requests `OptionalObjectAttributes=["RestoreStatus"]` and
  reads `IsRestoreInProgress` / `RestoreExpiryDate`.
- **`GLACIER_IR` objects produced an error each.** Glacier Instant Retrieval
  objects are readable without a restore, so `RestoreObject` returns
  `InvalidObjectState` for every one. They are now recognised and skipped.
- **Cross-region buckets failed or misrouted.** The client was built with no
  region. The bucket's region is now resolved once (including from the
  `x-amz-bucket-region` header on a rejected request) and pinned.
- **The object-count preview was wrong.** It stopped counting at 5 and then
  printed that number as though it were the total.
- **Non-numeric input crashed the script.** `int(restore_days_input)` raised an
  unhandled `ValueError`; `--days` is now validated and bounded.
- **Skipping a prefix aborted silently.** If the prefix matched a skip term the
  function returned with no output (the message was commented out) and exit
  code 0.
- **Deep Archive + Expedited.** Deep Archive does not support the Expedited
  tier; those objects are now identified before the request is sent.

### Security

- The secret access key was read with `input()`, echoing it to the terminal and
  into scrollback, and the access key ID was then printed in full. Secrets are
  now read with `getpass` and only ever displayed masked.

### Added

- **Desktop GUI** (issue #1): `s3-glacier-restore-gui`, or
  `s3-glacier-restore --gui`. A Tkinter window with bucket checking, an object
  preview, live activity log, dry run by default, a confirmation dialog naming
  the cost, and a working Stop button. It drives the same engine as the CLI.
  The logic layer holds no Tk imports and is covered by headless tests.
- **Concurrency** (`--concurrency`, default 16) with a matching HTTP connection
  pool and adaptive retries. Serial restores ran at roughly 5½ hours per
  million objects.
- **Resume** (`--state-file`): initiated restores are recorded as they happen;
  re-running the same command skips them.
- **Full CLI** — every prompt is now also a flag, so the tool can run from cron
  or CI. Prompts remain for interactive use.
- **Meaningful exit codes**: 0 success, 1 failures, 2 configuration, 3 aborted,
  130 interrupted. Previously every path exited 0.
- **Intelligent-Tiering support** (`--include-intelligent-tiering`), including
  the empty `RestoreRequest` those archive tiers require.
- **Object versions** (`--versions`) for versioned buckets.
- **S3 Batch Operations manifests** (`--manifest-out`) for restores past a few
  million objects.
- **`--include` / `--exclude`** glob filters, repeatable, alongside
  `--skip-file`.
- **Retrieval tier selection** (`--tier`); Bulk remains the default.
- `--profile`, `--session-token`, `--endpoint-url`, `--expected-bucket-owner`,
  `--requester-pays`, `--max-objects`, `--page-size`, `--max-attempts`.
- Structured logging with `--log-file`, `--verbose`, `--quiet`, and periodic
  progress with throughput.
- Graceful Ctrl-C: in-flight requests finish, the summary and state file stay
  accurate, and a second Ctrl-C exits immediately.
- Summary reporting object counts, total bytes, per-outcome breakdown, elapsed
  time, throughput, and the most common failure reasons.
- Packaging (`pyproject.toml`), `s3-glacier-restore` and
  `s3-glacier-restore-gui` console scripts, a suite of 195 tests, `ruff`
  linting, and GitHub Actions CI across Python 3.9–3.13 on Linux, macOS, and
  Windows.

### Changed

- **Filter patterns are globs, not substrings.** `raw` no longer excludes
  `brawl/`. Add wildcards (`*raw*`) to restore the old behaviour.
- **A missing `--skip-file` is now a fatal error.** It used to warn and
  continue, which silently restored everything the operator meant to skip.
- Zero-byte "folder marker" keys ending in `/` are no longer sent to
  `RestoreObject`.
- A live run without a TTY now requires `--yes` instead of blocking on a prompt
  that nothing can answer.
- Console output goes to stderr; prompts, banners, and the summary go to stdout
  so they can be piped.
- `skiplist.txt` expanded and rewritten as globs.

### Resolved issues

- **#1** — Tkinter GUI for bulk restore.
- **#2** — skip-list file and dry-run mode; both shipped in 1.1.0 and
  substantially reworked here (glob patterns, `--include`, and a dry run that
  runs the full classification pass).

## [1.1.0]

- Skip-list file support and a dry-run mode (contributed by R. Tucker, #2/#3).

## [1.0.0]

- Initial release: interactive recursive Bulk-tier restore under a prefix.
