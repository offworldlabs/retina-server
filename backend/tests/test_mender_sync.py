"""POST /api/internal/mender/snapshot (routes/mender_sync.py), through the whole app.

Requests go to main.app over httpx's ASGI transport, so each one crosses the real
middleware stack, and httpx hands the body over as the app reads it, which is
what lets the tests below see how much of it was read. Every request gets its
own session on the per-test database node_session migrated, so two pushes in
flight hold two connections, as on a server. The server clock is frozen at NOW.

The contract these hold the route to is docs/mender-snapshot.md.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import main
from core.mender_mirror import SYNC_STATE_ID, MenderDevice, MenderSyncState
from core.users import get_async_session
from routes import mender_sync

PATH = "/api/internal/mender/snapshot"
TOKEN = "fake-sync-token-for-the-test-suite-0123456789"
SOURCE = "mender-auto-accept@central"
OTHER_SOURCE = "mender-auto-accept@elsewhere"
CLIENT_ADDRESS = "192.0.2.10"
OTHER_ADDRESS = "198.51.100.7"
CAP = 2 * 1024 * 1024

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
T1 = NOW - timedelta(hours=1)


def _t(n: int) -> datetime:
    """The n-th sweep after T1, 30 s apart."""
    return T1 + timedelta(seconds=30 * n)


# Enough of a PEM for the fingerprint: its body decodes to three zero bytes.
PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
PUBKEY_FINGERPRINT = hashlib.sha256(b"\x00\x00\x00").hexdigest()
OTHER_PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAB\n-----END PUBLIC KEY-----\n"
UNDECODABLE_PUBKEY = "-----BEGIN PUBLIC KEY-----\nnot base64!\n-----END PUBLIC KEY-----\n"

NODE_X = "ret1a2b3c4d"
NODE_Y = "ret9f8e7d6c"
NODE_Z = "ret2b3c4d5e"


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def db_path(node_session) -> str:
    return node_session.bind.url.database


@pytest.fixture
def maker(node_session):
    return async_sessionmaker(node_session.bind, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _fresh_throttles():
    """The refusal and short-token log throttles start each test as at boot."""
    mender_sync._reset_for_tests()


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    """The throttles' monotonic clock, frozen at 0 as on a host just booted. Set clock[0] to move it."""
    now = [0.0]
    monkeypatch.setattr(mender_sync, "_monotonic", lambda: now[0])
    return now


def _client(address: str) -> httpx.AsyncClient:
    """A client whose requests come from `address`; `api` sets up the app for it."""
    transport = httpx.ASGITransport(app=main.app, client=(address, 50000))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
async def api(maker, monkeypatch):
    async def _session():
        async with maker() as session:
            yield session

    monkeypatch.setitem(main.app.dependency_overrides, get_async_session, _session)
    monkeypatch.setenv("MENDER_SYNC_TOKEN", TOKEN)
    monkeypatch.setattr(mender_sync, "_now", lambda: NOW)
    async with _client(CLIENT_ADDRESS) as client:
        yield client


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _device(
    mender_device_id: str = "dev-a",
    node_id: str | None = NODE_X,
    auth_status: str = "accepted",
    auth_set_id: str | None = "as-1",
    auth_set_pubkey: str | None = PUBKEY,
    **fields,
) -> dict:
    device = {
        "mender_device_id": mender_device_id,
        "node_id": node_id,
        "auth_status": auth_status,
        "auth_set_id": auth_set_id,
        "auth_set_pubkey": auth_set_pubkey,
        "device_type": "pi5-v3-arm64",
        "artifact_name": "owl-os-1.4.2",
        "inventory_updated_at": "2026-10-16T10:30:00Z",
    }
    device.update(fields)
    return device


def _pending(mender_device_id: str, node_id: str | None = None, **fields) -> dict:
    fields = {"auth_status": "pending", "auth_set_id": None, "auth_set_pubkey": None, **fields}
    return _device(mender_device_id, node_id, **fields)


def _fleet(count: int) -> list[dict]:
    return [_pending(f"dev-{index:03d}") for index in range(count)]


def _snapshot(generated_at: datetime, devices: list[dict], source: str = SOURCE) -> dict:
    return {"generated_at": _iso(generated_at), "source": source, "devices": devices}


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _push(api, body: dict, token: str = TOKEN) -> httpx.Response:
    return await api.post(PATH, json=body, headers=_auth(token))


async def _push_raw(api, content, headers: dict | None = None) -> httpx.Response:
    return await api.post(
        PATH, content=content, headers={**_auth(), "Content-Type": "application/json", **(headers or {})}
    )


async def _rows(maker) -> dict[str, MenderDevice]:
    async with maker() as session:
        devices = (await session.execute(select(MenderDevice))).scalars().all()
    return {device.mender_device_id: device for device in devices}


async def _state(maker) -> MenderSyncState | None:
    async with maker() as session:
        return await session.get(MenderSyncState, SYNC_STATE_ID)


async def _mirror(maker) -> tuple:
    """Everything the two tables hold, for comparing before and after."""
    columns = [column.key for column in MenderDevice.__table__.columns]
    rows = await _rows(maker)
    state = await _state(maker)
    state_values = None
    if state is not None:
        state_values = (state.last_snapshot_at, state.generated_at, state.device_count, state.source)
    return state_values, sorted(tuple(getattr(row, key) for key in columns) for row in rows.values())


def _streamed(chunks: list[bytes], events: list[str] | None = None):
    """An async body that notes each chunk the app takes from it in `events`."""
    events = [] if events is None else events

    async def _body():
        for chunk in chunks:
            events.append("chunk")
            yield chunk

    return _body()


@pytest.fixture
def trace(monkeypatch) -> list[str]:
    """The order the route reaches its decisions in, beside the body chunks taken.

    Starlette's BaseHTTPMiddleware (main.py's LimitUploadSize) pulls a chunk of
    the request while it sends any response, listening for a disconnect, so a
    chunk can be taken after the route has answered. What these tests hold is
    that none is taken before the decision.
    """
    events: list[str] = []
    for name in ("_configured_token", "_presented_token", "_read_capped"):
        real = getattr(mender_sync, name)
        if name == "_read_capped":

            async def _traced(*args, _real=real, _name=name):
                result = await _real(*args)
                events.append(_name)
                return result

        else:

            def _traced(*args, _real=real, _name=name):
                result = _real(*args)
                events.append(_name)
                return result

        monkeypatch.setattr(mender_sync, name, _traced)
    return events


def _before(events: list[str], marker: str) -> list[str]:
    return events[: events.index(marker)]


def _write_lock_free(db_path: str) -> bool:
    """Whether another connection could take SQLite's write lock right now."""
    with contextlib.closing(sqlite3.connect(db_path, timeout=0, isolation_level=None)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            return False
        connection.execute("ROLLBACK")
        return True


def _records(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == mender_sync.logger.name and r.levelno == level]


# ── Routing and auth ─────────────────────────────────────────────────────────


def test_the_route_is_mounted_outside_the_node_api():
    routes = [route for route in main.app.routes if getattr(route, "path", None) == PATH]
    assert len(routes) == 1
    assert routes[0].methods == {"POST"}
    assert routes[0].endpoint.__module__ == "routes.mender_sync"
    assert not PATH.startswith("/v1/nodes")


@pytest.mark.parametrize("value", [None, "", "   \t"], ids=["unset", "empty", "blank"])
async def test_without_a_token_the_route_answers_like_an_unknown_one(api, monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("MENDER_SYNC_TOKEN")
    else:
        monkeypatch.setenv("MENDER_SYNC_TOKEN", value)
    unknown = await api.post("/api/internal/mender/no-such-route", json=_snapshot(_t(0), []), headers=_auth())
    assert unknown.status_code == 404
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(0), []))
    assert response.status_code == 404
    assert response.content == unknown.content == b'{"detail":"Not Found"}'
    assert response.headers["content-type"] == unknown.headers["content-type"]
    assert response.headers["content-length"] == unknown.headers["content-length"]
    assert _records(caplog, logging.ERROR) == []


SHORT_TOKEN_LINE = f"MENDER_SYNC_TOKEN is shorter than 32 characters, so {PATH} answers 404; set a longer one"


async def test_a_short_token_is_unset_and_logs_an_error_naming_the_variable(api, monkeypatch, caplog):
    short = "fake-short-token-0123456789abcd"  # 31 characters
    monkeypatch.setenv("MENDER_SYNC_TOKEN", f"  {short}  ")
    with caplog.at_level(logging.DEBUG):
        response = await _push(api, _snapshot(_t(0), []), token=short)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert _records(caplog, logging.ERROR) == [SHORT_TOKEN_LINE]
    assert all(short not in record.getMessage() for record in caplog.records)


async def test_a_short_token_logs_one_error_at_once_then_one_a_minute_with_the_count(api, monkeypatch, caplog, clock):
    short = "fake-short-token-0123456789abcd"
    monkeypatch.setenv("MENDER_SYNC_TOKEN", short)
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        for second in (0, 0, 1, 30, 59.9, 60, 60.5, 119.9, 120, 500):
            clock[0] = second
            assert (await _push(api, _snapshot(_t(0), []), token=short)).status_code == 404
    assert _records(caplog, logging.ERROR) == [
        SHORT_TOKEN_LINE,
        f"{SHORT_TOKEN_LINE} (4 more requests since the last such line)",
        f"{SHORT_TOKEN_LINE} (2 more requests since the last such line)",
        # Nothing held back since the line at 120 s.
        SHORT_TOKEN_LINE,
    ]
    assert all(short not in record.getMessage() for record in caplog.records)


async def test_a_token_of_exactly_32_characters_is_enough(api, maker, monkeypatch):
    token = "fake-sync-token-0123456789abcdef"
    assert len(token) == 32
    monkeypatch.setenv("MENDER_SYNC_TOKEN", token)
    assert (await _push(api, _snapshot(_t(0), []), token="wrong")).status_code == 401
    assert (await _push(api, _snapshot(_t(0), []), token=token)).status_code == 200


