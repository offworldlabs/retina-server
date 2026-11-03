"""Tests for streaming endpoints.

/ws/aircraft, /ws/aircraft/live and its polling twin aircraft-live.json, and /api/radar/stream SSE.
"""

import asyncio

import anyio
import orjson
import pytest
from sqlalchemy import update
from starlette.testclient import WebSocketDenialResponse

from core import state, users
from core.api_access import Tier
from core.users import async_session_maker
from main import app
from services.tasks.aircraft_flush import _flush_once
from tests.route_helpers import entitled_key_headers

# One real node's aircraft and arcs around London and Paris, and two aircraft a
# box of 40..50 N, 10 W..10 E holds that the box clipped to 500 km does not.
_FRAME = {
    "now": 1.0,
    "aircraft": [
        {"hex": "LON1", "lat": 51.5, "lon": -0.1, "node_id": "test-n1"},
        {"hex": "PAR1", "lat": 48.9, "lon": 2.3, "node_id": "test-n1"},
        {"hex": "CORE", "lat": 45.0, "lon": 0.0, "node_id": "test-n1"},
        {"hex": "SOUTH", "lat": 41.0, "lon": 0.0, "node_id": "test-n1"},
        {"hex": "EAST", "lat": 45.0, "lon": 5.0, "node_id": "test-n1"},
    ],
    "detection_arcs": [
        {"hex": "LON1", "ambiguity_arc": [[51.4, -0.3], [51.6, 0.1]], "node_id": "test-n1"},
        {"hex": "PAR1", "ambiguity_arc": [[48.8, 2.2], [49.0, 2.4]], "node_id": "test-n1"},
    ],
    "ground_truth": {},
}


@pytest.fixture
def live_frame(monkeypatch):
    monkeypatch.delenv("MAP_VIEWPORT_MAX_KM", raising=False)
    monkeypatch.setattr(state, "connected_nodes", {"test-n1": {"is_synthetic": False}})
    _flush_once(_FRAME)


def _viewport(bbox: list) -> str:
    return orjson.dumps({"type": "viewport", "bbox": bbox}).decode()


class TestLiveWebSocket:
    def test_live_ws_accepts_no_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        state.ws_live_clients.clear()
        with client.websocket_connect("/ws/aircraft/live"):
            pass
        # After clean close the client is removed from the set
        assert len(state.ws_live_clients) == 0

    def test_each_live_socket_gets_only_its_viewport(self, client, monkeypatch, live_frame):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        with (
            client.websocket_connect("/ws/aircraft/live") as london,
            client.websocket_connect("/ws/aircraft/live") as paris,
        ):
            london.send_text(_viewport([51, -1, 52, 1]))
            paris.send_text(_viewport([48.5, 2, 49.5, 3]))
            frames = [orjson.loads(london.receive_text()), orjson.loads(paris.receive_text())]

        seen = [
            ([a["hex"] for a in f["aircraft"]], [a["hex"] for a in f["detection_arcs"]], f["messages"]) for f in frames
        ]
        assert seen == [(["LON1"], ["LON1"], 1), (["PAR1"], ["PAR1"], 1)]
        assert [f["viewport"] for f in frames] == [
            {"bbox": [51.0, -1.0, 52.0, 1.0], "clipped": False},
            {"bbox": [48.5, 2.0, 49.5, 3.0], "clipped": False},
        ]

    def test_a_box_named_within_a_quarter_second_of_an_answer_waits_for_the_broadcast(
        self, client, monkeypatch, live_frame
    ):
        from routes import streaming as streaming_mod
        from services import viewport

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        # One reading per viewport message: London, Paris in the same instant, then a second on.
        monkeypatch.setattr(viewport, "_clock", iter([100.0, 100.0, 101.0]).__next__)
        with client.websocket_connect("/ws/aircraft/live") as ws:
            ws.send_text(_viewport([51, -1, 52, 1]))
            london = orjson.loads(ws.receive_text())
            ws.send_text(_viewport([48.5, 2, 49.5, 3]))
            ws.send_text(_viewport([44, -1, 46, 1]))
            core = orjson.loads(ws.receive_text())

        assert [[a["hex"] for a in f["aircraft"]] for f in (london, core)] == [["LON1"], ["CORE"]]

    def test_live_ws_rejects_invalid_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-xyz")
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/aircraft/live?token=wrong") as ws:
                ws.receive_text()

    def test_live_ws_accepts_valid_token(self, client, monkeypatch):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "secret-xyz")
        state.ws_live_clients.clear()
        with client.websocket_connect("/ws/aircraft/live?token=secret-xyz"):
            pass
        assert len(state.ws_live_clients) == 0


class TestLivePollingJson:
    def test_a_box_is_required_and_an_oversized_one_is_clipped(self, client, live_frame):
        assert client.get("/api/radar/data/aircraft-live.json").status_code == 422

        body = client.get("/api/radar/data/aircraft-live.json?bbox=40,-10,50,10").json()
        assert body["viewport"] == {"bbox": [42.751699, -3.061819, 47.248301, 3.061819], "clipped": True}
        assert [a["hex"] for a in body["aircraft"]] == ["CORE"]


