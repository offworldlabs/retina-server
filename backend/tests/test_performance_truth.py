"""The independent-denominator scorer, on invented aircraft, nodes and detections.

One receiver and one transmitter 22.1 km apart (the geometry of
test_performance_data.py). Aircraft fly straight at a steady speed; a detection
that "sees" one carries the delay and Doppler the scorer itself predicts for it,
so the tests check the bookkeeping, the gates and the controls, not the physics.
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from retina_analytics.empirical_coverage import EmpiricalCoverageState

from services import adsb_writer, parquet_writer
from services import performance_truth as pt
from services import performance_truth_html as html
from services import performance_truth_job as job

NODE = "radar-alpha"
CONFIG = {
    "node_id": NODE,
    "version": 1,
    "created_at": "2026-01-01 00:00:00.000000",
    "rx_lat": 10.0,
    "rx_lon": 20.0,
    "rx_alt_ft": 300.0,
    "tx_lat": 10.2,
    "tx_lon": 20.0,
    "tx_alt_ft": 1000.0,
    "tx_callsign": "TEST-TX",
    "fc_hz": 200e6,
    "fs_hz": 2e6,
    "cpi_s": 0.5,
    "max_range_km": 59.96,
    "beam_azimuth_deg": None,
    "beam_width_deg": None,
    "delay_tolerance_us": 6.671,
    "doppler_tolerance_hz": 5.0,
    "superseded_at": None,
}
T0 = int(datetime(2026, 3, 10, 12, 0, tzinfo=UTC).timestamp() * 1000)
HALF_HOUR = 30 * 60_000
REGION = f"{NODE}+radar-beta"


def fixes_for(
    hexn: str, t_from: int, t_to: int, lat: float, lon: float, *, alt_ft=12000.0, gs_kt=300.0, track=90.0, step_ms=6000
):
    """Fixes of one straight flight, dead-reckoned from (lat, lon) at t_from."""
    v = gs_kt * 0.514444
    out = []
    for t in range(t_from, t_to + 1, step_ms):
        s = (t - t_from) / 1000.0
        d_north = v * s * math.cos(math.radians(track))
        d_east = v * s * math.sin(math.radians(track))
        out.append(
            (
                hexn,
                t,
                lat + d_north / 111_195.0,
                lon + d_east / (111_195.0 * math.cos(math.radians(lat))),
                alt_ft,
                gs_kt,
                track,
            )
        )
    return out


def truth_of(fixes, polls=None, region=REGION) -> pt.TruthRows:
    hexes = pt.pdata._Interner()
    rows = sorted(fixes, key=lambda f: (f[0], f[1]))
    ids = np.array([hexes.intern(f[0]) for f in rows], dtype=np.int64)
    order = np.lexsort((np.array([f[1] for f in rows]), ids))
    col = lambda i, dtype=float: np.array([rows[k][i] for k in order], dtype=dtype)  # noqa: E731
    if polls is None:
        polls = list(range(T0 - 20_000, T0 + 3 * 3_600_000, 10_000))
    n = len(rows)
    return pt.TruthRows(
        hexes=hexes,
        hex_id=ids[order],
        t_ms=col(1, np.int64),
        lat=col(2),
        lon=col(3),
        alt_ft=col(4),
        gs_kt=col(5),
        track_deg=col(6),
        vrate_ft_min=np.full(n, np.nan),
        mlat=np.zeros(n, dtype=bool),
        region=np.zeros(n, dtype=np.int64),
        regions=[region],
        polls={region: np.array(sorted(polls), dtype=np.int64)},
    )


def predicted(sites: pt.Sites, truth: pt.TruthRows, hexn: str, t_ms: int) -> tuple[float, float]:
    traj = pt.Trajectories(truth)
    pos = traj.at(np.array([pt.hex_lookup(truth.hexes, hexn)]), np.array([t_ms]))
    assert pos["valid"][0]
    version = sites.versions[int(sites.active(np.array([t_ms]))[0])]
    p = pt.predict(version.site, version.fc_hz, pos)
    return float(p["delay_us"][0]), float(p["doppler_hz"][0])


def detections_of(rows) -> pt.Detections:
    """rows: (frame_ts_ms, delay_us, doppler_hz, snr_db, tag_hex or None)."""
    rows = sorted(rows, key=lambda r: r[0])
    ts = np.array([r[0] for r in rows], dtype=np.int64)
    return pt.Detections(
        frame_ts_ms=ts,
        delay_us=np.array([r[1] for r in rows], dtype=float),
        doppler_hz=np.array([r[2] for r in rows], dtype=float),
        snr_db=np.array([r[3] for r in rows], dtype=float),
        tag_hex=[r[4] for r in rows],
        frames=np.unique(ts),
    )


def clutter_frames(t_from: int, t_to: int, step_ms: int = 1000):
    """A detection far from every aircraft in every frame, so the node reads as up."""
    return [(t, 150.0 + (t % 7), 5.0, 8.0, None) for t in range(t_from, t_to, step_ms)]


def seen_frames(sites, truth, hexn, t_from, t_to, *, tag=True, step_ms=1000, noise_us=0.2, noise_hz=0.5):
    """A detection per frame that fits the aircraft, over its fixes and the dead-reckoned edge either side."""
    out = []
    for i, t in enumerate(range(t_from - pt.TRUTH_EDGE_MS, t_to + pt.TRUTH_EDGE_MS, step_ms)):
        delay, doppler = predicted(sites, truth, hexn, t)
        out.append((t, delay + noise_us * math.sin(i), doppler + noise_hz * math.cos(i), 14.0, hexn if tag else None))
    return out


def score(truth, detections, *, configs=(CONFIG,), from_ms=T0, to_ms=T0 + HALF_HOUR, **extra) -> pt.WindowResult:
    sites = pt.Sites(configs)
    inputs = pt.WindowInputs(
        NODE, from_ms, to_ms, sites, truth, detections, class_for=lambda t: "narrowbody" if t else "unknown", **extra
    )
    return pt.score_window(inputs)


def geo(result: pt.WindowResult) -> dict:
    return result.summary["gates"]["geometric"]


# An aircraft inside the window for its whole flight: from 19 km north-east of the
# receiver heading west at 150 kt, so after ten minutes it is 35 km west-north-west,
# still 40 us inside the 200 us window.
FLIGHT_FROM, FLIGHT_TO = T0 + 5 * 60_000, T0 + 15 * 60_000


def flight(hexn="a1b2c3", **kw):
    kw.setdefault("gs_kt", 150.0)
    kw.setdefault("track", 270.0)
    return fixes_for(hexn, FLIGHT_FROM, FLIGHT_TO, 10.12, 20.12, **kw)


# ── Denominator ──────────────────────────────────────────────────────────────


def test_untagged_aircraft_is_an_opportunity_and_a_miss():
    truth = truth_of(flight())
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    result = score(truth, det)
    g = geo(result)
    # Ten minutes of presence is 60 buckets, plus the edge allowance either side.
    assert 60 <= g["n"] <= 62
    assert g["validated"] == 0 and g["raw"] == 0 and g["raw_pm1"] == 0
    assert g["encounters"] == 1
    assert result.summary["status"]["code"] == "insufficient"  # one encounter is not evidence


def test_detected_aircraft_is_validated_and_tagged():
    truth = truth_of(flight())
    sites = pt.Sites([CONFIG])
    det = detections_of(
        seen_frames(sites, truth, "a1b2c3", FLIGHT_FROM, FLIGHT_TO) + clutter_frames(T0, T0 + HALF_HOUR)
    )
    result = score(truth, det)
    g = geo(result)
    assert g["validated"] == g["n"]
    assert g["validated_tagged"] == g["n"]
    assert g["raw"] == g["n"] and g["refuted"] == 0 and g["ambiguous"] == 0
    diag = result.summary["diagnostics"]
    assert diag["tags_fitting_truth"] == diag["tagged_detections"]
    assert diag["delay_residual_us"]["mad"] < 0.5
    real = result.encounters.filter(pa.compute.equal(result.encounters["shift_s"], 0))
    assert real["latency_s"].to_pylist() == [0.0]
    assert result.summary["latency_s"] == {"encounters": 1, "detected": 1, "median": 0.0, "p90": 0.0}


def test_untagged_detection_that_fits_the_geometry_is_credited():
    """A node with no ADS-B receiver of its own is scored by geometry alone."""
    truth = truth_of(flight())
    sites = pt.Sites([CONFIG])
    det = detections_of(
        seen_frames(sites, truth, "a1b2c3", FLIGHT_FROM, FLIGHT_TO, tag=False) + clutter_frames(T0, T0 + HALF_HOUR)
    )
    g = geo(score(truth, det))
    assert g["validated"] == g["n"]
    assert g["raw"] == 0 and g["validated_tagged"] == 0


def test_chance_tag_on_wrong_geometry_is_refuted_not_credited():
    truth = truth_of(flight())
    # Every clutter detection carries the aircraft's hex: the node's association is wrong.
    det = detections_of([(t, d, f, s, "a1b2c3") for t, d, f, s, _ in clutter_frames(T0, T0 + HALF_HOUR)])
    result = score(truth, det)
    g = geo(result)
    assert g["raw"] == g["n"] and g["raw_pm1"] == g["n"]
    assert g["validated"] == 0
    assert g["refuted"] == g["n"]
    assert "tags_refuted" in result.summary["status"]["flags"]


def test_single_spurious_tag_credits_one_bucket_not_three_and_never_validates():
    truth = truth_of(flight())
    tag_t = T0 + 10 * 60_000 + 3_000
    rows = clutter_frames(T0, T0 + HALF_HOUR)
    rows = [(t, d, f, s, "a1b2c3" if t == tag_t else None) for t, d, f, s, _ in rows]
    result = score(truth, detections_of(rows))
    g = geo(result)
    assert g["raw"] == 1
    assert g["raw_pm1"] == 3  # the earlier method's ±1 rule
    assert g["validated"] == 0 and g["refuted"] == 1


def test_legacy_node_rate_reproduces_the_persistence_floor(tmp_path: Path):
    """The node's own fix rows persist 45 s after one spurious tag: the old score is 2/5, the new one 0."""
    truth = truth_of(flight())
    tag_t = T0 + 10 * 60_000 + 3_000
    rows = [(t, d, f, s, "a1b2c3" if t == tag_t else None) for t, d, f, s, _ in clutter_frames(T0, T0 + HALF_HOUR)]
    det = detections_of(rows)
    # Snapshot rows the server would write: the tag's fix, kept for the next four snapshots.
    adsb_root = tmp_path / "adsb"
    fix_rows = []
    for k in range(5):
        fix_rows.append(
            {
                "ts_ms": tag_t + 2_000 + 10_000 * k,
                "hex": "a1b2c3",
                "lat": 10.12,
                "lon": 20.12,
                "alt_baro": 12000,
                "gs": 300.0,
                "track": 90.0,
                "flight": None,
                "seen_ms": tag_t,
                "world": "real",
                "source": "node",
                "source_node_id": NODE,
            }
        )
    adsb_writer.write_adsb_parquet(
        rows=fix_rows, base_dir=adsb_root, write_ts=datetime(2026, 3, 10, 12, 40, tzinfo=UTC)
    )
    legacy = pt.legacy_node_rate(adsb_root, NODE, pt.Sites([CONFIG]), det, T0, T0 + HALF_HOUR)
    assert legacy["n"] == 5
    assert 0.3 <= legacy["pd"] <= 0.5
    assert geo(score(truth, det))["validated"] == 0


