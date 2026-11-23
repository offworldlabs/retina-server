"""Each reporting node's latest ADS-B fix, held beside the shared record.

state.adsb_aircraft keeps one record per hex, whichever node's fix is newest.
state.adsb_fixes_by_reporter keeps every reporter's own latest fix, so the
ADS-B archive can keep a public node's fix when a private node's is newer.
The reporter is a node id, which no public read may carry: the last two tests
pin where the map may be named and what the public reads show.
"""

import asyncio
import os
import time
import types
from collections import deque
from pathlib import Path

import orjson
import pytest
from fastapi.testclient import TestClient

from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services import feed_helpers, node_refs, publication
from services.feed_helpers import adsb_store
from services.frame_processor import process_one_frame
from services.tcp_handler import _apply_synthetic_adsb

_HEX = "cafe01"
_A = "ret0123abcd"
_B = "ret0a1b2c3d"
_KEY = {"X-API-Key": os.environ["RADAR_API_KEY"]}


@pytest.fixture(autouse=True)
def _archive_on(monkeypatch):
    monkeypatch.setattr(feed_helpers, "ADSB_SNAPSHOT_ENABLED", True)


def _fix(last_seen_ms: int) -> dict:
    return {"hex": _HEX, "lat": 48.0, "lon": 16.0, "last_seen_ms": last_seen_ms}


def _held() -> dict:
    """reporter -> last_seen_ms of the fix held for _HEX."""
    return {r: f["last_seen_ms"] for r, f in state.adsb_fixes_by_reporter[_HEX].items()}


def _app_client() -> TestClient:
    """The app without its lifespan: no background task runs against the
    stores this file builds, and none writes an archive when it stops."""
    from main import app

    return TestClient(app, raise_server_exceptions=False)


# ── The rule ─────────────────────────────────────────────────────────────────


def test_a_reporter_keeps_its_latest_fix_after_another_node_takes_the_shared_record():
    a_first, b_newer, a_second = _fix(1_000), _fix(3_000), _fix(2_000)

    assert adsb_store(_HEX, a_first, _A) is True
    assert adsb_store(_HEX, b_newer, _B) is True
    assert adsb_store(_HEX, a_second, _A) is False

    assert state.adsb_aircraft[_HEX]["last_seen_ms"] == 3_000
    assert _held() == {_A: 2_000, _B: 3_000}
    assert state.adsb_fixes_by_reporter[_HEX][_A] is a_second
    assert state.adsb_fixes_by_reporter[_HEX][_B] is state.adsb_aircraft[_HEX]


def test_a_reporters_older_fix_never_replaces_its_newer_one():
    newer, older, same_stamp = _fix(5_000), _fix(4_000), _fix(5_000)

    adsb_store(_HEX, newer, _A)
    adsb_store(_HEX, older, _A)
    assert _held() == {_A: 5_000}
    assert state.adsb_fixes_by_reporter[_HEX][_A] is newer

    # Only a strictly newer held fix wins: the frame path re-stores the
    # message the TCP path already stored, at the same stamp.
    adsb_store(_HEX, same_stamp, _A)
    assert state.adsb_fixes_by_reporter[_HEX][_A] is same_stamp


def test_a_store_that_names_no_reporter_notes_nothing():
    assert adsb_store(_HEX, _fix(1_000)) is True
    assert state.adsb_fixes_by_reporter == {}


def test_with_the_archive_off_nothing_is_noted_and_the_shared_store_is_unchanged(monkeypatch):
    monkeypatch.setattr(feed_helpers, "ADSB_SNAPSHOT_ENABLED", False)
    first, stale = _fix(2_000), _fix(1_000)

    assert adsb_store(_HEX, first, _A) is True
    assert adsb_store(_HEX, stale, _B) is False
    feed_helpers.note_adsb_reporter(_HEX, None, first)

    assert state.adsb_aircraft == {_HEX: first}
    assert state.adsb_fixes_by_reporter == {}


# ── The writers ──────────────────────────────────────────────────────────────


