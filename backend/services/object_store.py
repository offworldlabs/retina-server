"""S3-compatible object storage: Backblaze B2, Cloudflare R2, AWS S3 or MinIO.

Upload, download, list and delete in one bucket. With no store configured every
operation is a no-op, so the server runs fine without one.

The environment is read on first use, never at import, because main.py loads
.env after importing the services. It is then kept; _clear_cache() makes the
next call read it again.

    OBJECT_STORE_ENDPOINT_URL       e.g. https://s3.us-west-004.backblazeb2.com; unset for AWS S3
    OBJECT_STORE_REGION             optional; see below
    OBJECT_STORE_ACCESS_KEY_ID      required
    OBJECT_STORE_SECRET_ACCESS_KEY  required
    OBJECT_STORE_BUCKET             required

    Legacy: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (all three
    required), R2_BUCKET (default retina-data) and R2_ENDPOINT_URL (default
    https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com).

An empty or blank value counts as unset. If any OBJECT_STORE_* name is set, that
group alone is used: a missing key id, secret or bucket turns the store off with
one error naming them, and the R2_* names are never a fallback. Otherwise the
R2_* names are used when their three required ones are set, with a warning that
they are legacy. Otherwise the store is off.

An endpoint given as a bare host, as B2's console shows it, gets https://. Any
other endpoint must be http(s)://host[:port]; one that is not turns the store
off with one error naming the variable, and nothing else is tried instead.

The region is OBJECT_STORE_REGION when set; else the <region> of an
s3.<region>.backblazeb2.com endpoint; else "auto" for *.r2.cloudflarestorage.com;
else "us-east-1" (AWS, MinIO and other S3-compatibles).
"""

import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_OBJECT_STORE_NAMES = (
    "OBJECT_STORE_ENDPOINT_URL",
    "OBJECT_STORE_REGION",
    "OBJECT_STORE_ACCESS_KEY_ID",
    "OBJECT_STORE_SECRET_ACCESS_KEY",
    "OBJECT_STORE_BUCKET",
)
_OBJECT_STORE_REQUIRED = ("OBJECT_STORE_ACCESS_KEY_ID", "OBJECT_STORE_SECRET_ACCESS_KEY", "OBJECT_STORE_BUCKET")
_R2_NAMES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_ENDPOINT_URL")
_R2_DEFAULT_BUCKET = "retina-data"

_B2_HOST = re.compile(r"s3\.([a-z0-9-]+)\.backblazeb2\.com")
_R2_HOST_SUFFIX = ".r2.cloudflarestorage.com"


@dataclass(frozen=True)
class StoreConfig:
    """Where the store is and what signs for it. The repr leaves out both keys."""

    endpoint_url: str | None  # None is AWS S3
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    bucket: str
    provider: str  # "b2", "r2", "aws", or "s3" for any other endpoint
    source: str  # the env group it came from: "OBJECT_STORE" or "R2"


def _host(endpoint_url: str | None) -> str:
    """The endpoint's host name in lower case, without scheme, port or path."""
    if not endpoint_url:
        return ""
    try:
        return (urlsplit(endpoint_url).hostname or "").lower()
    except ValueError:
        return ""


def _provider(endpoint_url: str | None) -> str:
    if endpoint_url is None:
        return "aws"
    host = _host(endpoint_url)
    if _B2_HOST.fullmatch(host):
        return "b2"
    if host.endswith(_R2_HOST_SUFFIX):
        return "r2"
    return "s3"


def _region(endpoint_url: str | None, explicit: str = "") -> str:
    """The SigV4 region, in the order the module docstring gives."""
    host = _host(endpoint_url)
    b2 = _B2_HOST.fullmatch(host)
    if explicit:
        if b2 and b2.group(1) != explicit:
            logger.warning(
                "Object store: OBJECT_STORE_REGION is %s but the endpoint is in %s, so requests will fail to sign",
                explicit,
                b2.group(1),
            )
        return explicit
    if b2:
        return b2.group(1)
    if host.endswith(_R2_HOST_SUFFIX):
        return "auto"
    return "us-east-1"