# ── Gaps and bad records ─────────────────────────────────────────────────────


def test_node_without_frames_has_no_opportunities():
    truth = truth_of(flight())
    # Frames only during the first three minutes of the flight.
    det = detections_of(clutter_frames(FLIGHT_FROM, FLIGHT_FROM + 3 * 60_000))
    result = score(truth, det)
    g = geo(result)
    # Three minutes of frames plus the 60 s activity allowance after them.
    assert 18 <= g["n"] <= 25
    assert "node_partial" in result.summary["status"]["flags"]


def test_recorded_offline_span_removes_buckets():
    """Two silent minutes: the frame allowance alone keeps every bucket, the recorded offline event removes them."""
    truth = truth_of(flight())
    silent_from, silent_to = FLIGHT_FROM + 3 * 60_000, FLIGHT_FROM + 5 * 60_000
    det = detections_of([r for r in clutter_frames(T0, T0 + HALF_HOUR) if not (silent_from <= r[0] < silent_to)])
    full = geo(score(truth, det))["n"]
    assert full >= 60  # every bucket is within 60 s of a frame
    events = [
        {
            "ts": silent_to / 1000.0,
            "category": "node",
            "message": f"Node {NODE} went offline (no heartbeat for 120s)",
            "meta": {"node_id": NODE, "age_s": 120},
        }
    ]
    spans = pt.offline_spans(events, NODE, T0 + HALF_HOUR)
    assert spans and spans[0][0] == silent_from
    # The span closes at the next frame, so it removes the two silent minutes.
    reduced = geo(score(truth, det, offline=spans))["n"]
    assert 10 <= full - reduced <= 14


