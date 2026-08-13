"""boto3 session and client construction.

Two things here matter for large runs and are easy to get wrong:

* **Region.** ``head_bucket`` and ``restore_object`` against a bucket outside
  the client's region either fail with ``PermanentRedirect`` or take an extra
  round trip on every call. We resolve the bucket's real region once and pin
  the client to it.
* **Connection pool size.** botocore's default HTTP pool holds 10 connections.
  Running 32 restore threads against a 10-connection pool serialises them and
  emits urllib3 pool warnings. The pool is sized from the concurrency setting.
"""

from __future__ import annotations

import logging

import boto3
import botocore
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, NoCredentialsError

from .config import RestoreConfig

log = logging.getLogger(__name__)

USER_AGENT_SUFFIX = "s3-glacier-bulk-folder-restore"

# Used only when nothing else supplies a region; botocore refuses to build
# an S3 client without one.
DEFAULT_REGION = "us-east-1"


class AwsError(RuntimeError):
    """A fatal problem talking to AWS, already formatted for the operator."""


def build_session(cfg: RestoreConfig) -> boto3.session.Session:
    """Create a session from an explicit profile, explicit keys, or the chain."""
    kwargs = {}
    if cfg.profile:
        kwargs["profile_name"] = cfg.profile
    if cfg.access_key_id:
        kwargs["aws_access_key_id"] = cfg.access_key_id
        kwargs["aws_secret_access_key"] = cfg.secret_access_key
        if cfg.session_token:
            kwargs["aws_session_token"] = cfg.session_token
    if cfg.region:
        kwargs["region_name"] = cfg.region
    try:
        return boto3.session.Session(**kwargs)
    except botocore.exceptions.ProfileNotFound as exc:
        raise AwsError(str(exc)) from exc


def _boto_config(cfg: RestoreConfig, region: str | None = None) -> BotoConfig:
    return BotoConfig(
        region_name=region,
        # 'adaptive' adds client-side rate limiting on top of retries, which is
        # what keeps a wide fan-out from collapsing into a throttling storm.
        retries={"max_attempts": cfg.max_attempts, "mode": "adaptive"},
        max_pool_connections=max(cfg.concurrency + 4, 10),
        user_agent_extra=USER_AGENT_SUFFIX,
    )


def resolve_bucket_region(session: boto3.session.Session, cfg: RestoreConfig) -> str | None:
    """Return the bucket's region, or ``None`` if it cannot be determined.

    Falls back gracefully: ``s3:GetBucketLocation`` is not always granted, and
    custom endpoints (MinIO, Ceph) do not implement it meaningfully.
    """
    if cfg.region:
        return cfg.region
    if cfg.endpoint_url:
        # S3-compatible endpoints ignore the region but botocore still demands
        # one, so any valid value works as a placeholder.
        return session.region_name or DEFAULT_REGION

    probe = session.client("s3", config=_boto_config(cfg))
    try:
        kwargs = {"Bucket": cfg.bucket}
        if cfg.expected_bucket_owner:
            kwargs["ExpectedBucketOwner"] = cfg.expected_bucket_owner
        location = probe.get_bucket_location(**kwargs).get("LocationConstraint")
    except ClientError as exc:
        # Buckets in another region answer HeadBucket with the region header
        # even when they reject the call, so mine that before giving up.
        region = _region_from_error(exc)
        if region:
            log.debug("Recovered region %s from error response", region)
            return region
        log.warning(
            "Could not determine the region for '%s' (%s). Falling back to %s.",
            cfg.bucket,
            _error_code(exc),
            session.region_name or DEFAULT_REGION,
        )
        return session.region_name or DEFAULT_REGION
    except NoCredentialsError as exc:
        raise AwsError(
            "No AWS credentials found. Configure them with 'aws configure', "
            "set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, use --profile, or "
            "run on an instance/task with an IAM role."
        ) from exc

    # us-east-1 historically reports an empty constraint; 'EU' is a legacy
    # alias for eu-west-1.
    if location in (None, ""):
        return "us-east-1"
    if location == "EU":
        return "eu-west-1"
    return location


def build_client(session: boto3.session.Session, cfg: RestoreConfig, region: str | None):
    """Build the S3 client used for the whole run.

    botocore clients are thread-safe for API calls, so a single client is
    shared by every worker thread.
    """
    return session.client(
        "s3",
        region_name=region,
        endpoint_url=cfg.endpoint_url,
        config=_boto_config(cfg, region),
    )


def verify_access(client, cfg: RestoreConfig) -> None:
    """Confirm the bucket exists and is reachable before doing any work."""
    kwargs = {"Bucket": cfg.bucket}
    if cfg.expected_bucket_owner:
        kwargs["ExpectedBucketOwner"] = cfg.expected_bucket_owner
    try:
        client.head_bucket(**kwargs)
    except NoCredentialsError as exc:
        raise AwsError(
            "No AWS credentials found. Configure them with 'aws configure', "
            "set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, use --profile, or "
            "run on an instance/task with an IAM role."
        ) from exc
    except ClientError as exc:
        code = _error_code(exc)
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchBucket") or status == 404:
            raise AwsError(f"Bucket '{cfg.bucket}' does not exist.") from exc
        if code in ("403", "AccessDenied") or status == 403:
            raise AwsError(
                f"Access denied for bucket '{cfg.bucket}'. The credentials in "
                "use need s3:ListBucket and s3:RestoreObject on this bucket."
            ) from exc
        raise AwsError(f"Could not access bucket '{cfg.bucket}': {exc}") from exc


def caller_identity(session: boto3.session.Session, cfg: RestoreConfig) -> str:
    """Best-effort description of who we are, for the confirmation screen."""
    try:
        sts = session.client("sts", config=_boto_config(cfg))
        identity = sts.get_caller_identity()
        return f"{identity.get('Arn', 'unknown')} (account {identity.get('Account', '?')})"
    except Exception:  # noqa: BLE001 - purely informational
        return "unknown (sts:GetCallerIdentity unavailable)"


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _region_from_error(exc: ClientError) -> str | None:
    headers = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
    region = headers.get("x-amz-bucket-region")
    if region:
        return region
    return exc.response.get("Error", {}).get("Region") or None


def describe_error(exc: ClientError) -> tuple[str, str]:
    """Return ``(code, message)`` for an S3 error."""
    error = exc.response.get("Error", {})
    return str(error.get("Code", "Unknown")), str(error.get("Message", exc))