async def test_the_token_is_stripped_of_surrounding_whitespace(api, monkeypatch):
    monkeypatch.setenv("MENDER_SYNC_TOKEN", f"\n {TOKEN} \n")
    assert (await _push(api, _snapshot(_t(0), []))).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "")],
        [("Authorization", "Bearer")],
        [("Authorization", "Bearer ")],
        [("Authorization", "Bearer    ")],
        [("Authorization", TOKEN)],
        [("Authorization", f"Basic {TOKEN}")],
        [("Authorization", f"Token {TOKEN}")],
        [("Authorization", f"Bearer\t{TOKEN}")],
        [("Authorization", f"Bearer {TOKEN}x")],
        [("Authorization", f"Bearer {TOKEN[:-1]}")],
        [("Authorization", f"Bearer {TOKEN} {TOKEN}")],
        [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
        [("X-API-Key", TOKEN)],
    ],
    ids=[
        "missing",
        "empty",
        "scheme-only",
        "scheme-and-space",
        "scheme-and-spaces",
        "no-scheme",
        "basic",
        "other-scheme",
        "tab-separator",
        "longer",
        "prefix",
        "two-tokens",
        "two-headers",
        "other-header",
    ],
)
async def test_a_missing_malformed_or_wrong_authorization_is_401(api, maker, headers):
    response = await api.post(PATH, json=_snapshot(_t(0), [_device()]), headers=headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert await _mirror(maker) == (None, [])


@pytest.mark.parametrize(
    "header",
    [
        b"Bearer " + "café".encode() + TOKEN.encode()[4:],
        b"Bearer " + TOKEN.encode()[:-1] + b"\xff",
        b"Bearer \xe2\x82\xac",
        "Bearer éé".encode("latin-1"),
    ],
    ids=["utf8", "byte-ff", "euro", "latin1"],
)
async def test_a_non_ascii_authorization_is_401_rather_than_a_crash(api, header):
    response = await api.post(PATH, json=_snapshot(_t(0), []), headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


async def test_the_comparison_is_on_bytes_so_a_non_ascii_token_can_match(api, monkeypatch):
    token = "fake-sync-token-éé-0123456789abcdef"
    monkeypatch.setenv("MENDER_SYNC_TOKEN", token)
    header = b"Bearer " + token.encode()
    response = await api.post(PATH, json=_snapshot(_t(0), []), headers={"Authorization": header})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    [f"bEARER {TOKEN}", f"Bearer   {TOKEN}", f"Bearer {TOKEN}  "],
    ids=["scheme-case", "spaces-before", "spaces-after"],
)
async def test_the_scheme_is_case_insensitive_and_spaces_around_the_token_are_ignored(api, header):
    response = await api.post(PATH, json=_snapshot(_t(0), []), headers={"Authorization": header})
    assert response.status_code == 200


async def test_a_nodes_own_bearer_token_is_refused(api, registered_node):
    node_token, _node_id = registered_node
    assert (await _push(api, _snapshot(_t(0), []), token=node_token)).status_code == 401


@pytest.mark.parametrize("configured", [True, False], ids=["wrong-token", "unconfigured"])
async def test_the_body_is_not_read_before_the_token_is_checked(api, monkeypatch, trace, configured):
    if not configured:
        monkeypatch.delenv("MENDER_SYNC_TOKEN")
    body = _streamed([json.dumps(_snapshot(_t(0), [])).encode()], trace)
    response = await api.post(PATH, content=body, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == (401 if configured else 404)
    decided = "_presented_token" if configured else "_configured_token"
    assert "chunk" not in _before(trace, decided)
    assert "_read_capped" not in trace


async def test_a_plain_script_request_is_enough(api, maker):
    """No cookie, no Origin or Referer, no CSRF token: just the bearer token."""
    request = api.build_request(
        "POST",
        PATH,
        content=json.dumps(_snapshot(_t(0), [_device()])).encode(),
        headers={**_auth(), "Content-Type": "application/json"},
    )
    for name in ("accept", "accept-encoding", "connection", "user-agent"):
        del request.headers[name]
    assert {key.lower() for key in request.headers} == {
        "host",
        "content-length",
        "authorization",
        "content-type",
    }
    response = await api.send(request)
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert set(await _rows(maker)) == {"dev-a"}


REFUSED = "mender snapshot refused: missing or wrong bearer token from {}"
WRONG_TOKEN = "fake-wrong-token-0123456789abcdef-0123"


def _no_token_logged(caplog) -> None:
    for record in caplog.records:
        message = record.getMessage()
        assert WRONG_TOKEN not in message
        assert TOKEN not in message
        assert "Bearer" not in message


async def test_a_refused_token_logs_a_warning_with_the_client_address_only(api, caplog):
    with caplog.at_level(logging.DEBUG):
        assert (await _push(api, _snapshot(_t(0), []), token=WRONG_TOKEN)).status_code == 401
    assert _records(caplog, logging.WARNING) == [REFUSED.format(CLIENT_ADDRESS)]
    _no_token_logged(caplog)


async def test_a_burst_of_refusals_logs_one_line_at_once_then_one_a_minute_with_the_count(api, caplog, clock):
    async def _refused(client) -> None:
        assert (await _push(client, _snapshot(_t(0), []), token=WRONG_TOKEN)).status_code == 401

    async with _client(OTHER_ADDRESS) as other:
        with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
            # At 0 s, as on a host just booted: logged at once.
            await _refused(api)
            assert _records(caplog, logging.WARNING) == [REFUSED.format(CLIENT_ADDRESS)]
            # 300 more inside the window, no header at all among them; the last from elsewhere.
            for n in range(299):
                clock[0] = n / 5
                if n % 2:
                    assert (await api.post(PATH, json=_snapshot(_t(0), []))).status_code == 401
                else:
                    await _refused(api)
            clock[0] = 59.999
            await _refused(other)
            assert len(_records(caplog, logging.WARNING)) == 1
            clock[0] = 60
            await _refused(api)
            # The window restarts at the line just logged.
            clock[0] = 119.9
            await _refused(api)
            clock[0] = 120
            await _refused(other)
            # Quiet since the line at 120 s, so nothing to carry.
            clock[0] = 1000
            await _refused(api)
    assert _records(caplog, logging.WARNING) == [
        REFUSED.format(CLIENT_ADDRESS),
        f"{REFUSED.format(CLIENT_ADDRESS)} (300 more refused since the last such line, the last from {OTHER_ADDRESS})",
        f"{REFUSED.format(OTHER_ADDRESS)} (1 more refused since the last such line, the last from {CLIENT_ADDRESS})",
        REFUSED.format(CLIENT_ADDRESS),
    ]
    _no_token_logged(caplog)


async def test_refusals_and_the_short_token_error_are_throttled_apart(api, monkeypatch, caplog, clock):
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        assert (await _push(api, _snapshot(_t(0), []), token=WRONG_TOKEN)).status_code == 401
        clock[0] = 10
        monkeypatch.setenv("MENDER_SYNC_TOKEN", "fake-short-token-0123456789abcd")
        assert (await _push(api, _snapshot(_t(0), []))).status_code == 404
        clock[0] = 20
        monkeypatch.setenv("MENDER_SYNC_TOKEN", TOKEN)
        assert (await _push(api, _snapshot(_t(0), []), token=WRONG_TOKEN)).status_code == 401
        # A push with the right token logs neither line.
        assert (await _push(api, _snapshot(_t(0), []))).status_code == 200
    assert _records(caplog, logging.WARNING) == [REFUSED.format(CLIENT_ADDRESS)]
    assert _records(caplog, logging.ERROR) == [SHORT_TOKEN_LINE]


# ── Body cap ─────────────────────────────────────────────────────────────────


async def test_a_declared_length_over_the_cap_is_413_without_reading_the_body(api, maker, trace):
    body = _streamed([b"{" * 1024], trace)
    response = await _push_raw(api, body, headers={"Content-Length": str(CAP + 1)})
    assert response.status_code == 413
    assert response.json() == {"detail": f"Request body too large (max {CAP} bytes)"}
    assert _before(trace, "_read_capped") == ["_configured_token", "_presented_token"]
    assert await _mirror(maker) == (None, [])


async def test_a_chunked_body_is_counted_and_refused_once_past_the_cap(api, maker, trace):
    chunk = 256 * 1024
    body = _streamed([b" " * chunk] * 16, trace)
    response = await _push_raw(api, body)
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    # Eight chunks make exactly the cap; the ninth passes it, and the route reads no further.
    assert _before(trace, "_read_capped") == ["_configured_token", "_presented_token"] + ["chunk"] * 9
    assert await _mirror(maker) == (None, [])


def _padded(body: dict, size: int) -> bytes:
    raw = json.dumps(body).encode()
    return raw + b" " * (size - len(raw))


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
async def test_a_body_of_exactly_the_cap_is_taken_and_one_byte_more_is_not(api, declared):
    for size, status in ((CAP, 200), (CAP + 1, 413)):
        raw = _padded(_snapshot(_t(size - CAP), [_device()]), size)
        if declared:
            response = await _push_raw(api, raw)
        else:
            body = _streamed([raw[i : i + 65536] for i in range(0, len(raw), 65536)])
            response = await _push_raw(api, body)
        assert response.status_code == status, size


# ── Shape ────────────────────────────────────────────────────────────────────


def _valid() -> dict:
    return _snapshot(_t(1), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y)])


def _set(*path_and_value):
    *path, key, value = path_and_value

    def _mutate(body: dict) -> None:
        target = body
        for step in path:
            target = target[step]
        target[key] = value

    return _mutate


def _drop(*path):
    *path, key = path

    def _mutate(body: dict) -> None:
        target = body
        for step in path:
            target = target[step]
        del target[key]

    return _mutate