def test_truth_poll_gap_removes_buckets_and_fix_gap_removes_presence():
    polls = [
        t for t in range(T0 - 20_000, T0 + HALF_HOUR, 10_000) if not (FLIGHT_FROM + 60_000 <= t < FLIGHT_FROM + 180_000)
    ]
    truth = truth_of(flight(), polls=polls)
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    g = geo(score(truth, det))
    assert 50 <= g["n"] <= 56  # two minutes without a poll, less the 20 s slack either side, are gone
    # Now a two-minute hole in the fixes: the aircraft is unknown there, so no opportunity.
    holed = [f for f in flight() if not (FLIGHT_FROM + 240_000 < f[1] < FLIGHT_FROM + 360_000)]
    base = geo(score(truth_of(flight()), det))["n"]
    g2 = geo(score(truth_of(holed), det))
    assert g2["n"] <= base - 8


def test_ground_fix_and_stale_extrapolation_are_not_opportunities():
    fixes = flight(alt_ft=0.0)
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    assert geo(score(truth_of(fixes), det))["n"] == 0
    # Last fix at FLIGHT_TO: six seconds of dead reckoning, no more.
    truth = truth_of(flight())
    table = score(truth, det).buckets
    last = max(table["bucket_ms"].to_pylist())
    assert FLIGHT_TO <= last + pt.BUCKET_MS // 2 <= FLIGHT_TO + pt.TRUTH_EDGE_MS + pt.BUCKET_MS


