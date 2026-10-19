"""Tests for services/object_store.py.

Config resolution is tested through resolve_config() with plain dicts, and
anything that needs the module's lazy, cached state goes through
tests.object_store_env. The B2 wire tests answer every request from a
before-send handler, so nothing leaves the process; the functional tests run
against moto's in-process S3.
"""

import ast
import hashlib
import importlib.util
import io
import logging
import os
import re
import tempfile
import threading
import unittest
import unittest.mock
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from dotenv import load_dotenv
from moto import mock_aws
from urllib3.response import HTTPResponse

from services import object_store
from services.object_store import StoreConfig, resolve_config
from tests.env_reads import Module, Resolver
from tests.object_store_env import FAKE_AWS_ENV, STORE_ENV_NAMES, store_env

LOGGER = object_store.logger.name

B2_ENDPOINT = "https://s3.us-west-004.backblazeb2.com"
B2_ENV = {
    "OBJECT_STORE_ENDPOINT_URL": B2_ENDPOINT,
    "OBJECT_STORE_ACCESS_KEY_ID": "b2-key-id",
    "OBJECT_STORE_SECRET_ACCESS_KEY": "b2-secret",
    "OBJECT_STORE_BUCKET": "retina-data",
}
R2_ENV = {
    "R2_ACCOUNT_ID": "testaccount",
    "R2_ACCESS_KEY_ID": "r2-key-id",
    "R2_SECRET_ACCESS_KEY": "r2-secret",
}


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    return caplog


def _messages(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == level]


def _config(env: dict[str, str]) -> StoreConfig:
    config = resolve_config(env)
    assert config is not None
    return config


# ── Which env group ──────────────────────────────────────────────────────────


def test_object_store_names_win_when_both_groups_are_set(logs):
    config = _config({**R2_ENV, "R2_BUCKET": "r2-bucket", **B2_ENV})

    assert (config.source, config.provider, config.endpoint_url) == ("OBJECT_STORE", "b2", B2_ENDPOINT)
    assert (config.access_key_id, config.secret_access_key, config.bucket) == ("b2-key-id", "b2-secret", "retina-data")
    (info,) = _messages(logs, logging.INFO)
    assert "R2_*" in info and "ignored" in info
    assert _messages(logs, logging.WARNING) == []


def test_r2_names_are_the_fallback(logs):
    assert resolve_config(R2_ENV) == StoreConfig(
        endpoint_url="https://testaccount.r2.cloudflarestorage.com",
        region="auto",
        access_key_id="r2-key-id",
        secret_access_key="r2-secret",
        bucket="retina-data",
        provider="r2",
        source="R2",
    )
    (warning,) = _messages(logs, logging.WARNING)
    assert "legacy" in warning and "OBJECT_STORE_*" in warning


def test_r2_endpoint_url_overrides_the_account_endpoint():
    config = _config({**R2_ENV, "R2_ENDPOINT_URL": "http://localhost:9000", "R2_BUCKET": "other-bucket"})

    assert (config.endpoint_url, config.region, config.provider) == ("http://localhost:9000", "us-east-1", "s3")
    assert (config.bucket, config.source) == ("other-bucket", "R2")


def test_the_legacy_warning_is_logged_once_however_often_the_store_is_used(logs):
    with store_env(R2_ENV), unittest.mock.patch("boto3.client"):
        for _ in range(3):
            assert object_store.is_enabled()
            object_store.describe()
            object_store._get_client()

    assert len(_messages(logs, logging.WARNING)) == 1


