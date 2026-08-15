"""Run configuration and its validation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import DEEP_ARCHIVE_TIERS, RETRIEVAL_TIERS

# S3 accepts 1..N days; there is no documented hard upper bound, but a value
# this large is far more likely to be a typo than an intent, and the cost of
# getting it wrong is paying S3 Standard rates for a copy nobody wanted.
MAX_RESTORE_DAYS = 3650


class ConfigError(ValueError):
    """Raised when a configuration is internally inconsistent."""


@dataclass
class RestoreConfig:
    """Everything a run needs, already validated."""

    bucket: str
    prefix: str = ""
    days: int = 30
    tier: str = "Bulk"

    # Credentials / endpoint
    profile: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    expected_bucket_owner: str | None = None
    requester_pays: bool = False

    # Selection
    excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    ignore_case: bool = False
    versions: bool = False
    include_intelligent_tiering: bool = False
    max_objects: int | None = None

    # Execution
    concurrency: int = 16
    dry_run: bool = False
    state_file: str | None = None
    manifest_out: str | None = None
    page_size: int = 1000
    max_attempts: int = 10
    progress_every: int = 1000

    def validate(self) -> None:
        if not self.bucket or not self.bucket.strip():
            raise ConfigError("A bucket name is required.")
        if "/" in self.bucket:
            raise ConfigError(
                f"Bucket name {self.bucket!r} contains '/'. Pass the path "
                "portion with --prefix instead."
            )
        if self.tier not in RETRIEVAL_TIERS:
            raise ConfigError(
                f"Unknown retrieval tier {self.tier!r}. "
                f"Choose one of: {', '.join(RETRIEVAL_TIERS)}."
            )
        if not 1 <= self.days <= MAX_RESTORE_DAYS:
            raise ConfigError(f"--days must be between 1 and {MAX_RESTORE_DAYS} (got {self.days}).")
        if self.concurrency < 1:
            raise ConfigError("--concurrency must be at least 1.")
        if self.concurrency > 256:
            raise ConfigError(
                "--concurrency above 256 will be throttled by S3 long before it "
                "helps. Pick a lower value."
            )
        if not 1 <= self.page_size <= 1000:
            raise ConfigError("--page-size must be between 1 and 1000.")
        if self.max_objects is not None and self.max_objects < 1:
            raise ConfigError("--max-objects must be at least 1.")
        if self.max_attempts < 1:
            raise ConfigError("--max-attempts must be at least 1.")
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ConfigError("Provide both an access key ID and a secret access key, or neither.")
        if self.session_token and not self.access_key_id:
            raise ConfigError("--session-token requires an explicit access key ID and secret.")
        if self.profile and self.access_key_id:
            raise ConfigError("Use either --profile or explicit access keys, not both.")

    @property
    def tier_supported_for_deep_archive(self) -> bool:
        return self.tier in DEEP_ARCHIVE_TIERS

    def describe(self) -> list[str]:
        """Lines rendered in the pre-flight confirmation block."""
        lines = [
            f"Bucket                : {self.bucket}",
            f"Prefix                : {self.prefix or '(entire bucket)'}",
            f"Retrieval tier        : {self.tier}",
            f"Restore duration      : {self.days} day(s) (temporary copy)",
            f"Region                : {self.region or 'auto-detect'}",
            f"Concurrency           : {self.concurrency}",
            f"Object versions       : {'all versions' if self.versions else 'current only'}",
            f"Intelligent-Tiering   : {'included' if self.include_intelligent_tiering else 'skipped'}",
            f"Mode                  : {'DRY RUN (no restore calls)' if self.dry_run else 'LIVE'}",
        ]
        if self.max_objects:
            lines.append(f"Object limit          : {self.max_objects}")
        if self.state_file:
            lines.append(f"State file            : {self.state_file}")
        if self.manifest_out:
            lines.append(f"Batch manifest        : {self.manifest_out}")
        if self.expected_bucket_owner:
            lines.append(f"Expected bucket owner : {self.expected_bucket_owner}")
        if self.requester_pays:
            lines.append("Requester pays        : yes")
        return lines
