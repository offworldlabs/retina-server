"""Node detection performance against an independent ADS-B denominator.

PR #727's tables (services/performance_data.py) count opportunities from the
server's ADS-B archive. Where that archive holds only the nodes' own tags, an
aircraft enters the denominator only because a detection was matched to it, so
the fraction measures how long a tag persists, not whether aircraft are
detected (a lone chance tag scores exactly 1/3 under the ±1 bucket rule). This
module scores a node against aircraft that were there whether or not any radar
saw them, and validates every credited detection against that same truth.

**Truth.** Rows from an ADS-B source that never saw a detection: the recorder's
hourly files (deploy/perf-eval/truth_recorder.py, ADSBHub polled over each
receiver's region), or the archive's ``source="aggregator"`` rows where the
server's own ADSBHub poller runs. Each fix has a capture time, a position, a
pressure altitude, ground speed and track. Fixes of one aircraft form a
trajectory; a position at any instant is interpolated between two fixes at most
TRUTH_MAX_GAP_MS apart, or dead-reckoned up to TRUTH_EDGE_MS past the first or
last fix at its reported speed and track. Beyond that the aircraft is not known
to be anywhere, and it is not an opportunity: a gap in ADS-B is never read as
an absence of aircraft.

**Denominator.** For node n and the 10 s bucket b with centre t_b, aircraft h is
an opportunity when every one of these holds:

* h has a position at t_b (above) and its pressure altitude is positive;
* the config version active at t_b places the receiver and the transmitter, and
  the geometry puts h inside the processing window: the bistatic delay
  (Rt + Rr - L)/c is under the version's window (max_range_km / c), and h is
  above the receiver's horizon plane (up > 0 in the receiver's ENU frame,
  which, computed from ECEF, includes the earth's curvature);
* a coverage gate holds: ``geometric`` (nothing more), ``declared`` (also inside
  the declared antenna wedge, where one is declared), or ``empirical`` (also
  inside the learned field of view, compared in ground range, the range the
  field of view was learned in, from a state saved before the window);
* the node was up: it sent at least one archived frame within ACTIVE_WINDOW_MS
  of the bucket (the archive holds only frames with detections, so this is the
  loosest reading of the frames that is still a liveness signal), and no
  recorded offline span covers the bucket;
* truth was available: the node's region was polled successfully within
  TRUTH_POLL_SLACK_MS of the bucket, so a missing fix means a missing aircraft,
  not a missing poll.

**Numerators.** Several, kept apart:

* ``raw_pm1``: the node tagged h on a detection in bucket b, b-1 or b+1 (PR
  #727's rule, for comparison with its tables);
* ``raw``: the node tagged h in bucket b itself;
* ``validated``: a detection of the node in bucket b whose delay and Doppler
  agree with h's independently predicted delay and Doppler within
  VALIDATE_DELAY_US and VALIDATE_DOPPLER_HZ, with no second aircraft inside that
  box (an ambiguous match is counted apart). The detection need not carry a
  tag: a node with no ADS-B receiver of its own is scored by geometry alone;
* ``validated_tagged``: validated, and the node's own tag names h;
* ``refuted``: the node tagged h in bucket b but no detection so tagged agrees
  with h's geometry, evidence of a false association.

**Background.** The validated rule can match a detection to an aircraft by
chance. Its rate is measured, not assumed: every trajectory is shifted in time
by each of BACKGROUND_SHIFTS_MS, so that the traffic of ``shift`` later (the
same density and routes, unrelated to the detections of now) is scored against
the detections of now, as a set of control opportunities of its own, under the
same gates. The share of control opportunities that a detection fits is the
chance-association background, reported beside the validated rate and
subtracted from it for the ``net`` figure. Nothing is subtracted from the raw
figures.

**Encounters and uncertainty.** Consecutive opportunities of one aircraft with
gaps under ENCOUNTER_GAP_BUCKETS form an encounter. Buckets of one encounter
are correlated, so every interval here is a percentile bootstrap over
encounters (BOOTSTRAP_DRAWS draws), never a binomial on buckets; the net
figure's interval resamples the real and the control encounters independently.
Detection latency is the time from an encounter's first opportunity to its
first validated detection.

Everything is exact integer arithmetic on counts; the only estimates are the
bootstrap intervals and the interpolated positions.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from retina_analytics.constants import R_EARTH

from config.constants import DETECTIONS_PARTITION_SLACK_MS
from services import performance_data as pdata
from services.performance_data import _C_M_PER_S, _FT_TO_M, BUCKET_MS, _ecef

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
WINDOW_MS = 3 * 3_600_000
TRUTH_MAX_GAP_MS = 30_000
TRUTH_EDGE_MS = 6_000
TRUTH_POLL_SLACK_MS = 20_000
ACTIVE_WINDOW_MS = 60_000
ENCOUNTER_GAP_BUCKETS = 6
# The sited nodes' tag delay residual has a MAD of about 0.3 us and the Doppler
# residual against the independent trajectory about 1.0 Hz; the node's own
# association gate is 6.671 us and 5 Hz. Swept on WXIA's first real window
# (2026-11-15, 45 min, 9,265 opportunities): 3 us x 10 Hz read validated 0.554
# against a background of 0.124; 2 x 6 read 0.531 against 0.066; 1.5 x 5 read
# 0.514 against 0.047; 1 x 4 read 0.462 against 0.030. The net (validated less
# background) peaked at 1.5-2 us, so the box is 1.5 us x 5 Hz: about five MADs
# each way, under the node's gate, with a chance rate a tenth of the signal.
VALIDATE_DELAY_US = 1.5
VALIDATE_DOPPLER_HZ = 5.0
BACKGROUND_SHIFTS_MS = (600_000, 1_800_000)
MIN_OPPORTUNITIES = 30
MIN_ENCOUNTERS = 5
BOOTSTRAP_DRAWS = 400
GATES = ("geometric", "declared", "empirical")
COUNTED = ("raw_pm1", "raw", "validated", "validated_tagged", "matched_any", "ambiguous", "refuted")
RANGE_EDGES_KM = pdata.RANGE_EDGES_KM
ALT_EDGES_KFT = pdata.ALT_EDGES_KFT
DELAY_EDGES_US = tuple(range(0, 226, 25))
BISTATIC_EDGES_DEG = (0, 30, 60, 90, 120, 150, 180)
# Detection SNR histograms: 1 dB bins; a value outside the edges counts in the end bin.
SNR_HIST_EDGES_DB = tuple(range(0, 41))
BEARING_SECTOR_DEG = 30
BEARING_BINS = pdata.BEARING_BINS
_T_SPAN = 1 << 41  # ms since the epoch fits under this for the next 50 years
_KT_TO_M_S = 0.514444
_FT_MIN_TO_M_S = 0.3048 / 60.0


# ── Truth ────────────────────────────────────────────────────────────────────


@dataclass
class TruthRows:
    """Independent fixes, as arrays, with the regions they were polled in."""

    hexes: pdata._Interner
    hex_id: np.ndarray  # int64
    t_ms: np.ndarray  # int64 capture time
    lat: np.ndarray
    lon: np.ndarray
    alt_ft: np.ndarray
    gs_kt: np.ndarray
    track_deg: np.ndarray
    vrate_ft_min: np.ndarray
    mlat: np.ndarray  # bool
    region: np.ndarray  # int64 index into regions
    regions: list[str]
    # Successful poll receipt times per region.
    polls: dict[str, np.ndarray]
    flights: dict[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> TruthRows:
        z = np.zeros(0)
        return cls(
            pdata._Interner(),
            np.zeros(0, np.int64),
            np.zeros(0, np.int64),
            z,
            z,
            z,
            z,
            z,
            z,
            np.zeros(0, bool),
            np.zeros(0, np.int64),
            [],
            {},
        )

    def for_node(self, node_id: str) -> TruthRows:
        """The rows and polls of every region whose members include the node."""
        member = np.array([region_covers(r, node_id) for r in self.regions], dtype=bool)
        if not member.any():
            return TruthRows.empty()
        keep = member[self.region]
        polls = {r: p for r, p in self.polls.items() if region_covers(r, node_id)}
        return TruthRows(
            self.hexes,
            self.hex_id[keep],
            self.t_ms[keep],
            self.lat[keep],
            self.lon[keep],
            self.alt_ft[keep],
            self.gs_kt[keep],
            self.track_deg[keep],
            self.vrate_ft_min[keep],
            self.mlat[keep],
            self.region[keep],
            self.regions,
            polls,
            self.flights,
        )


def region_covers(region: str, node_id: str) -> bool:
    """Whether a region name (member ids joined by "+", or ``aggregator:<id>``) covers the node."""
    return node_id in region.removeprefix("aggregator:").split("+")


def hex_lookup(hexes: pdata._Interner, hexn: str | None) -> int:
    """The interner's id for a hex, -1 for none or unknown."""
    return hexes._ids.get(hexn, -1) if hexn else -1


def _hours(from_ms: int, to_ms: int) -> Iterable[datetime]:
    moment = datetime.fromtimestamp(from_ms / 1000.0, tz=UTC).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(to_ms / 1000.0, tz=UTC)
    while moment <= end:
        yield moment
        moment += timedelta(hours=1)


def truth_files(truth_root: str | Path, from_ms: int, to_ms: int) -> list[str]:
    """The recorder's hourly files whose receipt hour overlaps [from_ms, to_ms]."""
    out = []
    for hour in _hours(from_ms, to_ms):
        path = os.path.join(str(truth_root), "adsbhub", hour.strftime("%Y-%m-%d"), hour.strftime("%H") + ".jsonl.gz")
        if os.path.exists(path):
            out.append(path)
    return out