# Faults in the envelope only. A value a device holds is judged per device and
# never refuses the push: see "Per device" below.
INVALID = {
    "extra-field": _set("extra", 1),
    "extra-device-field": _set("devices", 0, "auth_set_fingerprint", "ab"),
    "missing-generated-at": _drop("generated_at"),
    "missing-source": _drop("source"),
    "missing-devices": _drop("devices"),
    "missing-mender-device-id": _drop("devices", 1, "mender_device_id"),
    "missing-device-field": _drop("devices", 0, "node_id"),
    "missing-inventory-field": _drop("devices", 0, "inventory_updated_at"),
    "naive-generated-at": _set("generated_at", "2026-10-16T11:30:00"),
    "numeric-generated-at": _set("generated_at", 1_790_000_000),
    # The parser takes a number of seconds in a string; ISO 8601 does not.
    "seconds-string-generated-at": _set("generated_at", "1790000000"),
    "space-separated-generated-at": _set("generated_at", "2026-10-16 11:30:00Z"),
    "date-only-generated-at": _set("generated_at", "2026-10-16"),
    "empty-generated-at": _set("generated_at", ""),
    "null-generated-at": _set("generated_at", None),
    # Year 1 at +05:00 is before the first instant a UTC timestamp can hold.
    "generated-at-before-utc-year-1": _set("generated_at", "0001-01-01T00:00:00+05:00"),
    "generated-at-past-the-clock-allowance": _set("generated_at", _iso(NOW + timedelta(seconds=300, microseconds=1))),
    "numeric-inventory-updated-at": _set("devices", 0, "inventory_updated_at", 1_790_000_000),
    "bool-inventory-updated-at": _set("devices", 0, "inventory_updated_at", True),
    "status-null": _set("devices", 1, "auth_status", None),
    "status-number": _set("devices", 1, "auth_status", 1),
    "null-mender-device-id": _set("devices", 0, "mender_device_id", None),
    "numeric-mender-device-id": _set("devices", 0, "mender_device_id", 6703),
    "node-id-number": _set("devices", 0, "node_id", 12),
    "auth-set-id-number": _set("devices", 0, "auth_set_id", 1),
    "pubkey-list": _set("devices", 0, "auth_set_pubkey", ["k"]),
    "device-type-bool": _set("devices", 0, "device_type", True),
    "artifact-name-object": _set("devices", 0, "artifact_name", {"name": "owl-os"}),
    "empty-source": _set("source", ""),
    "null-source": _set("source", None),
    "source-129": _set("source", "s" * 129),
    "source-newline": _set("source", "central\nforged log line"),
    "source-tab": _set("source", "central\t1"),
    "source-del": _set("source", "central\x7f"),
    "source-non-ascii": _set("source", "central-é"),
    "devices-null": _set("devices", None),
    "devices-object": _set("devices", {"dev-a": {}}),
    "devices-string": _set("devices", "dev-a"),
    "device-not-object": _set("devices", 0, "dev-a"),
    "device-null": _set("devices", 1, None),
    "device-list": _set("devices", 1, []),
}


@pytest.fixture
async def seeded(api, maker):
    """A mirror holding one snapshot, so a refused push has something to leave alone."""
    response = await _push(api, _snapshot(_t(0), [_device("dev-a", NODE_X), _pending("dev-c", NODE_Z)]))
    assert response.status_code == 200
    return await _mirror(maker)


@pytest.mark.parametrize("mutate", INVALID.values(), ids=INVALID.keys())
async def test_a_bad_shape_is_422_and_leaves_the_mirror_alone(api, maker, seeded, alerts, mutate):
    body = _valid()
    mutate(body)
    response = await _push(api, body)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    for error in detail:
        assert {"type", "loc", "msg"} <= set(error) <= {"type", "loc", "msg", "input"}
        assert error["loc"][0] == "body"
    assert await _mirror(maker) == seeded
    assert alerts == []


RAW_INVALID = {
    "empty": b"",
    "not-json": b"generated_at=now",
    "truncated": json.dumps(_snapshot(_t(1), [])).encode()[:-1],
    "a-list": b"[]",
    "a-string": b'"snapshot"',
    "bad-utf8": b'{"generated_at": "\xff", "source": "x", "devices": []}',
    "nan": b'{"generated_at": NaN, "source": "x", "devices": []}',
    "infinity-node-id": json.dumps(_snapshot(_t(1), [_device()])).replace(f'"{NODE_X}"', "Infinity").encode(),
}


@pytest.mark.parametrize("raw", RAW_INVALID.values(), ids=RAW_INVALID.keys())
async def test_a_body_that_is_not_a_snapshot_is_422(api, maker, seeded, raw):
    response = await _push_raw(api, raw)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert await _mirror(maker) == seeded


async def test_422_echoes_the_offending_values_and_nothing_more(api):
    secret_looking_key = "-----BEGIN PUBLIC KEY-----\n" + "Q" * 600 + "\n-----END PUBLIC KEY-----\n"
    without_node_id = _device("dev-b", NODE_Y, auth_set_pubkey=secret_looking_key)
    del without_node_id["node_id"]
    body = _snapshot(
        _t(1),
        [
            _device("dev-a", NODE_X, auth_set_pubkey=secret_looking_key, device_type=129),
            without_node_id,
            _pending("dev-c", extra={"nested": "x" * 50}),
        ],
    )
    body["generated_at"] = "1790000000"
    response = await _push(api, body)
    assert response.status_code == 422
    detail = {tuple(error["loc"]): error for error in response.json()["detail"]}
    assert detail[("body", "generated_at")]["input"] == "1790000000"
    assert detail[("body", "devices", 0, "device_type")]["input"] == 129
    # A missing field's input is the whole device, and an extra field's here is an object.
    assert "input" not in detail[("body", "devices", 1, "node_id")]
    assert "input" not in detail[("body", "devices", 2, "extra")]
    assert "Q" * 600 not in response.text
    assert "x" * 50 not in response.text

    invalid_json = b'{"source": "' + b"S" * 500 + b'", oops}'
    response = await _push_raw(api, invalid_json)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
    assert "input" not in response.json()["detail"][0]
    assert "S" * 500 not in response.text


async def test_the_boundaries_are_accepted(api, maker):
    devices = [
        _device(
            "d" * 64,
            "n" * 64,
            auth_set_id="a" * 64,
            auth_set_pubkey="A" * 4096,
            device_type="t" * 128,
            artifact_name="a" * 256,
            inventory_updated_at="2026-10-16T13:30:00+01:30",
        ),
        _pending("dev-1", None, auth_status="a" * 32, device_type=None, artifact_name=None, inventory_updated_at=None),
        _pending("dev-2", None, auth_status="preauthorized"),
        _pending("dev-3", None, auth_status="ambiguous"),
        _pending("dev-4", "", auth_status="z"),
        _pending("dev-5", NODE_Y, auth_status="decommissioned_by_hand"),
    ]
    assert len(devices[0]["auth_set_pubkey"]) == 4096
    for n, source in enumerate(("s" * 128, "~", " ")):
        response = await _push(api, _snapshot(_t(n), devices, source=source))
        assert response.status_code == 200, response.text
        assert (response.json()["accepted"], response.json()["rejected"]) == (6, 0)
    # At the clock allowance exactly.
    assert (await _push(api, _snapshot(NOW + timedelta(seconds=300), devices))).status_code == 200
    rows = await _rows(maker)
    assert rows["dev-1"].auth_status == "a" * 32
    assert rows["dev-5"].auth_status == "decommissioned_by_hand"
    edge = rows["d" * 64]
    assert (edge.node_id, edge.auth_set_id, edge.device_type, edge.artifact_name) == (
        "n" * 64,
        "a" * 64,
        "t" * 128,
        "a" * 256,
    )
    assert edge.auth_set_pubkey == "A" * 4096
    assert edge.inventory_updated_at == datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert {key: row.snapshot_issue for key, row in rows.items()} == dict.fromkeys(rows)


# ── 409 ──────────────────────────────────────────────────────────────────────


async def test_an_older_snapshot_is_409_and_changes_nothing(api, maker, caplog):
    assert (await _push(api, _snapshot(_t(2), [_device("dev-a", NODE_X)]))).status_code == 200
    before = await _mirror(maker)
    # Bad devices in a refused push are not logged either: nothing of it is applied.
    devices = [_pending("dev-b", NODE_Y), _pending(""), _pending("dev-c", auth_status="Pending")]
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), devices, source=OTHER_SOURCE))
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)
    assert await _mirror(maker) == before
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert _iso(_t(1)) in infos[0] and _iso(_t(2)) in infos[0]
    assert _records(caplog, logging.WARNING) == []


async def test_an_equal_generated_at_is_409_whatever_its_offset(api, maker):
    assert (await _push(api, _snapshot(_t(1), [_device()]))).status_code == 200
    before = await _mirror(maker)
    assert (await _push(api, _snapshot(_t(1), [_device()]))).status_code == 409
    same_instant = _t(1).astimezone(timezone(timedelta(hours=2))).isoformat()
    assert not same_instant.endswith("Z")
    body = _snapshot(_t(1), [])
    body["generated_at"] = same_instant
    assert (await _push(api, body)).status_code == 409
    assert await _mirror(maker) == before
    assert (await _push(api, _snapshot(_t(1) + timedelta(microseconds=1), [_device()]))).status_code == 200


async def test_the_write_lock_is_held_when_the_stored_snapshot_is_read(api, db_path, monkeypatch):
    held = []
    read = mender_sync.load_sync_state

    async def _probe(session):
        held.append(not _write_lock_free(db_path))
        return await read(session)

    monkeypatch.setattr(mender_sync, "load_sync_state", _probe)
    assert (await _push(api, _snapshot(_t(1), [_device()]))).status_code == 200
    assert (await _push(api, _snapshot(_t(1), [_device()]))).status_code == 409
    assert held == [True, True]
    # Released by the commit and by the rollback alike.
    assert _write_lock_free(db_path)


