"""The ADS-B snapshot archive: what one snapshot writes, and how it reaches disk."""

import asyncio
import logging
import math
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from core import state
from core.task_registry import TASK_EXPECTED_INTERVAL_S, get_stale_tasks
from services import adsb_writer, node_sites
from services import public_location as pl
from services.adsb_regions import cell_of
from services.geo import R_EARTH_KM, bearing_deg, haversine_km
from services.publication import PublicationUnavailable
from services.tasks import adsb_snapshot as snap

T = 1_790_000_000_000  # the snapshot's server time, epoch ms
SETTLE_MS = 60_000


def _dest(lat: float, lon: float, bearing: float, km: float) -> tuple[float, float]:
    """The point km away along a great circle, on the sphere haversine_km measures."""
    d, b, la1 = km / R_EARTH_KM, math.radians(bearing), math.radians(lat)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = math.radians(lon) + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2)
    )
    return math.degrees(la2), math.degrees(lo2)


A = (40.0, -100.0)  # node-a's true receiver
B = _dest(*A, 90.0, 10.0)  # node-b's, 10 km east, same cell
C = _dest(*A, 180.0, 420.0)  # node-c's, a lattice row further south


@pytest.fixture(autouse=True)
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(tmp_path / "adsb"))
    monkeypatch.setattr(state, "adsb_fixes_by_reporter", {}, raising=False)
    monkeypatch.setattr(state, "adsb_fallback", {})
    monkeypatch.setattr(state, "connected_nodes", {})
    monkeypatch.setattr(state, "adsb_snapshot_dropped", 0)
    monkeypatch.setattr(state, "adsb_snapshot_publication_unavailable", 0)
    # The privacy policy and the registry are inputs: node-priv is private and
    # node-anon has no registry row.
    monkeypatch.setattr(snap, "private_node_ids", lambda: frozenset({"node-priv"}))
    monkeypatch.setattr(snap, "public_identity", lambda node_id: None if node_id == "node-anon" else f"ref-{node_id}")
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_MIN_KM", "0.5")
    monkeypatch.setenv("NODE_FUZZ_MAX_KM", "1.0")
    monkeypatch.setenv("NODE_FUZZ_SITE_KM", "0.15")
    monkeypatch.setattr(node_sites, "_positions_from_files", dict)
    pl._reset_for_tests()
    node_sites._reset_for_tests()
    snap._reset_for_tests()
    yield tmp_path / "adsb"
    snap._reset_for_tests()
    pl._reset_for_tests()
    node_sites._reset_for_tests()


def _fix(lat: float, seen_ms: int, world: str = "real", lon: float = -100.0, **extra) -> dict:
    return {
        "flight": "DAL123",
        "lat": lat,
        "lon": lon,
        "alt_baro": 34000,
        "gs": 450.0,
        "track": 90.0,
        "last_seen_ms": seen_ms,
        "world": world,
        **extra,
    }


def _report(hexn: str, reporter: str | None, rec: dict) -> None:
    state.adsb_fixes_by_reporter.setdefault(hexn, {})[reporter] = rec


def _summary(rows: list[dict]) -> list[tuple]:
    return sorted(
        ((r["hex"], r["world"], r["source"], r["source_node_id"], r["lat"]) for r in rows),
        key=lambda t: (t[0], t[1], t[2], t[3] or ""),
    )


def _node(lat: float, lon: float) -> dict:
    return {
        "status": "connected",
        "is_synthetic": False,
        "config": {"rx_lat": lat, "rx_lon": lon, "tx_lat": 40.5, "tx_lon": -100.5},
    }


def _answer(fixes: dict[str, tuple[float, float]], seen_ms: int = T - 2000) -> dict:
    """One polled cell's ADSBHub answer, in the poller's record shape."""
    return {h: {**_fix(lat, seen_ms, lon=lon), "hex": h, "source": "adsb_service"} for h, (lat, lon) in fixes.items()}


