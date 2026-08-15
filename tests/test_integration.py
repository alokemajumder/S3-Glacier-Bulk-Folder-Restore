"""End-to-end tests against a real botocore client with a stubbed transport.

The fake client in ``conftest`` proves the engine's logic. These tests prove
something the fake cannot: that the parameters we send actually validate
against the S3 service model. A typo like ``OptionalObjectAttribute`` or a
malformed ``RestoreRequest`` fails here and nowhere else.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.stub import ANY, Stubber

from s3_glacier_restore.config import RestoreConfig
from s3_glacier_restore.engine import RestoreEngine
from s3_glacier_restore.lister import iter_objects
from s3_glacier_restore.models import Outcome


@pytest.fixture
def s3():
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
    )


def test_list_params_validate_against_the_service_model(s3):
    """OptionalObjectAttributes=['RestoreStatus'] must be a real S3 parameter."""
    cfg = RestoreConfig(bucket="bkt", prefix="data/")
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {
                        "Key": "data/a.bin",
                        "Size": 100,
                        "StorageClass": "GLACIER",
                        "RestoreStatus": {"IsRestoreInProgress": True},
                    }
                ],
                "IsTruncated": False,
            },
            {
                "Bucket": "bkt",
                "Prefix": "data/",
                "OptionalObjectAttributes": ["RestoreStatus"],
                "MaxKeys": 1000,
            },
        )
        objects = list(iter_objects(s3, cfg))

    assert len(objects) == 1
    assert objects[0].restore_in_progress is True
    assert objects[0].storage_class == "GLACIER"


def test_restore_params_validate_against_the_service_model(s3):
    cfg = RestoreConfig(bucket="bkt", prefix="", days=5, tier="Bulk", concurrency=1)
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "a.bin", "Size": 10, "StorageClass": "DEEP_ARCHIVE"}],
                "IsTruncated": False,
            },
        )
        stub.add_response(
            "restore_object",
            {},
            {
                "Bucket": "bkt",
                "Key": "a.bin",
                "RestoreRequest": {
                    "Days": 5,
                    "GlacierJobParameters": {"Tier": "Bulk"},
                },
            },
        )
        stats = RestoreEngine(s3, cfg).run()
        stub.assert_no_pending_responses()

    assert stats.get(Outcome.INITIATED) == 1


def test_intelligent_tiering_restore_request_validates(s3):
    """An empty RestoreRequest is what Intelligent-Tiering archive tiers take."""
    cfg = RestoreConfig(bucket="bkt", prefix="", include_intelligent_tiering=True, concurrency=1)
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "a.bin", "Size": 10, "StorageClass": "INTELLIGENT_TIERING"}],
                "IsTruncated": False,
            },
        )
        stub.add_response(
            "head_object", {"ArchiveStatus": "ARCHIVE_ACCESS"}, {"Bucket": "bkt", "Key": "a.bin"}
        )
        stub.add_response(
            "restore_object", {}, {"Bucket": "bkt", "Key": "a.bin", "RestoreRequest": {}}
        )
        stats = RestoreEngine(s3, cfg).run()
        stub.assert_no_pending_responses()

    assert stats.get(Outcome.INITIATED) == 1


def test_version_listing_params_validate(s3):
    cfg = RestoreConfig(bucket="bkt", prefix="", versions=True, concurrency=1)
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_object_versions",
            {
                "Versions": [
                    {
                        "Key": "a.bin",
                        "VersionId": "v1",
                        "Size": 10,
                        "StorageClass": "GLACIER",
                        "IsLatest": False,
                    }
                ],
                "IsTruncated": False,
            },
            {
                "Bucket": "bkt",
                "Prefix": "",
                "OptionalObjectAttributes": ["RestoreStatus"],
                "MaxKeys": 1000,
            },
        )
        stub.add_response(
            "restore_object",
            {},
            {
                "Bucket": "bkt",
                "Key": "a.bin",
                "VersionId": "v1",
                "RestoreRequest": ANY,
            },
        )
        stats = RestoreEngine(s3, cfg).run()
        stub.assert_no_pending_responses()

    assert stats.get(Outcome.INITIATED) == 1


def test_real_client_rejects_malformed_parameters(s3):
    """Confirms these tests would actually catch a bad request shape."""
    with pytest.raises(ParamValidationError):
        s3.restore_object(
            Bucket="bkt",
            Key="k",
            RestoreRequest={"Days": "not-a-number", "GlacierJobParameters": {"Tier": []}},
        )


def test_pagination_walks_every_page(s3):
    """Continuation tokens must be followed; v1's preview stopped at page one."""
    cfg = RestoreConfig(bucket="bkt", prefix="", page_size=3)
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": f"k{i}.bin", "Size": 1, "StorageClass": "GLACIER"} for i in range(3)
                ],
                "IsTruncated": True,
                "NextContinuationToken": "token-1",
            },
            {
                "Bucket": "bkt",
                "Prefix": "",
                "OptionalObjectAttributes": ["RestoreStatus"],
                "MaxKeys": 3,
            },
        )
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {"Key": f"k{i}.bin", "Size": 1, "StorageClass": "GLACIER"} for i in range(3, 5)
                ],
                "IsTruncated": False,
            },
            {
                "Bucket": "bkt",
                "Prefix": "",
                "OptionalObjectAttributes": ["RestoreStatus"],
                "MaxKeys": 3,
                "ContinuationToken": "token-1",
            },
        )
        keys = [o.key for o in iter_objects(s3, cfg)]
        stub.assert_no_pending_responses()

    assert keys == [f"k{i}.bin" for i in range(5)]


def test_client_error_is_captured_not_raised(s3):
    cfg = RestoreConfig(bucket="bkt", prefix="", concurrency=1)
    cfg.validate()
    with Stubber(s3) as stub:
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "a.bin", "Size": 1, "StorageClass": "GLACIER"}],
                "IsTruncated": False,
            },
        )
        stub.add_client_error(
            "restore_object",
            service_error_code="RestoreAlreadyInProgress",
            http_status_code=409,
        )
        stats = RestoreEngine(s3, cfg).run()

    assert stats.get(Outcome.ALREADY_IN_PROGRESS) == 1
    assert stats.failures == 0