def _push(api: TestClient, source: str, hex_code: str):
    body = {
        "ts_ms": int(time.time() * 1000),
        "source": source,
        "aircraft": [{"hex": hex_code, "lat": 34.85, "lon": -82.4, "alt_baro": 31000}],
    }
    r = api.post("/api/sim/adsb/push", headers=_KEY, json=body)
    assert r.json() == {"status": "ok", "updated": 1, "rejected_hex": 0}


def test_the_simulated_fleet_is_noted_under_no_node_and_the_real_relay_not_at_all():
    api = _app_client()
    try:
        _push(api, "real", "a97cf2")
        assert state.adsb_aircraft["a97cf2"]["world"] == "real"
        assert state.adsb_fixes_by_reporter == {}

        _push(api, "sim", "a1b2c3")
    finally:
        api.close()
    assert state.adsb_aircraft["a1b2c3"]["world"] == "sim"
    assert list(state.adsb_fixes_by_reporter) == ["a1b2c3"]
    assert list(state.adsb_fixes_by_reporter["a1b2c3"]) == [None]
    assert state.adsb_fixes_by_reporter["a1b2c3"][None] is state.adsb_aircraft["a1b2c3"]


_ENTRY = {"hex": "CAFE01", "lat": 48.0, "lon": 16.0, "alt_baro": 35000, "gs": 400, "track": 90}


def test_the_tcp_path_notes_the_node_that_sent_the_message():
    _apply_synthetic_adsb({"data": {"timestamp": int(time.time() * 1000), "adsb": [_ENTRY]}}, _A)

    assert list(state.adsb_fixes_by_reporter[_HEX]) == [_A]
    assert state.adsb_fixes_by_reporter[_HEX][_A] is state.adsb_aircraft[_HEX]


