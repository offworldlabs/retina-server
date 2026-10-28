"""The owner's data routes under /api/auth/me: analytics, the heartbeat report,
the configuration history, the events, the SNR trend and the archive.

What each view holds is pinned against the services by
tests/test_owner_views.py, tests/test_snr_series.py and
tests/test_archive_scope.py. These pin the routes: who may ask, the 404 that
cannot tell somebody else's node from an unknown one, the bounds, and the
shapes on the wire. Signed in for real throughout (tests/owner_helpers.py), as
tests/test_owner_routes.py is, and every archive read goes to a per-test
directory, never the real coverage_data.
"""

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote

import anyio
import numpy as np
import orjson
import pytest
from sqlalchemy import delete

from core import state
from core.nodes import Node, NodeEvent, NodeReport
from core.users import async_session_maker
from main import app
from routes.node_schemas import HeartbeatRequest
from services import node_auth, node_config, node_refs, publication, storage
from services import public_location as pl
from services import snr_series as ss
from services.node_config_store import upsert_config
from services.node_report_store import record_report
from services.parquet_writer import write_detections_parquet
from tests.node_helpers import register_test_node
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import own, register_node_row, set_node_owner

MINE = "ret0a1b2c3d"  # ada's, private
ALSO_MINE = "retc0ffee00"  # ada's, public
THEIRS = "ret9f8e7d6c"  # bob's, private
OPEN = "ret0badcafe"  # public, nobody's
UNKNOWN = "ret0123abcd"  # never registered

CONFIG = {
    "rx_lat": 51.42,
    "rx_lon": -0.91,
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

OWNER_SUMMARY_KEYS = {
    "node_id",
    "node_ref",
    "trust",
    "detection_area",
    "metrics",
    "reputation",
    "coverage_map",
    "empirical_coverage",
    "node_bias",
}
NODE_REPORT_KEYS = {
    "received_at",
    "state",
    "state_since",
    "uptime_s",
    "config_version",
    "health",
    "versions",
    "errors",
}
CONFIG_VERSION_KEYS = {
    "version",
    "created_at",
    "superseded_at",
    "fc_hz",
    "fs_hz",
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "tx_callsign",
    "beam_width_deg",
    "beam_azimuth_deg",
    "max_range_km",
    "cpi_s",
    "delay_tolerance_us",
    "doppler_tolerance_hz",
}

# When ada and bob took their nodes: before every record the tests below write.
# How the tenure bounds what an owner reads is tests/test_owner_tenure.py's.
OWNED_SINCE = datetime(2026, 1, 1, tzinfo=UTC)

NOT_FOUND = b'{"detail":"Node not found"}'
NO_FILE = b'{"detail":"Archive file not found"}'
EMPTY_LISTING = {"files": [], "count": 0, "total": 0}

# The eight routes this file adds and the five tests/test_owner_routes.py
# covers, as (method, path, body). {} is a node of the caller's.
EVERY_ROUTE = [
    ("GET", "/api/auth/me/nodes", None),
    ("GET", "/api/auth/me/nodes/{}", None),
    ("PATCH", "/api/auth/me/nodes/{}", {"name": "Shed"}),
    ("DELETE", "/api/auth/me/nodes/{}/claim", None),
    ("GET", "/api/auth/me/aircraft", None),
    ("GET", "/api/auth/me/analytics", None),
    ("GET", "/api/auth/me/nodes/{}/analytics", None),
    ("GET", "/api/auth/me/nodes/{}/report", None),
    ("GET", "/api/auth/me/nodes/{}/config-history", None),
    ("GET", "/api/auth/me/nodes/{}/events", None),
    ("GET", "/api/auth/me/nodes/{}/snr", None),
    ("GET", "/api/auth/me/archive", None),
    ("GET", "/api/auth/me/archive/year=2026/month=08/day=27/node_ref=x/part-120000-0.parquet", None),
]

# The new routes about one node, each with the query values it refuses for a
# node of the caller's.
PER_NODE = {
    "analytics": [],
    "report": [],
    "config-history": [{"limit": 0}, {"limit": 201}, {"limit": "many"}],
    "events": [{"limit": 0}, {"limit": 501}, {"limit": -1}],
    "snr": [{"hours": 0}, {"hours": 73}, {"hours": 2.5}],
}


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


@pytest.fixture
def fuzz_on(monkeypatch):
    """Public coordinates displaced, as every deployment runs: the owner's view
    must be the truth regardless."""
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", "test-salt-for-owner-data-routes")
    pl._reset_for_tests()
    assert pl.fuzz_enabled()
    yield
    pl._reset_for_tests()


@pytest.fixture(autouse=True)
def archive_dir(tmp_path, monkeypatch):
    """The detection archive, per test: the SNR trend and the archive routes read it."""
    base = tmp_path / "archive"
    base.mkdir()
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(base))
    return base


