"""The keyed live streams (routes/data_stream.py): the websocket and the event stream behind a key.

Driven over ASGI in the test's own loop, every wait bounded by WAIT_S: an
event stream never ends by itself, and Starlette's TestClient and httpx wait
for a response's whole body.
"""

import asyncio
from urllib.parse import urlencode

import orjson
import pytest
from sqlalchemy import update

from core import data_access, state, users
from core.api_access import Tier
from core.users import async_session_maker
from main import app
from routes import data_stream
from services import api_keys
from tests.test_admin_users import _Access
from tests.test_data_access import _account, _key

WAIT_S = 3.0
LONDON = "51,-1,52,1"
KEEPALIVE = ": keepalive\n\n"


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    monkeypatch.setattr(users, "access_identity", _Access())


@pytest.fixture
def ada() -> tuple[str, str]:
    """An entitled customer's key: its id, and the key itself."""
    return _key(_account("ada@example.com"))


async def _customer_streams(count: int) -> None:
    async with async_session_maker() as db, db.begin():
        await db.execute(update(Tier).where(Tier.name == "customer").values(max_concurrent_streams=count))


@pytest.fixture
def one_stream():
    """The customer tier at 1 stream open at once, and back at 10 after: tests/conftest.py keeps tiers."""
    asyncio.run(_customer_streams(1))
    yield
    asyncio.run(_customer_streams(10))


def _flush(monkeypatch, now: float, *fixes: tuple[str, float, float]) -> None:
    """A new flush of the real-only feed carrying these (hex, lat, lon) ADS-B fixes."""
    aircraft = [{"hex": h, "type": "adsb_icao", "flight": "BAW1", "lat": lat, "lon": lon} for h, lat, lon in fixes]
    monkeypatch.setattr(state, "latest_real_aircraft_json_bytes", orjson.dumps({"now": now, "aircraft": aircraft}))


@pytest.fixture
def feed(monkeypatch):
    _flush(monkeypatch, 1762041600.0, ("4ca1b2", 51.47, -0.45), ("4ca1b3", 51.5, 0.1), ("c0ffee", -17.0, 179.6))


class _Client:
    """One socket or request to the app over ASGI."""

    def __init__(self, kind: str, path: str, query: dict[str, str], headers: dict[str, str] | None = None) -> None:
        self._to_app: asyncio.Queue[dict] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict] = asyncio.Queue()
        self.put({"type": "websocket.connect"} if kind == "websocket" else {"type": "http.request", "body": b""})
        raw = [(name.encode(), value.encode()) for name, value in {"host": "testserver", **(headers or {})}.items()]
        scope = {"type": kind, "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1"}
        scope |= {"method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": ""}
        scope |= {"query_string": urlencode(query).encode(), "headers": raw, "client": ("127.0.0.1", 50000)}
        self._task = asyncio.create_task(app(scope, self._to_app.get, self._from_app.put))

    def put(self, message: dict) -> None:
        self._to_app.put_nowait(message)

    async def next(self) -> dict:
        return await asyncio.wait_for(self._from_app.get(), WAIT_S)

    async def frame(self) -> dict:
        return orjson.loads((await self.next())["text"])

    async def event(self) -> str:
        """The stream's next event past any keepalive comments, as sent; "" once it has ended."""
        async with asyncio.timeout(WAIT_S):
            while (body := (await self.next())["body"].decode()) == KEEPALIVE:
                pass
        return body

    async def leave(self, message: dict) -> None:
        """The client goes; the app must be done within WAIT_S."""
        self.put(message)
        await asyncio.wait_for(self._task, WAIT_S)


def _socket(key: str, **filters: str) -> _Client:
    return _Client("websocket", "/ws/v1/aircraft", {"apikey": key, **filters})


def _stream(key: str, **filters: str) -> _Client:
    return _Client("http", "/api/v1/stream", filters, {"x-apikey": key})


def _hexes(data: dict) -> list[str]:
    return [row["hex"] for row in data["aircraft"]]


def _aircraft_event(text: str) -> tuple[float, list[str]]:
    data = orjson.loads(text.removeprefix("event: aircraft\ndata: "))
    return data["now"], _hexes(data)


async def test_a_socket_carries_what_its_filters_match_until_its_key_is_revoked(ada, feed, monkeypatch):
    monkeypatch.setattr(data_access, "RECHECK_INTERVAL_S", 0.2)
    key_id, key = ada
    socket = _socket(key, bbox=LONDON)

    assert (await socket.next())["type"] == "websocket.accept"
    assert _hexes(await socket.frame()) == ["4ca1b2", "4ca1b3"]
    socket.put({"type": "websocket.receive", "text": '{"type": "filters", "hex": ["C0FFEE"], "alt_min": null}'})
    assert _hexes(await socket.frame()) == ["c0ffee"]

    await api_keys.revoke_key(key_id)
    assert await socket.next() == {"type": "websocket.close", "code": 1008, "reason": "invalid_key"}


async def test_an_event_stream_carries_what_its_filters_match_and_keepalives_until_its_key_is_revoked(
    ada, feed, monkeypatch
):
    monkeypatch.setattr(data_stream, "POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(data_stream, "SSE_KEEPALIVE_S", 0.1)
    monkeypatch.setattr(data_access, "RECHECK_INTERVAL_S", 0.3)
    key_id, key = ada
    stream = _stream(key, bbox=LONDON)

    start = await stream.next()
    headers = dict(start["headers"])
    assert (start["status"], headers[b"cache-control"], headers[b"x-accel-buffering"]) == (200, b"no-cache", b"no")
    assert _aircraft_event(await stream.event()) == (1762041600.0, ["4ca1b2", "4ca1b3"])
    assert (await stream.next())["body"] == KEEPALIVE.encode()
    _flush(monkeypatch, 1762041601.0, ("4ca1b4", 51.6, 0.2), ("c0fff0", -17.1, 179.7))
    assert _aircraft_event(await stream.event()) == (1762041601.0, ["4ca1b4"])

    await api_keys.revoke_key(key_id)
    error = await stream.event()
    assert error == 'event: error\ndata: {"reason":"invalid_key","message":"The key sent is not valid."}\n\n'
    assert await stream.event() == ""


async def test_a_stream_past_the_tiers_count_is_refused_until_one_is_given_back(ada, feed, one_stream):
    _, key = ada
    socket = _socket(key)
    assert (await socket.next())["type"] == "websocket.accept"
    assert (await socket.next())["type"] == "websocket.send"

    refused = _socket(key)
    start = await refused.next()
    assert (start["type"], start["status"], dict(start["headers"])[b"retry-after"]) == (
        "websocket.http.response.start",
        429,
        b"30",
    )
    assert orjson.loads((await refused.next())["body"])["detail"]["reason"] == "stream_limit"
    bad_filters = _socket(key, bbox="north")
    assert (await bad_filters.next())["status"] == 422
    rule = "bbox must be south,west,north,east in degrees, with south <= north"
    assert orjson.loads((await bad_filters.next())["body"]) == {"detail": rule}
    over = _stream(key)
    assert (await over.next())["status"] == 429
    assert orjson.loads((await over.next())["body"])["detail"]["reason"] == "stream_limit"

    await socket.leave({"type": "websocket.disconnect", "code": 1000})
    first = _stream(key)
    assert (await first.next())["status"] == 200
    assert (await _stream(key).next())["status"] == 429
    await first.leave({"type": "http.disconnect"})
    assert (await _stream(key).next())["status"] == 200