def _poll(cell, fixes: dict[str, tuple[float, float]], seen_ms: int = T - 2000) -> None:
    state.adsb_fallback = {cell: _answer(fixes, seen_ms)}


# ── what is archived ─────────────────────────────────────────────────────────


def test_external_truth_cache_and_feed_store_are_never_archived(monkeypatch):
    monkeypatch.setattr(state, "external_adsb_cache", {"aaa601": {**_fix(40.0, T - 1000), "hex": "aaa601"}})
    monkeypatch.setattr(state, "adsb_aircraft", {"aaa602": {**_fix(40.0, T - 1000), "hex": "aaa602"}})
    assert snap.snapshot_rows(T) == []
    for module in (snap, adsb_writer):
        source = Path(module.__file__).read_text()
        assert "external_adsb_cache" not in source
        assert "adsb_aircraft" not in source


def test_fix_age_decides_freshness_either_side_of_the_snapshot():
    _report("aaa101", "node-a", _fix(40.1, T - 44_000))
    _report("aaa102", "node-a", _fix(40.2, T - 46_000))
    _report("aaa103", "node-a", _fix(40.3, T + 46_000))
    _report("aaa104", "node-a", _fix(40.4, T + 44_000))
    assert sorted(r["hex"] for r in snap.snapshot_rows(T)) == ["aaa101", "aaa104"]


def test_every_publishable_reporter_of_a_real_aircraft_is_a_row_of_its_own():
    _report("aaa201", "node-a", _fix(40.1, T - 5000))
    _report("aaa201", "node-c", _fix(40.2, T - 2000))
    # Fresher, but private or unidentified: not written.
    _report("aaa201", "node-priv", _fix(40.9, T - 1000))
    _report("aaa201", "node-anon", _fix(40.8, T - 1000))
    # Equal capture times: each reporter's own fix all the same.
    _report("aaa202", "node-c", _fix(40.6, T - 3000))
    _report("aaa202", "node-a", _fix(40.3, T - 3000))
    # A fix too old to write.
    _report("aaa202", "node-b", _fix(40.5, T - 46_000))
    # One reporter under two spellings of a hex: one row, its fresher fix.
    _report("AAA203", "node-a", _fix(40.7, T - 4000))
    _report("aaa203", "node-a", _fix(40.8, T - 6000))
    assert _summary(snap.snapshot_rows(T)) == [
        ("aaa201", "real", "node", "node-a", 40.1),
        ("aaa201", "real", "node", "node-c", 40.2),
        ("aaa202", "real", "node", "node-a", 40.3),
        ("aaa202", "real", "node", "node-c", 40.6),
        ("aaa203", "real", "node", "node-a", 40.7),
    ]


def test_the_simulated_world_keeps_the_freshest_fix_per_aircraft_ties_to_the_smaller_id():
    _report("aaa211", "synth-a", _fix(41.1, T - 5000, world="sim"))
    _report("aaa211", "synth-b", _fix(41.2, T - 2000, world="sim"))
    _report("aaa212", "synth-b", _fix(41.6, T - 3000, world="sim"))
    _report("aaa212", "synth-a", _fix(41.3, T - 3000, world="sim"))
    _report("aaa213", "synth-a", _fix(41.7, T - 3000, world="sim"))
    _report("aaa213", None, _fix(41.8, T - 3000, world="sim"))
    # The same hex in the real world is a world of its own.
    _report("aaa211", "node-a", _fix(40.1, T - 1000))
    assert _summary(snap.snapshot_rows(T)) == [
        ("aaa211", "real", "node", "node-a", 40.1),
        ("aaa211", "sim", "node", "synth-b", 41.2),
        ("aaa212", "sim", "node", "synth-a", 41.3),
        ("aaa213", "sim", "node", None, 41.8),
    ]