@pytest.mark.parametrize("first", ["older", "newer"])
async def test_two_pushes_in_flight_together_end_with_the_newer_stored(api, maker, monkeypatch, first):
    """The first push to read the stored snapshot is held between that read and
    its write for up to a second, and the second push is sent once it is there.

    Without the write lock the second would read the same stored snapshot,
    pass the same check and land inside that second, and the first would then
    write over it. With it, the second waits in SQLite until the first commits.
    """
    generated = {"older": _t(1), "newer": _t(2)}
    second = "newer" if first == "older" else "older"
    first_has_read = asyncio.Event()
    second_done = asyncio.Event()
    read = mender_sync.load_sync_state
    reads = 0

    async def _held_read(session):
        nonlocal reads
        reads += 1
        is_first = reads == 1
        state = await read(session)
        if is_first:
            first_has_read.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(second_done.wait(), 1.0)
        return state

    monkeypatch.setattr(mender_sync, "load_sync_state", _held_read)

    async def _send(which: str) -> httpx.Response:
        device = _device(artifact_name=f"owl-os-{which}")
        return await _push(api, _snapshot(generated[which], [device], source=f"sender-{which}"))

    async def _send_first() -> httpx.Response:
        try:
            return await _send(first)
        finally:
            # A first push answered before it read anything must not leave the second waiting.
            first_has_read.set()

    async def _send_second() -> httpx.Response:
        await first_has_read.wait()
        try:
            return await _send(second)
        finally:
            second_done.set()

    first_response, second_response = await asyncio.gather(_send_first(), _send_second())
    responses = {first: first_response, second: second_response}
    assert responses["newer"].status_code == 200
    assert responses["older"].status_code == (200 if first == "older" else 409)
    state = await _state(maker)
    assert state is not None
    assert (state.generated_at, state.source) == (_t(2), "sender-newer")
    assert (await _rows(maker))["dev-a"].artifact_name == "owl-os-newer"


# ── Apply ────────────────────────────────────────────────────────────────────


async def test_the_first_push_inserts_every_device(api, maker):
    response = await _push(
        api,
        _snapshot(_t(1), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y), _pending("dev-c", None)]),
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 3, "rejected": 0, "absent": 0, "generated_at": _iso(_t(1))}
    rows = await _rows(maker)
    assert set(rows) == {"dev-a", "dev-b", "dev-c"}
    a = rows["dev-a"]
    assert (a.node_id, a.auth_status, a.auth_set_id, a.auth_set_pubkey) == (NODE_X, "accepted", "as-1", PUBKEY)
    assert a.auth_set_fingerprint == PUBKEY_FINGERPRINT
    assert (a.accepted_at, a.auth_set_changed_at, a.last_confirmed_at) == (_t(1), _t(1), _t(1))
    assert (a.device_type, a.artifact_name) == ("pi5-v3-arm64", "owl-os-1.4.2")
    assert a.inventory_updated_at == datetime(2026, 10, 16, 10, 30, tzinfo=UTC)
    assert (a.present, a.snapshot_issue) == (True, None)
    b = rows["dev-b"]
    assert (b.node_id, b.auth_status, b.auth_set_id, b.auth_set_pubkey, b.auth_set_fingerprint) == (
        NODE_Y,
        "pending",
        None,
        None,
        None,
    )
    assert (b.accepted_at, b.auth_set_changed_at, b.last_confirmed_at, b.present) == (None, None, _t(1), True)
    assert rows["dev-c"].node_id is None
    state = await _state(maker)
    assert state is not None
    assert (state.id, state.last_snapshot_at, state.generated_at, state.device_count, state.source) == (
        SYNC_STATE_ID,
        NOW,
        _t(1),
        3,
        SOURCE,
    )


async def test_a_second_push_updates_and_marks_the_missing_absent(api, maker):
    first = [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y), _pending("dev-c", NODE_Z)]
    assert (await _push(api, _snapshot(_t(1), first))).status_code == 200
    before = await _rows(maker)

    second = [_device("dev-a", NODE_X, artifact_name="owl-os-1.5.0", inventory_updated_at=None)]
    response = await _push(api, _snapshot(_t(2), second, source=SOURCE))
    assert response.json() == {"accepted": 1, "rejected": 0, "absent": 2, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert set(rows) == {"dev-a", "dev-b", "dev-c"}, "nothing is deleted"
    assert rows["dev-a"].artifact_name == "owl-os-1.5.0"
    assert rows["dev-a"].inventory_updated_at is None
    assert (rows["dev-a"].present, rows["dev-a"].last_confirmed_at) == (True, _t(2))
    for missing in ("dev-b", "dev-c"):
        assert rows[missing].present is False
        # Only presence moves: the row is otherwise as the last snapshot that listed it.
        for column in MenderDevice.__table__.columns:
            if column.key != "present":
                assert getattr(rows[missing], column.key) == getattr(before[missing], column.key), column.key
    state = await _state(maker)
    assert state is not None and (state.generated_at, state.device_count) == (_t(2), 1)

    # Already absent rows still count as absent.
    response = await _push(api, _snapshot(_t(3), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y)]))
    assert response.json() == {"accepted": 2, "rejected": 0, "absent": 1, "generated_at": _iso(_t(3))}


async def test_a_device_that_comes_back_is_present_again(api, maker):
    assert (await _push(api, _snapshot(_t(1), [_device("dev-a", NODE_X), _pending("dev-b")]))).status_code == 200
    assert (await _push(api, _snapshot(_t(2), [_pending("dev-b")]))).status_code == 200
    assert (await _rows(maker))["dev-a"].present is False
    response = await _push(api, _snapshot(_t(3), [_device("dev-a", NODE_X), _pending("dev-b")]))
    assert response.json()["absent"] == 0
    a = (await _rows(maker))["dev-a"]
    assert (a.present, a.last_confirmed_at) == (True, _t(3))
    # Accepted before it went and accepted on its return: no transition, so no move.
    assert (a.accepted_at, a.auth_set_changed_at) == (_t(1), _t(1))


async def test_accepted_at_and_auth_set_changed_at_move_only_on_a_change(api, maker):
    steps = [
        # (device as pushed, accepted_at, auth_set_changed_at) after the push
        (_device(auth_set_id="as-1"), 0, 0),
        (_device(auth_set_id="as-1", artifact_name="owl-os-1.5.0"), 0, 0),
        (_device(auth_set_id="as-2", auth_set_pubkey=OTHER_PUBKEY), 0, 2),
        (_device(auth_status="pending", auth_set_id=None, auth_set_pubkey=None), None, 3),
        (_device(auth_status="pending", auth_set_id=None, auth_set_pubkey=None), None, 3),
        (_device(auth_set_id="as-2", auth_set_pubkey=OTHER_PUBKEY), 5, 5),
        (_device(auth_status="ambiguous", auth_set_id=None, auth_set_pubkey=None), None, 6),
        (_device(auth_status="rejected", auth_set_id=None, auth_set_pubkey=None), None, 6),
        (_device(auth_set_id="as-3"), 8, 8),
        # A status change that keeps the set id, and back.
        (_device(auth_status="preauthorized", auth_set_id="as-3"), None, 8),
        (_device(auth_set_id="as-3"), 10, 8),
    ]
    for n, (device, accepted_at, changed_at) in enumerate(steps):
        assert (await _push(api, _snapshot(_t(n), [device]))).status_code == 200, n
        row = (await _rows(maker))["dev-a"]
        expected = (None if accepted_at is None else _t(accepted_at), _t(changed_at), _t(n))
        assert (row.accepted_at, row.auth_set_changed_at, row.last_confirmed_at) == expected, n
    assert (await _rows(maker))["dev-a"].auth_set_fingerprint == PUBKEY_FINGERPRINT


async def test_an_empty_or_blank_auth_set_id_is_the_same_set_as_null(api, maker):
    steps = [
        # (auth_set_id as pushed, as stored, auth_set_changed_at) after the push
        ("as-1", "as-1", 0),
        ("", None, 1),
        (None, None, 1),
        (" \t", None, 1),
        ("as-1", "as-1", 4),
    ]
    for n, (sent, stored, changed_at) in enumerate(steps):
        device = _device(auth_status="pending", auth_set_id=sent, auth_set_pubkey=None)
        assert (await _push(api, _snapshot(_t(n), [device]))).status_code == 200, n
        row = (await _rows(maker))["dev-a"]
        assert (row.auth_set_id, row.auth_set_changed_at, row.snapshot_issue) == (stored, _t(changed_at), None), n


@pytest.mark.parametrize(
    ("device", "accepted_at", "changed_at"),
    [
        (_device(), True, True),
        (_pending("dev-a"), False, False),
        (_device(auth_status="ambiguous", auth_set_id=None, auth_set_pubkey=None), False, False),
        (_device(auth_status="pending", auth_set_id="as-9", auth_set_pubkey=None), False, True),
        (_device(auth_status="pending", auth_set_id="", auth_set_pubkey=None), False, False),
    ],
    ids=["accepted", "pending", "ambiguous", "pending-with-an-auth-set", "pending-with-an-empty-auth-set-id"],
)
async def test_a_new_device_starts_its_timestamps_from_what_it_carries(api, maker, device, accepted_at, changed_at):
    assert (await _push(api, _snapshot(_t(1), [device]))).status_code == 200
    row = (await _rows(maker))["dev-a"]
    assert row.accepted_at == (_t(1) if accepted_at else None)
    assert row.auth_set_changed_at == (_t(1) if changed_at else None)


