# S3 Glacier Bulk Folder Restore

[![CI](https://github.com/alokemajumder/S3-Glacier-Bulk-Folder-Restore/actions/workflows/ci.yml/badge.svg)](https://github.com/alokemajumder/S3-Glacier-Bulk-Folder-Restore/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Recursively restore **every archived object under an S3 prefix** — Glacier
Flexible Retrieval, Glacier Deep Archive, and (optionally) Intelligent-Tiering
archive tiers — with one command.

The AWS Console makes you restore objects one at a time. That is fine for ten
objects and impossible for ten million. This tool walks the prefix, works out
which objects actually need a restore, and issues the requests concurrently,
resumably, and with a dry run you can trust.

```bash
# Preview: what would be restored, and how much data is involved?
s3-glacier-restore --bucket my-archive --prefix backups/2019/ --dry-run

# Do it: 7-day restore, 32 in parallel, resumable, logged
s3-glacier-restore --bucket my-archive --prefix backups/2019/ \
    --days 7 --concurrency 32 --state-file restore.state --log-file restore.log --yes
```

---

## Why not just loop over `aws s3api restore-object`?

A naive loop gets four things wrong, and all four cost money:

| | Naive loop | This tool |
|---|---|---|
| **Objects already restoring** | Re-requests them; you pay twice | Detected from `RestoreStatus` and skipped |
| **`GLACIER_IR` objects** | `InvalidObjectState` error each | Recognised as already readable |
| **Throttling at scale** | 3 retries, then failures | Adaptive retry with client-side rate limiting |
| **A crash at 90%** | Start over, pay again | `--state-file` resumes where it stopped |

Plus: one request at a time is roughly 5½ hours per million objects. At
`--concurrency 32` that is closer to 10 minutes.

---

## Features

- **Recursive and streaming.** Walks any prefix, including all subfolders.
  Objects are processed as they page in, so memory stays flat whether the
  prefix holds a thousand keys or fifty million.
- **Concurrent.** Tunable parallelism with an HTTP connection pool sized to
  match and adaptive retries tuned for S3's throttling behaviour.
- **Resumable.** `--state-file` records each initiated restore; re-running the
  same command picks up exactly where it left off.
- **Honest dry run.** `--dry-run` performs the full classification pass — same
  filters, same storage-class checks — and issues zero restore calls.
- **Correct storage-class handling.** Glacier and Deep Archive are restored;
  `GLACIER_IR` is recognised as instantly readable; Intelligent-Tiering is
  checked for an archive tier only when you ask.
- **Glob filtering.** `--exclude`, `--include`, and a `--skip-file` of
  patterns, so you never pay to retrieve `.DS_Store` and `Thumbs.db`.
- **Versioned buckets.** `--versions` restores non-current versions too.
- **Batch Operations manifest.** `--manifest-out` emits a CSV manifest for S3
  Batch Operations, which is the right tool past a few million objects.
- **Scriptable.** Every prompt has a flag; meaningful exit codes; structured
  logging to a file; graceful Ctrl-C.
- **Safe by default.** Bulk (cheapest) tier, confirmation before anything
  billable, and it refuses to start a live run unattended without `--yes`.

---

## Install

Requires Python 3.9 or newer (boto3 recommends 3.10+).

```bash
git clone https://github.com/alokemajumder/S3-Glacier-Bulk-Folder-Restore.git
cd S3-Glacier-Bulk-Folder-Restore

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
```

That installs the `s3-glacier-restore` command. Without installing, either of
these works from the repo directory:

```bash
pip install -r requirements.txt
python -m s3_glacier_restore --help
python glacier_restore.py --help      # original entry point, still supported
```

### Credentials

Any source the AWS SDK understands: `aws configure`, `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`, `--profile`, SSO, or an EC2/ECS/Lambda role. The
minimum IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation"],
      "Resource": "arn:aws:s3:::my-archive"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:RestoreObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::my-archive/*"
    }
  ]
}
```

`s3:ListBucketVersions` is only needed for `--versions`; `s3:GetObject` only
for `--include-intelligent-tiering` (which calls `HeadObject`) and for
downloading afterwards.

Passing keys on the command line is supported but discouraged — they land in
your shell history and in `ps` output. Use `--secret-access-key -` to be
prompted without echo.

---

## Usage

Three front ends, one engine: a **desktop window**, **guided prompts**, or
**flags** for automation.

### Desktop app

For anyone who would rather not touch a terminal (issue #1):

```bash
s3-glacier-restore-gui        # or: s3-glacier-restore --gui
```

The window has a bucket/prefix form, a **Check bucket** button that verifies
access and previews the first 25 objects with their storage classes, a live
activity log you can save to a file, and **Stop** to halt a run in progress.
Dry run is ticked by default, and a live restore asks for confirmation with
the cost implications spelled out. Filters, credentials, and the resume state
file live on the second tab.

Tkinter ships with most Python installations. If yours lacks it:
`apt install python3-tk` on Debian/Ubuntu, `brew install python-tk` on macOS.

### Command line

Run with no arguments for guided prompts, or drive it entirely with flags.

### The normal workflow

```bash
# 1. See what is there and what it would cost you to retrieve
s3-glacier-restore -b my-archive -p photos/2019/ --dry-run

# 2. Restore it, skipping filesystem junk, resumable
s3-glacier-restore -b my-archive -p photos/2019/ \
    --days 14 --skip-file skiplist.txt --state-file photos.state --yes

# 3. Bulk restores take 5-12 hours (up to 48 for Deep Archive). Then:
aws s3 cp --recursive s3://my-archive/photos/2019/ ./photos/
```

### Options that matter

| Flag | Effect |
|---|---|
| `--dry-run` / `-n` | Full classification pass, zero restore calls |
| `--days N` / `-d` | How long the restored copy stays available (default 30) |
| `--tier` / `-t` | `Bulk` (default, cheapest), `Standard`, `Expedited` |
| `--concurrency N` / `-c` | Parallel requests (default 16) |
| `--state-file PATH` | Record progress; re-run to resume |
| `--exclude GLOB` | Skip matching keys; repeatable |
| `--include GLOB` | Restore *only* matching keys; repeatable |
| `--skip-file PATH` | Read exclude patterns from a file |
| `--versions` | Restore non-current object versions too |
| `--include-intelligent-tiering` | Also restore archived Intelligent-Tiering objects |
| `--max-objects N` | Stop after N restorable objects — a bounded first run |
| `--manifest-out PATH` | Write a CSV manifest for S3 Batch Operations |
| `--yes` / `-y` | Skip confirmation (required for unattended runs) |
| `--log-file PATH` | Full debug log alongside the terse console output |

`s3-glacier-restore --help` lists everything.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Completed; no failures |
| `1` | Completed, but some objects failed |
| `2` | Bad configuration or unusable credentials |
| `3` | Aborted at the confirmation prompt |
| `130` | Interrupted (Ctrl-C / SIGTERM) |

---

## Filter patterns

Patterns are shell-style globs. A pattern **with** `/` matches the whole key;
a pattern **without** `/` matches the filename *or* any directory component,
at any depth.

```
.DS_Store       every file named .DS_Store, anywhere
*.tmp           every file ending in .tmp
@eaDir          everything inside a directory named @eaDir
2023/           everything under any "2023/" directory
logs/*.gz       logs/a.gz  (not logs/2024/a.gz — single * stops at '/')
logs/**         everything under logs/, at any depth
```

`--exclude` always beats `--include`. `--ignore-case` applies to both.

> **Changed in 2.0.** Patterns used to be substrings, which meant `raw` also
> excluded `brawl/` and `2019` also excluded `12019-scan/`. Matching is now
> glob-based. If you relied on the old behaviour, add wildcards: `*raw*`.

The bundled `skiplist.txt` covers the usual macOS, Windows, and NAS clutter.

---

## Very large restores

Direct `RestoreObject` calls scale to a few million objects comfortably. Past
that, S3 Batch Operations is what AWS built for the job — it runs the restores
server-side, retries for you, and writes a completion report:

```bash
# Produce the manifest without restoring anything
s3-glacier-restore -b my-archive -p backups/ --dry-run --manifest-out manifest.csv

# Upload it, then create a Batch Operations job with:
#   Manifest format: S3BatchOperations_CSV_20180820
#   Operation:       Restore
```

The manifest contains only objects that genuinely need a restore, after your
filters — so you are not paying Batch Operations to re-examine keys this tool
already ruled out.

---

## Costs and caveats

- **Use at your own risk.** Bulk restores of large archives are billable and
  hard to cancel. Run `--dry-run` first; it reports the object count and total
  bytes before you commit.
- Retrieval is billed **per GB and per request**, and the restored copy is
  billed at **S3 Standard rates for `--days` days** *on top of* the archive
  storage you are still paying for. See
  [S3 pricing](https://aws.amazon.com/s3/pricing/) — rates vary by region.
- The restore is **temporary**. After `--days`, the copy expires and the
  object is archive-only again. To keep the data, copy it elsewhere or apply a
  lifecycle rule before the copy expires.
- Bulk retrievals typically complete in 5–12 hours; Deep Archive Bulk can take
  up to 48 hours. This tool *initiates* restores — it does not wait for them.
- `GLACIER_IR` (Instant Retrieval) objects need no restore and are skipped.

---

## Development

```bash
pip install -e ".[dev]"
pytest                 # 195 tests, no AWS account or network needed
ruff check . && ruff format --check .
```

Tests cover the engine against an in-memory S3 fake, and the request shapes
against botocore's real S3 service model via `Stubber` — so a malformed
parameter fails in CI rather than against your bucket. The GUI's logic layer
(`GuiRunner`, `config_from_fields`) holds no Tkinter imports, so it is tested
headlessly too.

Layout:

```
s3_glacier_restore/
  cli.py        argument parsing, prompts, confirmation, exit codes
  gui.py        Tkinter window + a Tk-free GuiRunner/config layer
  engine.py     classification, bounded concurrency, cancellation
  lister.py     streaming pagination over objects and versions
  filters.py    glob include/exclude matching
  aws.py        session, region resolution, retry and pool tuning
  state.py      append-only resume checkpoint
  manifest.py   S3 Batch Operations CSV writer
  config.py     RestoreConfig + validation
  models.py     S3Object, Outcome, thread-safe Stats
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT — see [LICENSE](LICENSE). Provided as-is, without warranty.