def test_a_reporter_private_now_is_skipped_for_an_older_public_fix():
    _report("aaa301", "node-priv", _fix(40.9, T - 1000))
    _report("aaa301", "node-a", _fix(40.4, T - 8000))
    assert _summary(snap.snapshot_rows(T)) == [("aaa301", "real", "node", "node-a", 40.4)]


def test_a_real_reporter_without_a_public_identity_is_not_archived():
    _report("aaa401", "node-anon", _fix(40.1, T - 1000))
    assert snap.snapshot_rows(T) == []


def test_the_simulators_fleet_is_archived_under_no_node_but_its_real_relay_is_not():
    _report("aaa501", None, _fix(40.1, T - 1000, world="sim"))
    _report("aaa502", None, _fix(40.2, T - 1000, world="real"))
    assert _summary(snap.snapshot_rows(T)) == [("aaa501", "sim", "node", None, 40.1)]


def test_an_unreadable_privacy_policy_withholds_every_node_reporter(monkeypatch):
    def unavailable():
        raise PublicationUnavailable("cold")

    monkeypatch.setattr(snap, "private_node_ids", unavailable)
    _report("aaa701", "node-a", _fix(40.1, T - 1000))
    _report("aaa702", None, _fix(40.2, T - 1000, world="sim"))
    assert _summary(snap.snapshot_rows(T)) == [("aaa702", "sim", "node", None, 40.2)]
    assert state.adsb_snapshot_publication_unavailable == 1


def test_fields_are_mapped_to_the_archive_columns():
    _report("aaa801", None, _fix(40.1, T - 1000, world="sim", alt_baro="ground", flight="  "))
    _report("aaa802", None, _fix(40.2, T - 2000, world="sim", alt_baro=1234.6, gs="fast", flight=" UAL9 "))
    _report("aaa803", None, _fix(40.3, T - 3000, world="sim", alt_baro=3e9, track=float("nan")))
    rows = {r["hex"]: r for r in snap.snapshot_rows(T)}
    assert [(h, r["alt_baro"], r["gs"], r["track"], r["flight"], r["seen_ms"]) for h, r in sorted(rows.items())] == [
        ("aaa801", 0, 450.0, 90.0, None, T - 1000),
        ("aaa802", 1235, None, 90.0, "UAL9", T - 2000),
        ("aaa803", None, 450.0, None, "DAL123", T - 3000),
    ]
    assert {r["ts_ms"] for r in rows.values()} == {T}


def test_a_copy_resized_by_a_writer_is_taken_again():
    class Resized(dict):
        calls = 0

        def items(self):
            Resized.calls += 1
            if Resized.calls == 1:
                raise RuntimeError("dictionary changed size during iteration")
            return super().items()

    state.adsb_fixes_by_reporter = Resized({"aaa901": {None: _fix(40.1, T - 1000, world="sim")}})
    assert _summary(snap.snapshot_rows(T)) == [("aaa901", "sim", "node", None, 40.1)]


def test_pruning_drops_past_stale_entries_only():
    fresh_b = _fix(40.2, T - 10_000)
    ahead_c = _fix(40.3, T + 50_000)
    _report("ccc001", "node-a", _fix(40.1, T - 46_000))
    _report("ccc001", "node-b", fresh_b)
    _report("ccc002", "node-a", _fix(40.1, T - 50_000))
    _report("ccc003", "node-c", ahead_c)
    snap.snapshot_rows(T)
    assert state.adsb_fixes_by_reporter == {"ccc001": {"node-b": fresh_b}, "ccc003": {"node-c": ahead_c}}


# ── ADSBHub attribution and clipping ─────────────────────────────────────────


def test_the_clip_radius_is_one_figure_below_the_range_margin():
    # 150 km less the donut edge (1.0), the site radius (0.15), rounding (0.01)
    # and 0.5% of the 250 nm widest poll (2.315).
    assert snap.clip_radius_km() == pytest.approx(146.525)