@pytest.fixture(autouse=True)
def _fresh_snr_cache():
    ss._reset_for_tests()
    yield
    ss._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_reports_left():
    """Delete the node_reports rows a test wrote: conftest's _clean_db does not,
    and the table's foreign key to nodes would fail the next test's cleanup."""
    yield

    async def _delete():
        async with async_session_maker() as session:
            await session.execute(delete(NodeReport))
            await session.commit()

    asyncio.run(_delete())


@pytest.fixture
def ada(client, real):
    """The owner: (token, user id)."""
    token = sign_in(client, "ada@example.com")
    return token, user_id_of("ada@example.com")


@pytest.fixture
def bob(client, real):
    """Somebody else, who owns THEIRS: (token, user id)."""
    token = sign_in(client, "bob@example.com")
    return token, user_id_of("bob@example.com")


@pytest.fixture
def fleet(client, ada, bob):
    """MINE private and ALSO_MINE public, both ada's; THEIRS private and bob's;
    OPEN public and nobody's. Ada is signed in on the client. Returns the refs."""
    asyncio.run(register_node_row(MINE, "private"))
    asyncio.run(register_node_row(THEIRS, "private"))
    own(MINE, ada[1], since=OWNED_SINCE)
    own(ALSO_MINE, ada[1], since=OWNED_SINCE)
    own(THEIRS, bob[1], since=OWNED_SINCE)
    asyncio.run(register_node_row(OPEN))
    node_refs._reset_for_tests()
    publication._reset_for_tests()
    as_user(client, ada[0])
    return {node_id: _ref(node_id) for node_id in (MINE, ALSO_MINE, THEIRS, OPEN)}


def _ref(node_id: str) -> str:
    async def _go():
        async with async_session_maker() as session:
            return (await session.get(Node, node_id)).node_ref

    return asyncio.run(_go())


def _same_404(*responses, body: bytes = NOT_FOUND) -> None:
    first = responses[0]
    for r in responses:
        assert r.status_code == 404
        assert r.content == body
        assert r.headers["content-type"] == first.headers["content-type"]
        assert r.headers["content-length"] == first.headers["content-length"]


def _analyse(node_id: str) -> None:
    """Register `node_id` as an entry point does, and give it analytics with numpy in them."""
    geometry = ("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft", "fc_hz", "fs_hz", "max_range_km")
    register_test_node(node_id, {"node_id": node_id, **{k: CONFIG[k] for k in geometry}})
    state.node_analytics.record_detection_frame(
        node_id,
        {
            "delay": np.array([12.5, 30.25, 41.75]),
            "doppler": np.array([-40.0, 85.5, 12.0]),
            "snr": np.array([14.2, 9.8, 11.1]),
            "timestamp": 1_760_000_000_000,
        },
    )


# ── Who may ask ──────────────────────────────────────────────────────────────