def _endpoint_url(value: str) -> str | None:
    """value as an endpoint URL the client can use, or None when it is not one.

    A value without "://" is a bare host and gets https://, the only scheme B2 and
    R2 serve the S3 API on. Anything else is kept as given once it passes.
    """
    url = value if "://" in value else f"https://{value}"
    # urlsplit drops tabs and newlines silently, and a host never has a space.
    if any(char.isspace() for char in url):
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # a ValueError unless it is a number from 0 to 65535
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    # "https:/host" and "https//host" read as a bare host named "https".
    if parts.hostname in ("http", "https"):
        return None
    # A user:key@ part would show a key wherever the endpoint is shown.
    if "@" in parts.netloc:
        return None
    # The host check the client build makes; a host it rejects fails every build.
    from botocore.utils import is_valid_uri

    return url if is_valid_uri(url) else None


def _endpoint_or_error(name: str, value: str) -> str | None:
    url = _endpoint_url(value)
    if url is None:
        logger.error("Object store disabled: %s must be http(s)://host[:port]", name)
    return url


def resolve_config(env: Mapping[str, str]) -> StoreConfig | None:
    """The store config an environment describes, or None when it describes none.

    Logs what an operator needs to know (a partial OBJECT_STORE_* config, an
    endpoint that is not a URL, the R2_* names in use or ignored, a region the
    endpoint contradicts), never a key.
    """
    new = {name: (env.get(name) or "").strip() for name in _OBJECT_STORE_NAMES}
    legacy = {name: (env.get(name) or "").strip() for name in _R2_NAMES}

    if any(new.values()):
        if any(legacy.values()):
            logger.info("Object store: OBJECT_STORE_* is set, so the R2_* variables are ignored")
        missing = [name for name in _OBJECT_STORE_REQUIRED if not new[name]]
        if missing:
            logger.error("Object store disabled: OBJECT_STORE_* is set but missing %s", ", ".join(missing))
            return None
        endpoint = None
        if new["OBJECT_STORE_ENDPOINT_URL"]:
            endpoint = _endpoint_or_error("OBJECT_STORE_ENDPOINT_URL", new["OBJECT_STORE_ENDPOINT_URL"])
            if endpoint is None:
                return None
        return StoreConfig(
            endpoint_url=endpoint,
            region=_region(endpoint, new["OBJECT_STORE_REGION"]),
            access_key_id=new["OBJECT_STORE_ACCESS_KEY_ID"],
            secret_access_key=new["OBJECT_STORE_SECRET_ACCESS_KEY"],
            bucket=new["OBJECT_STORE_BUCKET"],
            provider=_provider(endpoint),
            source="OBJECT_STORE",
        )

    if legacy["R2_ACCOUNT_ID"] and legacy["R2_ACCESS_KEY_ID"] and legacy["R2_SECRET_ACCESS_KEY"]:
        logger.warning("Object store: the R2_* variables are legacy; set OBJECT_STORE_* instead")
        endpoint = f"https://{legacy['R2_ACCOUNT_ID']}{_R2_HOST_SUFFIX}"
        if legacy["R2_ENDPOINT_URL"]:
            endpoint = _endpoint_or_error("R2_ENDPOINT_URL", legacy["R2_ENDPOINT_URL"])
            if endpoint is None:
                return None
        return StoreConfig(
            endpoint_url=endpoint,
            region=_region(endpoint),
            access_key_id=legacy["R2_ACCESS_KEY_ID"],
            secret_access_key=legacy["R2_SECRET_ACCESS_KEY"],
            bucket=legacy["R2_BUCKET"] or _R2_DEFAULT_BUCKET,
            provider=_provider(endpoint),
            source="R2",
        )

    return None


# Filled on first use under the lock, so concurrent first calls resolve and log once.
_lock = threading.Lock()
_config_resolved = False
_config_cache: StoreConfig | None = None
_client_built = False
_client_cache: Any = None


def _config() -> StoreConfig | None:
    global _config_resolved, _config_cache
    if not _config_resolved:
        with _lock:
            if not _config_resolved:
                _config_cache = resolve_config(os.environ)
                _config_resolved = True
    return _config_cache