def test_load_truth_dedupes_and_takes_late_rows(tmp_path: Path):
    root = tmp_path / "truth"
    hour = datetime(2026, 3, 10, 12, tzinfo=UTC)
    rows = []
    fix = {
        "k": "a",
        "g": REGION,
        "n": T0 + 100,
        "r": T0 + 1_100,
        "c": T0 + 5_000,
        "h": "abc123",
        "la": 10.1,
        "lo": 20.1,
        "ab": 9000,
        "gs": 250,
        "tr": 90,
        "sp": 2.0,
    }
    rows += [json.dumps({"k": "p", "g": REGION, "n": T0 + 100, "r": T0 + 1_100, "st": 200, "na": 1, "nw": 1, "d": 90})]
    rows += [json.dumps(fix), json.dumps(fix)]  # the recorder would not repeat it, a reader must still cope
    rows += [json.dumps({"k": "p", "g": REGION, "r": T0 + 11_000, "st": 429, "err": "rate limit"})]
    (root / "adsbhub" / hour.strftime("%Y-%m-%d")).mkdir(parents=True)
    with gzip.open(root / "adsbhub" / hour.strftime("%Y-%m-%d") / "12.jsonl.gz", "wt") as fh:
        fh.write("\n".join(rows) + "\n")
    # A row received in the next hour's file, captured in this window: late data.
    late = dict(fix, r=T0 + 3_600_000 + 30_000, c=T0 + 3_600_000 - 10_000, la=10.2)
    with gzip.open(root / "adsbhub" / hour.strftime("%Y-%m-%d") / "13.jsonl.gz", "wt") as fh:
        fh.write(json.dumps(late) + "\n")
    truth = pt.load_truth(root, T0, T0 + 3_600_000)
    assert truth.t_ms.tolist() == [T0 + 5_000, T0 + 3_600_000 - 10_000]
    assert truth.polls[REGION].tolist() == [T0 + 1_100]  # the 429 is not a poll
    assert truth.for_node(NODE).t_ms.size == 2
    assert truth.for_node("radar-gamma").t_ms.size == 0


# ── Configuration and geometry ───────────────────────────────────────────────


def test_transmitter_change_mid_window_uses_each_version():
    moved = dict(CONFIG, version=2, tx_lat=10.0, tx_lon=20.25, created_at="2026-03-10 12:10:00.000000")
    configs = (CONFIG, moved)
    truth = truth_of(flight())
    sites = pt.Sites(configs)
    det = detections_of(
        seen_frames(sites, truth, "a1b2c3", FLIGHT_FROM, FLIGHT_TO) + clutter_frames(T0, T0 + HALF_HOUR)
    )
    result = score(truth, det, configs=configs)
    versions = result.buckets["config_version"].to_pylist()
    assert {1, 2} <= set(versions)
    assert geo(result)["validated"] == geo(result)["n"]
    # Scored against the first version alone, the second half's detections no longer fit.
    stale = score(truth, det, configs=(CONFIG,))
    assert geo(stale)["validated"] < geo(stale)["n"] * 0.6


def test_delay_window_boundary():
    truth = truth_of(flight())
    sites = pt.Sites([CONFIG])
    delay, _ = predicted(sites, truth, "a1b2c3", FLIGHT_FROM + 60_000)
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    tight = dict(CONFIG, max_range_km=(delay - 5.0) * 0.299792458)
    loose = dict(CONFIG, max_range_km=(delay + 5.0) * 0.299792458)
    inside = geo(score(truth, det, configs=(loose,)))["n"]
    outside = geo(score(truth, det, configs=(tight,)))["n"]
    assert inside > outside