class TestWhoMayAsk:
    @pytest.mark.parametrize("method, path, body", EVERY_ROUTE, ids=[f"{m} {p}" for m, p, _ in EVERY_ROUTE])
    def test_a_signed_out_caller_is_401(self, client, fleet, method, path, body):
        as_user(client, None)
        r = client.request(method, path.format(MINE), json=body)
        assert (r.status_code, r.json()) == (401, {"detail": "Not authenticated"})

    @pytest.mark.parametrize(
        "path, params",
        [
            ("/api/auth/me/nodes/{}/config-history", {"limit": 0}),
            ("/api/auth/me/nodes/{}/events", {"limit": 501}),
            ("/api/auth/me/nodes/{}/snr", {"hours": 73}),
            ("/api/auth/me/archive", {"limit": 0}),
        ],
    )
    def test_signing_in_is_asked_for_before_a_query_value_is_judged(self, client, fleet, path, params):
        as_user(client, None)
        r = client.get(path.format(MINE), params=params)
        assert (r.status_code, r.json()) == (401, {"detail": "Not authenticated"})

    @pytest.mark.parametrize("route", sorted(PER_NODE))
    def test_somebody_elses_node_answers_exactly_as_an_unknown_one(self, client, fleet, route):
        path = "/api/auth/me/nodes/{}/" + route
        answers = [client.get(path.format(node_id)) for node_id in (THEIRS, UNKNOWN)]
        for params in PER_NODE[route]:
            answers += [client.get(path.format(node_id), params=params) for node_id in (THEIRS, UNKNOWN)]
        _same_404(*answers)
        # The owner is answered, so the 404 is the ownership check's.
        assert client.get(path.format(MINE)).status_code == 200
        # And each refused value is refused for a node of the caller's, so the
        # 404s above won over a real 422 rather than meeting none.
        for params in PER_NODE[route]:
            assert client.get(path.format(MINE), params=params).status_code == 422, params

    def test_bob_is_answered_about_his_own_node_and_not_about_ada(self, client, fleet, bob):
        as_user(client, bob[0])
        assert client.get(f"/api/auth/me/nodes/{THEIRS}/report").status_code == 200
        _same_404(client.get(f"/api/auth/me/nodes/{MINE}/report"), client.get(f"/api/auth/me/nodes/{UNKNOWN}/report"))


# ── Analytics ────────────────────────────────────────────────────────────────


class TestAnalytics:
    def test_the_map_holds_the_private_node_by_its_ref_unmoved(self, client, fleet, fuzz_on):
        _analyse(MINE)
        _analyse(THEIRS)
        _analyse(OPEN)
        # The control: a public surface would have moved this receiver.
        assert pl.public_latlon(CONFIG["rx_lat"], CONFIG["rx_lon"], MINE) != (CONFIG["rx_lat"], CONFIG["rx_lon"])

        r = client.get("/api/auth/me/analytics")

        assert r.status_code == 200
        assert set(r.json()) == {"nodes"}
        assert set(r.json()["nodes"]) == {fleet[MINE]}
        summary = r.json()["nodes"][fleet[MINE]]
        assert set(summary) == OWNER_SUMMARY_KEYS
        assert (summary["node_id"], summary["node_ref"]) == (MINE, fleet[MINE])
        assert summary["detection_area"]["rx"] == {"lat": CONFIG["rx_lat"], "lon": CONFIG["rx_lon"]}
        assert b"location_uncertainty_km" not in r.content
        # Nothing of bob's node, or of the public one the caller does not own.
        for node_id in (THEIRS, OPEN):
            assert node_id not in r.text
            assert fleet[node_id] not in r.text
        assert orjson.loads(r.content) == r.json()

    def test_one_node_answers_as_its_entry_in_the_map(self, client, fleet, fuzz_on):
        _analyse(MINE)

        r = client.get(f"/api/auth/me/nodes/{MINE}/analytics")

        assert r.status_code == 200
        assert r.json() == client.get("/api/auth/me/analytics").json()["nodes"][fleet[MINE]]
        assert set(r.json()) == OWNER_SUMMARY_KEYS
        assert r.json()["detection_area"]["rx"] == {"lat": CONFIG["rx_lat"], "lon": CONFIG["rx_lon"]}
        assert b"location_uncertainty_km" not in r.content

    def test_a_node_with_no_analytics_yet_is_null(self, client, fleet):
        _analyse(MINE)

        r = client.get(f"/api/auth/me/nodes/{ALSO_MINE}/analytics")

        assert (r.status_code, r.content) == (200, b"null")
        assert set(client.get("/api/auth/me/analytics").json()["nodes"]) == {fleet[MINE]}

    def test_a_caller_with_nothing_analysed_gets_an_empty_map(self, client, fleet):
        assert client.get("/api/auth/me/analytics").json() == {"nodes": {}}
        as_user(client, sign_in(client, "carol@example.com"))
        _analyse(OPEN)
        assert client.get("/api/auth/me/analytics").json() == {"nodes": {}}