async def test_the_fingerprint_follows_the_pubkey(api, maker):
    other_fingerprint = hashlib.sha256(b"\x00\x00\x01").hexdigest()
    assert (await _push(api, _snapshot(_t(1), [_device()]))).status_code == 200
    assert (await _rows(maker))["dev-a"].auth_set_fingerprint == PUBKEY_FINGERPRINT
    assert (await _push(api, _snapshot(_t(2), [_device(auth_set_pubkey=OTHER_PUBKEY)]))).status_code == 200
    assert (await _rows(maker))["dev-a"].auth_set_fingerprint == other_fingerprint
    assert (await _push(api, _snapshot(_t(3), [_pending("dev-a")]))).status_code == 200
    assert (await _rows(maker))["dev-a"].auth_set_fingerprint is None


@pytest.mark.parametrize(
    "pubkey",
    [UNDECODABLE_PUBKEY, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 fake@test", "-----BEGIN PUBLIC KEY-----\né\n"],
    ids=["bad-base64", "openssh", "non-ascii"],
)
async def test_an_undecodable_pubkey_is_stored_without_a_fingerprint_and_warned_about(api, maker, caplog, pubkey):
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), [_device("dev-a", auth_set_pubkey=pubkey)]))
    assert response.status_code == 200
    row = (await _rows(maker))["dev-a"]
    assert (row.auth_set_pubkey, row.auth_set_fingerprint, row.auth_status) == (pubkey, None, "accepted")
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "'dev-a'" in warnings[0] and "pubkey" in warnings[0]


async def test_node_ids_can_swap_between_devices_in_one_push(api, maker):
    assert (
        await _push(api, _snapshot(_t(1), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y)]))
    ).status_code == 200
    response = await _push(api, _snapshot(_t(2), [_device("dev-a", NODE_Y), _pending("dev-b", NODE_X)]))
    assert response.status_code == 200
    rows = await _rows(maker)
    assert (rows["dev-a"].node_id, rows["dev-b"].node_id) == (NODE_Y, NODE_X)
    # A chain and a release in one push as well.
    response = await _push(
        api,
        _snapshot(_t(3), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Z), _pending("dev-c", NODE_Y)]),
    )
    assert response.status_code == 200
    rows = await _rows(maker)
    assert {key: row.node_id for key, row in rows.items()} == {"dev-a": NODE_X, "dev-b": NODE_Z, "dev-c": NODE_Y}
    response = await _push(api, _snapshot(_t(4), [_device("dev-a", None), _pending("dev-b", NODE_X)]))
    assert response.status_code == 200
    rows = await _rows(maker)
    assert (rows["dev-a"].node_id, rows["dev-b"].node_id, rows["dev-c"].present) == (None, NODE_X, False)


async def test_a_new_device_can_take_the_node_id_of_an_absent_one(api, maker):
    """A reflashed board: Mender gives it a new device id under the old node_id."""
    assert (await _push(api, _snapshot(_t(1), [_device("dev-old", NODE_X)]))).status_code == 200
    response = await _push(api, _snapshot(_t(2), [_device("dev-new", NODE_X, auth_set_id="as-2")]))
    assert response.json() == {"accepted": 1, "rejected": 0, "absent": 1, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert (rows["dev-old"].node_id, rows["dev-old"].present) == (NODE_X, False)
    assert (rows["dev-new"].node_id, rows["dev-new"].present) == (NODE_X, True)
    # And the old one back with the new one gone, in one push.
    response = await _push(api, _snapshot(_t(3), [_device("dev-old", NODE_X)]))
    assert response.status_code == 200
    rows = await _rows(maker)
    assert (rows["dev-old"].present, rows["dev-new"].present) == (True, False)


async def test_an_empty_device_list_is_a_valid_snapshot(api, maker):
    response = await _push(api, _snapshot(_t(1), []))
    assert response.json() == {"accepted": 0, "rejected": 0, "absent": 0, "generated_at": _iso(_t(1))}
    state = await _state(maker)
    assert state is not None and state.device_count == 0
    assert (await _push(api, _snapshot(_t(2), [_device()]))).json()["absent"] == 0
    response = await _push(api, _snapshot(_t(3), []))
    assert response.json() == {"accepted": 0, "rejected": 0, "absent": 1, "generated_at": _iso(_t(3))}
    assert (await _rows(maker))["dev-a"].present is False


async def test_generated_at_is_answered_in_utc(api, maker):
    body = _snapshot(_t(1), [])
    body["generated_at"] = "2026-10-16T13:30:00.25+02:00"
    response = await _push(api, body)
    assert response.json()["generated_at"] == "2026-10-16T11:30:00.250000Z"
    state = await _state(maker)
    assert state is not None
    assert state.generated_at == datetime(2026, 10, 16, 11, 30, 0, 250000, tzinfo=UTC)


async def test_an_applied_snapshot_logs_its_counts_and_source(api, caplog):
    assert (await _push(api, _snapshot(_t(1), [_device("dev-a"), _pending("dev-b")]))).status_code == 200
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        assert (await _push(api, _snapshot(_t(2), [_device("dev-a")]))).status_code == 200
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert "1 devices" in infos[0] and "1 absent" in infos[0] and repr(SOURCE) in infos[0]
    assert "1 accepted" in infos[0] and "0 rejected" in infos[0]
    # Clean devices log no line of their own; the one warning is the count drop.
    assert [w.split(" ")[2] for w in _records(caplog, logging.WARNING)] == ["lists"]


# ── Per device ───────────────────────────────────────────────────────────────

INVENTORY_AT = datetime(2026, 10, 16, 10, 30, tzinfo=UTC)
NODE_W = "ret3a4b5c6d"
NODE_V = "ret0a1b2c3d"

REPEATED = "mender_device_id repeated in the snapshot"
NODE_ID_TOO_LONG = "node_id too long"
NOT_A_STATUS = "auth_status not a status token"
AUTH_SET_ID_TOO_LONG = "auth_set_id too long"
PUBKEY_TOO_LONG = "auth_set_pubkey too long"
NO_AUTH_SET = "accepted without an auth set"


def _shared(count: int) -> str:
    return f"node_id shared by {count} devices"


# Stored and judged as "invalid": not a lowercase token of 1 to 32 characters.
BAD_STATUSES = {
    "upper-case": "Pending",
    "upper-case-accepted": "Accepted",
    "with-digit": "pending2",
    "leading-underscore": "_pending",
    "with-hyphen": "pre-authorized",
    "33-chars": "a" * 33,
    # re.match with "$" would take this one.
    "trailing-newline": "accepted\n",
    "leading-space": " accepted",
    "empty": "",
    "non-ascii": "accepté",
}

# Stored null, logged, no issue: not ISO 8601 with a timezone.
BAD_TIMES = {
    "naive": "2026-10-16T10:30:00",
    "seconds": "1790000000",
    "date-only": "2026-10-16",
    "space-separated": "2026-10-16 10:30:00Z",
    "garbage": "yesterday",
    "empty": "",
    "offset-24h": "2026-10-16T10:30:00+24:00",
    # Valid ISO 8601, but outside what a UTC timestamp can hold.
    "before-utc-year-1": "0001-01-01T00:00:00+05:00",
    "after-utc-year-9999": "9999-12-31T23:59:59-05:00",
}

DROPPED = None

# (dev-b as sent, the columns that differ from what was sent, logged).
# DROPPED: the entry is not stored at all.
PER_DEVICE = {
    "empty-mender-device-id": (_pending("", NODE_Y), DROPPED, True),
    "mender-device-id-65": (_pending("d" * 65, NODE_Y), DROPPED, True),
    "empty-node-id": (_pending("dev-b", ""), {"node_id": None}, False),
    "node-id-65": (_pending("dev-b", "n" * 65), {"node_id": None, "snapshot_issue": NODE_ID_TOO_LONG}, True),
    **{
        f"status-{name}": (
            _device("dev-b", NODE_Y, auth_status=status),
            {"auth_status": "invalid", "snapshot_issue": NOT_A_STATUS},
            True,
        )
        for name, status in BAD_STATUSES.items()
    },
    "accepted-auth-set-id-65": (
        _device("dev-b", NODE_Y, auth_set_id="a" * 65),
        {"auth_set_id": None, "snapshot_issue": f"{AUTH_SET_ID_TOO_LONG}; {NO_AUTH_SET}"},
        True,
    ),
    "accepted-pubkey-4097": (
        _device("dev-b", NODE_Y, auth_set_pubkey="A" * 4097),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None, "snapshot_issue": f"{PUBKEY_TOO_LONG}; {NO_AUTH_SET}"},
        True,
    ),
    "pending-auth-set-id-65": (
        _device("dev-b", NODE_Y, auth_status="pending", auth_set_id="a" * 65),
        {"auth_set_id": None, "snapshot_issue": AUTH_SET_ID_TOO_LONG},
        True,
    ),
    "pending-pubkey-4097": (
        _device("dev-b", NODE_Y, auth_status="pending", auth_set_pubkey="A" * 4097),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None, "snapshot_issue": PUBKEY_TOO_LONG},
        True,
    ),
    "accepted-without-auth-set-id": (
        _device("dev-b", NODE_Y, auth_set_id=None),
        {"snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "accepted-without-pubkey": (
        _device("dev-b", NODE_Y, auth_set_pubkey=None),
        {"snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "accepted-without-either": (
        _device("dev-b", NODE_Y, auth_set_id=None, auth_set_pubkey=None),
        {"snapshot_issue": NO_AUTH_SET},
        True,
    ),
    # Empty or whitespace only is stored null, as if sent null, whatever the status.
    "accepted-empty-auth-set-id": (
        _device("dev-b", NODE_Y, auth_set_id=""),
        {"auth_set_id": None, "snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "accepted-blank-auth-set-id": (
        _device("dev-b", NODE_Y, auth_set_id=" \t"),
        {"auth_set_id": None, "snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "accepted-empty-pubkey": (
        _device("dev-b", NODE_Y, auth_set_pubkey=""),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None, "snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "accepted-blank-pubkey": (
        _device("dev-b", NODE_Y, auth_set_pubkey="  "),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None, "snapshot_issue": NO_AUTH_SET},
        True,
    ),
    # Fingerprinted as zero bytes if kept, like "".
    "accepted-newline-pubkey": (
        _device("dev-b", NODE_Y, auth_set_pubkey="\n"),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None, "snapshot_issue": NO_AUTH_SET},
        True,
    ),
    "pending-empty-pubkey": (
        _device("dev-b", NODE_Y, auth_status="pending", auth_set_pubkey=""),
        {"auth_set_pubkey": None, "auth_set_fingerprint": None},
        False,
    ),
    "pending-blank-auth-set-id": (
        _device("dev-b", NODE_Y, auth_status="pending", auth_set_id="  "),
        {"auth_set_id": None},
        False,
    ),
    "device-type-129": (_pending("dev-b", NODE_Y, device_type="t" * 128 + "u"), {"device_type": "t" * 128}, True),
    "artifact-name-257": (
        _pending("dev-b", NODE_Y, artifact_name="a" * 256 + "b"),
        {"artifact_name": "a" * 256},
        True,
    ),
    **{
        f"inventory-{name}": (
            _pending("dev-b", NODE_Y, inventory_updated_at=value),
            {"inventory_updated_at": None},
            True,
        )
        for name, value in BAD_TIMES.items()
    },
    "undecodable-pubkey": (
        _device("dev-b", NODE_Y, auth_set_pubkey=UNDECODABLE_PUBKEY),
        {"auth_set_fingerprint": None},
        True,
    ),
    # Any status token is stored as sent, and only "accepted" registers.
    "noauth": (_pending("dev-b", NODE_Y, auth_status="noauth"), {}, False),
    "a-token-spelled-invalid": (_pending("dev-b", NODE_Y, auth_status="invalid"), {}, False),
    # The sender passes the set through when the status disagrees with it.
    "pending-with-an-auth-set": (_device("dev-b", NODE_Y, auth_status="pending"), {}, False),
    "rejected-with-an-auth-set": (_device("dev-b", NODE_Y, auth_status="rejected"), {}, False),
    "preauthorized-with-an-auth-set": (_device("dev-b", NODE_Y, auth_status="preauthorized"), {}, False),
    "ambiguous-with-an-auth-set": (_device("dev-b", NODE_Y, auth_status="ambiguous"), {}, False),
}

_STORED = (
    "node_id",
    "auth_status",
    "auth_set_id",
    "auth_set_pubkey",
    "auth_set_fingerprint",
    "device_type",
    "artifact_name",
    "inventory_updated_at",
    "snapshot_issue",
)


def _as_sent(device: dict) -> dict:
    """The columns a device sent cleanly is stored with. Only the values the helpers above send."""
    fingerprints = {PUBKEY: PUBKEY_FINGERPRINT, None: None}
    times = {"2026-10-16T10:30:00Z": INVENTORY_AT, None: None}
    return {
        **{key: device[key] for key in _STORED[:4]},
        "auth_set_fingerprint": fingerprints.get(device["auth_set_pubkey"]),
        "device_type": device["device_type"],
        "artifact_name": device["artifact_name"],
        "inventory_updated_at": times.get(device["inventory_updated_at"]),
        "snapshot_issue": None,
    }


def _stored(row: MenderDevice) -> dict:
    return {key: getattr(row, key) for key in _STORED}


@pytest.mark.parametrize("seeded_first", [False, True], ids=["inserted", "updated"])
@pytest.mark.parametrize(("sent", "differs", "logged"), PER_DEVICE.values(), ids=PER_DEVICE.keys())
async def test_one_bad_device_is_handled_alone_and_the_rest_is_applied(
    api, maker, caplog, alerts, sent, differs, logged, seeded_first
):
    a, c = _device("dev-a", NODE_X), _pending("dev-c", NODE_Z)
    if seeded_first:
        seed = [_device("dev-a", NODE_W), _pending("dev-b", NODE_Y), _pending("dev-c", NODE_V)]
        assert (await _push(api, _snapshot(_t(0), seed))).status_code == 200
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), [a, sent, c]))
    assert response.status_code == 200, response.text
    dropped = differs is DROPPED
    clean = not dropped and differs.get("snapshot_issue") is None
    absent = 1 if dropped and seeded_first else 0
    assert response.json() == {
        "accepted": 3 if clean else 2,
        "rejected": 0 if clean else 1,
        "absent": absent,
        "generated_at": _iso(_t(1)),
    }

    rows = await _rows(maker)
    # The rest of the fleet, applied in the same push.
    assert _stored(rows["dev-a"]) == _as_sent(a)
    assert _stored(rows["dev-c"]) == _as_sent(c)
    assert all(rows[key].last_confirmed_at == _t(1) for key in ("dev-a", "dev-c"))
    if dropped:
        assert set(rows) == ({"dev-a", "dev-b", "dev-c"} if seeded_first else {"dev-a", "dev-c"})
        if seeded_first:
            assert rows["dev-b"].present is False
    else:
        b = rows["dev-b"]
        assert _stored(b) == {**_as_sent(sent), **differs}
        assert (b.present, b.last_confirmed_at) == (True, _t(1))
    state = await _state(maker)
    assert state is not None and (state.generated_at, state.device_count) == (_t(1), 3)

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == (1 if logged else 0), warnings
    if logged:
        assert warnings[0].startswith("mender snapshot devices[1] ")
    assert alerts == []