def test_a_polled_fix_is_attributed_to_every_node_whose_clip_disc_holds_it():
    assert cell_of(*A) == cell_of(*B) != cell_of(*C)
    state.connected_nodes = {"node-b": _node(*B), "node-a": _node(*A), "node-c": _node(*C)}
    north = _dest(*_dest(*A, 90.0, 5.0), 0.0, 148.5)  # inside the polled disc, outside both clips
    _poll(
        cell_of(*A),
        {
            "aaa001": _dest(*A, 0.0, 50.0),
            "aaa002": north,
            "aaa003": _dest(*C, 0.0, 20.0),  # beside node-c, whose cell was not polled
        },
    )
    snap.snapshot_rows(T - SETTLE_MS)
    rows = snap.snapshot_rows(T)
    assert [(r["hex"], r["world"], r["source"], r["source_node_id"]) for r in rows] == [
        ("aaa001", "real", "aggregator", "node-a,node-b")
    ]


def test_the_clip_disc_is_centred_on_the_published_position():
    state.connected_nodes = {"node-a": _node(*A)}
    pub = pl.public_latlon(*A, "node-a")
    offset_km = haversine_km(*A, *pub)
    assert 0.5 <= offset_km <= 1.0
    away = bearing_deg(*A, *pub)
    _poll(
        cell_of(*A),
        {
            # Inside the published clip disc, beyond the true one.
            "aaa011": _dest(*pub, away, 146.3),
            # Inside the true clip disc, beyond the published one.
            "aaa012": _dest(*pub, away + 180.0, 146.8),
        },
    )
    snap.snapshot_rows(T - SETTLE_MS)
    assert [(r["hex"], r["source_node_id"]) for r in snap.snapshot_rows(T)] == [("aaa011", "node-a")]


def test_a_node_attributes_only_once_settled_in_a_polled_cell():
    state.connected_nodes = {"node-a": _node(*A)}
    _poll(cell_of(*A), {"aaa021": _dest(*A, 0.0, 30.0)}, seen_ms=T + 30_000)
    assert snap.snapshot_rows(T) == []
    assert snap.snapshot_rows(T + SETTLE_MS - 1) == []
    assert [r["hex"] for r in snap.snapshot_rows(T + SETTLE_MS)] == ["aaa021"]


def test_a_fix_counts_only_through_the_cell_that_polled_it():
    D = _dest(*A, 90.0, 110.0)  # node-d, across the column border east of node-a
    assert cell_of(*D) != cell_of(*A)
    state.connected_nodes = {"node-a": _node(*A), "node-d": _node(*D)}
    # Both fixes lie inside both clip discs; each cell's answer speaks for its own nodes.
    state.adsb_fallback = {
        cell_of(*A): _answer({"aaa032": _dest(*A, 0.0, 20.0)}),
        cell_of(*D): _answer({"aaa031": _dest(*A, 90.0, 50.0)}),
    }
    snap.snapshot_rows(T - SETTLE_MS)
    assert sorted((r["hex"], r["source_node_id"]) for r in snap.snapshot_rows(T)) == [
        ("aaa031", "node-d"),
        ("aaa032", "node-a"),
    ]


D = _dest(*A, 90.0, 110.0)  # node-d's true receiver, in the next column east
WEST_60 = _dest(*A, 270.0, 60.0)  # inside node-a's clip disc only
EAST_60 = _dest(*D, 90.0, 60.0)  # inside node-d's clip disc only
BETWEEN = _dest(*A, 90.0, 55.0)  # inside both
# 148.5 km from node-d: inside its cell's 150 km polled disc, beyond its clip disc.
D_BAND_EAST = _dest(*D, 90.0, 148.5)  # and outside node-a's
D_BAND_WEST = _dest(*A, 270.0, 38.5)  # and inside node-a's