# ── Region ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "endpoint, region",
    [
        ("https://s3.us-west-004.backblazeb2.com", "us-west-004"),
        ("https://s3.us-west-004.backblazeb2.com:443", "us-west-004"),
        ("https://s3.us-west-004.backblazeb2.com/some/path", "us-west-004"),
        ("https://s3.us-west-004.backblazeb2.com/", "us-west-004"),
        ("HTTPS://S3.US-WEST-004.BACKBLAZEB2.COM", "us-west-004"),
        ("https://S3.Us-West-004.BackblazeB2.com:443/", "us-west-004"),
        ("https://s3.eu-central-003.backblazeb2.com", "eu-central-003"),
    ],
)
def test_the_region_comes_from_a_b2_endpoint(endpoint, region):
    config = _config({**B2_ENV, "OBJECT_STORE_ENDPOINT_URL": endpoint})

    assert (config.region, config.provider, config.endpoint_url) == (region, "b2", endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://s3.us-west-004.backblazeb2.com.example.net",
        "https://storage.example.net/s3.us-west-004.backblazeb2.com",
        "https://xs3.us-west-004.backblazeb2.com",
    ],
)
def test_only_a_b2_host_gives_a_b2_region(endpoint):
    config = _config({**B2_ENV, "OBJECT_STORE_ENDPOINT_URL": endpoint})

    assert (config.region, config.provider) == ("us-east-1", "s3")


def test_an_explicit_region_wins_and_warns_when_the_b2_endpoint_disagrees(logs):
    config = _config({**B2_ENV, "OBJECT_STORE_REGION": "eu-central-003"})

    assert config.region == "eu-central-003"
    (warning,) = _messages(logs, logging.WARNING)
    assert "eu-central-003" in warning and "us-west-004" in warning


@pytest.mark.parametrize(
    "endpoint, region",
    [
        (B2_ENDPOINT, "us-west-004"),
        ("https://testaccount.r2.cloudflarestorage.com", "us-east-1"),
        ("http://localhost:9000", "eu-west-1"),
        (None, "eu-west-1"),
    ],
)
def test_an_explicit_region_is_used_quietly_when_nothing_contradicts_it(endpoint, region, logs):
    env = {**B2_ENV, "OBJECT_STORE_REGION": region}
    if endpoint is None:
        del env["OBJECT_STORE_ENDPOINT_URL"]
    else:
        env["OBJECT_STORE_ENDPOINT_URL"] = endpoint

    assert _config(env).region == region
    assert _messages(logs, logging.WARNING) == []


@pytest.mark.parametrize(
    "endpoint, region, provider",
    [
        ("https://testaccount.r2.cloudflarestorage.com", "auto", "r2"),
        ("https://testaccount.eu.r2.cloudflarestorage.com", "auto", "r2"),
        ("http://localhost:9000", "us-east-1", "s3"),
        ("https://storage.example.net", "us-east-1", "s3"),
        (None, "us-east-1", "aws"),
    ],
)
def test_the_region_is_auto_for_r2_and_us_east_1_otherwise(endpoint, region, provider):
    env = dict(B2_ENV)
    if endpoint is None:
        del env["OBJECT_STORE_ENDPOINT_URL"]
    else:
        env["OBJECT_STORE_ENDPOINT_URL"] = endpoint

    config = _config(env)
    assert (config.endpoint_url, config.region, config.provider) == (endpoint, region, provider)


# ── Endpoint ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value, endpoint",
    [
        ("s3.us-west-004.backblazeb2.com", B2_ENDPOINT),
        ("  s3.us-west-004.backblazeb2.com\n", B2_ENDPOINT),
        ("s3.us-west-004.backblazeb2.com:443", f"{B2_ENDPOINT}:443"),
    ],
    ids=["bare", "padded", "with-port"],
)
def test_a_bare_b2_host_gets_https(value, endpoint, logs):
    """The Endpoint field of B2's console shows the host without a scheme."""
    env = {**B2_ENV, "OBJECT_STORE_ENDPOINT_URL": value}
    config = _config(env)
    assert (config.endpoint_url, config.provider, config.region) == (endpoint, "b2", "us-west-004")

    with store_env(env):
        assert object_store.describe() == {
            "provider": "b2",
            "endpoint": endpoint,
            "region": "us-west-004",
            "bucket": "retina-data",
            "source": "OBJECT_STORE",
        }
        client = object_store._get_client()
        assert client is not None
        assert (client.meta.endpoint_url, client.meta.region_name) == (endpoint, "us-west-004")
        url = client.generate_presigned_url("get_object", Params={"Bucket": "retina-data", "Key": "k"}, ExpiresIn=60)
        assert url.startswith(f"{endpoint}/retina-data/k?")
    assert _messages(logs, logging.ERROR) == []