def test_declared_wedge_gate_leaves_the_geometric_denominator_alone():
    truth = truth_of(flight())
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    aimed_away = dict(CONFIG, beam_azimuth_deg=225.0, beam_width_deg=40.0)
    result = score(truth, det, configs=(aimed_away,))
    assert result.summary["gates"]["geometric"]["n"] > 0
    assert result.summary["gates"]["declared"]["n"] == 0


def test_learned_fov_gate_compares_ground_range():
    truth = truth_of(flight())
    det = detections_of(clutter_frames(T0, T0 + HALF_HOUR))
    seen_ground: list[float] = []

    def in_fov(bearing_deg, range_km):
        seen_ground.extend(np.asarray(range_km).tolist())
        return np.asarray(range_km) <= 10.0

    result = score(truth, det, in_learned_fov=in_fov)
    assert result.summary["gates"]["empirical"]["n"] == 0
    rr = result.buckets["rr_km"].to_numpy()
    ground = result.buckets["ground_km"].to_numpy()
    assert np.all(rr >= ground - 1e-6)  # slant is never shorter than ground
    assert seen_ground and max(seen_ground) <= max(ground) + 1e-6


def test_fov_snapshot_before_window_is_chosen(tmp_path: Path):
    paths = job.Paths(tmp_path)
    early = EmpiricalCoverageState(10.0, 20.0, max_range_km=60.0)
    late = EmpiricalCoverageState(10.0, 20.0, max_range_km=60.0)
    for k in range(30):
        late.add_point(10.1 + k * 0.001, 20.1, ts=T0 / 1000.0 - 100 + k)
    (paths.fov / NODE).mkdir(parents=True)
    early.save_to_file(str(paths.fov / NODE / f"{T0 - 3_600_000}-aaaa.json"))
    late.save_to_file(str(paths.fov / NODE / f"{T0 + 3_600_000}-bbbb.json"))
    gate, info = job.fov_gate(paths, NODE, T0)
    assert info["fov_saved_ms"] == T0 - 3_600_000 and info["fov_after_window"] is False
    gate_after, info_after = job.fov_gate(paths, NODE, T0 - 7_200_000)
    assert info_after["fov_after_window"] is True and info_after["fov_saved_ms"] == T0 - 3_600_000
    assert gate is not None and gate_after is not None
    assert job.fov_gate(paths, "radar-none", T0) == (None, {"fov": "none"})


# ── Controls and uncertainty ─────────────────────────────────────────────────


def test_random_tags_do_not_earn_a_detection_probability():
    rng = np.random.default_rng(3)
    fixes = []
    for i in range(12):
        fixes += fixes_for(
            f"{i:06x}",
            T0 + i * 120_000,
            T0 + i * 120_000 + 20 * 60_000,
            10.0 + rng.uniform(-0.3, 0.3),
            20.0 + rng.uniform(-0.3, 0.3),
            track=float(rng.uniform(0, 360)),
        )
    truth = truth_of(fixes)
    hexes = sorted({f[0] for f in fixes})
    rows = []
    for t in range(T0, T0 + HALF_HOUR, 1000):
        for _ in range(4):
            rows.append(
                (
                    t,
                    float(rng.uniform(0, 200)),
                    float(rng.uniform(-300, 300)),
                    7.0,
                    hexes[int(rng.integers(len(hexes)))],
                )
            )
    result = score(truth, detections_of(rows), to_ms=T0 + HALF_HOUR)
    g = geo(result)
    assert g["n"] > 150
    raw = g["raw_pm1"] / g["n"]
    validated = g["validated"] / g["n"]
    assert g["background_n"] > 50
    background = g["background"] / g["background_n"]
    assert raw > 0.5  # random tags look like detections to the old numerator
    assert validated < 0.15
    assert abs(validated - background) < 0.1  # and the control explains what is left
    net = pt.rate(g, "net")
    assert net is not None and net < 0.05
    assert {"background_dominated", "tags_refuted"} & set(result.summary["status"]["flags"])


def test_two_aircraft_with_the_same_geometry_are_ambiguous():
    a = flight("aaaaaa")
    # A second aircraft on the same path, 200 m above: the same delay and Doppler within the box.
    b = fixes_for("bbbbbb", FLIGHT_FROM, FLIGHT_TO, 10.12, 20.12, alt_ft=12200.0, gs_kt=150.0, track=270.0)
    truth = truth_of(a + b)
    sites = pt.Sites([CONFIG])
    det = detections_of(
        seen_frames(sites, truth, "aaaaaa", FLIGHT_FROM, FLIGHT_TO) + clutter_frames(T0, T0 + HALF_HOUR)
    )
    g = geo(score(truth, det))
    assert g["validated"] == 0 and g["ambiguous"] == g["n"]
    assert g["matched_any"] == g["n"]


