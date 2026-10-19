"""Every public read leaves a private node out, whoever asks, its owner included.

One private node is fed into every store a public read is built from: the
connected fleet, the analytics manager (pair overlaps, the blocked list and a
neighbour's penalty that quotes it), the associator, custody, the detection
archive, the aircraft frame, the default pipeline and the ADS-B truth. Two
public nodes beside it are the controls, so an absence cannot pass by an answer
being empty.

Each read is made twice, with no cookie and with the session of the private
node's own owner. Neither answer may carry the node's id, its ref, its name,
its custody identity or its coordinates, true or as the fuzz would publish
them, and the two answers are the same bytes wherever the route itself does
not stamp the time or answer a session with more columns. The owner reads the
node from /api/auth/me/* instead (routes/owner.py).

The route table is held to the list below: a public read added later fails
here until it is added to the sweep. Everything goes through a TestClient
without the lifespan, which would start the refresh loop and rewrite the
cached payloads built here.
"""

import asyncio
import hashlib
import re
from datetime import UTC, datetime, timedelta

import anyio
import orjson
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from retina_analytics.association import OverlapZone
from retina_custody.models import NodeIdentity
from retina_custody.packet_signer import canonicalize

from core import state
from core.users import get_current_user, require_admin
from main import app
from routes import radar, stats, streaming
from routes.owner import owned_node
from routes.test import require_admin_or_radar_key
from services import node_auth, node_refs, public_location, publication, storage, tracks_store
from services.node_auth import bearer_node
from services.parquet_writer import write_detections_parquet
from services.tasks import track_archive
from services.tasks.aircraft_flush import _flush_once
from services.tasks.analytics_refresh import _refresh_analytics_and_nodes
from services.track_writer import write_tracks_parquet
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import register_node_row, set_node_owner

PRIV = "ret1a2b3c4d"
PUB = "ret2b3c4d5e"
PUB2 = "retc0ffee00"

# Invented positions. PRIV is close enough to PUB for their footprints to
# overlap, so the manager pairs them, and every coordinate carries seven
# decimals so a number elsewhere cannot match one by chance.
PRIV_CONFIG = {
    "rx_lat": 52.2047139,
    "rx_lon": 0.1189347,
    "rx_alt_ft": 61.0,
    "tx_lat": 52.3197712,
    "tx_lon": 0.0741853,
    "tx_alt_ft": 812.0,
    "fc_hz": 195_936_000.0,
    "fs_hz": 2_000_000.0,
    "max_range_km": 60.0,
    "name": "Quarry Mast",
}
PUB_CONFIG = {
    "rx_lat": 52.3412,
    "rx_lon": 0.2577,
    "rx_alt_ft": 40.0,
    "tx_lat": 52.4103,
    "tx_lon": 0.3551,
    "tx_alt_ft": 900.0,
    "fc_hz": 195_936_000.0,
    "fs_hz": 2_000_000.0,
    "max_range_km": 60.0,
    "name": "Fen Tower",
}
PUB2_CONFIG = {**PUB_CONFIG, "rx_lat": 52.3011, "rx_lon": 0.4122, "name": "Dyke Road"}

PRIV_FINGERPRINT = "7e1d0c5b4a39f281"
PRIV_SERIAL = "SER-QM-20261013"

# Aircraft only the private node detects, one only the public node detects,
# and a solve the three made together.
PRIV_HEX, PUB_HEX, SOLVE_HEX = "a1c0de", "b2c0de", "c3c0de"
OWNER = "owner@example.com"


def _number_tokens(value: float) -> list[str]:
    """The ways a coordinate can be written: as stored, and rounded to six
    decimals, each one a whole number in the text rather than a prefix of one."""
    return [orjson.dumps(value).decode(), f"{round(value, 6):.6f}".rstrip("0")]


def _contains_number(body: str, token: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(token)}(?!\d)", body) is not None


# ── The world ────────────────────────────────────────────────────────────────


def _configured(node_id: str, config: dict) -> dict:
    return {"node_id": node_id, **config}


def _connect(node_id: str, config: dict) -> None:
    state.connected_nodes[node_id] = {
        "status": "active",
        "config": _configured(node_id, config),
        "config_hash": "0123456789abcdef",
        "last_heartbeat": "2026-10-13T09:00:00+00:00",
        "peer": "http",
        "is_synthetic": False,
        "capabilities": {},
    }