@pytest.mark.parametrize(
    ("in_d_fresher", "written"),
    [
        pytest.param(
            EAST_60,
            [("node-d", EAST_60), ("node-a", WEST_60)],
            id="fresher-inside-its-own-cells-disc-only",
        ),
        pytest.param(D_BAND_EAST, [("node-a", WEST_60)], id="fresher-inside-no-disc"),
        pytest.param(
            BETWEEN,
            [("node-a,node-d", BETWEEN), ("node-a", WEST_60)],
            id="fresher-inside-both-discs",
        ),
        pytest.param(D_BAND_WEST, [("node-a", WEST_60)], id="fresher-inside-another-cells-disc-only"),
    ],
)
def test_an_aircraft_in_two_cells_is_attributed_by_the_fix_written(in_d_fresher, written):
    assert cell_of(*D) != cell_of(*A)
    state.connected_nodes = {"node-a": _node(*A), "node-d": _node(*D)}
    # node-a's cell holds an older fix of the aircraft, inside node-a's clip disc only.
    # Each cell's fix inside its own node's disc is a row of its own, freshest first.
    state.adsb_fallback = {
        cell_of(*A): _answer({"aaa041": WEST_60}, seen_ms=T - 8000),
        cell_of(*D): _answer({"aaa041": in_d_fresher}, seen_ms=T - 2000),
    }
    snap.snapshot_rows(T - SETTLE_MS)
    assert [(r["hex"], r["source_node_id"], r["lat"], r["lon"]) for r in snap.snapshot_rows(T)] == [
        ("aaa041", attributed, *position) for attributed, position in written
    ]


def test_one_fix_in_two_cells_answers_is_written_once():
    state.connected_nodes = {"node-a": _node(*A), "node-d": _node(*D)}
    state.adsb_fallback = {
        cell_of(*A): _answer({"aaa051": BETWEEN}, seen_ms=T - 2000),
        cell_of(*D): _answer({"aaa051": BETWEEN}, seen_ms=T - 2000),
    }
    snap.snapshot_rows(T - SETTLE_MS)
    assert [(r["hex"], r["source_node_id"], r["lat"], r["lon"]) for r in snap.snapshot_rows(T)] == [
        ("aaa051", "node-a,node-d", *BETWEEN)
    ]


# ── buffer and flush ─────────────────────────────────────────────────────────


def test_a_flush_writes_one_sorted_part_in_the_flush_days_partition(archive):
    _report("bbb002", None, _fix(40.2, T - 1000, world="sim"))
    _report("bbb001", None, _fix(40.1, T - 1000, world="sim"))
    snap.take_snapshot(T)
    _report("bbb001", "node-a", _fix(40.3, T + 5000))
    snap.take_snapshot(T + 10_000)

    key = snap.flush_adsb_snapshots(write_ts=datetime(2026, 10, 23, 23, 59, 58, tzinfo=timezone.utc))
    assert re.fullmatch(r"year=2026/month=10/day=23/part-235958-[0-9a-f]{32}\.parquet", key)
    path = archive / key
    assert pq.read_schema(path).remove_metadata() == adsb_writer.SCHEMA
    assert pq.ParquetFile(path).num_row_groups == 1
    table = pq.read_table(path)
    assert list(zip(*(table.column(c).to_pylist() for c in ("ts_ms", "hex", "world", "source_node_id")))) == [
        (T, "bbb001", "sim", None),
        (T, "bbb002", "sim", None),
        (T + 10_000, "bbb001", "real", "node-a"),
        (T + 10_000, "bbb001", "sim", None),
        (T + 10_000, "bbb002", "sim", None),
    ]
    assert snap.flush_adsb_snapshots() is None