def _num(v: Any) -> float:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else math.nan


def _rows_to_truth(
    hexes: pdata._Interner,
    cols: Mapping[str, list],
    regions: list[str],
    polls: Mapping[str, Sequence[int]],
    flights=None,
) -> TruthRows:
    order = np.lexsort((np.array(cols["t_ms"], dtype=np.int64), np.array(cols["hex_id"], dtype=np.int64)))
    n = len(order)

    def col(name: str, dtype=float) -> np.ndarray:
        values = cols.get(name)
        if values is None:
            return np.full(n, np.nan) if dtype is float else np.zeros(n, dtype=dtype)
        return np.array(values, dtype=dtype)[order]

    return TruthRows(
        hexes=hexes,
        hex_id=col("hex_id", np.int64),
        t_ms=col("t_ms", np.int64),
        lat=col("la"),
        lon=col("lo"),
        alt_ft=col("ab"),
        gs_kt=col("gs"),
        track_deg=col("tr"),
        vrate_ft_min=col("vr"),
        mlat=col("ml", bool),
        region=col("region", np.int64),
        regions=regions,
        polls={name: np.array(sorted(set(times)), dtype=np.int64) for name, times in polls.items()},
        flights=dict(flights or {}),
    )


def load_truth(truth_root: str | Path, from_ms: int, to_ms: int) -> TruthRows:
    """Every recorded fix captured in [from_ms, to_ms], and every successful poll received in it.

    A fix is read from the file of the hour it was received in; receipt lags
    capture by seconds, so the files of the hours around the span are read too
    and the capture time decides. A fix repeated by several polls is one row.
    """
    hexes = pdata._Interner()
    regions: dict[str, int] = {}
    polls: dict[str, list[int]] = {}
    seen: set[tuple[int, int]] = set()
    cols: dict[str, list] = {k: [] for k in ("hex_id", "t_ms", "la", "lo", "ab", "gs", "tr", "vr", "ml", "region")}
    flights: dict[str, str] = {}
    for path in truth_files(truth_root, from_ms - 120_000, to_ms + 120_000):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    kind = row.get("k")
                    name = row.get("g")
                    if not isinstance(name, str):
                        continue
                    if kind == "p":
                        recv = row.get("r")
                        if (
                            row.get("st") == 200
                            and isinstance(recv, int)
                            and from_ms - TRUTH_POLL_SLACK_MS <= recv <= to_ms + TRUTH_POLL_SLACK_MS
                        ):
                            polls.setdefault(name, []).append(recv)
                        continue
                    if kind != "a":
                        continue
                    t = row.get("c")
                    if not isinstance(t, int) or t < from_ms or t > to_ms:
                        continue
                    hexn = row.get("h")
                    if not isinstance(hexn, str) or not hexn:
                        continue
                    hex_id = hexes.intern(hexn)
                    if (hex_id, t) in seen:
                        continue
                    seen.add((hex_id, t))
                    lat, lon = _num(row.get("la")), _num(row.get("lo"))
                    if not (math.isfinite(lat) and math.isfinite(lon)):
                        continue
                    cols["hex_id"].append(hex_id)
                    cols["t_ms"].append(t)
                    cols["la"].append(lat)
                    cols["lo"].append(lon)
                    cols["ab"].append(_num(row.get("ab")))
                    cols["gs"].append(_num(row.get("gs")))
                    cols["tr"].append(_num(row.get("tr")))
                    cols["vr"].append(_num(row.get("vr")))
                    cols["ml"].append(bool(row.get("ml")))
                    cols["region"].append(regions.setdefault(name, len(regions)))
                    flight = row.get("f")
                    if isinstance(flight, str) and flight and hexn not in flights:
                        flights[hexn] = flight
        except (OSError, EOFError) as exc:
            logger.warning("performance truth: unreadable %s: %s", path, exc)
    return _rows_to_truth(hexes, cols, list(regions), polls, flights)


def truth_from_adsb_store(adsb_root: str | Path, node_id: str, from_ms: int, to_ms: int) -> TruthRows:
    """Aggregator rows of the server's ADS-B archive that list the node, as truth.

    Where the server's own ADSBHub poller runs, its snapshot writes every polled
    fix inside a disc around each node's published position, attributed to the
    nodes whose disc holds it (services/tasks/adsb_snapshot.py). The attribution
    reads positions, never detections, so these rows are independent of the
    radar. A fix repeated by several snapshots (same hex and capture time) is
    one row; the snapshots themselves stand in for polls.
    """
    hexes = pdata._Interner()
    cols: dict[str, list] = {k: [] for k in ("hex_id", "t_ms", "la", "lo", "ab", "gs", "tr", "region")}
    snapshots: list[int] = []
    seen: set[tuple[int, int]] = set()
    low, high = from_ms - 60_000, to_ms + 60_000
    columns = ["ts_ms", "hex", "lat", "lon", "alt_baro", "gs", "track", "seen_ms", "world", "source", "source_node_id"]
    for day in _days(from_ms, to_ms):
        for path in pdata._adsb_files(adsb_root, day):
            for table in pdata._row_groups(path, columns, {"ts_ms": (low, high)}):
                table = table.filter(
                    pc.and_(
                        pc.and_(pc.equal(table["world"], "real"), pc.equal(table["source"], "aggregator")),
                        pc.and_(pc.greater_equal(table["ts_ms"], low), pc.less(table["ts_ms"], high)),
                    )
                )
                if not table.num_rows:
                    continue
                listed = np.array(
                    [node_id in (ids or "").split(",") for ids in table["source_node_id"].to_pylist()], dtype=bool
                )
                snapshots.extend(np.unique(table["ts_ms"].to_numpy()[listed]).tolist())
                for row in table.filter(pa.array(listed)).to_pylist():
                    t = row["seen_ms"]
                    if t < from_ms or t > to_ms:
                        continue
                    hex_id = hexes.intern(row["hex"].lower())
                    if (hex_id, t) in seen:
                        continue
                    seen.add((hex_id, t))
                    cols["hex_id"].append(hex_id)
                    cols["t_ms"].append(t)
                    cols["la"].append(row["lat"])
                    cols["lo"].append(row["lon"])
                    cols["ab"].append(_num(row["alt_baro"]))
                    cols["gs"].append(_num(row["gs"]))
                    cols["tr"].append(_num(row["track"]))
                    cols["region"].append(0)
    region = f"aggregator:{node_id}"
    return _rows_to_truth(hexes, cols, [region], {region: snapshots})


def merge_truth(parts: Sequence[TruthRows]) -> TruthRows:
    """Rows of several sources as one, hex ids re-interned."""
    parts = [p for p in parts if p.t_ms.size or p.polls]
    if not parts:
        return TruthRows.empty()
    if len(parts) == 1:
        return parts[0]
    hexes = pdata._Interner()
    regions: list[str] = []
    polls: dict[str, np.ndarray] = {}
    flights: dict[str, str] = {}
    cols: dict[str, list] = {k: [] for k in ("hex_id", "t_ms", "la", "lo", "ab", "gs", "tr", "vr", "ml", "region")}
    for part in parts:
        lookup = [hexes.intern(name) for name in part.hexes.names]
        offset = len(regions)
        regions.extend(part.regions)
        polls.update(part.polls)
        flights.update(part.flights)
        cols["hex_id"].extend(lookup[i] for i in part.hex_id.tolist())
        cols["t_ms"].extend(part.t_ms.tolist())
        cols["la"].extend(part.lat.tolist())
        cols["lo"].extend(part.lon.tolist())
        cols["ab"].extend(part.alt_ft.tolist())
        cols["gs"].extend(part.gs_kt.tolist())
        cols["tr"].extend(part.track_deg.tolist())
        cols["vr"].extend(part.vrate_ft_min.tolist())
        cols["ml"].extend(part.mlat.tolist())
        cols["region"].extend((part.region + offset).tolist())
    return _rows_to_truth(hexes, cols, regions, {k: v.tolist() for k, v in polls.items()}, flights)


# ── Trajectories ─────────────────────────────────────────────────────────────