def _analyse(penalties_on) -> None:
    """Every node in the manager, with detections in one delay band so each pair
    overlaps; the private node blocked, and quoted by its neighbour's penalty."""
    analytics = state.node_analytics
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    for node_id, config in ((PRIV, PRIV_CONFIG), (PUB, PUB_CONFIG), (PUB2, PUB2_CONFIG)):
        analytics.register_node(node_id, _configured(node_id, config))
        for i in range(3):
            analytics.record_detection_frame(
                node_id,
                {"timestamp": now_ms + i, "delay": [20.0, 30.0, 40.0], "doppler": [5.0, -5.0, 1.0], "snr": [9.0] * 3},
            )
    analytics.reputations[PUB].evaluate_neighbour_consistency(0.0, 0.9, neighbour_id=PRIV)
    analytics.reputations[PRIV].blocked = True


def _associate() -> None:
    assoc = state.node_associator
    for node_id in (PRIV, PUB, PUB2):
        assoc.node_geometries[node_id] = object()
        assoc._pending_tracks[node_id] = [object()]
    for a, b in ((PRIV, PUB), (PUB, PUB2), (PRIV, PUB2)):
        assoc.overlap_zones[(a, b)] = OverlapZone(node_a_id=a, node_b_id=b, grid_points=[(52.3, 0.2, 1.0)] * 4)


def _custody(node_id: str, fingerprint: str, serial: str) -> None:
    state.node_identities[node_id] = NodeIdentity(
        node_id=node_id,
        public_key_pem="",
        public_key_fingerprint=fingerprint,
        serial_number=serial,
        signing_mode="software",
        registered_at="2026-10-01T00:00:00Z",
    )
    entry = {
        "node_id": node_id,
        "hour_utc": "2026-10-01T00:00:00Z",
        "prev_hash": "genesis",
        "detections_hash": "d" * 64,
        "n_detections": 1,
        "node_config_hash": "c" * 64,
        "firmware_version": "1.0",
        "timestamp_utc": "2026-10-01T00:00:00Z",
    }
    entry_hash = hashlib.sha256(canonicalize(entry)).hexdigest()
    state.chain_entries[node_id] = [
        {**entry, "entry_hash": entry_hash, "signature": "", "signing_mode": "software", "_verified": True}
    ]
    state.iq_commitments[node_id] = [{"node_id": node_id, "capture_id": f"cap-{serial}"}]


def _archive(base, node_id: str, config: dict) -> str:
    """One file of `node_id`'s detections, its geometry fanned into every row
    as the archive writer does, and the stored key."""
    ts = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
    frames = [{"timestamp": int(ts.timestamp() * 1000), "delay": [20.0], "doppler": [5.0], "snr": [9.0]}]
    key = write_detections_parquet(
        node_id=node_id, frames=frames, base_dir=base, write_ts=ts, node_cfg=_configured(node_id, config)
    )
    assert key is not None
    return key


def _tracks(base) -> None:
    """The tracks archive: the solve the three nodes made, which is withheld,
    and one the public nodes made alone."""
    ts = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)
    solve = int(ts.timestamp() * 1000)
    common = {"timestamp_ms": solve - 500, "lat": 52.30, "lon": 0.25, "alt_m": 9000.0}
    records = [
        {
            **common,
            "solve_ts_ms": solve,
            "n_nodes": 3,
            "contributing_node_ids": [PRIV, PUB, PUB2],
            "adsb_hex": PRIV_HEX,
        },
        {
            **common,
            "solve_ts_ms": solve + 1000,
            "n_nodes": 2,
            "contributing_node_ids": [PUB, PUB2],
            "adsb_hex": PUB_HEX,
        },
    ]
    assert write_tracks_parquet(records=records, base_dir=base, write_ts=ts + timedelta(seconds=30))


def _arc(node_id: str, lat: float, lon: float) -> dict:
    return {"node_id": node_id, "points": [[lat, lon], [lat + 0.01, lon + 0.01]], "delay_us": 20.0}


def _frame() -> dict:
    p = PRIV_CONFIG
    return {
        "now": 1_760_000_000.0,
        "messages": 3,
        "aircraft": [
            {"hex": PRIV_HEX, "lat": 52.25, "lon": 0.15, "node_id": PRIV, "position_source": "solver_single_node"},
            {"hex": PUB_HEX, "lat": 52.36, "lon": 0.29, "node_id": PUB, "position_source": "solver_single_node"},
            {
                "hex": SOLVE_HEX,
                "lat": 52.30,
                "lon": 0.25,
                "multinode": True,
                "n_nodes": 3,
                "position_source": "multinode_solve",
                "contributing_node_ids": [PRIV, PUB, PUB2],
            },
        ],
        # An ambiguity arc is an ellipse with the receiver at a focus, so the
        # private one is drawn through the receiver itself.
        "detection_arcs": [_arc(PRIV, p["rx_lat"], p["rx_lon"]), _arc(PUB, 52.36, 0.29)],
        "detecting_nodes": {PRIV_HEX: [PRIV], PUB_HEX: [PUB], SOLVE_HEX: [PRIV, PUB, PUB2]},
        "ground_truth": {},
        "ground_truth_meta": {},
        "anomaly_hexes": [],
    }