async def test_a_flagged_device_keeps_the_timestamp_rules(api, maker):
    steps = [
        # (device as pushed, accepted_at, auth_set_changed_at, issue) after the push
        (_device(), 0, 0, None),
        (_device(auth_set_pubkey=None), 0, 0, NO_AUTH_SET),
        (_device(auth_status="Accepted"), None, 0, NOT_A_STATUS),
        (_device(), 3, 0, None),
        # The set id too long is stored null, which is a change of set.
        (_device(auth_set_id="a" * 65), 3, 4, f"{AUTH_SET_ID_TOO_LONG}; {NO_AUTH_SET}"),
        (_device(), 3, 5, None),
    ]
    for n, (device, accepted_at, changed_at, issue) in enumerate(steps):
        assert (await _push(api, _snapshot(_t(n), [device]))).status_code == 200, n
        row = (await _rows(maker))["dev-a"]
        expected = (None if accepted_at is None else _t(accepted_at), _t(changed_at), _t(n), issue, True)
        assert (row.accepted_at, row.auth_set_changed_at, row.last_confirmed_at, row.snapshot_issue, row.present) == (
            expected
        ), n


async def test_a_repeated_mender_device_id_keeps_the_first_entry_with_an_issue(api, maker, caplog):
    devices = [
        _device("dev-a", NODE_X),
        _pending("dev-b", NODE_Y),
        _pending("dev-a", NODE_Z, artifact_name="owl-os-copy"),
        # A dropped copy does not count as a second holder of NODE_Y.
        _pending("dev-a", NODE_Y),
    ]
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), devices))
    assert response.json() == {"accepted": 1, "rejected": 3, "absent": 0, "generated_at": _iso(_t(1))}
    rows = await _rows(maker)
    assert set(rows) == {"dev-a", "dev-b"}
    assert _stored(rows["dev-a"]) == {**_as_sent(devices[0]), "snapshot_issue": REPEATED}
    assert _stored(rows["dev-b"]) == _as_sent(devices[1])
    warnings = _records(caplog, logging.WARNING)
    assert [warning.split(":")[0] for warning in warnings] == [
        "mender snapshot devices[0] 'dev-a'",
        "mender snapshot devices[2] 'dev-a'",
        "mender snapshot devices[3] 'dev-a'",
    ]
    assert REPEATED in warnings[0]
    assert "dropped" in warnings[1] and "devices[0]" in warnings[1].split(":", 1)[1]

    # Listed once on the next push, the issue is gone.
    response = await _push(api, _snapshot(_t(2), devices[:2]))
    assert response.json() == {"accepted": 2, "rejected": 0, "absent": 0, "generated_at": _iso(_t(2))}
    assert (await _rows(maker))["dev-a"].snapshot_issue is None


async def test_a_node_id_shared_by_several_devices_flags_every_one_of_them(api, maker):
    devices = [
        _device("dev-a", NODE_X),
        _pending("dev-b", NODE_X),
        _device("dev-c", NODE_X, auth_set_id="as-2"),
        _pending("dev-d", NODE_Y),
        # No node_id is never shared, nor is one stored null for being too long.
        _pending("dev-e", None),
        _pending("dev-f", None),
        _pending("dev-g", ""),
        _pending("dev-h", ""),
        _pending("dev-i", "n" * 65),
        _pending("dev-j", "n" * 65),
    ]
    response = await _push(api, _snapshot(_t(1), devices))
    assert response.json() == {"accepted": 5, "rejected": 5, "absent": 0, "generated_at": _iso(_t(1))}
    rows = await _rows(maker)
    assert {key: row.snapshot_issue for key, row in rows.items()} == {
        "dev-a": _shared(3),
        "dev-b": _shared(3),
        "dev-c": _shared(3),
        "dev-d": None,
        "dev-e": None,
        "dev-f": None,
        "dev-g": None,
        "dev-h": None,
        "dev-i": NODE_ID_TOO_LONG,
        "dev-j": NODE_ID_TOO_LONG,
    }
    assert all(row.present for row in rows.values())
    assert [rows[key].node_id for key in ("dev-a", "dev-b", "dev-c")] == [NODE_X] * 3