def test_bootstrap_rates_are_percentile_intervals_over_encounters():
    n_opp = np.array([10, 10, 10, 10, 10, 10], dtype=float)
    n_det = np.array([10, 0, 10, 0, 10, 0], dtype=float)
    out = pt.bootstrap_rates(n_opp, n_det, draws=500)
    assert out["pd"] == 0.5 and out["lo"] < 0.5 < out["hi"] and out["encounters"] == 6
    one = pt.bootstrap_rates(np.array([10.0]), np.array([5.0]))
    assert one == {"pd": 0.5, "lo": None, "hi": None, "encounters": 1}
    assert pt.bootstrap_rates(np.zeros(0), np.zeros(0)) is None
    net = pt.bootstrap_rates(n_opp, n_det, control_opp=np.full(6, 10.0), control_det=np.full(6, 1.0))
    assert abs(net["pd"] - 0.4) < 1e-9 and net["lo"] < 0.4 < net["hi"]


def test_sparse_class_is_marked_not_estimated():
    section = {
        "helicopter": {
            "n": 4,
            "encounters": 1,
            "raw_pm1": 0.5,
            "validated": 1.0,
            "background": 0.0,
            "net": 1.0,
            "refuted_share": None,
        }
    }
    page = html.cell_table(section, "By class")
    assert "too few opportunities" in page and "100.0 %" not in page


# ── The job: windows, ledger, idempotence ────────────────────────────────────


def registry(path: Path, configs=(CONFIG,), last_seen="2026-03-10 14:00:00") -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE node_configs (id INTEGER PRIMARY KEY, node_id TEXT, version INTEGER, rx_lat REAL, rx_lon REAL, rx_alt_ft REAL,"
        " tx_lat REAL, tx_lon REAL, tx_alt_ft REAL, tx_callsign TEXT, fc_hz REAL, fs_hz REAL, beam_width_deg REAL, beam_azimuth_deg REAL,"
        " max_range_km REAL, created_at TEXT, superseded_at TEXT, cpi_s REAL, delay_tolerance_us REAL, doppler_tolerance_hz REAL)"
    )
    con.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, last_seen_at TEXT)")
    for c in configs:
        con.execute(
            "INSERT INTO node_configs (node_id, version, rx_lat, rx_lon, rx_alt_ft, tx_lat, tx_lon, tx_alt_ft, tx_callsign, fc_hz, fs_hz,"
            " beam_width_deg, beam_azimuth_deg, max_range_km, created_at, superseded_at, cpi_s, delay_tolerance_us, doppler_tolerance_hz)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c["node_id"],
                c["version"],
                c["rx_lat"],
                c["rx_lon"],
                c["rx_alt_ft"],
                c["tx_lat"],
                c["tx_lon"],
                c["tx_alt_ft"],
                c["tx_callsign"],
                c["fc_hz"],
                c["fs_hz"],
                c["beam_width_deg"],
                c["beam_azimuth_deg"],
                c["max_range_km"],
                c["created_at"],
                c["superseded_at"],
                c["cpi_s"],
                c["delay_tolerance_us"],
                c["doppler_tolerance_hz"],
            ),
        )
        con.execute("INSERT OR REPLACE INTO nodes VALUES (?, ?)", (c["node_id"], last_seen))
    con.commit()
    con.close()


def write_truth_files(root: Path, fixes, from_ms: int, to_ms: int) -> None:
    by_hour: dict[str, list[str]] = {}
    for t in range(from_ms, to_ms, 10_000):
        hour = datetime.fromtimestamp(t / 1000.0, tz=UTC)
        key = hour.strftime("%Y-%m-%d/%H")
        by_hour.setdefault(key, []).append(
            json.dumps({"k": "p", "g": REGION, "n": t, "r": t + 800, "st": 200, "na": 1, "nw": 1, "d": 50})
        )
    for hexn, t, lat, lon, alt, gs, track in fixes:
        hour = datetime.fromtimestamp(t / 1000.0, tz=UTC)
        key = hour.strftime("%Y-%m-%d/%H")
        by_hour.setdefault(key, []).append(
            json.dumps(
                {
                    "k": "a",
                    "g": REGION,
                    "n": t + 1000,
                    "r": t + 1800,
                    "c": t,
                    "h": hexn,
                    "la": lat,
                    "lo": lon,
                    "ab": alt,
                    "gs": gs,
                    "tr": track,
                    "sp": 1.0,
                }
            )
        )
    for key, lines in by_hour.items():
        day, hh = key.split("/")
        (root / "adsbhub" / day).mkdir(parents=True, exist_ok=True)
        with gzip.open(root / "adsbhub" / day / f"{hh}.jsonl.gz", "wt") as fh:
            fh.write("\n".join(lines) + "\n")