class TestAircraftWSInitialSnapshot:
    def test_initial_snapshot_sent_if_aircraft_present(self, client, monkeypatch):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")

        payload = {"aircraft": [{"hex": "d00d", "lat": 1, "lon": 2}], "now": 1.0}
        # The public pair: latest_aircraft_json_public is the dict guard,
        # latest_aircraft_json_bytes is its serialization and the body sent.
        # The unredacted latest_aircraft_json belongs to the owner feed.
        monkeypatch.setattr(state, "latest_aircraft_json_public", payload)
        monkeypatch.setattr(state, "latest_aircraft_json_bytes", orjson.dumps(payload))

        with client.websocket_connect("/ws/aircraft") as ws:
            msg = ws.receive_text()
            body = orjson.loads(msg)
            assert body["aircraft"][0]["hex"] == "d00d"

    def test_no_initial_send_when_empty(self, client, monkeypatch):
        from routes import streaming as streaming_mod

        monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
        monkeypatch.setattr(state, "latest_aircraft_json_public", {})
        # If we don't call receive, the socket will just stay open; close it.
        with client.websocket_connect("/ws/aircraft"):
            pass


# ── The data-access gate on the whole network's feeds ───────────────────────

_REAL = b'{"now":2.0,"aircraft":[{"hex":"REAL01"}]}'
_FULL = {"now": 2.0, "aircraft": [{"hex": "REAL01"}, {"hex": "SYNTH01"}]}


@pytest.fixture
def two_feeds(monkeypatch):
    """The real-only frame and the whole fleet's, told apart by SYNTH01."""
    from routes import streaming as streaming_mod

    monkeypatch.setattr(streaming_mod, "_WS_AUTH_TOKEN", "")
    monkeypatch.setattr(state, "latest_real_aircraft_json_bytes", _REAL)
    monkeypatch.setattr(state, "latest_aircraft_json_public", _FULL)
    monkeypatch.setattr(state, "latest_aircraft_json_bytes", orjson.dumps(_FULL))


async def _customer_streams(streams: int) -> None:
    async with async_session_maker() as db, db.begin():
        await db.execute(update(Tier).where(Tier.name == "customer").values(max_concurrent_streams=streams))


@pytest.fixture
def one_stream():
    """The customer tier at 1 stream open at once, and back at 10 after: tests/conftest.py keeps tiers."""
    asyncio.run(_customer_streams(1))
    yield
    asyncio.run(_customer_streams(10))


def _key() -> str:
    return asyncio.run(entitled_key_headers("reader@example.com"))["x-apikey"]


def _stream(headers: dict[str, str], chunks: int) -> tuple[int, list[bytes]]:
    """The status and the first `chunks` body chunks of /api/radar/stream, then the client goes away.

    Driven as ASGI: the TestClient reads a body to its end, which this one never reaches.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/radar/stream",
        "raw_path": b"/api/radar/stream",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *((k.encode(), v.encode()) for k, v in headers.items())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    status: list[int] = []
    body: list[bytes] = []

    async def run() -> None:
        enough = anyio.Event()
        requests = iter([{"type": "http.request", "body": b"", "more_body": False}])

        async def receive():
            if (message := next(requests, None)) is not None:
                return message
            await enough.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                status.append(message["status"])
                return
            if message.get("body"):
                body.append(message["body"])
            if len(body) == chunks or not message.get("more_body"):
                enough.set()

        with anyio.fail_after(5):
            await app(scope, receive, send)

    anyio.run(run)
    return status[0], body


def test_a_socket_without_credentials_is_refused_401(client, monkeypatch, two_feeds):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    with pytest.raises(WebSocketDenialResponse) as refused, client.websocket_connect("/ws/aircraft"):
        pass
    detail = {"reason": "missing_credentials", "message": "Send an API key, or sign in."}
    assert (refused.value.status_code, refused.value.json()) == (401, {"detail": detail})


def test_a_keyed_socket_gets_the_real_only_frame_and_stays_out_of_the_broadcast(client, two_feeds):
    with client.websocket_connect(f"/ws/aircraft?apikey={_key()}") as ws:
        assert ws.receive_text() == '{"now":2.0,"aircraft":[{"hex":"REAL01"}]}'
        assert not state.ws_clients


def test_an_administrators_socket_keeps_the_whole_feed(client, two_feeds):
    with client.websocket_connect("/ws/aircraft") as ws:
        assert orjson.loads(ws.receive_text())["aircraft"] == [{"hex": "REAL01"}, {"hex": "SYNTH01"}]


def test_a_keyed_socket_past_its_tiers_streams_is_refused_429(client, two_feeds, one_stream):
    key = _key()
    with client.websocket_connect(f"/ws/aircraft?apikey={key}") as first:
        first.receive_text()
        with pytest.raises(WebSocketDenialResponse) as refused, client.websocket_connect(f"/ws/aircraft?apikey={key}"):
            pass
    denial = refused.value
    assert (denial.status_code, denial.headers["retry-after"], denial.json()["detail"]["reason"]) == (
        429,
        "30",
        "stream_limit",
    )


def test_an_event_stream_without_credentials_is_401(monkeypatch, two_feeds):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    refused = b'{"detail":{"reason":"missing_credentials","message":"Send an API key, or sign in."}}'
    assert _stream({}, 1) == (401, [refused])


def test_a_keyed_event_stream_sends_the_real_only_frame(two_feeds):
    assert _stream({"x-apikey": _key()}, 1) == (200, [b'data: {"now":2.0,"aircraft":[{"hex":"REAL01"}]}\n\n'])


def test_an_idle_event_stream_sends_a_keepalive(monkeypatch):
    """Before any flush: the real-only placeholder is no frame, so nothing but the comment."""
    from routes import streaming as streaming_mod

    monkeypatch.setattr(streaming_mod, "SSE_KEEPALIVE_S", 0.01)
    assert _stream({"x-apikey": _key()}, 1) == (200, [b": keepalive\n\n"])