class World:
    """The private node's owner and every handle the sweep looks for."""

    def __init__(self, token: str, refs: dict[str, str], keys: dict[str, str]):
        self.token = token
        self.refs = refs
        self.keys = keys

    def published_key(self, node_id: str) -> str:
        return self.keys[node_id].replace(f"node_id={node_id}", f"node_ref={self.refs[node_id]}")

    def private_markers(self) -> list[str]:
        """What may not appear: identity strings, then coordinate tokens."""
        return [PRIV, self.refs[PRIV], PRIV_CONFIG["name"], PRIV_FINGERPRINT, PRIV_SERIAL, PRIV_HEX]

    def private_numbers(self) -> list[str]:
        p = PRIV_CONFIG
        fuzzed = public_location.public_latlon(p["rx_lat"], p["rx_lon"], PRIV)
        values = (p["rx_lat"], p["rx_lon"], p["tx_lat"], p["tx_lon"], *fuzzed)
        return [token for value in values for token in _number_tokens(value)]


@pytest.fixture
def api():
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


@pytest.fixture
def world(api, monkeypatch, tmp_path, penalties_on):
    real_sessions(monkeypatch)
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    public_location._reset_for_tests()
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
    # Another worker's TestTowerStats writes the shared file mid-sweep otherwise.
    monkeypatch.setattr(stats, "_STATS_PATH", str(tmp_path / "tower_stats.json"))

    token = sign_in(api, OWNER)
    refs = {
        PRIV: asyncio.run(register_node_row(PRIV, "private")),
        PUB: asyncio.run(register_node_row(PUB, "public")),
        PUB2: asyncio.run(register_node_row(PUB2, "public")),
    }
    asyncio.run(set_node_owner(PRIV, user_id_of(OWNER)))
    publication._reset_for_tests()
    node_refs._reset_for_tests()

    for node_id, config in ((PRIV, PRIV_CONFIG), (PUB, PUB_CONFIG), (PUB2, PUB2_CONFIG)):
        _connect(node_id, config)
    _analyse(penalties_on)
    _associate()

    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(archive))
    keys = {PRIV: _archive(archive, PRIV, PRIV_CONFIG), PUB: _archive(archive, PUB, PUB_CONFIG)}
    tracks = tmp_path / "tracks"
    monkeypatch.setattr(track_archive, "_TRACKS_DIR", str(tracks))
    tracks_store._reset_for_tests()
    _tracks(tracks)

    # The default pipeline is the one node /api/radar/status and receiver.json
    # describe; a deployment can point it at a real node, here the private one.
    for field in ("node_id", "rx_lat", "rx_lon"):
        monkeypatch.setitem(radar._default_pipeline.config, field, _configured(PRIV, PRIV_CONFIG)[field])

    # Truth beside both the private node and the public ones, which explain it.
    monkeypatch.setitem(state.external_adsb_cache, "4ca1e2", {"lat": 52.27, "lon": 0.2, "last_seen_ms": 1})
    state.latest_missed_detections.update(
        {nid: {"in_range": 4, "detected": 3, "missed": 1, "miss_rate": 0.25} for nid in (PRIV, PUB)}
    )

    _flush_once(_frame())
    _refresh_analytics_and_nodes()
    # After the refresh, which appends its own hourly entries to every connected
    # node's chain in a shape the verifier cannot read.
    _custody(PRIV, PRIV_FINGERPRINT, PRIV_SERIAL)
    _custody(PUB, "0f1e2d3c4b5a6978", "SER-FT-0001")
    return World(token, refs, keys)


# ── The reads ────────────────────────────────────────────────────────────────