# ── The heartbeat report ─────────────────────────────────────────────────────


async def _beat(node_id: str, now: datetime, **beat) -> None:
    async with async_session_maker() as session:
        async with session.begin():
            await record_report(session, node_id, HeartbeatRequest.model_validate(beat), now)


class TestReport:
    def test_null_before_the_first_beat(self, client, fleet):
        r = client.get(f"/api/auth/me/nodes/{MINE}/report")
        assert (r.status_code, r.content) == (200, b"null")

    def test_the_last_beat_with_exactly_the_contracts_keys(self, client, fleet):
        at = datetime(2026, 10, 12, 9, 30, 15, tzinfo=UTC)
        boot = {"boot_id": "b00t0001", "config_version": 2}
        asyncio.run(_beat(MINE, at, state="starting", uptime_s=5, errors=["sdr not found"], **boot))
        asyncio.run(
            _beat(
                MINE,
                at + timedelta(minutes=1),
                state="streaming",
                uptime_s=65,
                health={"cpu_pct": 12.5, "disk_free_mb": 2048, "temp_c": 48.0, "blah2": "up"},
                versions={"retina_node": "1.4.2"},
                **boot,
            )
        )

        r = client.get(f"/api/auth/me/nodes/{MINE}/report")

        assert r.status_code == 200
        report = r.json()
        assert set(report) == NODE_REPORT_KEYS
        assert (report["state"], report["uptime_s"], report["config_version"]) == ("streaming", 65, 2)
        assert report["received_at"] == "2026-10-12T09:31:15Z"
        assert report["state_since"] == "2026-10-12T09:31:15Z"
        assert report["health"]["cpu_pct"] == 12.5
        assert report["versions"]["retina_node"] == "1.4.2"
        assert report["errors"] == [{"at": "2026-10-12T09:30:15Z", "message": "sdr not found"}]
        assert orjson.loads(r.content) == report

    def test_another_nodes_beat_is_not_this_ones(self, client, fleet):
        asyncio.run(
            _beat(ALSO_MINE, datetime.now(UTC), state="streaming", uptime_s=5, boot_id="b00t0002", config_version=None)
        )
        assert client.get(f"/api/auth/me/nodes/{MINE}/report").content == b"null"


# ── Configuration history ────────────────────────────────────────────────────


async def _configure(node_id: str, *frequencies: float) -> None:
    """One configuration version per frequency, as registration and PUT write them."""
    for fc_hz in frequencies:
        config = node_config.validate_config({**CONFIG, "fc_hz": fc_hz})
        async with async_session_maker() as session:
            async with session.begin():
                await upsert_config(session, node_id, config)


class TestConfigHistory:
    @pytest.fixture
    def versions(self, fleet):
        asyncio.run(_configure(MINE, 570e6, 571e6, 572e6))
        asyncio.run(_configure(ALSO_MINE, 490e6))

    def test_newest_first_with_exactly_the_contracts_keys(self, client, versions):
        r = client.get(f"/api/auth/me/nodes/{MINE}/config-history")

        assert r.status_code == 200
        assert set(r.json()) == {"versions"}
        history = r.json()["versions"]
        assert [(v["version"], v["fc_hz"]) for v in history] == [(3, 572e6), (2, 571e6), (1, 570e6)]
        assert all(set(v) == CONFIG_VERSION_KEYS for v in history)
        assert history[0]["superseded_at"] is None
        assert all(v["superseded_at"].endswith("Z") for v in history[1:])
        assert all(v["created_at"].endswith("Z") for v in history)

    @pytest.mark.parametrize("limit, versions_shown", [(1, [3]), (2, [3, 2]), (200, [3, 2, 1])])
    def test_a_limit_keeps_the_newest(self, client, versions, limit, versions_shown):
        r = client.get(f"/api/auth/me/nodes/{MINE}/config-history", params={"limit": limit})
        assert r.status_code == 200
        assert [v["version"] for v in r.json()["versions"]] == versions_shown

    @pytest.mark.parametrize("limit", [0, 201, -1, "", "ten"])
    def test_a_limit_outside_1_to_200_is_422(self, client, versions, limit):
        r = client.get(f"/api/auth/me/nodes/{MINE}/config-history", params={"limit": limit})
        assert r.status_code == 422

    def test_a_node_with_no_versions_has_an_empty_history(self, client, fleet):
        assert client.get(f"/api/auth/me/nodes/{MINE}/config-history").json() == {"versions": []}


