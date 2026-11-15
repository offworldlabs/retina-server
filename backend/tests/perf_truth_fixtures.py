"""Invented aircraft, nodes and detections for the independent-denominator scorer.

The scenario every builder here shares: one receiver at (10.0, 20.0) and one
transmitter at (10.2, 20.0), 22.1 km apart (the geometry of
test_performance_data.py), registered as node `NODE` with `CONFIG`, a 30-minute
scoring window starting at `T0` (2026-03-10 12:00 UTC), and truth polled for
`REGION` every 10 s. Aircraft fly straight at a steady speed; a detection that
"sees" one carries the delay and Doppler the scorer itself predicts for it, so
tests built on these check the bookkeeping, the gates and the controls, not the
physics.

What each builder makes:

- `fixes_for(hexn, t_from, t_to, lat, lon, ...)`: ADS-B fixes of one straight
  flight, dead-reckoned every 6 s from (lat, lon) at t_from (default 12 000 ft,
  300 kt, heading east).
- `flight(hexn="a1b2c3", **kw)`: the standard aircraft, inside the window for
  its whole flight: from 19 km north-east of the receiver heading west at
  150 kt between `FLIGHT_FROM` and `FLIGHT_TO` (T0+5 min to T0+15 min), so
  after ten minutes it is 35 km west-north-west, still 40 us inside the 200 us
  delay window.
- `truth_of(fixes, polls=None, region=REGION)`: those fixes as a
  `pt.TruthRows` (interned hexes, sorted per aircraft), with a poll every 10 s
  from T0-20 s for three hours unless `polls` is given.
- `predicted(sites, truth, hexn, t_ms)`: the scorer's own (delay_us, doppler_hz)
  for an aircraft at an instant, under the configuration active then.
- `detections_of(rows)`: `pt.Detections` from (frame_ts_ms, delay_us,
  doppler_hz, snr_db, tag_hex or None) rows.
- `clutter_frames(t_from, t_to, step_ms=1000)`: one detection per frame far
  from every aircraft (~150 us), so the node reads as up without seeing anything.
- `seen_frames(sites, truth, hexn, t_from, t_to, *, tag=True, ...)`: one
  detection per frame that fits the aircraft (predicted delay and Doppler plus a
  small deterministic wobble), over its fixes and the dead-reckoned edge either
  side; tagged with its hex unless `tag=False`.
- `score(truth, detections, *, configs=(CONFIG,), from_ms=T0, to_ms=T0+HALF_HOUR,
  **extra)`: runs `pt.score_window` for `NODE`, classing every aircraft with a
  type as "narrowbody"; `geo(result)` is its geometric gate section.
- `registry(path, configs=(CONFIG,), last_seen=...)`: a users.db with the
  node_configs and nodes tables the job reads.
- `write_truth_files(root, fixes, from_ms, to_ms)`: the hourly adsbhub
  jsonl.gz truth files (a poll every 10 s plus the fixes) under root.
- `write_archive(archive_root, det, write_ts, node_id=NODE)`: the detections as
  a node's Parquet archive, one frame per timestamp, tags carried as ADS-B rows;
  pass `node_id` to write a second node's archive.
- `fixture_perf_setup`, the pytest fixture `perf_setup`: a complete job tree
  under tmp_path: registry, truth files and archive for one 80 kt flight seen for
  twenty minutes of the window containing T0, returned as
  (job.Settings, window_start_ms).

Reuse from another test module by importing the names it needs, as tests
import sibling helpers (see `from tests.migration_helpers import ...` in
conftest.py):

    from tests.perf_truth_fixtures import (
        CONFIG,
        NODE,
        T0,
        fixture_perf_setup,  # noqa: F401  registers the perf_setup fixture
        score,
    )

    def test_something(perf_setup):
        settings, window = perf_setup

A fixture imported into a test module is collected by pytest like one defined
there. It is registered under the name `perf_setup` while the function is
`fixture_perf_setup` (pytest's own advice for a fixture requested under its own
name): were the function called perf_setup, every test parameter `perf_setup`
would shadow the import and ruff would flag each one (F811). Ruff cannot see
that the import registers anything, hence the `# noqa: F401`.
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from services import parquet_writer
from services import performance_truth as pt
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


# ── The job's inputs on disk ─────────────────────────────────────────────────


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


def write_archive(archive_root: Path, det: pt.Detections, write_ts: datetime, node_id: str = NODE) -> None:
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
        node_id=node_id, frames=list(frames.values()), base_dir=archive_root, write_ts=write_ts
    )


# Named as pytest's docs suggest for a fixture requested under its own name: a test
# parameter called perf_setup would shadow an imported function perf_setup, and
# ruff (F811) flags that on every test that takes it. Import fixture_perf_setup
# and ask for perf_setup.
@pytest.fixture(name="perf_setup")
def fixture_perf_setup(tmp_path: Path):
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