def test_the_frame_path_notes_the_node_that_sent_the_frame():
    frame = {
        "timestamp": int(time.time() * 1000),
        "delay": [50.0, 52.0],
        "doppler": [10.0, 15.0],
        "snr": [20.0, 21.0],
        "adsb": [_ENTRY],
    }
    process_one_frame(_B, frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

    assert list(state.adsb_fixes_by_reporter[_HEX]) == [_B]
    assert state.adsb_fixes_by_reporter[_HEX][_B] is state.adsb_aircraft[_HEX]


# ── The reporter stays server-side ───────────────────────────────────────────

_MAY_NAME_THE_MAP = {
    "core/state.py",
    "services/feed_helpers.py",
    "routes/sim_ingest.py",
    "services/tasks/adsb_snapshot.py",
}
_NOT_SOURCE = {"tests", ".venv", "__pycache__", "data", "coverage_data", ".pytest_cache", ".ruff_cache"}


def test_only_the_writers_and_the_archive_name_the_reporter_map():
    backend = Path(__file__).resolve().parents[1]
    naming = set()
    for root, dirs, files in os.walk(backend):
        dirs[:] = [d for d in dirs if d not in _NOT_SOURCE]
        for name in files:
            path = Path(root, name)
            if name.endswith(".py") and "adsb_fixes_by_reporter" in path.read_text(encoding="utf-8"):
                naming.add(path.relative_to(backend).as_posix())

    assert {"core/state.py", "services/feed_helpers.py"} <= naming
    assert naming <= _MAY_NAME_THE_MAP, naming - _MAY_NAME_THE_MAP


# A reporting node that exists nowhere else in the server, and a public real
# node that claims the aircraft it reported, so the fix reaches every feed.
_SENTINEL = "ret5e6f7a8b"
_PUB = "ret9c0d1e2f"
_AC_HEX = "4b1a2c"
_CALLSIGN = "LEAKCHK1"
_FIX_LAT, _FIX_LON = 33.91, -83.85
_PUB_CONFIG = {
    "node_id": _PUB,
    "rx_lat": 34.0,
    "rx_lon": -84.0,
    "tx_lat": 33.82,
    "tx_lon": -83.68,
    "beam_azimuth_deg": 125.0,
    "beam_width_deg": 90,
    "max_range_km": 100,
}


def _public_world(monkeypatch) -> str:
    """Return the public node's ref once the sentinel's fix is in every store."""
    from services.tasks.analytics_refresh import _refresh_missed_detections
    from tests.ownership_helpers import register_node_row

    pub_ref = asyncio.run(register_node_row(_PUB, "public"))
    publication._reset_for_tests()
    node_refs._reset_for_tests()
    state.connected_nodes[_PUB] = {"status": "active", "config": dict(_PUB_CONFIG), "is_synthetic": False}
    state.node_pipelines[_PUB] = types.SimpleNamespace(
        config=dict(_PUB_CONFIG), tracker=types.SimpleNamespace(tracks=[])
    )

    now_ms = int(time.time() * 1000)
    fix = {"hex": _AC_HEX, "flight": _CALLSIGN, "lat": _FIX_LAT, "lon": _FIX_LON, "alt_baro": 34000, "track": 187.0}
    _apply_synthetic_adsb({"data": {"timestamp": now_ms, "adsb": [fix]}}, _SENTINEL)
    assert state.adsb_fixes_by_reporter[_AC_HEX][_SENTINEL] is state.adsb_aircraft[_AC_HEX]

    # The public node's claim on that aircraft: the feed draws it with the
    # callsign from the shared record.
    claim_fix = {"lat": _FIX_LAT, "lon": _FIX_LON, "alt_baro": 34000, "gs": 420.0, "track": 187.0}
    state.known_claims[_AC_HEX] = deque(
        [
            {
                "node_id": _PUB,
                "delay_us": 120.0,
                "doppler_hz": -30.0,
                "pred_delay_us": 119.0,
                "pred_doppler_hz": -29.0,
                "ts_ms": now_ms,
                "adsb_fix": claim_fix | {"fix_ts_ms": now_ms - 1_000},
                "contested": False,
            }
        ],
        maxlen=state.KNOWN_CLAIMS_PER_HEX_MAX,
    )
    monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")

    from services.aircraft_feed import build_combined_aircraft_json
    from services.tasks.aircraft_flush import _flush_once

    _flush_once(build_combined_aircraft_json(types.SimpleNamespace(geolocated_tracks={}, config={})))
    _refresh_missed_detections([(_PUB, state.connected_nodes[_PUB])])
    assert state.latest_missed_detections[_PUB]["in_range"] == 1
    state.latest_analytics_bytes = orjson.dumps({"nodes": {pub_ref: {"metrics": {}, "trust": {}, "reputation": {}}}})
    return pub_ref


def test_no_public_read_names_the_node_that_reported_a_fix(monkeypatch):
    from routes import streaming
    from services.tasks.aircraft_flush import owner_bytes

    pub_ref = _public_world(monkeypatch)
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")

    api = _app_client()
    try:
        reads = {
            path: api.get(path)
            for path in (
                "/api/v1/solver/aircraft",
                "/api/v1/solver/aircraft?real_only=true",
                "/api/radar/data/aircraft.json",
                "/api/v1/ground-truth/aircraft",
                "/api/radar/leaderboard",
                "/api/health",
            )
        }
        bodies = {path: r.text for path, r in reads.items()}
        for path in ("/ws/aircraft", "/ws/aircraft/live"):
            with api.websocket_connect(path) as ws:
                bodies[path] = ws.receive_text()
    finally:
        api.close()
    bodies["owner feed"] = owner_bytes(state.latest_aircraft_json, {_PUB}).decode()
    bodies["missed detections"] = orjson.dumps(state.latest_missed_detections).decode()

    assert {path: r.status_code for path, r in reads.items()} == dict.fromkeys(reads, 200)
    # The controls: every feed carries the aircraft the sentinel reported, so
    # an absence below cannot pass by the fix never reaching the read.
    feeds = ("/api/v1/solver/aircraft", "/api/v1/solver/aircraft?real_only=true", "/api/radar/data/aircraft.json")
    for where in (*feeds, "/ws/aircraft", "/ws/aircraft/live", "owner feed"):
        assert _CALLSIGN in bodies[where], f"{where} lost the aircraft the fix describes"
    (row,) = reads["/api/radar/leaderboard"].json()["leaderboard"]
    assert (row["node_ref"], row["in_range"]) == (pub_ref, 1)
    for where, body in bodies.items():
        assert _SENTINEL not in body, f"{where} names the node that reported the fix"
