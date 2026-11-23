"""Give a test its own object store environment.

    from tests.object_store_env import FAKE_AWS_ENV, store_env

    with store_env(FAKE_AWS_ENV), mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        assert object_store.upload_bytes("k", b"v")

In a unittest.TestCase, call `self.enterContext(store_env(FAKE_AWS_ENV))` in
setUp; the store is turned off for a test with `store_env({})`.

STORE_ENV_NAMES  The ten variables services/object_store.py reads.
store_env(env)   Sets exactly those of them that env gives and removes the
                 others, then puts all ten back as they were on exit. It clears
                 the module's cached config and client on entry and on exit, so
                 the code under test resolves the store afresh. A name outside
                 STORE_ENV_NAMES is a ValueError.
FAKE_AWS_ENV     Keys and the bucket "retina-data" with no endpoint, so the
                 client goes to AWS S3, which moto's mock_aws intercepts.
"""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from services import object_store

STORE_ENV_NAMES = (
    "OBJECT_STORE_ENDPOINT_URL",
    "OBJECT_STORE_REGION",
    "OBJECT_STORE_ACCESS_KEY_ID",
    "OBJECT_STORE_SECRET_ACCESS_KEY",
    "OBJECT_STORE_BUCKET",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_ENDPOINT_URL",
)

FAKE_AWS_ENV: Mapping[str, str] = MappingProxyType(
    {
        "OBJECT_STORE_ACCESS_KEY_ID": "AKIATEST",
        "OBJECT_STORE_SECRET_ACCESS_KEY": "testsecret",
        "OBJECT_STORE_BUCKET": "retina-data",
    }
)


@contextmanager
def store_env(env: Mapping[str, str]) -> Iterator[None]:
    unknown = sorted(set(env) - set(STORE_ENV_NAMES))
    if unknown:
        raise ValueError(f"not an object store variable: {', '.join(unknown)}")
    saved = {name: os.environ.get(name) for name in STORE_ENV_NAMES}
    try:
        for name in STORE_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.update(env)
        object_store._clear_cache()
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        object_store._clear_cache()