async def test_several_issues_are_joined_and_the_whole_is_cut_to_128(api, maker):
    joined = f"{NODE_ID_TOO_LONG}; {AUTH_SET_ID_TOO_LONG}; {PUBKEY_TOO_LONG}; {NO_AUTH_SET}"
    devices = [_device("dev-a", "n" * 65, auth_set_id="a" * 65, auth_set_pubkey="A" * 4097)]
    response = await _push(api, _snapshot(_t(1), devices))
    assert response.json()["rejected"] == 1
    assert (await _rows(maker))["dev-a"].snapshot_issue == joined

    # Collisions first, so the cut takes the device's own faults.
    devices = [
        _device("dev-a", NODE_X, auth_set_id="a" * 65, auth_set_pubkey="A" * 4097),
        _pending("dev-b", NODE_X),
        _pending("dev-a", NODE_Y),
    ]
    whole = f"{REPEATED}; {_shared(2)}; {AUTH_SET_ID_TOO_LONG}; {PUBKEY_TOO_LONG}; {NO_AUTH_SET}"
    assert len(whole) > 128
    response = await _push(api, _snapshot(_t(2), devices))
    assert response.json() == {"accepted": 0, "rejected": 3, "absent": 0, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert rows["dev-a"].snapshot_issue == whole[:128]
    assert rows["dev-b"].snapshot_issue == _shared(2)


# ── Per device: the node_id index ────────────────────────────────────────────


async def test_a_clean_device_takes_a_node_id_another_gives_up_while_turning_bad(api, maker):
    """dev-a leaves NODE_X with an issue, and dev-b takes NODE_X cleanly, in one
    push. Written the other way round, dev-b would meet dev-a still clean."""
    first = [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y)]
    assert (await _push(api, _snapshot(_t(1), first))).status_code == 200
    second = [_device("dev-a", NODE_Y), _pending("dev-c", NODE_Y), _pending("dev-b", NODE_X)]
    response = await _push(api, _snapshot(_t(2), second))
    assert response.json() == {"accepted": 1, "rejected": 2, "absent": 0, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert {key: (row.node_id, row.snapshot_issue) for key, row in rows.items()} == {
        "dev-a": (NODE_Y, _shared(2)),
        "dev-b": (NODE_X, None),
        "dev-c": (NODE_Y, _shared(2)),
    }


async def test_an_issue_is_written_before_another_device_clears_its_own(api, maker):
    """dev-s clears its issue and takes NODE_X in the push where dev-a, which
    held NODE_X cleanly, turns bad."""
    first = [_device("dev-a", NODE_X), _pending("dev-s", NODE_Y, auth_status="Pending")]
    assert (await _push(api, _snapshot(_t(1), first))).json()["rejected"] == 1
    second = [_device("dev-a", "n" * 65), _pending("dev-s", NODE_X)]
    response = await _push(api, _snapshot(_t(2), second))
    assert response.json() == {"accepted": 1, "rejected": 1, "absent": 0, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert {key: (row.node_id, row.snapshot_issue) for key, row in rows.items()} == {
        "dev-a": (None, NODE_ID_TOO_LONG),
        "dev-s": (NODE_X, None),
    }


async def test_flagged_pairs_beside_clean_holders_of_their_node_ids_apply(api, maker):
    """A flagged pair may share a node_id with a clean row that is absent, or
    that held it a push ago and moves on in this one."""
    first = [_device("dev-a", NODE_X), _pending("dev-z", NODE_Y), _pending("dev-old", NODE_W)]
    assert (await _push(api, _snapshot(_t(1), first))).status_code == 200
    second = [
        _device("dev-a", NODE_V),
        _pending("dev-b", NODE_X),
        _pending("dev-c", NODE_X),
        _pending("dev-d", NODE_W),
        _pending("dev-e", NODE_W),
        _pending("dev-z", NODE_Y),
    ]
    response = await _push(api, _snapshot(_t(2), second))
    assert response.json() == {"accepted": 2, "rejected": 4, "absent": 1, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert {key: (row.node_id, row.present, row.snapshot_issue) for key, row in rows.items()} == {
        "dev-a": (NODE_V, True, None),
        "dev-b": (NODE_X, True, _shared(2)),
        "dev-c": (NODE_X, True, _shared(2)),
        "dev-d": (NODE_W, True, _shared(2)),
        "dev-e": (NODE_W, True, _shared(2)),
        "dev-old": (NODE_W, False, None),
        "dev-z": (NODE_Y, True, None),
    }
    # A clean row joining a flagged pair's node_id flags all three.
    third = [*second[1:5], _device("dev-a", NODE_X), _pending("dev-z", NODE_Y)]
    response = await _push(api, _snapshot(_t(3), third))
    assert response.json() == {"accepted": 1, "rejected": 5, "absent": 1, "generated_at": _iso(_t(3))}
    rows = await _rows(maker)
    assert [rows[key].snapshot_issue for key in ("dev-a", "dev-b", "dev-c")] == [_shared(3)] * 3


async def test_a_shared_node_id_clears_once_one_sharer_is_gone(api, maker):
    pair = [_device("dev-a", NODE_X), _pending("dev-b", NODE_X)]
    assert (await _push(api, _snapshot(_t(1), pair))).json()["rejected"] == 2
    # Both still listed under NODE_X: both keep the issue.
    assert (await _push(api, _snapshot(_t(2), pair))).json()["rejected"] == 2

    response = await _push(api, _snapshot(_t(3), [_device("dev-a", NODE_X)]))
    assert response.json() == {"accepted": 1, "rejected": 0, "absent": 1, "generated_at": _iso(_t(3))}
    rows = await _rows(maker)
    assert (rows["dev-a"].snapshot_issue, rows["dev-a"].present) == (None, True)
    # Not listed, so not cleared: kept as the last snapshot that listed it.
    assert (rows["dev-b"].snapshot_issue, rows["dev-b"].present) == (_shared(2), False)

    # Back with a node_id of its own, it is clean too.
    response = await _push(api, _snapshot(_t(4), [_device("dev-a", NODE_X), _pending("dev-b", NODE_Y)]))
    assert response.json() == {"accepted": 2, "rejected": 0, "absent": 0, "generated_at": _iso(_t(4))}
    assert {key: row.snapshot_issue for key, row in (await _rows(maker)).items()} == {"dev-a": None, "dev-b": None}


async def test_a_shared_node_id_clears_for_the_sharer_that_stays_either_way(api, maker):
    pair = [_device("dev-a", NODE_X), _pending("dev-b", NODE_X)]
    assert (await _push(api, _snapshot(_t(1), pair))).json()["rejected"] == 2
    response = await _push(api, _snapshot(_t(2), [_pending("dev-b", NODE_X)]))
    assert response.json() == {"accepted": 1, "rejected": 0, "absent": 1, "generated_at": _iso(_t(2))}
    rows = await _rows(maker)
    assert {key: (row.present, row.snapshot_issue) for key, row in rows.items()} == {
        "dev-a": (False, _shared(2)),
        "dev-b": (True, None),
    }


@pytest.mark.parametrize(
    "bad",
    [
        _device("dev-a", "n" * 65),
        _device("dev-a", NODE_X, auth_status="Accepted"),
        _device("dev-a", NODE_X, auth_set_id="a" * 65),
        _device("dev-a", NODE_X, auth_set_pubkey="A" * 4097),
        _device("dev-a", NODE_X, auth_set_pubkey=None),
    ],
    ids=["node-id", "status", "auth-set-id", "pubkey", "no-auth-set"],
)
async def test_an_issue_is_cleared_by_the_next_push_that_lists_the_device_cleanly(api, maker, bad):
    assert (await _push(api, _snapshot(_t(1), [bad]))).json()["rejected"] == 1
    assert (await _rows(maker))["dev-a"].snapshot_issue is not None
    # Sent the same way again, it keeps the issue.
    assert (await _push(api, _snapshot(_t(2), [bad]))).json()["rejected"] == 1
    assert (await _rows(maker))["dev-a"].snapshot_issue is not None
    response = await _push(api, _snapshot(_t(3), [_device("dev-a", NODE_X)]))
    assert response.json() == {"accepted": 1, "rejected": 0, "absent": 0, "generated_at": _iso(_t(3))}
    row = (await _rows(maker))["dev-a"]
    assert _stored(row) == _as_sent(_device("dev-a", NODE_X))


# ── Per device: counts and logs ──────────────────────────────────────────────


async def test_the_counts_add_up_to_the_devices_sent(api, maker, alerts):
    assert (await _push(api, _snapshot(_t(1), [*_fleet(4), _pending("dev-gone")]))).status_code == 200
    devices = [
        _device("dev-a", NODE_X),
        _pending("dev-b", NODE_Y, device_type="t" * 200),  # cut, still accepted
        _pending("dev-c", NODE_Z, auth_status="Pending"),  # stored with an issue
        _pending(""),  # dropped
        _pending("dev-a", NODE_W),  # dropped, and its first copy flagged
        *_fleet(4),
    ]
    response = await _push(api, _snapshot(_t(2), devices))
    body = response.json()
    assert body == {"accepted": 5, "rejected": 4, "absent": 1, "generated_at": _iso(_t(2))}
    assert body["accepted"] + body["rejected"] == len(devices)
    rows = await _rows(maker)
    assert sum(1 for row in rows.values() if row.present and row.snapshot_issue is None) == 5
    # device_count and the alerts go by the devices sent, dropped ones included.
    state = await _state(maker)
    assert state is not None and state.device_count == len(devices)
    assert alerts == []


async def test_dropped_devices_still_count_for_the_count_drop_alert(api, maker, alerts):
    assert (await _push(api, _snapshot(_t(1), _fleet(10)))).status_code == 200
    devices = [*_fleet(2), *[_pending("") for _ in range(8)]]
    response = await _push(api, _snapshot(_t(2), devices))
    assert response.json() == {"accepted": 2, "rejected": 8, "absent": 8, "generated_at": _iso(_t(2))}
    state = await _state(maker)
    assert state is not None and state.device_count == 10
    assert alerts == []


@pytest.mark.parametrize("count", [20, 21, 2000])
async def test_per_device_warnings_are_capped_with_one_line_for_the_rest(api, maker, caplog, count):
    devices = [_pending(f"dev-{index:04d}", auth_status="Pending") for index in range(count)]
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), devices))
    assert response.json() == {"accepted": 0, "rejected": count, "absent": 0, "generated_at": _iso(_t(1))}
    assert len(await _rows(maker)) == count
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == min(count, 20) + (1 if count > 20 else 0)
    for index, warning in enumerate(warnings[:20]):
        assert warning.startswith(f"mender snapshot devices[{index}] 'dev-{index:04d}': stored with issue"), warning
    if count > 20:
        assert warnings[20] == f"mender snapshot: {count - 20} more devices with a fault, not logged one by one"