def write_archive(archive_root: Path, det: pt.Detections, write_ts: datetime) -> None:
    frames: dict[int, dict] = {}
    for t, d, f, s, tag in zip(
        det.frame_ts_ms.tolist(),
        det.delay_us.tolist(),
        det.doppler_hz.tolist(),
        det.snr_db.tolist(),
        det.tag_hex,
        strict=True,
    ):
        frame = frames.setdefault(
            t,
            {
                "timestamp": t,
                "delay": [],
                "doppler": [],
                "snr": [],
                "adsb": [],
                "_signing_mode": "unknown",
                "_signature_valid": False,
            },
        )
        frame["delay"].append(d)
        frame["doppler"].append(f)
        frame["snr"].append(s)
        frame["adsb"].append({"hex": tag, "lat": 10.12, "lon": 20.12, "alt_baro": 12000} if tag else None)
    parquet_writer.write_detections_parquet(
        node_id=NODE, frames=list(frames.values()), base_dir=archive_root, write_ts=write_ts
    )


@pytest.fixture
def perf_setup(tmp_path: Path):
    root = tmp_path / "perf"
    (root / "registry").mkdir(parents=True)
    registry(root / "registry" / "users.db")
    archive_root, adsb_root, fov_dir = tmp_path / "archive", tmp_path / "adsb", tmp_path / "fov"
    for d in (archive_root, adsb_root, fov_dir):
        d.mkdir()
    window = job.window_start(T0)
    truth_fixes = fixes_for("a1b2c3", window + 20 * 60_000, window + 40 * 60_000, 10.12, 20.12, gs_kt=80.0, track=270.0)
    truth = truth_of(truth_fixes)
    sites = pt.Sites([CONFIG])
    det = detections_of(
        seen_frames(sites, truth, "a1b2c3", window + 20 * 60_000, window + 40 * 60_000)
        + clutter_frames(window, window + job.WINDOW_MS, 2000)
    )
    write_truth_files(root / "truth", truth_fixes, window, window + job.WINDOW_MS)
    write_archive(
        archive_root, det, datetime.fromtimestamp((window + job.WINDOW_MS) / 1000.0, tz=UTC) + timedelta(minutes=20)
    )
    settings = job.Settings(archive_root=archive_root, adsb_root=adsb_root, fov_dir=fov_dir, paths=job.Paths(root))
    return settings, window


def test_run_scores_complete_windows_and_reruns_are_idempotent(perf_setup):
    settings, window = perf_setup
    now = window + job.WINDOW_MS + 2 * 3_600_000  # the window closed two hours ago: complete
    report = job.run(settings, now_ms=now, backfill_from_ms=window)
    assert [s["window"] for s in report["scored"]] == [job.window_label(window)]
    assert report["failed"] == []
    ledger = job.load_ledger(settings.paths)
    entry = ledger["windows"][job.window_label(window)]
    assert entry["status"] == "ok" and entry["nodes"] == {NODE: "insufficient"}  # one encounter only
    summary = job.read_json(job.window_dir(settings.paths, window) / f"{NODE}.summary.json")
    first = summary["gates"]["geometric"]
    assert first["validated"] == first["n"] > 100
    assert (settings.paths.out / "index.html").exists() and (settings.paths.out / "latest.json").exists()
    latest = job.read_json(settings.paths.out / "latest.json")
    assert latest["nodes"][NODE]["spans"]["latest"]["n"] == first["n"]

    again = job.run(settings, now_ms=now + 60_000)
    assert [s["reason"] for s in again["scored"]] == ["reconcile"]
    second = job.read_json(job.window_dir(settings.paths, window) / f"{NODE}.summary.json")["gates"]["geometric"]
    assert second == first  # nothing appended, nothing counted twice
    assert len(job.load_ledger(settings.paths)["windows"]) == 1