def test_a_failed_write_is_retried_before_newer_snapshots(archive, monkeypatch):
    _report("bbb101", None, _fix(40.1, T, world="sim"))
    snap.take_snapshot(T)

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snap, "write_adsb_parquet", fail)
    with pytest.raises(OSError, match="disk full"):
        snap.flush_adsb_snapshots()
    snap.take_snapshot(T + 10_000)
    monkeypatch.setattr(snap, "write_adsb_parquet", adsb_writer.write_adsb_parquet)

    retried = snap.flush_adsb_snapshots()
    newer = snap.flush_adsb_snapshots()
    assert pq.read_table(archive / retried).column("ts_ms").to_pylist() == [T]
    assert pq.read_table(archive / newer).column("ts_ms").to_pylist() == [T + 10_000]
    assert snap.flush_adsb_snapshots() is None


def test_a_full_buffer_drops_the_oldest_snapshot_and_counts_it(archive, monkeypatch):
    monkeypatch.setattr(snap, "_buffer", deque(maxlen=2))
    _report("bbb201", None, _fix(40.1, T + 10_000, world="sim"))
    for ts in (T, T + 10_000, T + 20_000):
        snap.take_snapshot(ts)
    assert state.adsb_snapshot_dropped == 1
    key = snap.flush_adsb_snapshots()
    assert pq.read_table(archive / key).column("ts_ms").to_pylist() == [T + 10_000, T + 20_000]


def test_the_writer_skips_invalid_rows_with_one_warning(archive, caplog):
    good = {
        "ts_ms": T,
        "hex": "bbb301",
        "lat": 40.0,
        "lon": -100.0,
        "alt_baro": 2**31,
        "seen_ms": T,
        "world": "sim",
        "source": "node",
        "source_node_id": None,
    }
    bad = [
        {**good, "hex": ""},
        {**good, "lat": float("nan")},
        {**good, "world": "real"},  # a real node row must name its node
        {**good, "source": "aggregator"},  # an aggregator row must name its nodes
        {**good, "seen_ms": 1.5},
    ]
    with caplog.at_level(logging.WARNING, logger=adsb_writer.__name__):
        key = adsb_writer.write_adsb_parquet(rows=[*bad, good], base_dir=archive)
    assert [r.getMessage() for r in caplog.records] == [
        "ADS-B archive: skipped 5 of 6 rows (hex=1, lat=1, seen_ms=1, source_node_id=2)"
    ]
    assert pq.read_table(archive / key).select(["hex", "alt_baro"]).to_pylist() == [{"hex": "bbb301", "alt_baro": None}]
    assert adsb_writer.write_adsb_parquet(rows=bad, base_dir=archive / "none") is None
    assert not (archive / "none").exists()


# ── the task ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_task_returns_at_once_when_disabled(monkeypatch):
    monkeypatch.setattr(snap, "ADSB_SNAPSHOT_ENABLED", False)
    monkeypatch.setattr(state, "task_last_success", {})
    await asyncio.wait_for(snap.adsb_snapshot_task(), timeout=1.0)
    assert "adsb_snapshot" not in state.task_last_success
    assert "adsb_snapshot" not in get_stale_tasks()


@pytest.mark.asyncio
async def test_a_good_iteration_reports_success_and_shutdown_writes_the_buffer(archive, monkeypatch):
    monkeypatch.setattr(snap, "ADSB_SNAPSHOT_ENABLED", True)
    monkeypatch.setattr(snap, "ADSB_SNAPSHOT_FLUSH_INTERVAL_S", 3600)
    monkeypatch.setattr(state, "task_last_success", {})
    monkeypatch.setattr(state, "task_error_counts", defaultdict(int))
    _report("bbb401", None, _fix(40.1, int(time.time() * 1000), world="sim"))
    sleeps = 0

    async def sleep(interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(snap.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await snap.adsb_snapshot_task()
    assert "adsb_snapshot" in state.task_last_success
    assert state.task_error_counts["adsb_snapshot"] == 0
    (part,) = archive.rglob("part-*.parquet")
    assert pq.read_table(part).column("hex").to_pylist() == ["bbb401"]


def test_the_task_is_registered_for_staleness():
    assert TASK_EXPECTED_INTERVAL_S["adsb_snapshot"] == 180