# ── Events ───────────────────────────────────────────────────────────────────

ADMIN_EMAIL = "staff@example.com"


def _record_events(node_id: str, rows: list[tuple[str, int | None, str]]) -> None:
    async def _go():
        start = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
        async with async_session_maker() as session:
            async with session.begin():
                for i, (kind, epoch, actor) in enumerate(rows):
                    session.add(
                        NodeEvent(
                            node_id=node_id,
                            kind=kind,
                            epoch=epoch,
                            actor=actor,
                            occurred_at=start + timedelta(minutes=i),
                        )
                    )
                    # One flush per row, so ids follow the list order.
                    await session.flush()

    asyncio.run(_go())


class TestEvents:
    @pytest.fixture
    def recorded(self, fleet, ada, bob):
        """One row per shape a writer stores an actor in, oldest first, with the
        role each reads as to ada."""
        rows = [
            ("registered", 1, bob[1], "owner"),
            ("graduated", 1, f"admin:{ADMIN_EMAIL}", "admin"),
            ("renamed", None, ada[1], "you"),
            ("returned_to_probation", 2, "poller", "system"),
            # The column is NOT NULL, so an actor nobody named is the empty string.
            ("made_public", None, "", "system"),
        ]
        _record_events(MINE, [row[:3] for row in rows])
        return rows

    def test_newest_first_with_each_actor_as_a_role(self, client, recorded):
        r = client.get(f"/api/auth/me/nodes/{MINE}/events")

        assert r.status_code == 200
        assert r.json() == {
            "events": [
                {"kind": kind, "at": f"2026-10-12T08:{i:02d}:00Z", "actor": role, "epoch": epoch}
                for i, (kind, epoch, _actor, role) in reversed(list(enumerate(recorded)))
            ]
        }

    def test_no_stored_actor_reaches_the_wire(self, client, recorded, bob):
        body = client.get(f"/api/auth/me/nodes/{MINE}/events").content
        assert ADMIN_EMAIL.encode() not in body
        assert b"example.com" not in body
        assert bob[1].encode() not in body
        assert b"admin:" not in body
        assert b"poller" not in body

    def test_the_roles_are_the_callers_own_reading(self, client, recorded, ada, bob):
        """Once the node changes hands, what was recorded under ada is hers
        and bob reads none of it; what he does himself is "you" to him."""
        asyncio.run(set_node_owner(MINE, bob[1]))
        as_user(client, bob[0])

        assert client.get(f"/api/auth/me/nodes/{MINE}/events").json() == {"events": []}
        assert client.patch(f"/api/auth/me/nodes/{MINE}", json={"name": "Bob's"}).status_code == 200
        events = client.get(f"/api/auth/me/nodes/{MINE}/events").json()["events"]

        assert [(e["kind"], e["actor"]) for e in events] == [("renamed", "you")]
        as_user(client, ada[0])
        _same_404(client.get(f"/api/auth/me/nodes/{MINE}/events"), client.get(f"/api/auth/me/nodes/{UNKNOWN}/events"))

    @pytest.mark.parametrize("limit, kinds", [(1, ["made_public"]), (2, ["made_public", "returned_to_probation"])])
    def test_a_limit_keeps_the_newest(self, client, recorded, limit, kinds):
        r = client.get(f"/api/auth/me/nodes/{MINE}/events", params={"limit": limit})
        assert [e["kind"] for e in r.json()["events"]] == kinds

    def test_500_is_the_most_that_may_be_asked_for(self, client, recorded):
        r = client.get(f"/api/auth/me/nodes/{MINE}/events", params={"limit": 500})
        assert r.status_code == 200
        assert len(r.json()["events"]) == len(recorded)

    @pytest.mark.parametrize("limit", [0, 501, -5, "all"])
    def test_a_limit_outside_1_to_500_is_422(self, client, recorded, limit):
        assert client.get(f"/api/auth/me/nodes/{MINE}/events", params={"limit": limit}).status_code == 422