class Trajectories:
    """Positions and velocities of every truth aircraft at any instant, from its fixes.

    The fixes arrive sorted by aircraft then time (``_rows_to_truth``), so one
    aircraft's rows are a contiguous run and ``keys`` is sorted.
    """

    def __init__(self, truth: TruthRows) -> None:
        self.hexes = truth.hexes
        self.hex_id = truth.hex_id
        self.t_ms = truth.t_ms
        self.lat = truth.lat
        self.lon = truth.lon
        self.alt_ft = np.where(np.isfinite(truth.alt_ft), truth.alt_ft, np.nan)
        speed = np.where(np.isfinite(truth.gs_kt), truth.gs_kt, 0.0) * _KT_TO_M_S
        track = np.radians(np.where(np.isfinite(truth.track_deg), truth.track_deg, 0.0))
        self.ve = speed * np.sin(track)
        self.vn = speed * np.cos(track)
        self.vu = np.where(np.isfinite(truth.vrate_ft_min), truth.vrate_ft_min, 0.0) * _FT_MIN_TO_M_S
        self.keys = self.hex_id * _T_SPAN + self.t_ms
        self.n = self.t_ms.size

    def present(self, from_ms: int, to_ms: int) -> np.ndarray:
        """Hex ids with a fix inside the span, widened by the edge allowance."""
        keep = (self.t_ms >= from_ms - TRUTH_EDGE_MS) & (self.t_ms <= to_ms + TRUTH_EDGE_MS)
        return np.unique(self.hex_id[keep])

    def spans(self, hex_id: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Each aircraft's first and last fix time; an aircraft without fixes gets an empty span (first > last).

        ``at`` is valid only within TRUTH_EDGE_MS of this span, so a query
        outside it can be skipped without being made.
        """
        q = np.asarray(hex_id, dtype=np.int64)
        far = np.iinfo(np.int64).max // 4
        if not self.n:
            return np.full(q.size, far, dtype=np.int64), np.full(q.size, -far, dtype=np.int64)
        first = np.searchsorted(self.hex_id, q, side="left")
        last = np.searchsorted(self.hex_id, q, side="right") - 1
        has = last >= first
        t_first = np.where(has, self.t_ms[np.clip(first, 0, self.n - 1)], far)
        t_last = np.where(has, self.t_ms[np.clip(last, 0, self.n - 1)], -far)
        return t_first, t_last

    def at(self, hex_id: np.ndarray, t_ms: np.ndarray, shift_ms: int = 0) -> dict[str, np.ndarray]:
        """Each (aircraft, instant): lat, lon, alt_ft, ENU velocity and ``valid``.

        With ``shift_ms`` the trajectories are read ``shift_ms`` later than asked,
        the time-shifted control.
        """
        q_t = np.asarray(t_ms, dtype=np.int64) + shift_ms
        q_h = np.asarray(hex_id, dtype=np.int64)
        m = q_t.size
        out = {
            "lat": np.full(m, np.nan),
            "lon": np.full(m, np.nan),
            "alt_ft": np.full(m, np.nan),
            "ve": np.zeros(m),
            "vn": np.zeros(m),
            "vu": np.zeros(m),
            "valid": np.zeros(m, dtype=bool),
        }
        if not self.n or not m:
            return out
        q = q_h * _T_SPAN + q_t
        after = np.searchsorted(self.keys, q, side="right")
        prev = after - 1
        has_prev = (prev >= 0) & (self.hex_id[np.clip(prev, 0, self.n - 1)] == q_h)
        has_next = (after < self.n) & (self.hex_id[np.clip(after, 0, self.n - 1)] == q_h)
        p = np.clip(prev, 0, self.n - 1)
        nx = np.clip(after, 0, self.n - 1)
        dt_prev = np.where(has_prev, q_t - self.t_ms[p], np.inf)
        dt_next = np.where(has_next, self.t_ms[nx] - q_t, np.inf)

        both = has_prev & has_next & ((dt_prev + dt_next) <= TRUTH_MAX_GAP_MS)
        w = np.zeros(m)
        span = dt_prev + dt_next
        w[both] = dt_prev[both] / np.maximum(span[both], 1.0)
        for name, src in (
            ("lat", self.lat),
            ("lon", self.lon),
            ("alt_ft", self.alt_ft),
            ("ve", self.ve),
            ("vn", self.vn),
            ("vu", self.vu),
        ):
            out[name][both] = src[p[both]] * (1 - w[both]) + src[nx[both]] * w[both]

        near_prev = has_prev & (dt_prev <= TRUTH_EDGE_MS)
        near_next = has_next & (dt_next <= TRUTH_EDGE_MS)
        for mask, idx, sign, dt in (
            (~both & near_prev, p, 1.0, dt_prev),
            (~both & ~near_prev & near_next, nx, -1.0, dt_next),
        ):
            if not mask.any():
                continue
            i = idx[mask]
            seconds = sign * dt[mask] / 1000.0
            lat0 = self.lat[i]
            out["ve"][mask], out["vn"][mask], out["vu"][mask] = self.ve[i], self.vn[i], self.vu[i]
            out["lat"][mask] = lat0 + self.vn[i] * seconds / 111_195.0
            out["lon"][mask] = self.lon[i] + self.ve[i] * seconds / (
                111_195.0 * np.maximum(np.cos(np.radians(lat0)), 0.01)
            )
            out["alt_ft"][mask] = self.alt_ft[i] + self.vu[i] * seconds / _FT_TO_M
        out["valid"] = np.isfinite(out["lat"]) & np.isfinite(out["lon"]) & np.isfinite(out["alt_ft"])
        return out


# ── Sites ────────────────────────────────────────────────────────────────────


@dataclass
class SiteVersion:
    site: pdata._Site | None
    fc_hz: float
    created_ms: int
    version: int


class Sites:
    """A node's config versions in time order; which one is active at each instant."""

    def __init__(self, configs: Sequence[Mapping[str, Any]]) -> None:
        ordered = sorted(configs, key=lambda c: pdata._created_ms(c["created_at"]))
        self.versions = [
            SiteVersion(
                pdata._site(c),
                float(c.get("fc_hz") or math.nan),
                pdata._created_ms(c["created_at"]),
                int(c.get("version") or i + 1),
            )
            for i, c in enumerate(ordered)
        ]
        self._created = np.array([v.created_ms for v in self.versions], dtype=np.int64)

    def active(self, t_ms: np.ndarray) -> np.ndarray:
        """Index of the version active at each instant: the latest created by then, else the oldest."""
        if not self.versions:
            return np.full(np.asarray(t_ms).shape, -1, dtype=np.int64)
        return np.maximum(np.searchsorted(self._created, np.asarray(t_ms, dtype=np.int64), side="right") - 1, 0)

    def split(self, t_ms: np.ndarray) -> Iterable[tuple[SiteVersion, np.ndarray]]:
        active = self.active(t_ms)
        for v in np.unique(active).tolist():
            if v >= 0 and self.versions[v].site is not None:
                yield self.versions[v], np.flatnonzero(active == v)


def _enu_to_ecef_velocity(
    lat: np.ndarray, lon: np.ndarray, ve: np.ndarray, vn: np.ndarray, vu: np.ndarray
) -> np.ndarray:
    la, lo = np.radians(lat), np.radians(lon)
    east = np.stack([-np.sin(lo), np.cos(lo), np.zeros_like(lo)], axis=-1)
    north = np.stack([-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)], axis=-1)
    up = np.stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)], axis=-1)
    return ve[:, None] * east + vn[:, None] * north + vu[:, None] * up