async def test_sent_strings_in_the_log_are_cut_and_quoted(api, caplog):
    long_id = "\n" + "x" * 2000
    devices = [
        _pending(long_id),
        _pending("dev-b", NODE_Y, inventory_updated_at="y" * 2000 + "\nforged"),
        _pending("dev-c", NODE_Z, auth_status="\n" + "z" * 2000),
    ]
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(_t(1), devices))
    assert response.json() == {"accepted": 1, "rejected": 2, "absent": 0, "generated_at": _iso(_t(1))}
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 3
    assert repr(long_id[:64]) in warnings[0] and "dropped" in warnings[0]
    assert repr("y" * 64) in warnings[1]
    assert repr("\n" + "z" * 63) in warnings[2]
    for warning in warnings:
        assert "\n" not in warning
        assert all(letter * 65 not in warning for letter in "xyz")


async def _pushed_and_logged(api, caplog, generated_at: datetime, devices: list[dict]) -> tuple[dict, list[str]]:
    """Push, check its one INFO line carries the counts answered, and return them and the WARNING lines."""
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        response = await _push(api, _snapshot(generated_at, devices))
    assert response.status_code == 200, response.text
    body = response.json()
    infos = _records(caplog, logging.INFO)
    assert len(infos) == 1
    assert f"{body['accepted']} accepted, {body['rejected']} rejected, {body['absent']} absent" in infos[0]
    return body, _records(caplog, logging.WARNING)


async def test_an_issue_is_logged_when_new_or_changed_and_not_while_it_stays(api, caplog):
    no_auth_set = _device("dev-a", NODE_X, auth_set_pubkey=None)
    bad_status = _pending("dev-b", NODE_Y, auth_status="Pending")
    status_line = (
        f"mender snapshot devices[1] 'dev-b': stored with issue {NOT_A_STATUS!r}; "
        "auth_status 'Pending' stored as 'invalid'"
    )
    steps = [
        # (devices as pushed, rejected, the WARNING lines logged)
        (
            [no_auth_set, bad_status],
            2,
            [f"mender snapshot devices[0] 'dev-a': stored with issue {NO_AUTH_SET!r}", status_line],
        ),
        # The same faults again: no line, and the INFO line still counts them.
        ([no_auth_set, bad_status], 2, []),
        ([no_auth_set, bad_status], 2, []),
        # dev-a's issue changes.
        (
            [_device("dev-a", "n" * 65, auth_set_pubkey=None), bad_status],
            2,
            [f"mender snapshot devices[0] 'dev-a': stored with issue '{NODE_ID_TOO_LONG}; {NO_AUTH_SET}'"],
        ),
        # Cleared: no line of its own.
        ([_device("dev-a", NODE_X), bad_status], 1, []),
        # Back after a clean push: new again.
        ([no_auth_set, bad_status], 2, [f"mender snapshot devices[0] 'dev-a': stored with issue {NO_AUTH_SET!r}"]),
        # A new device with dev-b's fault is new, whatever dev-b has.
        (
            [no_auth_set, bad_status, _pending("dev-c", NODE_Z, auth_status="Pending")],
            3,
            [status_line.replace("[1] 'dev-b'", "[2] 'dev-c'")],
        ),
    ]
    for n, (devices, rejected, logged) in enumerate(steps):
        body, warnings = await _pushed_and_logged(api, caplog, _t(n), devices)
        assert (body["rejected"], warnings) == (rejected, logged), n


async def test_a_fleet_that_stays_faulty_logs_nothing_on_the_next_push(api, maker, caplog):
    devices = [_pending(f"dev-{index:04d}", auth_status="Pending") for index in range(25)]
    _, first = await _pushed_and_logged(api, caplog, _t(1), devices)
    assert len(first) == 21
    assert first[20] == "mender snapshot: 5 more devices with a fault, not logged one by one"
    # Not even the line for the rest.
    body, warnings = await _pushed_and_logged(api, caplog, _t(2), devices)
    assert (body["rejected"], warnings) == (25, [])
    assert {row.snapshot_issue for row in (await _rows(maker)).values()} == {NOT_A_STATUS}


async def test_dropped_devices_and_notes_are_logged_on_every_push(api, caplog):
    devices = [
        # An issue and a cut.
        _device("dev-a", "n" * 65, device_type="t" * 200),
        # Clean, a field nulled.
        _pending("dev-b", NODE_Y, inventory_updated_at="yesterday"),
        _pending(""),
        # Clean, the key undecodable.
        _device("dev-d", NODE_Z, auth_set_pubkey=UNDECODABLE_PUBKEY),
    ]
    cut = "device_type cut from 200 to 128 characters"
    rest = [
        "mender snapshot devices[1] 'dev-b': stored; inventory_updated_at 'yesterday' is not ISO 8601 "
        "with a timezone, stored null",
        "mender snapshot devices[2] '': dropped, mender_device_id is 0 characters, not 1 to 64",
        "mender snapshot devices[3] 'dev-d': stored; auth_set_pubkey undecodable, stored without a fingerprint",
    ]
    _, warnings = await _pushed_and_logged(api, caplog, _t(1), devices)
    assert warnings == [f"mender snapshot devices[0] 'dev-a': stored with issue {NODE_ID_TOO_LONG!r}; {cut}", *rest]
    # The issue is not repeated, the cut is.
    _, warnings = await _pushed_and_logged(api, caplog, _t(2), devices)
    assert warnings == [f"mender snapshot devices[0] 'dev-a': stored with its issue unchanged; {cut}", *rest]


# ── Alerts ───────────────────────────────────────────────────────────────────


async def test_the_first_snapshot_raises_no_alert(api, alerts):
    assert (await _push(api, _snapshot(_t(1), _fleet(3), source=OTHER_SOURCE))).status_code == 200
    assert alerts == []


async def test_a_changed_source_alerts_and_warns(api, alerts, caplog):
    assert (await _push(api, _snapshot(_t(1), _fleet(3)))).status_code == 200
    assert (await _push(api, _snapshot(_t(2), _fleet(3)))).status_code == 200
    assert alerts == []
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        assert (await _push(api, _snapshot(_t(3), _fleet(3), source=OTHER_SOURCE))).status_code == 200
    assert [alert[0] for alert in alerts] == ["mender_snapshot_source_changed"]
    message = alerts[0][1]
    assert repr(SOURCE) in message and repr(OTHER_SOURCE) in message
    warnings = _records(caplog, logging.WARNING)
    assert warnings == [message]


@pytest.mark.parametrize(
    ("before", "after", "alerted"),
    [
        (10, 8, False),
        (10, 7, True),
        (5, 4, False),
        (5, 3, True),
        (100, 80, False),
        (100, 79, True),
        (1, 0, True),
        (0, 0, False),
        (3, 3, False),
        (3, 9, False),
    ],
    ids=lambda value: str(value),
)
async def test_a_drop_of_more_than_a_fifth_alerts(api, alerts, caplog, before, after, alerted):
    fleet = _fleet(before)
    assert (await _push(api, _snapshot(_t(1), fleet))).status_code == 200
    with caplog.at_level(logging.DEBUG, logger=mender_sync.logger.name):
        assert (await _push(api, _snapshot(_t(2), _fleet(after)))).status_code == 200
    expected = ["mender_snapshot_count_drop"] if alerted else []
    assert [alert[0] for alert in alerts] == expected
    assert len(_records(caplog, logging.WARNING)) == len(expected)
    if alerted:
        assert f"{after} devices" in alerts[0][1] and f"from {before}" in alerts[0][1]


async def test_alerts_are_sent_once_the_snapshot_is_committed(api, db_path, monkeypatch):
    from services import alerting

    assert (await _push(api, _snapshot(_t(1), _fleet(5)))).status_code == 200
    seen = []

    def _read_committed(alert_type, message, meta=None):
        with contextlib.closing(sqlite3.connect(db_path)) as connection:
            seen.append(
                (alert_type, *connection.execute("SELECT source, device_count FROM mender_sync_state").fetchone())
            )

    monkeypatch.setattr(alerting, "send_alert", _read_committed)
    assert (await _push(api, _snapshot(_t(2), _fleet(1), source=OTHER_SOURCE))).status_code == 200
    assert seen == [
        ("mender_snapshot_source_changed", OTHER_SOURCE, 1),
        ("mender_snapshot_count_drop", OTHER_SOURCE, 1),
    ]


async def test_no_push_that_is_refused_alerts(api, maker, alerts, monkeypatch):
    assert (await _push(api, _snapshot(_t(1), _fleet(10)))).status_code == 200
    alarming = _snapshot(_t(2), [], source=OTHER_SOURCE)

    assert (await _push(api, alarming, token="fake-wrong-token-0123456789abcdef")).status_code == 401
    with_extra = {**alarming, "extra": True}
    assert (await _push(api, with_extra)).status_code == 422
    assert (await _push_raw(api, _padded(alarming, CAP + 1))).status_code == 413
    assert (await _push(api, _snapshot(_t(1), [], source=OTHER_SOURCE))).status_code == 409
    assert (await _push(api, _snapshot(_t(0), [], source=OTHER_SOURCE))).status_code == 409
    monkeypatch.delenv("MENDER_SYNC_TOKEN")
    assert (await _push(api, alarming)).status_code == 404
    assert alerts == []

    # The same push, accepted, alerts on both counts.
    monkeypatch.setenv("MENDER_SYNC_TOKEN", TOKEN)
    assert (await _push(api, alarming)).status_code == 200
    assert [alert[0] for alert in alerts] == ["mender_snapshot_source_changed", "mender_snapshot_count_drop"]