# ── SNR ──────────────────────────────────────────────────────────────────────


def _frame(ts_ms: int, snr: list[float]) -> dict:
    return {
        "timestamp": ts_ms,
        "delay": [10.0 + i for i in range(len(snr))],
        "doppler": [-50.0 + i for i in range(len(snr))],
        "snr": snr,
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


class TestSnr:
    def test_a_node_with_no_detections_answers_an_empty_day(self, client, fleet):
        r = client.get(f"/api/auth/me/nodes/{MINE}/snr")
        assert r.status_code == 200
        assert r.json() == {"hours": 24, "bucket_s": 300, "through_ms": None, "points": []}

    @pytest.mark.parametrize("hours", [1, 72])
    def test_the_window_asked_for_is_the_one_answered(self, client, fleet, hours):
        r = client.get(f"/api/auth/me/nodes/{MINE}/snr", params={"hours": hours})
        assert r.status_code == 200
        assert r.json()["hours"] == hours

    @pytest.mark.parametrize("hours", [0, 73, -1, 2.5, "day"])
    def test_hours_outside_1_to_72_is_422(self, client, fleet, hours):
        assert client.get(f"/api/auth/me/nodes/{MINE}/snr", params={"hours": hours}).status_code == 422

    def test_an_archived_file_in_the_window_is_the_series(self, client, fleet, archive_dir):
        now_ms = int(time.time() * 1000)
        minute = 60_000
        mine = [
            _frame(now_ms - 26 * 60 * minute, [99.0]),  # before the window
            _frame(now_ms - 180 * minute, [10.0, 20.0]),
            _frame(now_ms - 60 * minute, [12.0]),
            _frame(now_ms - 2 * minute, [8.0, 9.0, 10.0]),
        ]
        write_ts = datetime.now(UTC)
        assert write_detections_parquet(node_id=MINE, frames=mine, base_dir=archive_dir, write_ts=write_ts)
        # Bob's detections, in the same window, are not ada's series.
        theirs = [_frame(now_ms - 30 * minute, [50.0] * 7)]
        assert write_detections_parquet(node_id=THEIRS, frames=theirs, base_dir=archive_dir, write_ts=write_ts)

        r = client.get(f"/api/auth/me/nodes/{MINE}/snr")

        assert r.status_code == 200
        series = r.json()
        assert (series["hours"], series["bucket_s"]) == (24, 300)
        assert series["through_ms"] == now_ms - 2 * minute
        points = series["points"]
        assert points
        assert [p["t_ms"] for p in points] == sorted(p["t_ms"] for p in points)
        assert all(p["t_ms"] % 300_000 == 0 for p in points)
        assert [p["count"] for p in points] == [2, 1, 3]
        assert [p["median_db"] for p in points] == [15.0, 12.0, 9.0]
        assert all(set(p) == {"t_ms", "median_db", "p90_db", "count"} for p in points)
        assert orjson.loads(r.content) == series


# ── The archive ──────────────────────────────────────────────────────────────

# (node, write time on 2026-08-27, the delay its one detection carries). The
# delay marks each file, so an answer can be read back by it.
_WRITES = [
    (MINE, (10, 0, 0), 1.0),
    (MINE, (11, 0, 0), 2.0),
    (ALSO_MINE, (12, 0, 0), 3.0),
    (THEIRS, (13, 0, 0), 4.0),
    (OPEN, (14, 0, 0), 5.0),
]


@pytest.fixture
def files(fleet, archive_dir):
    """Every node's files: SimpleNamespace(refs, stored, published), the last
    two mapping each file's marker to its key on disk and the key it goes out
    under."""
    stored, published = {}, {}
    for node_id, (h, m, s), marker in _WRITES:
        ts = datetime(2026, 8, 27, h, m, s, tzinfo=UTC)
        frames = [{"timestamp": int(ts.timestamp() * 1000), "delay": [marker], "doppler": [12.5], "snr": [9.0]}]
        key = write_detections_parquet(node_id=node_id, frames=frames, base_dir=archive_dir, write_ts=ts)
        assert key is not None
        stored[marker] = key
        published[marker] = key.replace(f"node_id={node_id}", f"node_ref={fleet[node_id]}")
    return SimpleNamespace(refs=fleet, stored=stored, published=published)


def _markers(files, listing: dict) -> list[float]:
    by_key = {key: marker for marker, key in files.published.items()}
    return sorted(by_key[f["key"]] for f in listing["files"])


def _raw_get(client, raw_path: str, token: str):
    """GET `raw_path` exactly as written, a literal `..` included, as a client
    that sends the path as-is would. The test client folds `..` segments away
    before sending, so the request goes to the app as a server hands it over:
    the path percent-decoded, nothing else done to it. Answers (status, body).

    The body is handed over once and the client then stays connected until the
    response is complete, as the test client's own transport does: the
    middleware reads the channel again and refuses a second request message."""
    sent: list[dict] = []

    async def call():
        complete = anyio.Event()
        requested = False

        async def receive():
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await complete.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete.set()

        await app(scope, receive, send)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"cookie", f"auth_token={token}".encode())],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    client.portal.call(call)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


