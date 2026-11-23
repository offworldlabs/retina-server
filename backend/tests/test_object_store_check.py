"""Tests for scripts/object_store_check.py, against moto's in-process S3."""

import contextlib
import io
import unittest
import unittest.mock

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from scripts import object_store_check
from services import object_store
from tests.object_store_env import FAKE_AWS_ENV, store_env

BUCKET = FAKE_AWS_ENV["OBJECT_STORE_BUCKET"]
CHECK_NAMES = ("put", "get", "list", "multipart", "missing", "delete", "gone")


def run_check() -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = object_store_check.main(["--multipart-mib", "9"])
    return code, out.getvalue(), err.getvalue()


class ObjectStoreCheckTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(store_env(FAKE_AWS_ENV))
        self.enterContext(mock_aws())
        self.s3 = boto3.client("s3", region_name="us-east-1")
        self.s3.create_bucket(Bucket=BUCKET)

    def keys_under_healthcheck(self) -> list[str]:
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="healthcheck/")
        return [obj["Key"] for page in pages for obj in page.get("Contents", [])]

    def lines(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if line.startswith(("PASS ", "FAIL "))]

    def test_configured_store_passes_every_check_and_leaves_nothing(self):
        code, out, _ = run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.lines(out), [f"PASS {name}" for name in CHECK_NAMES])
        self.assertIn("7/7 checks passed", out)
        self.assertIn("provider=aws region=us-east-1 bucket=retina-data", out)
        self.assertEqual(self.keys_under_healthcheck(), [])

    def test_store_off_exits_2(self):
        with store_env({}):
            code, out, _ = run_check()
        self.assertEqual(code, 2)
        self.assertIn("Object store is off", out)
        self.assertEqual(self.lines(out), [])

    def test_failed_put_exits_1_and_cleans_up(self):
        with unittest.mock.patch.object(object_store, "upload_bytes", return_value=False):
            code, out, _ = run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL put: upload_bytes returned False", out)
        self.assertIn("PASS multipart", out)
        self.assertRegex(out, r"\d of 7 checks failed")
        self.assertEqual(self.keys_under_healthcheck(), [])

    def test_failed_delete_is_reported_and_cleanup_removes_the_object(self):
        with unittest.mock.patch.object(object_store, "delete_key", return_value=False):
            code, out, _ = run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL delete: delete_key returned False", out)
        self.assertIn("FAIL gone: 1 key(s) still listed", out)
        self.assertIn("cleanup: 1 of 1 leftover key(s) removed", out)
        self.assertEqual(self.keys_under_healthcheck(), [])

    def test_credentials_never_printed(self):
        code, out, err = run_check()
        self.assertEqual(code, 0, out)
        for secret in (FAKE_AWS_ENV["OBJECT_STORE_SECRET_ACCESS_KEY"], FAKE_AWS_ENV["OBJECT_STORE_ACCESS_KEY_ID"]):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, err)

    def test_missing_key_error_other_than_no_such_key_fails_the_check(self):
        real = object_store._get_client()

        class Denying:
            def __getattr__(self, name):
                return getattr(real, name)

            def get_object(self, **kwargs):
                if "never-written" in kwargs["Key"]:
                    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")
                return real.get_object(**kwargs)

        with unittest.mock.patch.object(object_store, "_get_client", return_value=Denying()):
            code, out, _ = run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL missing: get_object on a missing key raised AccessDenied, not NoSuchKey", out)
        self.assertIn("PASS put", out)
        self.assertIn("1 of 7 checks failed", out)
        self.assertEqual(self.keys_under_healthcheck(), [])

    def test_failed_listing_fails_gone_instead_of_passing(self):
        real = object_store._get_client()

        class FailingList:
            def __getattr__(self, name):
                return getattr(real, name)

            def list_objects_v2(self, **kwargs):
                if kwargs.get("MaxKeys") == 1:
                    raise ClientError({"Error": {"Code": "InternalError", "Message": "boom"}}, "ListObjectsV2")
                return real.list_objects_v2(**kwargs)

        with unittest.mock.patch.object(object_store, "_get_client", return_value=FailingList()):
            code, out, _ = run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL gone: listing failed: ClientError", out)
        self.assertIn("PASS delete", out)
        self.assertIn("1 of 7 checks failed", out)
        self.assertEqual(self.keys_under_healthcheck(), [])

    def test_no_client_fails_gone(self):
        with unittest.mock.patch.object(object_store, "_get_client", return_value=None):
            code, out, _ = run_check()
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL gone: no object store client", out)
