from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from s3_glacier_restore import aws
from s3_glacier_restore.aws import (
    AwsError,
    _boto_config,
    describe_error,
    resolve_bucket_region,
    verify_access,
)
from s3_glacier_restore.config import RestoreConfig


class FakeSession:
    def __init__(self, response=None, error=None, region_name=None):
        self.response = response
        self.error = error
        self.region_name = region_name

    def client(self, service, **kwargs):
        return FakeProbe(self.response, self.error)


class FakeProbe:
    def __init__(self, response, error):
        self.response = response
        self.error = error

    def get_bucket_location(self, **kwargs):
        if self.error:
            raise self.error
        return self.response


def error(code, headers=None, status=400):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers or {}},
        },
        "GetBucketLocation",
    )


# ------------------------------------------------------------ region logic --


@pytest.mark.parametrize(
    "constraint,expected",
    [
        (None, "us-east-1"),  # legacy empty constraint
        ("", "us-east-1"),
        ("EU", "eu-west-1"),  # legacy alias
        ("eu-central-1", "eu-central-1"),
        ("ap-southeast-2", "ap-southeast-2"),
    ],
)
def test_location_constraint_mapping(constraint, expected):
    session = FakeSession(response={"LocationConstraint": constraint})
    assert resolve_bucket_region(session, RestoreConfig(bucket="b")) == expected


def test_explicit_region_wins():
    session = FakeSession(response={"LocationConstraint": "eu-central-1"})
    cfg = RestoreConfig(bucket="b", region="us-west-2")
    assert resolve_bucket_region(session, cfg) == "us-west-2"


def test_region_recovered_from_error_header():
    """A cross-region bucket reports its region even when it rejects the call."""
    session = FakeSession(error=error("AccessDenied", {"x-amz-bucket-region": "ap-south-1"}))
    assert resolve_bucket_region(session, RestoreConfig(bucket="b")) == "ap-south-1"


def test_region_falls_back_when_undeterminable(caplog):
    session = FakeSession(error=error("AccessDenied"), region_name=None)
    assert resolve_bucket_region(session, RestoreConfig(bucket="b")) == aws.DEFAULT_REGION


def test_custom_endpoint_skips_get_bucket_location():
    session = FakeSession(error=AssertionError("must not be called"))
    cfg = RestoreConfig(bucket="b", endpoint_url="http://localhost:9000")
    assert resolve_bucket_region(session, cfg) == aws.DEFAULT_REGION


# ------------------------------------------------------------ boto config --


def test_connection_pool_tracks_concurrency():
    """A 10-connection default pool would serialise 32 worker threads."""
    cfg = RestoreConfig(bucket="b", concurrency=64)
    config = _boto_config(cfg, "us-east-1")
    assert config.max_pool_connections >= 64
    assert config.retries == {"max_attempts": 10, "mode": "adaptive"}


def test_small_concurrency_keeps_a_sane_pool_floor():
    assert _boto_config(RestoreConfig(bucket="b", concurrency=1)).max_pool_connections == 10


# ------------------------------------------------------------ access check --


class FakeClient:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    def head_bucket(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return {}


def head_error(code, status):
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadBucket",
    )


def test_verify_access_passes():
    client = FakeClient()
    verify_access(client, RestoreConfig(bucket="b"))
    assert client.calls == [{"Bucket": "b"}]


def test_verify_access_forwards_expected_owner():
    client = FakeClient()
    verify_access(client, RestoreConfig(bucket="b", expected_bucket_owner="123456789012"))
    assert client.calls[0]["ExpectedBucketOwner"] == "123456789012"


@pytest.mark.parametrize(
    "code,status,fragment",
    [
        ("404", 404, "does not exist"),
        ("NoSuchBucket", 404, "does not exist"),
        ("403", 403, "Access denied"),
        ("AccessDenied", 403, "Access denied"),
        ("SomethingElse", 500, "Could not access"),
    ],
)
def test_verify_access_error_messages(code, status, fragment):
    with pytest.raises(AwsError) as exc:
        verify_access(FakeClient(head_error(code, status)), RestoreConfig(bucket="b"))
    assert fragment in str(exc.value)


def test_access_denied_message_names_the_required_permissions():
    with pytest.raises(AwsError) as exc:
        verify_access(FakeClient(head_error("AccessDenied", 403)), RestoreConfig(bucket="b"))
    assert "s3:ListBucket" in str(exc.value)
    assert "s3:RestoreObject" in str(exc.value)


def test_describe_error():
    code, message = describe_error(
        ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "Op")
    )
    assert (code, message) == ("Throttling", "slow down")