def test_a_bare_r2_endpoint_gets_https(logs):
    env = {**R2_ENV, "R2_ENDPOINT_URL": "testaccount.r2.cloudflarestorage.com"}
    endpoint = "https://testaccount.r2.cloudflarestorage.com"
    config = _config(env)
    assert (config.endpoint_url, config.provider, config.region, config.source) == (endpoint, "r2", "auto", "R2")

    with store_env(env):
        client = object_store._get_client()
        assert client is not None
        assert (client.meta.endpoint_url, client.meta.region_name) == (endpoint, "auto")
    assert _messages(logs, logging.ERROR) == []


@pytest.mark.parametrize("name", ["OBJECT_STORE_ENDPOINT_URL", "R2_ENDPOINT_URL"])
@pytest.mark.parametrize(
    "endpoint",
    ["http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:9000", "https://storage.example.net:8443/s3"],
    ids=["localhost", "ipv4", "ipv6", "port-and-path"],
)
def test_a_full_endpoint_url_is_kept_as_given(name, endpoint, logs):
    env = {**(B2_ENV if name.startswith("OBJECT_STORE_") else R2_ENV), name: endpoint}
    config = _config(env)
    assert (config.endpoint_url, config.provider, config.region) == (endpoint, "s3", "us-east-1")

    with store_env(env):
        client = object_store._get_client()
        assert client is not None
        assert client.meta.endpoint_url == endpoint
    assert _messages(logs, logging.ERROR) == []


_NOT_ENDPOINTS = {
    "ftp": "ftp://s3.us-west-004.backblazeb2.com",
    "no-host": "https://",
    "no-scheme": "://bad",
    "space-in-host": "s3.us-west-004 .backblazeb2.com",
    "space-in-path": "https://s3.us-west-004.backblazeb2.com/a b",
    "newline-in-host": "s3.us-west-004.\nbackblazeb2.com",
    "port-not-a-number": "https://s3.us-west-004.backblazeb2.com:port",
    "port-out-of-range": "https://s3.us-west-004.backblazeb2.com:65536",
    "one-slash": "https:/s3.us-west-004.backblazeb2.com",
    "no-colon": "https//s3.us-west-004.backblazeb2.com",
    "user-and-key": "https://key-id:secret@s3.us-west-004.backblazeb2.com",
    "underscore-in-host": "https://s3_us_west_004.backblazeb2.com",
    "broken-ipv6": "http://[::1:9000",
}


@pytest.mark.parametrize("name", ["OBJECT_STORE_ENDPOINT_URL", "R2_ENDPOINT_URL"])
@pytest.mark.parametrize("value", list(_NOT_ENDPOINTS.values()), ids=list(_NOT_ENDPOINTS))
def test_an_endpoint_that_is_not_a_url_turns_the_store_off(name, value, logs):
    """Off with one error, and nothing tried instead: the R2_* names are set
    beside OBJECT_STORE_*, and R2_ACCOUNT_ID beside R2_ENDPOINT_URL."""
    env = {**R2_ENV, name: value}
    if name.startswith("OBJECT_STORE_"):
        env = {**B2_ENV, **env}

    with store_env(env), unittest.mock.patch("boto3.client") as build:
        for _ in range(3):
            assert object_store.is_enabled() is False
        assert object_store.describe() is None
        assert object_store.upload_bytes("k", b"v") is False

    build.assert_not_called()
    assert _messages(logs, logging.ERROR) == [f"Object store disabled: {name} must be http(s)://host[:port]"]


# ── Off ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [{}, {"R2_BUCKET": "retina-data"}, {"R2_ACCOUNT_ID": "testaccount", "R2_ACCESS_KEY_ID": "r2-key-id"}],
    ids=["nothing", "r2-bucket-only", "r2-without-secret"],
)
def test_unset_means_every_operation_is_a_no_op(env, logs):
    with store_env(env), unittest.mock.patch("boto3.client") as build:
        assert object_store.is_enabled() is False
        assert object_store.describe() is None
        assert object_store.upload_bytes("k", b"v") is False
        assert object_store.upload_file("k", "/nonexistent/path") is False
        assert object_store.download_bytes("k") is None
        assert object_store.list_keys("archive/") == []
        assert object_store.delete_key("k") is False
        assert object_store.delete_keys(["a", "b"]) == 0

    build.assert_not_called()
    assert [r for r in logs.records if r.name == LOGGER] == []