# (path template, what the public node shows there, same bytes both ways). The
# template names the route-table entry the request reaches; {priv} and {pub}
# are the two nodes' refs, {priv_key} and {pub_key} their archive keys. A
# control of None is a route that names no node at all.
_READS = [
    ("/api/health", None, True),
    ("/api/stats/summary", None, True),
    ("/api/radar/data/receiver.json", None, True),
    ("/api/radar/data/aircraft.json", PUB_HEX, True),
    ("/api/radar/data/aircraft-live.json", PUB_HEX, True),
    ("/api/radar/status", None, True),
    ("/api/radar/nodes", "{pub}", True),
    # A session adds the miss counts, which is the one way it may differ.
    ("/api/radar/leaderboard", "{pub}", False),
    ("/api/radar/analytics", "{pub}", True),
    ("/api/radar/analytics?real_only=true", "{pub}", True),
    ("/api/radar/analytics/{priv}", None, True),
    ("/api/radar/analytics/{pub}", "{pub}", True),
    ("/api/radar/association/overlaps", "{pub}", True),
    ("/api/radar/association/status", "{pub}", True),
    ("/api/radar/accuracy", None, True),
    ("/api/radar/anomalies", None, True),
    ("/api/data/archive", "{pub}", True),
    ("/api/data/archive?node_ref={priv}", None, True),
    ("/api/data/archive/{priv_key}", None, True),
    ("/api/data/archive/{pub_key}", "{pub}", True),
    ("/api/data/tracks?from=2026-10-12T09:00:00Z&to=2026-10-12T10:00:00Z&fleet=all", "{pub}", True),
    ("/api/data/tracks/availability?from=2026-10-12&to=2026-10-12&fleet=all", None, True),
    ("/api/simulation/config", None, True),
    ("/api/custody/status", "{pub}", True),
    ("/api/custody/chain/{priv}", None, True),
    ("/api/custody/chain/{pub}", "{pub}", True),
    ("/api/custody/verify/{priv}", None, True),
    ("/api/custody/verify/{pub}", "{pub}", True),
    # These four stamp the time they answered.
    ("/api/v1/solver/aircraft", PUB_HEX, False),
    ("/api/v1/solver/aircraft?real_only=true", PUB_HEX, False),
    ("/api/v1/ground-truth/aircraft", None, False),
    ("/api/v1/ground-truth/real", "4ca1e2", False),
    ("/api/v1/docs", None, True),
    ("/", None, True),
    ("/openapi.json", None, True),
]

_SOCKETS = ["/ws/aircraft", "/ws/aircraft/live"]
_STREAM = "/api/radar/stream"


def _filled(template: str, world: World) -> str:
    return template.format(
        priv=world.refs[PRIV],
        pub=world.refs[PUB],
        priv_key=world.published_key(PRIV),
        pub_key=world.published_key(PUB),
    )


def _route_of(template: str) -> str:
    """The route-table path a read reaches."""
    path = template.split("?", 1)[0]
    path = re.sub(r"\{(priv|pub)\}", "{node_ref}", path)
    return re.sub(r"\{(priv|pub)_key\}", "{key:path}", path)


def _assert_withheld(world: World, body: str, where: str) -> None:
    for marker in world.private_markers():
        assert marker not in body, f"{where} names the private node: {marker}"
    for token in world.private_numbers():
        assert not _contains_number(body, token), f"{where} carries a private coordinate: {token}"


def _both(api, world: World, path: str):
    as_user(api, None)
    anonymous = api.get(path)
    as_user(api, world.token)
    try:
        owner = api.get(path)
    finally:
        as_user(api, None)
    return anonymous, owner


@pytest.mark.parametrize(("template", "control", "same"), _READS, ids=[r[0] for r in _READS])
def test_neither_the_owner_nor_anyone_else_is_shown_the_private_node(api, world, template, control, same):
    path = _filled(template, world)
    anonymous, owner = _both(api, world, path)

    for who, response in (("anonymous", anonymous), ("owner", owner)):
        _assert_withheld(world, response.text, f"{path} ({who})")
        if control is not None:
            assert response.status_code == 200, (who, response.text)
            assert _filled(control, world) in response.text, f"{path} ({who}) lost the public node"
    assert anonymous.status_code == owner.status_code
    if same:
        assert anonymous.content == owner.content


def test_a_private_ref_answers_as_an_unknown_one_for_its_owner_too(api, world):
    unknown = node_auth.mint_node_ref()
    for template in ("/api/radar/analytics/{ref}", "/api/custody/chain/{ref}", "/api/custody/verify/{ref}"):
        private = _both(api, world, template.format(ref=world.refs[PRIV]))
        missing = _both(api, world, template.format(ref=unknown))
        for got, expected in zip(private, missing, strict=True):
            assert (got.status_code, got.content) == (expected.status_code, expected.content) != (200, b"")