class TestArchiveListing:
    def test_it_lists_the_callers_files_private_ones_included_under_their_refs(self, client, files):
        r = client.get("/api/auth/me/archive")

        assert r.status_code == 200
        assert _markers(files, r.json()) == [1.0, 2.0, 3.0]
        assert r.json()["count"] == r.json()["total"] == 3
        assert f"node_ref={files.refs[MINE]}" in r.text
        assert "node_id=" not in r.text
        for node_id in (MINE, ALSO_MINE, THEIRS, OPEN):
            assert node_id not in r.text
        # Bob's private file, and the public one the caller does not own.
        assert files.refs[THEIRS] not in r.text
        assert files.refs[OPEN] not in r.text

    def test_the_public_archive_does_list_the_public_file_it_leaves_out(self, client, files):
        """The control: the public node's file is there to be listed, so its
        absence above is the scope's doing."""
        as_user(client, None)
        assert 5.0 in _markers(files, client.get("/api/data/archive").json())

    def test_the_filter_takes_the_callers_private_ref(self, client, files):
        r = client.get("/api/auth/me/archive", params={"node_ref": files.refs[MINE]})
        assert _markers(files, r.json()) == [1.0, 2.0]
        assert all(f"/node_ref={files.refs[MINE]}/" in f["key"] for f in r.json()["files"])

    @pytest.mark.parametrize("asked", ["bob's private node", "a public node", "unknown", "my node's id"])
    def test_a_ref_that_is_not_the_callers_lists_nothing(self, client, files, asked):
        value = {
            "bob's private node": files.refs[THEIRS],
            "a public node": files.refs[OPEN],
            "unknown": node_auth.mint_node_ref(),
            "my node's id": MINE,
        }[asked]
        r = client.get("/api/auth/me/archive", params={"node_ref": value})
        assert (r.status_code, r.json()) == (200, EMPTY_LISTING)

    @pytest.mark.parametrize("query", [{}, {"date": "2026/08/27"}, {"limit": 1}, {"limit": 1, "offset": 1}])
    def test_an_owned_public_node_lists_as_the_public_archive_lists_it(self, client, files, query):
        params = {"node_ref": files.refs[ALSO_MINE], **query}
        mine = client.get("/api/auth/me/archive", params=params)
        as_user(client, None)
        theirs = client.get("/api/data/archive", params=params)
        assert mine.status_code == theirs.status_code == 200
        assert mine.content == theirs.content

    def test_a_page_is_cut_from_the_callers_files(self, client, files):
        pages = [client.get("/api/auth/me/archive", params={"limit": 2, "offset": o}).json() for o in (0, 2)]
        assert [p["count"] for p in pages] == [2, 1]
        assert {p["total"] for p in pages} == {3}
        assert sorted(m for p in pages for m in _markers(files, p)) == [1.0, 2.0, 3.0]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}, {"limit": "all"}])
    def test_the_public_archives_bounds_hold(self, client, files, params):
        assert client.get("/api/auth/me/archive", params=params).status_code == 422
        as_user(client, None)
        assert client.get("/api/data/archive", params=params).status_code == 422

    def test_500_files_may_be_asked_for(self, client, files):
        assert client.get("/api/auth/me/archive", params={"limit": 500}).status_code == 200