def test_a_partial_object_store_config_is_off_and_never_falls_back_to_r2(logs):
    env = {**R2_ENV, "OBJECT_STORE_ENDPOINT_URL": B2_ENDPOINT, "OBJECT_STORE_ACCESS_KEY_ID": "partial-key-id"}

    with store_env(env), unittest.mock.patch("boto3.client") as build:
        for _ in range(3):
            assert object_store.is_enabled() is False
        assert object_store.describe() is None
        assert object_store.upload_bytes("k", b"v") is False

    build.assert_not_called()
    (error,) = _messages(logs, logging.ERROR)
    assert "OBJECT_STORE_SECRET_ACCESS_KEY" in error and "OBJECT_STORE_BUCKET" in error
    assert "OBJECT_STORE_ACCESS_KEY_ID" not in error and "partial-key-id" not in error


@pytest.mark.parametrize(
    "given, missing",
    [
        ({"OBJECT_STORE_REGION": "us-west-004"}, ["ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "BUCKET"]),
        ({"OBJECT_STORE_ENDPOINT_URL": B2_ENDPOINT}, ["ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "BUCKET"]),
        ({"OBJECT_STORE_ACCESS_KEY_ID": "k", "OBJECT_STORE_SECRET_ACCESS_KEY": "s"}, ["BUCKET"]),
        ({"OBJECT_STORE_BUCKET": "retina-data"}, ["ACCESS_KEY_ID", "SECRET_ACCESS_KEY"]),
    ],
)
def test_the_error_names_each_missing_variable(given, missing, logs):
    assert resolve_config({**R2_ENV, **given}) is None

    (error,) = _messages(logs, logging.ERROR)
    assert set(re.findall(r"OBJECT_STORE_([A-Z_]+)", error)) == set(missing)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
def test_blank_values_count_as_unset(blank, logs):
    env = dict.fromkeys(STORE_ENV_NAMES, blank) | {
        "R2_ACCOUNT_ID": f" testaccount{blank}",
        "R2_ACCESS_KEY_ID": " r2-key-id ",
        "R2_SECRET_ACCESS_KEY": "r2-secret\n",
    }
    config = _config(env)
    assert (config.source, config.endpoint_url) == ("R2", "https://testaccount.r2.cloudflarestorage.com")
    assert (config.access_key_id, config.secret_access_key, config.bucket) == ("r2-key-id", "r2-secret", "retina-data")

    assert resolve_config({**R2_ENV, "R2_ACCOUNT_ID": blank}) is None

    assert resolve_config({**B2_ENV, "OBJECT_STORE_BUCKET": blank}) is None
    (error,) = _messages(logs, logging.ERROR)
    assert error.endswith("missing OBJECT_STORE_BUCKET")


def test_object_store_values_are_stripped():
    config = _config({name: f"  {value}\n" for name, value in {**B2_ENV, "OBJECT_STORE_REGION": "us-west-004"}.items()})

    assert config == StoreConfig(
        endpoint_url=B2_ENDPOINT,
        region="us-west-004",
        access_key_id="b2-key-id",
        secret_access_key="b2-secret",
        bucket="retina-data",
        provider="b2",
        source="OBJECT_STORE",
    )


# ── Lazy, cached, and what it shows ──────────────────────────────────────────


def test_the_environment_is_read_on_first_use_not_at_import():
    """main.py imports the services before load_dotenv(), so the module has to
    read its env when first used. A fresh copy of it is imported with no store
    env, which is then set, as load_dotenv would."""
    spec = importlib.util.spec_from_file_location("object_store_fresh_import", object_store.__file__)
    assert spec is not None and spec.loader is not None
    with store_env({}):
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        os.environ.update(B2_ENV)

        assert fresh.is_enabled()
        assert fresh.describe() == {
            "provider": "b2",
            "endpoint": B2_ENDPOINT,
            "region": "us-west-004",
            "bucket": "retina-data",
            "source": "OBJECT_STORE",
        }


def test_clear_cache_makes_the_next_call_read_the_environment_again():
    with store_env({}):
        assert not object_store.is_enabled()
        os.environ.update(B2_ENV)
        assert not object_store.is_enabled()
        object_store._clear_cache()
        assert object_store.is_enabled()


_KEY_ID = "KEYID-must-not-be-shown"
_SECRET = "SECRET-must-not-be-shown"


@pytest.mark.parametrize(
    "env",
    [
        {**B2_ENV, "OBJECT_STORE_ACCESS_KEY_ID": _KEY_ID, "OBJECT_STORE_SECRET_ACCESS_KEY": _SECRET},
        {
            **R2_ENV,
            **B2_ENV,
            "OBJECT_STORE_REGION": "eu-central-003",
            "OBJECT_STORE_ACCESS_KEY_ID": _KEY_ID,
            "OBJECT_STORE_SECRET_ACCESS_KEY": _SECRET,
        },
        {"OBJECT_STORE_ACCESS_KEY_ID": _KEY_ID, "OBJECT_STORE_SECRET_ACCESS_KEY": _SECRET},
        {"R2_ACCOUNT_ID": "testaccount", "R2_ACCESS_KEY_ID": _KEY_ID, "R2_SECRET_ACCESS_KEY": _SECRET},
    ],
    ids=["b2", "region-mismatch-and-r2-ignored", "partial", "r2"],
)
def test_keys_never_appear_in_the_logs_or_the_config_repr(env, logs):
    with store_env(env):
        object_store.is_enabled()
        object_store._get_client()
        config = object_store._config()
        shown = [repr(config), str(config), repr(object_store.describe())]

    assert any(r.name == LOGGER for r in logs.records), "nothing was logged, so there was nothing to check"
    for text in [logs.text, *shown]:
        assert _KEY_ID not in text
        assert _SECRET not in text


def test_the_client_logs_where_it_points_once(logs):
    with store_env(B2_ENV):
        object_store._get_client()
        object_store._get_client()

    assert _messages(logs, logging.INFO) == [
        "Object store client initialised: provider=b2 region=us-west-004 bucket=retina-data source=OBJECT_STORE"
    ]


def test_describe_names_the_store_without_credentials():
    with store_env(FAKE_AWS_ENV):
        assert object_store.describe() == {
            "provider": "aws",
            "endpoint": "",
            "region": "us-east-1",
            "bucket": "retina-data",
            "source": "OBJECT_STORE",
        }
    with store_env(R2_ENV):
        assert object_store.describe() == {
            "provider": "r2",
            "endpoint": "https://testaccount.r2.cloudflarestorage.com",
            "region": "auto",
            "bucket": "retina-data",
            "source": "R2",
        }
    with store_env({}):
        assert object_store.describe() is None


def test_the_module_reads_exactly_the_store_env_names():
    """The env names come out of tests/env_reads.py the way test_env_example.py
    collects them, and store_env() covers exactly those."""
    source = Path(object_store.__file__).read_text()
    module = Module("services.object_store", ast.parse(source))
    reads = Resolver({module.name: module}).reads()

    assert [r.problem for r in reads if r.problem] == []
    assert set().union(*(r.keys for r in reads)) == set(STORE_ENV_NAMES)


def test_store_env_sets_exactly_the_given_names_and_puts_them_back(monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "ambient")
    monkeypatch.delenv("OBJECT_STORE_BUCKET", raising=False)

    with store_env({"OBJECT_STORE_BUCKET": "inside"}):
        assert os.environ["OBJECT_STORE_BUCKET"] == "inside"
        assert "R2_BUCKET" not in os.environ

    assert os.environ["R2_BUCKET"] == "ambient"
    assert "OBJECT_STORE_BUCKET" not in os.environ
    with pytest.raises(ValueError, match="AWS_REGION"), store_env({"AWS_REGION": "us-east-1"}):
        pass


# ── The suite's own environment ──────────────────────────────────────────────


def test_the_suite_runs_with_no_store_configured():
    """conftest.py empties the ten names before any test runs, so a shell that
    exports a real bucket never has the suite write to it."""
    assert {name: os.environ.get(name) for name in STORE_ENV_NAMES} == dict.fromkeys(STORE_ENV_NAMES, "")
    assert resolve_config(os.environ) is None


def test_a_backend_dotenv_cannot_configure_a_store_for_the_suite():
    """main.py's load_dotenv() leaves a variable that is already set alone, so the
    empty values conftest.py sets keep a worktree's backend/.env out as well."""
    dotenv = "".join(f"{name}=from-dotenv\n" for name in STORE_ENV_NAMES)
    with unittest.mock.patch.dict(os.environ):
        load_dotenv(stream=io.StringIO(dotenv))
        loaded = sorted(name for name in STORE_ENV_NAMES if os.environ.get(name))

    assert loaded == []


# ── On the wire, for B2 (no network) ─────────────────────────────────────────


@dataclass(frozen=True)
class _Sent:
    method: str
    url: str
    headers: dict[str, str]

    @property
    def query(self) -> str:
        return urlsplit(self.url).query


def _canned(method: str, url: str) -> bytes:
    query = urlsplit(url).query
    if method == "POST" and query == "uploads":
        return b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>"
    if method == "POST" and "uploadId=" in query:
        return b"<CompleteMultipartUploadResult><ETag>&quot;0&quot;</ETag></CompleteMultipartUploadResult>"
    if method == "GET":
        return b'{"canned": true}'
    return b""


class _Wire:
    """Answers each request with a canned 200 and keeps what was sent."""

    def __init__(self):
        self.sent: list[_Sent] = []
        self._lock = threading.Lock()

    def answer(self, request, **_kwargs):
        headers = {k.lower(): v.decode() if isinstance(v, bytes) else v for k, v in request.headers.items()}
        with self._lock:
            self.sent.append(_Sent(request.method, request.url, headers))
        body = _canned(request.method, request.url)
        headers = {"ETag": '"0"', "Content-Length": str(len(body))}
        raw = HTTPResponse(body=io.BytesIO(body), headers=headers, status=200, preload_content=False)
        return AWSResponse(request.url, 200, headers, raw)


@pytest.fixture
def b2_client():
    with store_env(B2_ENV):
        client = object_store._get_client()
        assert client is not None
        yield client


@pytest.fixture
def wire(b2_client):
    sent = _Wire()
    b2_client.meta.events.register("before-send.s3", sent.answer)
    return sent


_B2_SCOPE = "/us-west-004/s3/aws4_request,"


def test_the_b2_client_pins_path_style_and_checksums_when_required(b2_client):
    config = b2_client.meta.config
    assert config.s3 == {"addressing_style": "path"}
    assert (config.request_checksum_calculation, config.response_checksum_validation) == (
        "when_required",
        "when_required",
    )
    assert b2_client.meta.region_name == "us-west-004"


def test_b2_put_object_is_path_style_and_sends_no_checksum_headers(wire):
    payload = b'{"frame": 1}'
    assert object_store.upload_bytes("archive/2026/10/19/a.json", payload)

    (put,) = wire.sent
    assert (put.method, put.url) == ("PUT", f"{B2_ENDPOINT}/retina-data/archive/2026/10/19/a.json")
    assert _B2_SCOPE in put.headers["authorization"]
    assert "x-amz-sdk-checksum-algorithm" not in put.headers
    assert "x-amz-trailer" not in put.headers
    assert "aws-chunked" not in put.headers.get("content-encoding", "")
    assert put.headers["x-amz-content-sha256"] == hashlib.sha256(payload).hexdigest()


def test_b2_get_object_is_path_style_and_asks_for_no_checksum(wire):
    assert object_store.download_bytes("archive/a.json") == b'{"canned": true}'

    (get,) = wire.sent
    assert (get.method, get.url) == ("GET", f"{B2_ENDPOINT}/retina-data/archive/a.json")
    assert _B2_SCOPE in get.headers["authorization"]
    assert "x-amz-checksum-mode" not in get.headers


def test_b2_multipart_upload_file_sends_no_checksum_headers(wire, tmp_path):
    path = tmp_path / "big.parquet"
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))  # past s3transfer's 8 MiB multipart threshold

    assert object_store.upload_file("archive/big.parquet", str(path), "application/octet-stream")

    create = [s for s in wire.sent if s.method == "POST" and s.query == "uploads"]
    parts = [s for s in wire.sent if s.method == "PUT"]
    complete = [s for s in wire.sent if s.method == "POST" and s.query.startswith("uploadId=")]
    assert (len(create), len(parts), len(complete), len(wire.sent)) == (1, 2, 1, 4)
    for sent in wire.sent:
        assert sent.url.startswith(f"{B2_ENDPOINT}/retina-data/archive/big.parquet?")
        assert _B2_SCOPE in sent.headers["authorization"]
    assert "x-amz-checksum-algorithm" not in create[0].headers
    for part in parts:
        assert "x-amz-trailer" not in part.headers
        assert "x-amz-sdk-checksum-algorithm" not in part.headers
        assert "aws-chunked" not in part.headers.get("content-encoding", "")


def test_a_b2_presigned_get_is_path_style_with_the_b2_region(b2_client):
    url = b2_client.generate_presigned_url(
        "get_object", Params={"Bucket": "retina-data", "Key": "archive/a.parquet"}, ExpiresIn=300
    )

    assert url.startswith(f"{B2_ENDPOINT}/retina-data/archive/a.parquet?")
    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Credential"][0].endswith("/us-west-004/s3/aws4_request")


def test_the_aws_client_keeps_botocore_default_checksums(monkeypatch):
    monkeypatch.delenv("AWS_REQUEST_CHECKSUM_CALCULATION", raising=False)
    monkeypatch.delenv("AWS_RESPONSE_CHECKSUM_VALIDATION", raising=False)
    bare = boto3.client("s3", region_name="us-east-1", aws_access_key_id="AKIATEST", aws_secret_access_key="testsecret")

    with store_env(FAKE_AWS_ENV):
        client = object_store._get_client()
        assert client is not None
        assert client.meta.config.request_checksum_calculation == bare.meta.config.request_checksum_calculation
        assert client.meta.config.response_checksum_validation == bare.meta.config.response_checksum_validation
        assert client.meta.config.s3 == bare.meta.config.s3
        assert (client.meta.region_name, client.meta.endpoint_url) == ("us-east-1", bare.meta.endpoint_url)


# ── Every operation against moto ─────────────────────────────────────────────


class TestObjectStoreOnMoto(unittest.TestCase):
    """Each test gets the fake AWS env, a fresh moto S3 and an empty bucket."""

    def setUp(self):
        self.enterContext(store_env(FAKE_AWS_ENV))
        self.enterContext(mock_aws())
        boto3.client(
            "s3", region_name="us-east-1", aws_access_key_id="AKIATEST", aws_secret_access_key="testsecret"
        ).create_bucket(Bucket="retina-data")

    # ── is_enabled ──

    def test_is_enabled_true(self):
        self.assertTrue(object_store.is_enabled())

    def test_is_enabled_false_when_no_creds(self):
        with store_env({}):
            self.assertFalse(object_store.is_enabled())

    # ── upload_bytes ──

    def test_upload_bytes_success(self):
        self.assertTrue(object_store.upload_bytes("test/hello.json", b'{"hello": "world"}'))

    def test_upload_bytes_content_is_stored(self):
        payload = b'{"answer": 42}'
        object_store.upload_bytes("test/data.json", payload)
        self.assertEqual(object_store.download_bytes("test/data.json"), payload)

    def test_upload_bytes_noop_when_disabled(self):
        with store_env({}):
            self.assertFalse(object_store.upload_bytes("test/x.json", b"data"))

    # ── upload_file ──

    def test_upload_file_success(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())) / "state.json"
        tmp.write_bytes(b'{"snapshot": true}')
        self.assertTrue(object_store.upload_file("snapshots/state.json", str(tmp)))
        self.assertEqual(object_store.download_bytes("snapshots/state.json"), b'{"snapshot": true}')

    def test_upload_file_noop_when_disabled(self):
        with store_env({}):
            self.assertFalse(object_store.upload_file("k", "/nonexistent/path"))

    # ── download_bytes ──

    def test_download_bytes_returns_none_for_missing_key(self):
        self.assertIsNone(object_store.download_bytes("does/not/exist.json"))

    def test_download_bytes_round_trip(self):
        data = b'{"key": "value", "n": 123}'
        object_store.upload_bytes("round/trip.json", data)
        self.assertEqual(object_store.download_bytes("round/trip.json"), data)

    def test_download_bytes_noop_when_disabled(self):
        with store_env({}):
            self.assertIsNone(object_store.download_bytes("any/key"))

    # ── list_keys ──

    def test_list_keys_empty_bucket(self):
        self.assertEqual(object_store.list_keys("archive/"), [])

    def test_list_keys_with_prefix(self):
        object_store.upload_bytes("archive/2025/01/file1.json", b"1")
        object_store.upload_bytes("archive/2025/02/file2.json", b"2")
        object_store.upload_bytes("snapshots/state.json", b"3")
        self.assertEqual(
            sorted(object_store.list_keys("archive/")),
            ["archive/2025/01/file1.json", "archive/2025/02/file2.json"],
        )

    def test_list_keys_no_prefix(self):
        object_store.upload_bytes("a.json", b"a")
        object_store.upload_bytes("b.json", b"b")
        self.assertEqual(sorted(object_store.list_keys()), ["a.json", "b.json"])

    def test_list_keys_noop_when_disabled(self):
        with store_env({}):
            self.assertEqual(object_store.list_keys(), [])

    # ── delete_key ──

    def test_delete_key_removes_object(self):
        object_store.upload_bytes("to_delete.json", b"bye")
        self.assertTrue(object_store.delete_key("to_delete.json"))
        self.assertIsNone(object_store.download_bytes("to_delete.json"))

    def test_delete_key_nonexistent_returns_true(self):
        # S3 delete_object on a missing key is idempotent (204)
        self.assertTrue(object_store.delete_key("ghost.json"))

    def test_delete_key_noop_when_disabled(self):
        with store_env({}):
            self.assertFalse(object_store.delete_key("x"))

    # ── delete_keys ──

    def test_delete_keys_bulk(self):
        keys = [f"file/{i}.json" for i in range(10)]
        for k in keys:
            object_store.upload_bytes(k, b"x")
        self.assertEqual(object_store.delete_keys(keys), 10)
        self.assertEqual(object_store.list_keys("file/"), [])

    def test_delete_keys_empty_list(self):
        self.assertEqual(object_store.delete_keys([]), 0)

    def test_delete_keys_noop_when_disabled(self):
        with store_env({}):
            self.assertEqual(object_store.delete_keys(["a", "b"]), 0)

    # ── when the boto3 call raises ──

    def _failing_client(self, method: str):
        """Patch _get_client with a client whose named method raises."""
        client = unittest.mock.MagicMock()
        getattr(client, method).side_effect = Exception("boto3 error")
        # download_bytes catches client.exceptions.NoSuchKey before Exception, so it
        # has to be a real exception class for the except clause to evaluate.
        client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        return unittest.mock.patch.object(object_store, "_get_client", return_value=client)

    def test_upload_bytes_exception_returns_false(self):
        with self._failing_client("put_object"):
            self.assertFalse(object_store.upload_bytes("k", b"data"))

    def test_upload_file_exception_returns_false(self):
        with self._failing_client("upload_file"):
            self.assertFalse(object_store.upload_file("k", "/some/path"))

    def test_download_bytes_exception_returns_none(self):
        with self._failing_client("get_object"):
            self.assertIsNone(object_store.download_bytes("k"))

    def test_list_keys_exception_returns_empty(self):
        with self._failing_client("get_paginator"):
            self.assertEqual(object_store.list_keys(), [])

    def test_delete_key_exception_returns_false(self):
        with self._failing_client("delete_object"):
            self.assertFalse(object_store.delete_key("k"))

    def test_delete_keys_exception_returns_zero(self):
        with self._failing_client("delete_objects"):
            self.assertEqual(object_store.delete_keys(["a", "b"]), 0)

    def test_get_client_exception_returns_none(self):
        """boto3.client() itself raises: _get_client returns None and logs why."""
        with unittest.mock.patch("boto3.client", side_effect=Exception("no boto3")), self.assertLogs(LOGGER, "ERROR"):
            object_store._clear_cache()
            self.assertIsNone(object_store._get_client())
        object_store._clear_cache()