def test_the_owners_archive_listing_by_its_own_ref_is_empty(api, world):
    for response in _both(api, world, f"/api/data/archive?node_ref={world.refs[PRIV]}"):
        assert response.json() == {"files": [], "count": 0, "total": 0}


def test_the_private_archive_file_is_the_missing_files_404(api, world):
    missing = world.published_key(PUB).replace(world.refs[PUB], node_auth.mint_node_ref())
    for response in _both(api, world, f"/api/data/archive/{world.published_key(PRIV)}"):
        assert (response.status_code, response.content) == (404, api.get(f"/api/data/archive/{missing}").content)


def test_the_default_pipeline_on_a_private_node_names_no_position(api, world):
    for response in _both(api, world, "/api/radar/data/receiver.json"):
        assert (response.json()["lat"], response.json()["lon"]) == (None, None)
    for response in _both(api, world, "/api/radar/status"):
        assert (response.json()["config"]["rx_lat"], response.json()["config"]["rx_lon"]) == (None, None)


def test_the_shared_solve_stays_without_the_private_contributor(api, world):
    for response in _both(api, world, "/api/radar/data/aircraft.json"):
        solve = next(ac for ac in response.json()["aircraft"] if ac["hex"] == SOLVE_HEX)
        assert sorted(solve["contributing_node_refs"]) == sorted([world.refs[PUB], world.refs[PUB2]])


def test_the_analytics_cross_node_block_keeps_the_public_pair_only(api, world):
    """The manager pairs all three nodes and blocks the private one; what is
    published is the public pair, and a blocked list without it."""
    body = api.get("/api/radar/analytics").json()["cross_node"]
    pairs = [{p["node_a"], p["node_b"]} for p in body["pair_overlaps"]]
    assert pairs == [{world.refs[PUB], world.refs[PUB2]}]
    assert body["blocked_nodes"] == []
    assert body["coverage_suggestions"] == []


def test_a_neighbours_penalty_quotes_no_private_node(api, world):
    """The public node's own entry keeps its penalty, with the private
    neighbour taken out of the sentence rather than named by its ref."""
    for path in ("/api/radar/analytics", f"/api/radar/analytics/{world.refs[PUB]}"):
        body = api.get(path).json()
        entry = body["nodes"][world.refs[PUB]] if "nodes" in body else body
        reasons = [p["reason"] for p in entry["reputation"]["recent_penalties"]]
        assert reasons == ["Inconsistent with trusted neighbour [unpublished node] (overlap=0.00)"]


# ── The streams ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", _SOCKETS)
def test_neither_public_socket_shows_the_private_node(api, world, path):
    frames = []
    for token in (None, world.token):
        as_user(api, token)
        with api.websocket_connect(path) as ws:
            frames.append(ws.receive_text())
    as_user(api, None)

    for who, frame in zip(("anonymous", "owner"), frames, strict=True):
        _assert_withheld(world, frame, f"{path} ({who})")
        assert PUB_HEX in frame
    assert frames[0] == frames[1]