def ground_km(rx_lat: float, rx_lon: float, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Great-circle range from the receiver on the sphere the field of view is learned on."""
    d_lat = np.radians(lat - rx_lat)
    d_lon = np.radians(lon - rx_lon)
    a = np.sin(d_lat / 2) ** 2 + math.cos(math.radians(rx_lat)) * np.cos(np.radians(lat)) * np.sin(d_lon / 2) ** 2
    return R_EARTH * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def predict(site: pdata._Site, fc_hz: float, pos: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Bistatic delay, Doppler, ranges, bearing, horizon height and bistatic angle of each position.

    Doppler is -(d/dt)(Rt + Rr) / lambda, the sign RETINA's nodes report: on
    tagged detections of a sited node the residual against the fix's own speed
    and track has a median of -0.1 Hz with this sign and -41 Hz with the other.
    """
    lat, lon, alt_m = pos["lat"], pos["lon"], pos["alt_ft"] * _FT_TO_M
    look = site.look(lat, lon, alt_m)
    target = _ecef(lat, lon, alt_m)
    u_r = (target - site.rx) / np.maximum(look.rr_m, 1.0)[:, None]
    u_t = (target - site.tx) / np.maximum(look.rt_m, 1.0)[:, None]
    vel = _enu_to_ecef_velocity(lat, lon, pos["ve"], pos["vn"], pos["vu"])
    range_rate = np.sum(vel * (u_r + u_t), axis=1)
    wavelength = _C_M_PER_S / fc_hz if fc_hz and math.isfinite(fc_hz) else math.nan
    cos_beta = np.clip(np.sum(u_r * u_t, axis=1), -1.0, 1.0)
    return {
        "delay_us": look.delay_us,
        "doppler_hz": -range_rate / wavelength,
        "rr_km": look.rr_m / 1000.0,
        "rt_km": look.rt_m / 1000.0,
        "ground_km": ground_km(site.rx_lat_deg, site.rx_lon_deg, lat, lon),
        "up_m": look.up_m,
        "bearing_deg": look.bearing_deg,
        "bistatic_deg": np.degrees(np.arccos(cos_beta)),
    }


# ── Detections ───────────────────────────────────────────────────────────────


@dataclass
class Detections:
    frame_ts_ms: np.ndarray
    delay_us: np.ndarray
    doppler_hz: np.ndarray
    snr_db: np.ndarray
    tag_hex: list[str | None]
    frames: np.ndarray  # sorted distinct frame times

    @property
    def n(self) -> int:
        return self.frame_ts_ms.size


_DETECTION_COLUMNS = ("frame_ts_ms", "delay_us", "doppler_hz", "snr_db", "adsb_hex")


def _days(from_ms: int, to_ms: int) -> list[date]:
    first = datetime.fromtimestamp(from_ms / 1000.0, tz=UTC).date()
    last = datetime.fromtimestamp(to_ms / 1000.0, tz=UTC).date()
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def load_detections(archive_root: str | Path, node_id: str, from_ms: int, to_ms: int) -> Detections:
    """The node's archived detections with from_ms <= frame_ts_ms < to_ms."""
    parts = []
    for day in _days(from_ms - DETECTIONS_PARTITION_SLACK_MS, to_ms):
        for path in pdata._detection_files(archive_root, node_id, day):
            for table in pdata._row_groups(path, _DETECTION_COLUMNS, {"frame_ts_ms": (from_ms, to_ms - 1)}):
                table = table.filter(pdata._between(table["frame_ts_ms"], from_ms, to_ms))
                if table.num_rows:
                    parts.append(table)
    if not parts:
        z = np.zeros(0)
        return Detections(np.zeros(0, np.int64), z, z, z, [], np.zeros(0, np.int64))
    table = pa.concat_tables(parts).combine_chunks()
    ts = table["frame_ts_ms"].to_numpy(zero_copy_only=False).astype(np.int64)
    order = np.argsort(ts, kind="stable")
    tags = pc.utf8_lower(table["adsb_hex"]).to_pylist() if "adsb_hex" in table.column_names else [None] * len(ts)
    return Detections(
        frame_ts_ms=ts[order],
        delay_us=pdata._floats(table, "delay_us")[order],
        doppler_hz=pdata._floats(table, "doppler_hz")[order],
        snr_db=pdata._floats(table, "snr_db")[order],
        tag_hex=[tags[i] for i in order.tolist()],
        frames=np.unique(ts),
    )


def offline_spans(events: Iterable[Mapping[str, Any]], node_id: str, horizon_ms: int) -> list[tuple[int, int]]:
    """(from_ms, to_ms) spans the server's events log says the node was offline.

    An event ``Node X went offline (no heartbeat for Ns)`` at ``ts`` opens a span
    at ``ts - N``; it closes at the node's next frame, which the caller supplies
    through ``horizon_ms`` when it has none (the end of the window).
    """
    spans = []
    for event in events:
        meta = event.get("meta") or {}
        if event.get("category") != "node" or meta.get("node_id") != node_id:
            continue
        message = str(event.get("message", ""))
        if "went offline" not in message:
            continue
        ts = event.get("ts")
        age = meta.get("age_s")
        if not isinstance(ts, int | float) or not isinstance(age, int | float):
            continue
        start = int((ts - age) * 1000)
        spans.append((start, horizon_ms))
    return sorted(spans)


def active_buckets(frames: np.ndarray, bucket_starts: np.ndarray, spans: Sequence[tuple[int, int]]) -> np.ndarray:
    """Whether each bucket had a frame within ACTIVE_WINDOW_MS and lay in no offline span.

    An offline span closes at the first frame after it opens.
    """
    n = bucket_starts.size
    if not n:
        return np.zeros(0, dtype=bool)
    centres = bucket_starts + BUCKET_MS // 2
    if frames.size:
        after = np.searchsorted(frames, centres - ACTIVE_WINDOW_MS, side="left")
        before = np.searchsorted(frames, centres + ACTIVE_WINDOW_MS, side="right")
        active = before > after
    else:
        active = np.zeros(n, dtype=bool)
    for start, end in spans:
        if frames.size:
            later = frames[frames > start]
            close = int(later[0]) if later.size else end
        else:
            close = end
        active &= ~((centres >= start) & (centres < close))
    return active


def truth_available(polls: Mapping[str, np.ndarray], bucket_starts: np.ndarray) -> np.ndarray:
    """Whether any of the node's regions was polled successfully within TRUTH_POLL_SLACK_MS of each bucket."""
    out = np.zeros(bucket_starts.size, dtype=bool)
    for times in polls.values():
        if not times.size:
            continue
        after = np.searchsorted(times, bucket_starts - TRUTH_POLL_SLACK_MS, side="left")
        before = np.searchsorted(times, bucket_starts + BUCKET_MS + TRUTH_POLL_SLACK_MS, side="right")
        out |= before > after
    return out


# ── Scoring one node and window ──────────────────────────────────────────────


@dataclass
class WindowInputs:
    node_id: str
    from_ms: int
    to_ms: int
    sites: Sites
    truth: TruthRows
    detections: Detections
    offline: Sequence[tuple[int, int]] = ()
    in_learned_fov: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None
    types_for: Callable[[Sequence[str]], list[str | None]] | None = None
    class_for: Callable[[str | None], str] | None = None
    validate_delay_us: float = VALIDATE_DELAY_US
    validate_doppler_hz: float = VALIDATE_DOPPLER_HZ
    background_shifts_ms: Sequence[int] = BACKGROUND_SHIFTS_MS


def _match(
    det: Detections,
    traj: Trajectories,
    sites: Sites,
    hex_ids: np.ndarray,
    tol_delay: float,
    tol_doppler: float,
    shift_ms: int = 0,
    tag_ids: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """For each detection: the nearest truth aircraft in the (delay, Doppler) box, if any.

    Returns per detection ``match`` (hex id, -1 when none), ``ambiguous`` (a
    second aircraft also inside the box), ``d_delay`` and ``d_doppler`` of the
    match, ``tag_present`` (the detection's own tag names a truth aircraft) and
    ``tag_fits`` (that aircraft's geometry fits the detection). Candidates are
    the aircraft of ``hex_ids`` (sorted). Pairing is done per bucket, against
    only the candidates whose fixes, widened by TRUTH_EDGE_MS, reach the
    bucket's frame times (no other aircraft has a position there), and each
    one's geometry is predicted once per distinct frame time, then read for
    every detection of that frame; the pairs left out would all have been
    positionless, so the result is the one the full grid gives. ``tag_ids``,
    the detections' tags as hex ids (-1 for none), is looked up when not
    given. With ``shift_ms`` the trajectories are read that much later: the
    control.
    """
    n = det.n
    out = {
        "match": np.full(n, -1, dtype=np.int64),
        "ambiguous": np.zeros(n, dtype=bool),
        "d_delay": np.full(n, np.nan),
        "d_doppler": np.full(n, np.nan),
        "tag_fits": np.zeros(n, dtype=bool),
        "tag_present": np.zeros(n, dtype=bool),
    }
    if not n or not hex_ids.size or not traj.n:
        return out
    if tag_ids is None:
        tag_ids = np.array([hex_lookup(traj.hexes, h) for h in det.tag_hex], dtype=np.int64)
    # A tag names a truth aircraft when that aircraft has fixes in the window, wherever it was at the time.
    out["tag_present"] = (tag_ids >= 0) & np.isin(tag_ids, hex_ids)
    first_fix, last_fix = traj.spans(hex_ids)
    reach_from, reach_to = first_fix - TRUTH_EDGE_MS, last_fix + TRUTH_EDGE_MS
    buckets = det.frame_ts_ms // BUCKET_MS
    starts = np.flatnonzero(np.r_[True, buckets[1:] != buckets[:-1]])
    ends = np.r_[starts[1:], n]
    # Each bucket's candidates at its distinct frame times, all positioned and predicted in one pass.
    grids = []
    for s, e in zip(starts.tolist(), ends.tolist(), strict=True):
        frames, frame_of = np.unique(det.frame_ts_ms[s:e], return_inverse=True)
        cand = hex_ids[(reach_from <= frames[-1] + shift_ms) & (reach_to >= frames[0] + shift_ms)]
        grids.append((s, e, cand, frames, frame_of))
    grid_h = np.concatenate([np.repeat(cand, frames.size) for _, _, cand, frames, _ in grids])
    grid_t = np.concatenate([np.tile(frames, cand.size) for _, _, cand, frames, _ in grids])
    pos = traj.at(grid_h, grid_t, shift_ms)
    valid = pos["valid"] & (pos["alt_ft"] > 0)
    pred_delay = np.full(grid_t.size, np.nan)
    pred_doppler = np.full(grid_t.size, np.nan)
    for version, picked in sites.split(grid_t[valid]):
        at = np.flatnonzero(valid)[picked]
        p = predict(version.site, version.fc_hz, {k: v[at] for k, v in pos.items()})
        pred_delay[at] = p["delay_us"]
        pred_doppler[at] = p["doppler_hz"]
    offset = 0
    for s, e, cand, frames, frame_of in grids:
        part = slice(offset, offset + cand.size * frames.size)
        offset = part.stop
        if not cand.size or not valid[part].any():
            continue
        rows = np.arange(s, e)
        # Candidate x detection, each detection reading the prediction at its frame.
        bucket_delay = pred_delay[part].reshape(cand.size, frames.size)[:, frame_of]
        bucket_doppler = pred_doppler[part].reshape(cand.size, frames.size)[:, frame_of]
        d_delay = (det.delay_us[rows][None, :] - bucket_delay) / tol_delay
        d_doppler = (det.doppler_hz[rows][None, :] - bucket_doppler) / tol_doppler
        dist = np.where(np.isfinite(d_delay) & np.isfinite(d_doppler), np.hypot(d_delay, d_doppler), np.inf)
        inside = dist <= 1.0
        count = inside.sum(axis=0)
        best = np.argmin(dist, axis=0)
        hit = count >= 1
        out["match"][rows[hit]] = cand[best[hit]]
        out["ambiguous"][rows] = count >= 2
        out["d_delay"][rows[hit]] = d_delay[best[hit], np.flatnonzero(hit)] * tol_delay
        out["d_doppler"][rows[hit]] = d_doppler[best[hit], np.flatnonzero(hit)] * tol_doppler
        tagged = tag_ids[rows] >= 0
        if tagged.any():
            tags = tag_ids[rows][tagged]
            # A tagged aircraft outside the candidates has no position in this bucket, so its tag cannot fit.
            at_c = np.searchsorted(cand, tags)
            in_c = (at_c < cand.size) & (cand[np.clip(at_c, 0, cand.size - 1)] == tags)
            cols = np.flatnonzero(tagged)[in_c]
            out["tag_fits"][rows[cols]] = inside[at_c[in_c], cols]
    return out


@dataclass
class WindowResult:
    node_id: str
    from_ms: int
    to_ms: int
    buckets: pa.Table  # one row per (aircraft, bucket) with a known position, real and control
    encounters: pa.Table
    summary: dict[str, Any]


@dataclass
class _Grid:
    """Every (aircraft, bucket) with a known position, at a time shift, and its geometry."""

    hex_id: np.ndarray
    bucket: np.ndarray
    pos: dict[str, np.ndarray]
    geo: dict[str, np.ndarray]
    version: np.ndarray
    eligible: np.ndarray
    declared: np.ndarray
    empirical: np.ndarray

    @property
    def size(self) -> int:
        return self.hex_id.size


_GEO_KEYS = ("delay_us", "doppler_hz", "rr_km", "rt_km", "ground_km", "up_m", "bearing_deg", "bistatic_deg")


def _grid(
    traj: Trajectories,
    sites: Sites,
    centres: np.ndarray,
    from_ms: int,
    to_ms: int,
    shift_ms: int,
    in_learned_fov: Callable[[np.ndarray, np.ndarray], np.ndarray] | None,
) -> _Grid:
    hex_ids = traj.present(from_ms + shift_ms, to_ms + shift_ms)
    n_buckets = centres.size
    grid_h = np.repeat(hex_ids, n_buckets)
    grid_b = np.tile(np.arange(n_buckets), hex_ids.size)
    pos = traj.at(grid_h, centres[grid_b], shift_ms)
    known = pos["valid"]
    grid_h, grid_b = grid_h[known], grid_b[known]
    pos = {k: v[known] for k, v in pos.items()}
    m = grid_h.size
    geo = {k: np.full(m, np.nan) for k in _GEO_KEYS}
    window_us = np.full(m, np.nan)
    version_no = np.full(m, -1, dtype=np.int64)
    declared = np.zeros(m, dtype=bool)
    for version, picked in sites.split(centres[grid_b]):
        p = predict(version.site, version.fc_hz, {k: v[picked] for k, v in pos.items()})
        for k in geo:
            geo[k][picked] = p[k]
        window_us[picked] = version.site.window_us
        version_no[picked] = version.version
        declared[picked] = version.site.in_declared_beam(p["bearing_deg"])
    eligible = (pos["alt_ft"] > 0) & (version_no >= 0) & (geo["delay_us"] < window_us) & (geo["up_m"] > 0)
    gate_declared = eligible & declared
    if in_learned_fov is not None and m:
        gate_empirical = eligible & np.asarray(in_learned_fov(geo["bearing_deg"], geo["ground_km"]), dtype=bool)
    else:
        gate_empirical = gate_declared.copy()
    return _Grid(grid_h, grid_b, pos, geo, version_no, eligible, gate_declared, gate_empirical)


def _encounter_ids(
    hex_id: np.ndarray, bucket: np.ndarray, opportunity: np.ndarray, first_id: int
) -> tuple[np.ndarray, int]:
    """Encounter id per row (-1 outside opportunities); ids start at first_id. Returns the next free id."""
    ids = np.full(hex_id.size, -1, dtype=np.int64)
    order = np.lexsort((bucket, hex_id))
    opp = order[opportunity[order]]  # the opportunities, in (aircraft, bucket) order
    if not opp.size:
        return ids, first_id
    h, b = hex_id[opp], np.asarray(bucket[opp], dtype=np.int64)
    # A new encounter opens at another aircraft, or after a gap over ENCOUNTER_GAP_BUCKETS.
    opens = np.r_[True, (h[1:] != h[:-1]) | (b[1:] - b[:-1] > ENCOUNTER_GAP_BUCKETS)]
    ids[opp] = first_id - 1 + np.cumsum(opens)
    return ids, first_id + int(opens.sum())


def _snr_per_row(
    row_keys: np.ndarray, det_keys: np.ndarray, snr_db: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per row: the highest and the median SNR of the detections sharing its key, and how many there are.

    ``det_keys`` are the (aircraft, bucket) keys of the unambiguously matched
    detections and ``snr_db`` their SNR. A row without one gets NaN, NaN and 0.
    A detection without an SNR is counted but enters neither statistic.
    """
    m = row_keys.size
    top = np.full(m, np.nan)
    mid = np.full(m, np.nan)
    count = np.zeros(m, dtype=np.int64)
    if not m or not det_keys.size:
        return top, mid, count
    uniq, inverse, counts = np.unique(det_keys, return_inverse=True, return_counts=True)
    at = np.minimum(np.searchsorted(uniq, row_keys), uniq.size - 1)
    found = uniq[at] == row_keys
    count[found] = counts[at[found]]
    finite = np.isfinite(snr_db)
    if finite.any():
        group, level = inverse[finite], snr_db[finite]
        order = np.lexsort((level, group))  # by key, then by SNR
        group, level = group[order], level[order]
        starts = np.flatnonzero(np.r_[True, group[1:] != group[:-1]])
        sizes = np.diff(np.r_[starts, group.size])
        key_top = np.full(uniq.size, np.nan)
        key_mid = np.full(uniq.size, np.nan)
        key_top[group[starts]] = level[starts + sizes - 1]
        key_mid[group[starts]] = (level[starts + (sizes - 1) // 2] + level[starts + sizes // 2]) / 2.0
        top[found] = key_top[at[found]]
        mid[found] = key_mid[at[found]]
    return top, mid, count


def score_window(inputs: WindowInputs) -> WindowResult:
    """Score one node over one window; see the module docstring for every definition."""
    node_id, from_ms, to_ms = inputs.node_id, inputs.from_ms, inputs.to_ms
    truth = inputs.truth.for_node(node_id) if inputs.truth.regions else inputs.truth
    traj = Trajectories(truth)
    starts = np.arange(from_ms, to_ms, BUCKET_MS, dtype=np.int64)
    n_buckets = starts.size
    centres = starts + BUCKET_MS // 2
    active = active_buckets(inputs.detections.frames, starts, inputs.offline)
    available = truth_available(truth.polls, starts)
    det = inputs.detections
    det_bucket = (det.frame_ts_ms - from_ms) // BUCKET_MS
    in_window = (det_bucket >= 0) & (det_bucket < n_buckets)
    tag_ids = np.array([hex_lookup(truth.hexes, h) for h in det.tag_hex], dtype=np.int64)
    tagged = tag_ids >= 0
    tag_keys = np.unique(tag_ids[tagged] * _T_SPAN + det_bucket[tagged])

    class_for = inputs.class_for or (lambda t: "unknown")
    type_cache: dict[int, str | None] = {}

    def types_of(hex_ids: np.ndarray) -> None:
        missing = [h for h in hex_ids.tolist() if h not in type_cache]
        if not missing:
            return
        names = [truth.hexes.names[h] for h in missing]
        types = inputs.types_for(names) if inputs.types_for else [None] * len(names)
        type_cache.update(zip(missing, types, strict=True))

    frames_per_bucket = np.bincount(
        ((det.frames - from_ms) // BUCKET_MS)[(det.frames >= from_ms) & (det.frames < to_ms)], minlength=n_buckets
    )
    dets_per_bucket = np.bincount(det_bucket[in_window], minlength=n_buckets)

    tables: list[pa.Table] = []
    encounter_tables: list[pa.Table] = []
    next_id = 1
    real_match: dict[str, np.ndarray] | None = None
    for shift in (0, *inputs.background_shifts_ms):
        grid = _grid(traj, inputs.sites, centres, from_ms, to_ms, int(shift), inputs.in_learned_fov)
        m = grid.size
        up = active[grid.bucket]
        avail = available[grid.bucket]
        opportunity = grid.eligible & up & avail
        keys = grid.hex_id * _T_SPAN + grid.bucket
        hex_ids = np.unique(grid.hex_id)
        match = _match(
            det,
            traj,
            inputs.sites,
            hex_ids,
            inputs.validate_delay_us,
            inputs.validate_doppler_hz,
            int(shift),
            tag_ids=tag_ids,
        )
        hit = (match["match"] >= 0) & in_window
        clean = hit & ~match["ambiguous"]
        clean_keys = match["match"][clean] * _T_SPAN + det_bucket[clean]
        validated = np.isin(keys, np.unique(clean_keys))
        # The SNR of the looks that validate the bucket: its best, its median, and how many there were.
        snr_top, snr_mid, looks = _snr_per_row(keys, clean_keys, det.snr_db[clean])
        matched_any = np.isin(keys, np.unique(match["match"][hit] * _T_SPAN + det_bucket[hit]))
        amb = hit & match["ambiguous"]
        ambiguous = np.isin(keys, np.unique(match["match"][amb] * _T_SPAN + det_bucket[amb]))
        if shift == 0:
            real_match = match
            raw = np.isin(keys, tag_keys)
            raw_pm1 = raw | np.isin(keys - 1, tag_keys) | np.isin(keys + 1, tag_keys)
            tagged_fit = clean & (tag_ids == match["match"])
            validated_tagged = np.isin(keys, np.unique(match["match"][tagged_fit] * _T_SPAN + det_bucket[tagged_fit]))
            fits = tagged & match["tag_fits"] & in_window
            refuted = raw & ~np.isin(keys, np.unique(tag_ids[fits] * _T_SPAN + det_bucket[fits]))
        else:
            # The node's tags name the aircraft of now, not the shifted ones: raw figures have no control.
            raw = raw_pm1 = validated_tagged = refuted = np.zeros(m, dtype=bool)
        encounter, next_id = _encounter_ids(grid.hex_id, grid.bucket, opportunity, next_id)
        types_of(hex_ids)
        row_type = [type_cache[h] for h in grid.hex_id.tolist()]
        row_class = [class_for(t) for t in row_type]
        hex_names = [truth.hexes.names[h] for h in grid.hex_id.tolist()]
        table = pa.table(
            {
                "node_id": pa.array([node_id] * m, pa.string()),
                "shift_s": pa.array(np.full(m, int(shift) // 1000, dtype=np.int64), pa.int64()),
                "bucket_ms": pa.array(starts[grid.bucket] if m else np.zeros(0, np.int64), pa.int64()),
                "hex": pa.array(hex_names, pa.string()),
                "type": pa.array(row_type, pa.string()),
                "class": pa.array(row_class, pa.string()),
                "lat": pa.array(grid.pos["lat"], pa.float64()),
                "lon": pa.array(grid.pos["lon"], pa.float64()),
                "alt_ft": pa.array(grid.pos["alt_ft"], pa.float64()),
                "rr_km": pa.array(grid.geo["rr_km"], pa.float64()),
                "rt_km": pa.array(grid.geo["rt_km"], pa.float64()),
                "ground_km": pa.array(grid.geo["ground_km"], pa.float64()),
                "delay_us": pa.array(grid.geo["delay_us"], pa.float64()),
                "doppler_hz": pa.array(grid.geo["doppler_hz"], pa.float64()),
                "bearing_deg": pa.array(grid.geo["bearing_deg"], pa.float64()),
                "bistatic_deg": pa.array(grid.geo["bistatic_deg"], pa.float64()),
                "up_m": pa.array(grid.geo["up_m"], pa.float64()),
                "config_version": pa.array(grid.version, pa.int64()),
                "geometric": pa.array(grid.eligible, pa.bool_()),
                "declared": pa.array(grid.declared, pa.bool_()),
                "empirical": pa.array(grid.empirical, pa.bool_()),
                "node_active": pa.array(up, pa.bool_()),
                "truth_available": pa.array(avail, pa.bool_()),
                "opportunity": pa.array(opportunity, pa.bool_()),
                "raw_pm1": pa.array(raw_pm1, pa.bool_()),
                "raw": pa.array(raw, pa.bool_()),
                "validated": pa.array(validated, pa.bool_()),
                "validated_tagged": pa.array(validated_tagged, pa.bool_()),
                "matched_any": pa.array(matched_any, pa.bool_()),
                "ambiguous": pa.array(ambiguous, pa.bool_()),
                "refuted": pa.array(refuted, pa.bool_()),
                "snr_db": pa.array(snr_top, pa.float64(), from_pandas=True),
                "snr_med_db": pa.array(snr_mid, pa.float64(), from_pandas=True),
                "n_matched": pa.array(looks, pa.int64()),
                "encounter": pa.array(encounter, pa.int64()),
                "frames_in_bucket": pa.array(
                    frames_per_bucket[grid.bucket] if m else np.zeros(0, np.int64), pa.int64()
                ),
                "detections_in_bucket": pa.array(
                    dets_per_bucket[grid.bucket] if m else np.zeros(0, np.int64), pa.int64()
                ),
            }
        )
        tables.append(table)
        encounter_tables.append(_encounters(table))
    buckets = pa.concat_tables(tables)
    encounters = pa.concat_tables(encounter_tables)
    summary = summarize(buckets, encounters, node_id, from_ms, to_ms, len(inputs.background_shifts_ms))
    assert real_match is not None
    summary["diagnostics"] = _diagnostics(
        det, real_match, tag_ids, in_window, det_bucket, n_buckets, active, available, frames_per_bucket, truth
    )
    summary["status"] = status_of(summary)
    return WindowResult(node_id, from_ms, to_ms, buckets, encounters, summary)


_ENCOUNTER_SCHEMA = pa.schema(
    [
        ("node_id", pa.string()),
        ("shift_s", pa.int64()),
        ("encounter", pa.int64()),
        ("hex", pa.string()),
        ("type", pa.string()),
        ("class", pa.string()),
        ("first_ms", pa.int64()),
        ("last_ms", pa.int64()),
        ("n_opp", pa.int64()),
        ("n_opp_declared", pa.int64()),
        ("n_opp_empirical", pa.int64()),
        ("n_raw_pm1", pa.int64()),
        ("n_raw", pa.int64()),
        ("n_validated", pa.int64()),
        ("n_validated_tagged", pa.int64()),
        ("n_matched_any", pa.int64()),
        ("n_ambiguous", pa.int64()),
        ("n_refuted", pa.int64()),
        ("latency_s", pa.float64()),
        ("min_ground_km", pa.float64()),
        ("max_ground_km", pa.float64()),
        ("mean_alt_ft", pa.float64()),
    ]
)


def _encounters(buckets: pa.Table) -> pa.Table:
    """One row per encounter: its aircraft, span, counts and detection latency."""
    enc = buckets["encounter"].to_numpy()
    keep = enc >= 0
    if not keep.any():
        return _ENCOUNTER_SCHEMA.empty_table()
    t = buckets.filter(pa.array(keep))
    e = t["encounter"].to_numpy()
    # first_idx: each encounter's first row, which names its aircraft, type and class.
    ids, first_idx, inverse = np.unique(e, return_index=True, return_inverse=True)
    k = ids.size

    def total(name: str) -> np.ndarray:
        return np.bincount(inverse, weights=t[name].to_numpy(zero_copy_only=False).astype(float), minlength=k)

    bucket_ms = t["bucket_ms"].to_numpy()
    first_ms = np.full(k, np.iinfo(np.int64).max, dtype=np.int64)
    last_ms = np.zeros(k, dtype=np.int64)
    np.minimum.at(first_ms, inverse, bucket_ms)
    np.maximum.at(last_ms, inverse, bucket_ms)
    validated = t["validated"].to_numpy(zero_copy_only=False)
    first_det = np.full(k, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(first_det, inverse[validated], bucket_ms[validated])
    latency = np.where(first_det < np.iinfo(np.int64).max, (first_det - first_ms) / 1000.0, np.nan)
    ground = t["ground_km"].to_numpy()
    min_ground = np.full(k, np.inf)
    max_ground = np.full(k, -np.inf)
    np.minimum.at(min_ground, inverse, ground)
    np.maximum.at(max_ground, inverse, ground)
    opp = t["opportunity"].to_numpy(zero_copy_only=False)
    declared = t["declared"].to_numpy(zero_copy_only=False) & opp
    empirical = t["empirical"].to_numpy(zero_copy_only=False) & opp
    hexes, types, classes = t["hex"].to_pylist(), t["type"].to_pylist(), t["class"].to_pylist()
    counts = np.bincount(inverse, minlength=k)
    return pa.table(
        {
            "node_id": pa.array([t["node_id"][0].as_py()] * k, pa.string()),
            "shift_s": pa.array(np.full(k, t["shift_s"][0].as_py(), dtype=np.int64), pa.int64()),
            "encounter": pa.array(ids, pa.int64()),
            "hex": pa.array([hexes[i] for i in first_idx.tolist()], pa.string()),
            "type": pa.array([types[i] for i in first_idx.tolist()], pa.string()),
            "class": pa.array([classes[i] for i in first_idx.tolist()], pa.string()),
            "first_ms": pa.array(first_ms, pa.int64()),
            "last_ms": pa.array(last_ms + BUCKET_MS, pa.int64()),
            "n_opp": pa.array(counts, pa.int64()),
            "n_opp_declared": pa.array(
                np.bincount(inverse, weights=declared, minlength=k).astype(np.int64), pa.int64()
            ),
            "n_opp_empirical": pa.array(
                np.bincount(inverse, weights=empirical, minlength=k).astype(np.int64), pa.int64()
            ),
            "n_raw_pm1": pa.array(total("raw_pm1").astype(np.int64), pa.int64()),
            "n_raw": pa.array(total("raw").astype(np.int64), pa.int64()),
            "n_validated": pa.array(total("validated").astype(np.int64), pa.int64()),
            "n_validated_tagged": pa.array(total("validated_tagged").astype(np.int64), pa.int64()),
            "n_matched_any": pa.array(total("matched_any").astype(np.int64), pa.int64()),
            "n_ambiguous": pa.array(total("ambiguous").astype(np.int64), pa.int64()),
            "n_refuted": pa.array(total("refuted").astype(np.int64), pa.int64()),
            "latency_s": pa.array(latency, pa.float64()),
            "min_ground_km": pa.array(min_ground, pa.float64()),
            "max_ground_km": pa.array(max_ground, pa.float64()),
            "mean_alt_ft": pa.array(total("alt_ft") / np.maximum(counts, 1), pa.float64()),
        },
        schema=_ENCOUNTER_SCHEMA,
    )


# ── Summaries ────────────────────────────────────────────────────────────────

DIMENSIONS: dict[str, tuple[str, Sequence[float] | None, str]] = {
    "class": ("class", None, ""),
    "type": ("type", None, ""),
    "range_ground": ("ground_km", RANGE_EDGES_KM, " km"),
    "range_slant": ("rr_km", RANGE_EDGES_KM, " km"),
    "delay": ("delay_us", DELAY_EDGES_US, " us"),
    "altitude": ("alt_ft", tuple(e * 1000 for e in ALT_EDGES_KFT), " ft"),
    "bearing": ("bearing_deg", tuple(range(0, 361, BEARING_SECTOR_DEG)), " deg"),
    "bistatic": ("bistatic_deg", BISTATIC_EDGES_DEG, " deg"),
    "hour": ("hour", tuple(range(0, 25)), "h"),
}


def _bins(values: np.ndarray, edges: Sequence[float]) -> np.ndarray:
    """Index of the bin holding each value, -1 outside the edges."""
    idx = np.searchsorted(np.asarray(edges, dtype=float), values, side="right") - 1
    idx[(idx < 0) | (idx >= len(edges) - 1) | ~np.isfinite(values)] = -1
    return idx


def _labels(edges: Sequence[float], unit: str = "") -> list[str]:
    return [f"{edges[i]:g}-{edges[i + 1]:g}{unit}" for i in range(len(edges) - 1)]


def _cell_labels(table: pa.Table, dim: str) -> tuple[np.ndarray, list[str]]:
    column, edges, unit = DIMENSIONS[dim]
    if dim == "hour":
        values = ((table["bucket_ms"].to_numpy() // 3_600_000) % 24).astype(float)
        idx = _bins(values + 0.5, edges)
        return idx, [f"{h:02d}h" for h in range(24)]
    if edges is None:
        names = table[column].to_pylist()
        labels = sorted({n or "unknown" for n in names})
        lookup = {label: i for i, label in enumerate(labels)}
        return np.array([lookup[n or "unknown"] for n in names], dtype=np.int64), labels
    values = table[column].to_numpy(zero_copy_only=False).astype(float)
    return _bins(values, edges), _labels(edges, unit)


def _counted_columns(table: pa.Table) -> dict[str, np.ndarray]:
    """The columns ``_counts`` reads, as arrays, converted once rather than once per cell."""
    columns = {name: table[name].to_numpy(zero_copy_only=False) for name in ("encounter", *COUNTED)}
    if "snr_db" in table.column_names:  # absent from tables scored before the SNR columns existed
        columns["snr_db"] = table["snr_db"].to_numpy(zero_copy_only=False).astype(float)
    return columns


def _counts(columns: Mapping[str, np.ndarray], rows: np.ndarray, control: np.ndarray, shifts: int) -> dict[str, Any]:
    """Counts over the real rows selected, plus the control rows' opportunities and matches, per shift."""
    real = rows & ~control
    ctrl = rows & control
    cell: dict[str, Any] = {
        "n": int(real.sum()),
        "encounters": int(np.unique(columns["encounter"][real]).size),
        "background": float(columns["validated"][ctrl].sum()) / max(shifts, 1),
        "background_n": float(ctrl.sum()) / max(shifts, 1),
    }
    for name in COUNTED:
        cell[name] = int(columns[name][real].sum())
    if "snr_db" in columns:
        # Additive, so windows merge by summing: the validated buckets' best-look SNR, in dB.
        snr = columns["snr_db"][real]
        snr = snr[np.isfinite(snr)]
        cell["snr_n"] = int(snr.size)
        cell["snr_sum_db"] = float(snr.sum())
    return cell


def _shift_count(table: pa.Table) -> int:
    shifts = pc.unique(table["shift_s"]).to_pylist()
    return len([s for s in shifts if s])


def cells(table: pa.Table, gate: str = "geometric") -> dict[str, dict[str, dict[str, Any]]]:
    """Per dimension and label: opportunities under the gate, every numerator, and the control's counts."""
    opp = table["opportunity"].to_numpy(zero_copy_only=False) & table[gate].to_numpy(zero_copy_only=False)
    control = table["shift_s"].to_numpy() != 0
    shifts = _shift_count(table)
    columns = _counted_columns(table)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for dim in DIMENSIONS:
        idx, labels = _cell_labels(table, dim)
        section: dict[str, dict[str, Any]] = {}
        for i, label in enumerate(labels):
            rows = opp & (idx == i)
            if not rows.any():
                continue
            cell = _counts(columns, rows, control, shifts)
            if cell["n"] or cell["background_n"]:
                section[label] = cell
        out[dim] = section
    return out


def totals(table: pa.Table, gate: str = "geometric") -> dict[str, Any]:
    opp = table["opportunity"].to_numpy(zero_copy_only=False) & table[gate].to_numpy(zero_copy_only=False)
    control = table["shift_s"].to_numpy() != 0
    out = _counts(_counted_columns(table), opp, control, _shift_count(table))
    real = opp & ~control
    out["aircraft"] = int(pc.count_distinct(table["hex"].filter(pa.array(real))).as_py()) if real.any() else 0
    return out


def rate(cell: Mapping[str, Any], numerator: str) -> float | None:
    """A numerator over the cell's opportunities; ``background`` over the control's; ``net`` their difference.

    ``snr_mean_db`` is the mean best-look SNR of the cell's validated buckets, in dB.
    """
    n = cell.get("n", 0)
    if numerator == "snr_mean_db":
        k = cell.get("snr_n", 0)
        return (cell.get("snr_sum_db", 0.0) / k) if k else None
    if numerator == "background":
        bg_n = cell.get("background_n", 0.0)
        return (cell.get("background", 0.0) / bg_n) if bg_n else None
    if numerator == "net":
        validated, background = rate(cell, "validated"), rate(cell, "background")
        return None if validated is None or background is None else validated - background
    return (cell.get(numerator, 0) / n) if n else None


def summarize(
    buckets: pa.Table, encounters: pa.Table, node_id: str, from_ms: int, to_ms: int, shifts: int
) -> dict[str, Any]:
    """The window's figures per coverage gate, plus the encounter bootstrap under the geometric gate."""
    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "node_id": node_id,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "background_shifts": shifts,
        "gates": {gate: totals(buckets, gate) for gate in GATES},
        "cells": {gate: cells(buckets, gate) for gate in GATES},
    }
    out["bootstrap"] = bootstrap_from_encounters(encounters)
    out["latency_s"] = latency_summary(encounters)
    return out


def latency_summary(encounters: pa.Table) -> dict[str, Any]:
    real = encounters.filter(pc.equal(encounters["shift_s"], 0)) if encounters.num_rows else encounters
    latency = real["latency_s"].to_numpy() if real.num_rows else np.zeros(0)
    finite = latency[np.isfinite(latency)]
    return {
        "encounters": int(real.num_rows),
        "detected": int(finite.size),
        "median": float(np.median(finite)) if finite.size else None,
        "p90": float(np.percentile(finite, 90)) if finite.size else None,
    }


def _weights(size: int, draws: int, rng: np.random.Generator) -> np.ndarray:
    return rng.multinomial(size, np.full(size, 1.0 / size), size=draws).astype(float)


def bootstrap_rates(
    n_opp: np.ndarray,
    n_det: np.ndarray,
    *,
    control_opp: np.ndarray | None = None,
    control_det: np.ndarray | None = None,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = 7,
) -> dict[str, Any] | None:
    """A rate and its 95 % percentile interval, resampling encounters (rows) with replacement.

    The rate is sum(n_det) / sum(n_opp). With ``control_opp`` and ``control_det``
    (the control encounters' counts) it is the net rate, less
    sum(control_det) / sum(control_opp), the two sets resampled independently.
    """
    n_opp = np.asarray(n_opp, dtype=float)
    n_det = np.asarray(n_det, dtype=float)
    if n_opp.size == 0 or n_opp.sum() <= 0:
        return None
    point = float(n_det.sum() / n_opp.sum())
    net = control_opp is not None and control_det is not None
    c_opp = np.asarray(control_opp, dtype=float) if net else np.zeros(0)
    c_det = np.asarray(control_det, dtype=float) if net else np.zeros(0)
    if net:
        if c_opp.size == 0 or c_opp.sum() <= 0:
            return None
        point -= float(c_det.sum() / c_opp.sum())
    size = int(n_opp.size)
    if size < 2 or (net and c_opp.size < 2):
        return {"pd": point, "lo": None, "hi": None, "encounters": size}
    rng = np.random.default_rng(seed)
    w = _weights(size, draws, rng)
    opp = w @ n_opp
    values = np.divide(w @ n_det, opp, out=np.full(draws, np.nan), where=opp > 0)
    if net:
        wc = _weights(int(c_opp.size), draws, rng)
        copp = wc @ c_opp
        values = values - np.divide(wc @ c_det, copp, out=np.full(draws, np.nan), where=copp > 0)
    values = values[np.isfinite(values)]
    if values.size < draws // 2:
        return {"pd": point, "lo": None, "hi": None, "encounters": size}
    lo, hi = np.percentile(values, [2.5, 97.5])
    return {"pd": point, "lo": float(lo), "hi": float(hi), "encounters": size}


def bootstrap_from_encounters(
    encounters: pa.Table, numerators: Sequence[str] = ("raw_pm1", "raw", "validated", "validated_tagged")
) -> dict[str, Any]:
    """Encounter-bootstrap figures: each numerator, the background (control encounters) and the net rate."""
    out: dict[str, Any] = {}
    if not encounters.num_rows:
        return out
    shift = encounters["shift_s"].to_numpy()
    real = encounters.filter(pa.array(shift == 0))
    control = encounters.filter(pa.array(shift != 0))
    if not real.num_rows:
        return out
    n_opp = real["n_opp"].to_numpy().astype(float)
    validated = real["n_validated"].to_numpy().astype(float)
    c_opp = control["n_opp"].to_numpy().astype(float)
    c_det = control["n_validated"].to_numpy().astype(float)
    for name in numerators:
        out[name] = bootstrap_rates(n_opp, real[f"n_{name}"].to_numpy().astype(float))
    out["background"] = bootstrap_rates(c_opp, c_det)
    out["net"] = bootstrap_rates(n_opp, validated, control_opp=c_opp, control_det=c_det)
    classes = np.array(real["class"].to_pylist(), dtype=object)
    c_classes = np.array(control["class"].to_pylist(), dtype=object)
    by_class = {}
    for label in sorted(set(classes.tolist())):
        rows = classes == label
        c_rows = c_classes == label
        by_class[label] = {
            "validated": bootstrap_rates(n_opp[rows], validated[rows]),
            "raw_pm1": bootstrap_rates(n_opp[rows], real["n_raw_pm1"].to_numpy()[rows].astype(float)),
            "net": bootstrap_rates(n_opp[rows], validated[rows], control_opp=c_opp[c_rows], control_det=c_det[c_rows]),
        }
    out["by_class"] = by_class
    return out


def _diagnostics(
    det: Detections,
    match: Mapping[str, np.ndarray],
    tag_ids: np.ndarray,
    in_window: np.ndarray,
    det_bucket: np.ndarray,
    n_buckets: int,
    active: np.ndarray,
    available: np.ndarray,
    frames_per_bucket: np.ndarray,
    truth: TruthRows,
) -> dict[str, Any]:
    tagged = (tag_ids >= 0) & in_window
    tag_present = match["tag_present"] & tagged
    matched = in_window & (match["match"] >= 0)
    clean = matched & ~match["ambiguous"]
    snr = det.snr_db
    covered = np.zeros(det.n, dtype=bool)
    covered[in_window] = available[det_bucket[in_window]]
    # Tags naming an aircraft the truth never saw, counted only where truth was available.
    unknown_tags = int((tagged & ~match["tag_present"] & covered).sum())
    return {
        "buckets": int(n_buckets),
        "active_buckets": int(active.sum()),
        "truth_buckets": int(available.sum()),
        "frames": int(det.frames.size),
        "buckets_with_frames": int((frames_per_bucket > 0).sum()),
        "detections": int(in_window.sum()),
        "tagged_detections": int(tagged.sum()),
        "tags_in_truth": int(tag_present.sum()),
        "tags_fitting_truth": int((match["tag_fits"] & tagged).sum()),
        "tags_unknown_to_truth": int(unknown_tags),
        "matched_detections": int(matched.sum()),
        "ambiguous_detections": int((match["ambiguous"] & in_window).sum()),
        "delay_residual_us": _spread(match["d_delay"][matched]),
        "doppler_residual_hz": _spread(match["d_doppler"][matched]),
        # SNR of the detections: all of them, those fitting one truth aircraft, the rest, and the tagged.
        "snr_db": {
            "all": _levels(snr[in_window]),
            "matched": _levels(snr[clean]),
            "unmatched": _levels(snr[in_window & ~matched]),
            "tagged": _levels(snr[tagged]),
        },
        "snr_hist": {
            "edges_db": list(SNR_HIST_EDGES_DB),
            "all": _snr_hist(snr[in_window]),
            "matched": _snr_hist(snr[clean]),
            "tagged": _snr_hist(snr[tagged]),
        },
        "truth_fixes": int(truth.t_ms.size),
        "truth_aircraft": int(np.unique(truth.hex_id).size),
        "truth_polls": int(sum(p.size for p in truth.polls.values())),
        "truth_regions": list(truth.polls),
    }


def _levels(values: np.ndarray) -> dict[str, Any]:
    values = values[np.isfinite(values)]
    if not values.size:
        return {"n": 0}
    p5, p25, p50, p75, p95 = np.percentile(values, [5, 25, 50, 75, 95])
    return {
        "n": int(values.size),
        "p5": float(p5),
        "p25": float(p25),
        "median": float(p50),
        "p75": float(p75),
        "p95": float(p95),
    }


def _snr_hist(values: np.ndarray) -> list[int]:
    edges = np.asarray(SNR_HIST_EDGES_DB, dtype=float)
    values = values[np.isfinite(values)]
    inside = np.clip(values, edges[0], np.nextafter(edges[-1], edges[0]))
    return np.histogram(inside, bins=edges)[0].astype(int).tolist()


def hist_quantile(edges: Sequence[float], counts: Sequence[float], q: float) -> float | None:
    """The q-quantile of a histogram, linear within its bin; None when the histogram is empty."""
    c = np.asarray(counts, dtype=float)
    total = float(c.sum())
    if total <= 0:
        return None
    cum = np.cumsum(c)
    i = min(int(np.searchsorted(cum, q * total, side="left")), c.size - 1)
    inside = (q * total - (cum[i] - c[i])) / c[i] if c[i] else 0.0
    return float(edges[i] + inside * (edges[i + 1] - edges[i]))


def _spread(values: np.ndarray) -> dict[str, Any]:
    values = values[np.isfinite(values)]
    if not values.size:
        return {"n": 0}
    med = float(np.median(values))
    return {
        "n": int(values.size),
        "median": med,
        "mad": float(np.median(np.abs(values - med))),
        "p90_abs": float(np.percentile(np.abs(values), 90)),
    }


def status_of(summary: Mapping[str, Any]) -> dict[str, Any]:
    """A data-quality verdict for the node and window, never a 0 % read as a result."""
    diag = summary["diagnostics"]
    geo = summary["gates"]["geometric"]
    flags = []
    buckets = max(diag["buckets"], 1)
    truth_share = diag["truth_buckets"] / buckets
    active_share = diag["active_buckets"] / buckets
    if diag["truth_buckets"] == 0:
        code = "no_truth"
    elif diag["active_buckets"] == 0:
        code = "node_inactive"
    elif geo["n"] < MIN_OPPORTUNITIES or geo["encounters"] < MIN_ENCOUNTERS:
        code = "insufficient"
    else:
        code = "ok"
    if 0 < truth_share < 0.9:
        flags.append("truth_gaps")
    if 0 < active_share < 0.9:
        flags.append("node_partial")
    if geo["n"]:
        bg = rate(geo, "background") or 0.0
        val = geo["validated"] / geo["n"]
        if val > 0 and bg >= 0.5 * val:
            flags.append("background_dominated")
        if geo["raw"] and geo["refuted"] / geo["raw"] > 0.3:
            flags.append("tags_refuted")
        if geo["ambiguous"] / geo["n"] > 0.1:
            flags.append("ambiguous_geometry")
    if diag["tagged_detections"] and diag["tags_in_truth"] / diag["tagged_detections"] < 0.5:
        flags.append("tags_not_in_truth")
    return {
        "code": code,
        "flags": flags,
        "truth_share": round(truth_share, 3),
        "active_share": round(active_share, 3),
    }


# ── The legacy denominator, for comparison ───────────────────────────────────


def legacy_node_rate(
    adsb_root: str | Path, node_id: str, sites: Sites, det: Detections, from_ms: int, to_ms: int
) -> dict[str, Any]:
    """PR #727's node-opportunity figure over the window: the node's own ADS-B rows, tags within ±1 bucket.

    Rows are the archive's ``source="node"`` rows attributed to the node, world
    "real", airborne, bucketed by snapshot time, kept inside the window and
    above the horizon; detected when the node tagged the aircraft in that
    bucket or a neighbour. Returned for comparison only.
    """
    columns = ["ts_ms", "hex", "lat", "lon", "alt_baro", "world", "source", "source_node_id"]
    hexes = pdata._Interner()
    keys: list[np.ndarray] = []
    for day in _days(from_ms, to_ms):
        for path in pdata._adsb_files(adsb_root, day):
            for table in pdata._row_groups(path, columns, {"ts_ms": (from_ms, to_ms - 1)}):
                table = table.filter(
                    pc.and_(
                        pc.and_(pc.equal(table["world"], "real"), pc.equal(table["source"], "node")),
                        pc.and_(
                            pc.equal(table["source_node_id"], node_id), pdata._between(table["ts_ms"], from_ms, to_ms)
                        ),
                    )
                )
                if not table.num_rows:
                    continue
                ts = table["ts_ms"].to_numpy()
                lat, lon, alt = (
                    pdata._floats(table, "lat"),
                    pdata._floats(table, "lon"),
                    pdata._floats(table, "alt_baro"),
                )
                ok = np.isfinite(lat) & np.isfinite(lon) & (alt > 0)
                hex_id = pdata._hex_ids(hexes, table, "hex")
                for version, picked in sites.split(ts[ok]):
                    at = np.flatnonzero(ok)[picked]
                    look = version.site.look(lat[at], lon[at], alt[at] * _FT_TO_M)
                    keep = (look.delay_us < version.site.window_us) & (look.up_m > 0)
                    rows = at[keep]
                    keys.append(hex_id[rows] * _T_SPAN + (ts[rows] - from_ms) // BUCKET_MS)
    opp = np.unique(np.concatenate(keys)) if keys else np.zeros(0, dtype=np.int64)
    tag_ids = np.array([hexes._ids.get(h, -1) if h else -1 for h in det.tag_hex], dtype=np.int64)
    bucket = (det.frame_ts_ms - from_ms) // BUCKET_MS
    tagged = tag_ids >= 0
    tag_keys = np.unique(tag_ids[tagged] * _T_SPAN + bucket[tagged])
    hit = np.isin(opp, tag_keys) | np.isin(opp - 1, tag_keys) | np.isin(opp + 1, tag_keys)
    return {"n": int(opp.size), "detected": int(hit.sum()), "pd": float(hit.mean()) if opp.size else None}