def _build_client(config: StoreConfig) -> Any:
    try:
        import boto3
        from botocore.config import Config

        options: dict[str, Any] = {
            "retries": {"max_attempts": 3, "mode": "adaptive"},
            "connect_timeout": 10,
            "read_timeout": 30,
        }
        if config.endpoint_url:
            # Not AWS: path-style URLs, and checksums only where an operation requires one.
            # botocore >= 1.36 otherwise sends CRC32 trailers that B2 has rejected. B2's S3 API:
            # https://www.backblaze.com/docs/cloud-storage-call-the-s3-compatible-api
            options["s3"] = {"addressing_style": "path"}
            options["request_checksum_calculation"] = "when_required"
            options["response_checksum_validation"] = "when_required"
        client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name=config.region,
            config=Config(**options),
        )
    except Exception:
        logger.exception("Failed to initialise the object store client")
        return None
    logger.info(
        "Object store client initialised: provider=%s region=%s bucket=%s source=%s",
        config.provider,
        config.region,
        config.bucket,
        config.source,
    )
    return client


def _get_client() -> Any:
    """The boto3 S3 client, built once; None when the store is off or the build failed."""
    global _client_built, _client_cache
    if not _client_built:
        config = _config()
        with _lock:
            if not _client_built:
                _client_cache = None if config is None else _build_client(config)
                _client_built = True
    return _client_cache


def _target() -> tuple[Any, str] | None:
    """The client and the bucket, or None when there is nothing to talk to."""
    config = _config()
    if config is None:
        return None
    client = _get_client()
    if client is None:
        return None
    return client, config.bucket


def is_enabled() -> bool:
    """True if an object store is configured."""
    return _config() is not None


def describe() -> dict[str, str] | None:
    """Where the store is: provider, endpoint ("" for AWS S3), region, bucket and
    source. No credentials. None when the store is off."""
    config = _config()
    if config is None:
        return None
    return {
        "provider": config.provider,
        "endpoint": config.endpoint_url or "",
        "region": config.region,
        "bucket": config.bucket,
        "source": config.source,
    }


def upload_bytes(key: str, data: bytes, content_type: str = "application/json") -> bool:
    """Upload raw bytes. Returns True on success."""
    target = _target()
    if target is None:
        return False
    client, bucket = target
    try:
        client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return True
    except Exception:
        logger.exception("Object store upload failed: %s", key)
        return False


def upload_file(key: str, file_path: str, content_type: str = "application/json") -> bool:
    """Upload a local file, in parts when it is large. Returns True on success."""
    target = _target()
    if target is None:
        return False
    client, bucket = target
    try:
        client.upload_file(file_path, bucket, key, ExtraArgs={"ContentType": content_type})
        return True
    except Exception:
        logger.exception("Object store upload_file failed: %s → %s", file_path, key)
        return False


def download_bytes(key: str) -> bytes | None:
    """Download an object. Returns its bytes, or None if it is missing or the call fails."""
    target = _target()
    if target is None:
        return None
    client, bucket = target
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except client.exceptions.NoSuchKey:
        return None
    except Exception:
        logger.exception("Object store download failed: %s", key)
        return None


def list_keys(prefix: str = "") -> list[str]:
    """List all object keys under a prefix. Paginates automatically."""
    target = _target()
    if target is None:
        return []
    client, bucket = target
    try:
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys
    except Exception:
        logger.exception("Object store list failed: prefix=%s", prefix)
        return []


def delete_key(key: str) -> bool:
    """Delete an object. Returns True on success."""
    target = _target()
    if target is None:
        return False
    client, bucket = target
    try:
        client.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        logger.exception("Object store delete failed: %s", key)
        return False


def delete_keys(keys: list[str]) -> int:
    """Bulk-delete up to 1000 keys. Returns count of successfully deleted."""
    target = _target()
    if target is None:
        return 0
    if not keys:
        return 0
    client, bucket = target
    try:
        # S3 DeleteObjects accepts max 1000 per call
        batch = [{"Key": k} for k in keys[:1000]]
        resp = client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        errors = resp.get("Errors", [])
        return len(batch) - len(errors)
    except Exception:
        logger.exception("Object store bulk delete failed (%d keys)", len(keys))
        return 0


def _clear_cache() -> None:
    """Forget the resolved config and the client, so the next call reads os.environ again. For tests."""
    global _config_resolved, _config_cache, _client_built, _client_cache
    with _lock:
        _config_resolved = False
        _config_cache = None
        _client_built = False
        _client_cache = None
