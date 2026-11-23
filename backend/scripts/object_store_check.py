"""Check that the configured object store works for every call the app makes.

Usage, from backend/ (in the container: docker compose exec -w /app/backend server ...):
    python3 -m scripts.object_store_check [--multipart-mib N]

Writes a few small objects and one random N MiB object (default 9, above the
8 MiB multipart threshold) under healthcheck/object-store-check/<time>-<id>/,
reads, lists and deletes them, and removes whatever is left when it finishes.
It prints PASS or FAIL for each step and no credentials.

Exit code: 0 all passed, 1 a step failed, 2 no object store is configured.

On B2 a deleted object leaves a hidden version until the bucket's lifecycle rule
"Keep only the last version" removes it, so the bucket shows a few files for a day.
"""

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime

from botocore.exceptions import ClientError

from services import object_store

logger = logging.getLogger("object_store_check")

_CHUNK = 1024 * 1024


class CheckFailed(Exception):
    pass


def _random_file(path: str, mib: int) -> str:
    """Fill path with mib MiB of random bytes and return their SHA-256."""
    digest = hashlib.sha256()
    with open(path, "wb") as f:
        for _ in range(mib):
            chunk = os.urandom(_CHUNK)
            digest.update(chunk)
            f.write(chunk)
    return digest.hexdigest()


class Run:
    def __init__(self, bucket: str, prefix: str, tmp_dir: str, multipart_mib: int):
        self.bucket = bucket
        self.prefix = prefix
        self.tmp_dir = tmp_dir
        self.multipart_mib = multipart_mib
        self.payload = json.dumps({"check": "object-store", "at": datetime.now(UTC).isoformat()}).encode()
        self.put_key = prefix + "put.json"
        self.multipart_key = prefix + "multipart.bin"
        self.delete_key = prefix + "delete.json"
        self.missing_key = prefix + "never-written.json"
        self.results: list[bool] = []

    def run(self, name: str, check: Callable[[], None]) -> None:
        try:
            check()
        except CheckFailed as e:
            print(f"FAIL {name}: {e}")
            self.results.append(False)
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")
            self.results.append(False)
        else:
            print(f"PASS {name}")
            self.results.append(True)

    def put(self) -> None:
        if not object_store.upload_bytes(self.put_key, self.payload):
            raise CheckFailed("upload_bytes returned False")

    def get(self) -> None:
        got = object_store.download_bytes(self.put_key)
        if got != self.payload:
            raise CheckFailed("download_bytes returned None" if got is None else "bytes differ from what was put")

    def list(self) -> None:
        if self.put_key not in object_store.list_keys(self.prefix):
            raise CheckFailed("the key is not in list_keys")

    def multipart(self) -> None:
        path = os.path.join(self.tmp_dir, "multipart.bin")
        sha = _random_file(path, self.multipart_mib)
        if not object_store.upload_file(self.multipart_key, path, content_type="application/octet-stream"):
            raise CheckFailed("upload_file returned False")
        got = object_store.download_bytes(self.multipart_key)
        if got is None:
            raise CheckFailed("download_bytes returned None")
        if hashlib.sha256(got).hexdigest() != sha:
            raise CheckFailed("SHA-256 differs from what was uploaded")

    def missing(self) -> None:
        client = object_store._get_client()
        try:
            client.get_object(Bucket=self.bucket, Key=self.missing_key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code != "NoSuchKey":
                raise CheckFailed(f"get_object on a missing key raised {code}, not NoSuchKey") from e
        else:
            raise CheckFailed("get_object on a missing key succeeded")
        if object_store.download_bytes(self.missing_key) is not None:
            raise CheckFailed("download_bytes on a missing key did not return None")

    def delete(self) -> None:
        if not object_store.upload_bytes(self.delete_key, self.payload):
            raise CheckFailed("could not write the object to delete")
        one = object_store.delete_key(self.delete_key)
        many = object_store.delete_keys([self.put_key, self.multipart_key])
        if not one:
            raise CheckFailed("delete_key returned False")
        if many != 2:
            raise CheckFailed(f"delete_keys deleted {many} of 2")

    def gone(self) -> None:
        # list_keys returns [] on any error, so list through the client to see a failure.
        client = object_store._get_client()
        if client is None:
            raise CheckFailed("no object store client")
        try:
            listed = client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, MaxKeys=1)
        except Exception as e:
            raise CheckFailed(f"listing failed: {type(e).__name__}: {e}") from e
        count = listed.get("KeyCount")
        if count is None:
            raise CheckFailed("the listing has no KeyCount")
        if count:
            raise CheckFailed(f"{count} key(s) still listed")


def _cleanup(prefix: str) -> None:
    left = object_store.list_keys(prefix)
    if left:
        removed = object_store.delete_keys(left)
        print(f"cleanup: {removed} of {len(left)} leftover key(s) removed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the configured object store with a real round trip.")
    ap.add_argument(
        "--multipart-mib",
        type=int,
        default=9,
        help="size of the multipart test object in MiB; above 8 to use multipart (default: 9)",
    )
    args = ap.parse_args(argv)
    if args.multipart_mib < 1:
        ap.error("--multipart-mib must be at least 1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    where = object_store.describe()
    if where is None:
        print(
            "Object store is off: no OBJECT_STORE_* or R2_* config, or an incomplete one "
            "(the missing variable is named in the log)."
        )
        return 2
    print(f"Object store: provider={where['provider']} region={where['region']} bucket={where['bucket']}")
    print(f"Config from:  {where['source']}_* variables")
    print(f"Endpoint:     {where['endpoint'] or '(AWS S3)'}")

    prefix = f"healthcheck/object-store-check/{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}/"
    print(f"Prefix:       {prefix}\n")

    with tempfile.TemporaryDirectory() as tmp_dir:
        run = Run(where["bucket"], prefix, tmp_dir, args.multipart_mib)
        try:
            for name, check in (
                ("put", run.put),
                ("get", run.get),
                ("list", run.list),
                ("multipart", run.multipart),
                ("missing", run.missing),
                ("delete", run.delete),
                ("gone", run.gone),
            ):
                run.run(name, check)
        finally:
            _cleanup(prefix)

    total = len(run.results)
    failed = total - sum(run.results)
    print()
    if failed:
        print(f"{failed} of {total} checks failed")
        return 1
    print(f"{total}/{total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