class TestArchiveDownload:
    def test_the_owner_downloads_their_private_file(self, client, files):
        r = client.get(f"/api/auth/me/archive/{files.published[2.0]}")

        assert r.status_code == 200
        body = r.json()
        assert body["node_ref"] == files.refs[MINE]
        assert "node_id" not in body
        assert MINE not in r.text
        assert body["detections"][0]["delay"] == [2.0]

    def test_an_owned_public_file_is_the_public_archives_body(self, client, files):
        mine = client.get(f"/api/auth/me/archive/{files.published[3.0]}")
        as_user(client, None)
        theirs = client.get(f"/api/data/archive/{files.published[3.0]}")
        assert mine.status_code == theirs.status_code == 200
        assert mine.content == theirs.content

    def test_a_key_that_is_not_the_callers_is_404_as_an_unknown_key_is(self, client, files, archive_dir):
        name = files.stored[2.0].rsplit("/", 1)[-1]
        # A real copy of ada's file just outside the archive, so a key that
        # climbs out to it is refused by the path check, not by its absence.
        beside = archive_dir.parent / f"node_id={MINE}"
        beside.mkdir()
        shutil.copy(archive_dir / files.stored[2.0], beside / name)
        day = "year=2026/month=08/day=27"
        keys = {
            "an unknown ref": f"{day}/node_ref={node_auth.mint_node_ref()}/{name}",
            "bob's private file": files.published[4.0],
            "a public node's file": files.published[5.0],
            "my file under its stored key": files.stored[2.0],
            "an encoded climb out under my ref": f"%2e%2e/node_ref={files.refs[MINE]}/{name}",
            "an encoded climb from inside": f"{day}/%2e%2e/%2e%2e/%2e%2e/%2e%2e/node_ref={files.refs[MINE]}/{name}",
            "a climb with an encoded slash": f"..%2fnode_ref={files.refs[MINE]}/{name}",
            "my ref after bob's file": f"{files.stored[4.0]}/node_ref={files.refs[MINE]}/%2e%2e",
            "no node segment": name,
            "nothing at all": "",
        }

        answers = {label: client.get(f"/api/auth/me/archive/{key}") for label, key in keys.items()}

        _same_404(*answers.values(), body=NO_FILE)
        # The control: the public archive serves the public node's file.
        as_user(client, None)
        assert client.get(f"/api/data/archive/{files.published[5.0]}").status_code == 200

    def test_a_literal_climb_under_an_owned_ref_is_the_same_404(self, client, files, archive_dir, ada):
        name = files.stored[2.0].rsplit("/", 1)[-1]
        beside = archive_dir.parent / f"node_id={MINE}"
        beside.mkdir()
        shutil.copy(archive_dir / files.stored[2.0], beside / name)
        unknown = client.get(f"/api/auth/me/archive/year=2026/node_ref={node_auth.mint_node_ref()}/{name}")

        for raw in (
            f"/api/auth/me/archive/../node_ref={files.refs[MINE]}/{name}",
            f"/api/auth/me/archive/year=2026/month=08/day=27/../../../../node_ref={files.refs[MINE]}/{name}",
        ):
            assert _raw_get(client, raw, ada[0]) == (404, unknown.content)
        assert unknown.content == NO_FILE
        # The same route serves the file under its own key, so the 404 above is
        # the climb being refused.
        assert _raw_get(client, f"/api/auth/me/archive/{files.published[2.0]}", ada[0])[0] == 200

    def test_a_caller_with_no_nodes_downloads_nothing(self, client, files):
        as_user(client, sign_in(client, "carol@example.com"))
        _same_404(client.get(f"/api/auth/me/archive/{files.published[3.0]}"), body=NO_FILE)
        assert client.get("/api/auth/me/archive").json() == EMPTY_LISTING