def _first_event(path: str, cookie: str | None) -> bytes:
    """The first event the stream sends, then the client goes away."""
    headers = [(b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", f"auth_token={cookie}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    chunks: list[bytes] = []

    async def run() -> None:
        first = anyio.Event()
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await first.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                first.set()

        with anyio.fail_after(5):
            await app(scope, receive, send)

    anyio.run(run)
    assert chunks, f"{path} sent nothing"
    return chunks[0]


def test_the_event_stream_shows_neither_caller_the_private_node(world):
    events = [_first_event(_STREAM, None), _first_event(_STREAM, world.token)]
    for who, event in zip(("anonymous", "owner"), events, strict=True):
        _assert_withheld(world, event.decode(), f"{_STREAM} ({who})")
        assert PUB_HEX.encode() in event
    assert events[0] == events[1]


# ── The route table ──────────────────────────────────────────────────────────

# A route depending on one of these answers nobody without credentials.
_GATES = (require_admin, require_admin_or_radar_key, get_current_user, owned_node, bearer_node)
# Routes that read the session cookie in the handler rather than through a
# dependency: the caller's own session and nodes, which is what they serve.
_SESSION_PREFIXES = ("/api/auth/", "/ws/aircraft/owner")


def _gated(dependant) -> bool:
    return any(dep.call in _GATES or _gated(dep) for dep in dependant.dependencies)


def _public_reads(routes) -> set[str]:
    found = set()
    for route in routes:
        if isinstance(route, APIRoute) and "GET" not in route.methods:
            continue
        if not isinstance(route, APIRoute | APIWebSocketRoute):
            continue
        if route.path.startswith(_SESSION_PREFIXES) or _gated(route.dependant):
            continue
        found.add(route.path)
    return found


def test_the_sweep_covers_every_public_read():
    swept = {_route_of(template) for template, _, _ in _READS} | set(_SOCKETS) | {_STREAM}
    assert _public_reads(app.routes) == swept


def test_the_route_table_walk_sees_a_new_public_read():
    from fastapi import APIRouter

    stray = APIRouter()

    @stray.get("/api/radar/new-surface")
    async def new_surface():
        return {}

    assert _public_reads([*app.routes, *stray.routes]) - _public_reads(app.routes) == {"/api/radar/new-surface"}


# ── The ADS-B truth ──────────────────────────────────────────────────────────
#
# Fetched in regions around the receivers, private ones included, so the
# aircraft around a private node far from any public one are a disc centred on
# it. While a private node has a position, only what the public nodes' own
# regions hold is served.

# 52.34 N, where a degree of longitude is about 68 km: NEAR_PUB is 100 km east
# of the public receiver, PAST_PUB 200 km, beyond its 150 km margin.
NEAR_PUB = {"lat": 52.3412, "lon": 1.7277, "last_seen_ms": 1}
PAST_PUB = {"lat": 52.3412, "lon": 3.2077, "last_seen_ms": 1}
FAR_AWAY = {"lat": 40.4168, "lon": -3.7038, "last_seen_ms": 1}
# Beside the far private receiver.
BESIDE_FAR = {"lat": 47.2034567, "lon": 8.8154321, "last_seen_ms": 1}


@pytest.fixture
def truth(api, monkeypatch):
    """PUB public, PRIV private and its owner signed in; the caller places PRIV
    and says how it registered."""
    real_sessions(monkeypatch)
    token = sign_in(api, OWNER)

    def _place(rx_lat: float, rx_lon: float, publication_choice: str = "private") -> str:
        asyncio.run(register_node_row(PRIV, publication_choice))
        asyncio.run(register_node_row(PUB, "public"))
        asyncio.run(set_node_owner(PRIV, user_id_of(OWNER)))
        publication._reset_for_tests()
        node_refs._reset_for_tests()
        _connect(PUB, PUB_CONFIG)
        _connect(PRIV, {**PRIV_CONFIG, "rx_lat": rx_lat, "rx_lon": rx_lon})
        for icao, entry in (("4ca001", NEAR_PUB), ("4ca002", PAST_PUB), ("4ca003", FAR_AWAY), ("4ca004", BESIDE_FAR)):
            monkeypatch.setitem(state.external_adsb_cache, icao, dict(entry))
        return token

    return _place


def _truth_hexes(api, token: str | None) -> set[str]:
    as_user(api, token)
    try:
        return {ac["hex"] for ac in api.get("/api/v1/ground-truth/real").json()["aircraft"]}
    finally:
        as_user(api, None)


def test_the_truth_around_a_lone_private_node_is_withheld_from_everyone(api, truth):
    token = truth(47.1234567, 8.7654321)
    assert _truth_hexes(api, None) == _truth_hexes(api, token) == {"4ca001"}


def test_the_truth_a_private_node_reaches_past_the_public_ones_is_withheld(api, truth):
    """120 km east of the public receiver: its region reaches 200 km east, the
    public one's does not."""
    token = truth(52.3412, 2.0077)
    assert _truth_hexes(api, None) == _truth_hexes(api, token) == {"4ca001"}


def test_with_no_private_node_every_aircraft_is_served(api, truth):
    truth(47.1234567, 8.7654321, "public")
    assert _truth_hexes(api, None) == {"4ca001", "4ca002", "4ca003", "4ca004"}


def test_a_private_node_with_no_position_withholds_nothing(api, truth):
    """Nothing was fetched for it, so there is nothing of it to withhold."""
    truth(0.0, 0.0)
    assert _truth_hexes(api, None) == {"4ca001", "4ca002", "4ca003", "4ca004"}


def test_before_the_policy_is_known_no_truth_is_served(api, truth, monkeypatch):
    truth(47.1234567, 8.7654321, "public")

    def fail():
        raise RuntimeError("database unavailable")

    publication._reset_for_tests()
    monkeypatch.setattr(publication, "_query", fail)
    assert _truth_hexes(api, None) == set()