def test_incomplete_window_is_not_scored_and_changed_inputs_are_rescored(perf_setup):
    settings, window = perf_setup
    too_early = window + job.WINDOW_MS + 30 * 60_000  # the archive flush has not had its hour yet
    report = job.run(settings, now_ms=too_early, backfill_from_ms=window, aggregate=False)
    assert report["scored"] == []
    now = window + 5 * job.WINDOW_MS
    job.run(settings, now_ms=now, aggregate=False)
    ledger = job.load_ledger(settings.paths)
    assert job.window_label(window) in ledger["windows"]
    # Later windows have no truth and no frames: scored, recorded, and shown as such.
    later = ledger["windows"][job.window_label(window + job.WINDOW_MS)]
    assert later["nodes"][NODE] in ("no_truth", "node_inactive", "insufficient")
    later_summary = job.read_json(job.window_dir(settings.paths, window + job.WINDOW_MS) / f"{NODE}.summary.json")
    assert later_summary["gates"]["geometric"]["n"] == 0
    assert ledger["windows"][job.window_label(window + 2 * job.WINDOW_MS)]["nodes"] == {NODE: "no_truth"}
    # A new truth file for the first window changes its inputs digest, so it is scored again.
    extra = fixes_for("ffeedd", window + 50 * 60_000, window + 55 * 60_000, 10.11, 20.11)
    write_truth_files(settings.paths.truth, extra, window + 50 * 60_000, window + 50 * 60_000 + 1)
    report = job.run(settings, now_ms=now + 8 * job.WINDOW_MS, aggregate=False)
    reasons = {s["window"]: s["reason"] for s in report["scored"]}
    assert reasons[job.window_label(window)] == "changed"


def test_failed_node_marks_the_window_partial_and_keeps_other_results(perf_setup, monkeypatch):
    settings, window = perf_setup
    now = window + 2 * job.WINDOW_MS
    original = pt.score_window

    def explode(inputs):
        if inputs.node_id == NODE:
            raise RuntimeError("boom")
        return original(inputs)

    monkeypatch.setattr(pt, "score_window", explode)
    report = job.run(settings, now_ms=now, backfill_from_ms=window)
    assert report["failed"] == [{"window": job.window_label(window), "nodes": [NODE]}]
    entry = job.load_ledger(settings.paths)["windows"][job.window_label(window)]
    assert entry["status"] == "partial" and entry["nodes"][NODE] == "failed"
    assert not (job.window_dir(settings.paths, window) / f"{NODE}.summary.json").exists()
    latest = job.read_json(settings.paths.out / "latest.json")
    assert latest["windows_partial"] == [job.window_label(window)]
    monkeypatch.setattr(pt, "score_window", original)
    retry = job.run(settings, now_ms=now + 60_000)
    assert [s["reason"] for s in retry["scored"]] == ["retry"]
    assert job.load_ledger(settings.paths)["windows"][job.window_label(window)]["status"] == "ok"


def test_dashboard_renders_statuses_and_figures(perf_setup):
    settings, window = perf_setup
    job.run(settings, now_ms=window + 2 * job.WINDOW_MS, backfill_from_ms=window)
    page = (settings.paths.out / "index.html").read_text()
    assert "too few opportunities" in page and NODE in page and "TEST-TX" in page
    assert "Coverage gates side by side" in page
    doc = job.read_json(settings.paths.out / "nodes" / f"{NODE}.json")
    assert doc["history"][0]["validated"] == 1.0
    assert doc["spans"]["cumulative"]["legacy_node_rate"]["pd"] is None  # no node rows in this archive


def test_window_arithmetic():
    assert job.window_start(T0 + 1) == T0
    assert job.parse_label(job.window_label(T0)) == T0
    end = job.latest_complete_end(T0 + job.WINDOW_MS + 3_600_000 + 15 * 60_000)
    assert end == T0 + job.WINDOW_MS
    assert job.latest_complete_end(T0 + job.WINDOW_MS + 3_600_000 + 14 * 60_000) == T0


def test_truth_from_adsb_store_reads_aggregator_rows_listing_the_node(tmp_path: Path):
    rows = []
    for k in range(6):
        rows.append(
            {
                "ts_ms": T0 + 10_000 * k,
                "hex": "ABC123",
                "lat": 10.12,
                "lon": 20.12 + 0.001 * k,
                "alt_baro": 12000,
                "gs": 300.0,
                "track": 90.0,
                "flight": None,
                "seen_ms": T0 + 10_000 * k - 2_000,
                "world": "real",
                "source": "aggregator",
                "source_node_id": f"radar-beta,{NODE}" if k % 2 else "radar-beta",
            }
        )
    adsb_writer.write_adsb_parquet(rows=rows, base_dir=tmp_path, write_ts=datetime(2026, 3, 10, 12, 40, tzinfo=UTC))
    truth = pt.truth_from_adsb_store(tmp_path, NODE, T0, T0 + HALF_HOUR)
    assert truth.t_ms.size == 3 and truth.hexes.names == ["abc123"]
    assert truth.for_node(NODE).t_ms.size == 3
    table = pa.table({"x": [1]})
    assert table.num_rows == 1
