"""Count tables of what one node could have seen and what it detected, one UTC day at a time.

node_day_table turns a node-day of archive files into a small table of counts,
merge_tables adds day tables up, and a fit reads the sum. Nothing here writes a
file, and a table holds counts only: a position, a baseline or any other
position-valued figure never enters one.

An opportunity is an aircraft the node could have seen, in one 10 s bucket:

- ``"aggregator"``: an ADSBHub row listing the node, so independent of the radar.
  A fix stays in the snapshot for up to 45 s and is written every 10 s, so one
  fix repeats across rows: the time is the fix's own (``seen_ms``), and each
  (hex, bucket) counts once across the day's files, at a position the aircraft
  really had. The bucket must also hold a row of the node in the detection
  archive, because aggregator rows go on being written while a node is offline
  and an outage is not a miss. A frame with no detections leaves no row, so a
  quiet node's empty buckets drop out with its outages.
- ``"node"``: the node's own ADS-B rows, by snapshot time, every row counted.
  These exist only within 45 s of a tag, so the result measures how steadily a
  tagged aircraft stays tagged, not how often one is detected.

Either way the row is in the real world, airborne, under the node's delay
window, above the receiver's horizon and inside the field of view, and its time
falls in the day, so a row belongs to one day whichever folder it sits in.

An opportunity is detected when the same hex has a key in its bucket or either
neighbour: a tag (``adsb_hex``, not gated) or a claim that passes the calibration
gate. Partition folders are flush days, so frames of a day can sit in the
folders either side of it; rows are kept by their own timestamps, never by folder.

Files are read one at a time, a row group at a time, columns projected. What
grows across a day is sorted int64 key arrays and the count tables. The
``rx_*`` columns of the detection archive hold the published position, so no
receiver, transmitter or RF column is ever read from a row: geometry takes its
true positions from the ``configs`` argument.
"""

from __future__ import annotations

import functools
import logging
import math
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, NamedTuple

import numpy as np
import pyarrow as pa
import pyarrow.compute
import pyarrow.parquet as pq

from config.constants import CAL_CLAIM_DELAY_US, CAL_CLAIM_DOPPLER_HZ
from services import archive_reader, compaction
from services.node_config import canonical_config, position_status, resolve_altitudes
from services.node_kind import is_synthetic_node

logger = logging.getLogger(__name__)

# pyarrow generates its compute functions at import, out of a type checker's sight.
pc: Any = pyarrow.compute

SCHEMA_VERSION = 1
BUCKET_MS = 10_000
GATE_MARGIN_DB = 1.0
DEFAULT_WINDOW_US = 200.0
# The stretch before the window's edge kept out of the fit: detections fall off
# there for reasons other than the target.
FIT_EDGE_MARGIN_US = 25.0
SNR_BIN_DB = 0.25
RESIDUAL_BIN_US = 0.05
RESIDUAL_CLIP_US = 1000.0
DELAY_BIN_US = 25
BEARING_BIN_DEG = 5
BEARING_BINS = 72
RING_KM = 5.0
RING_ALT_LOW_FT = 5_000
RING_ALT_HIGH_FT = 20_000
RANGE_EDGES_KM = (0, 10, 20, 30, 40, 50, 60, 80, 100, 150)
ALT_EDGES_KFT = (0, 2, 5, 10, 20, 30, 45)
GRID_RANGE_EDGES_KM = (0, 20, 40, 60, 100)
GRID_ALT_EDGES_KFT = (0, 5, 10, 20, 45)

_DAY_MS = 86_400_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Above the buckets of a day and the one either side of it, so a key's hex and
# bucket can never run into each other.
_KEY_SPAN = 1 << 14
_FT_TO_M = 0.3048
_C_KM_PER_US = 0.299792458
_C_M_PER_S = 299_792_458.0
_WGS84_A = 6378137.0
_WGS84_F = 1 / 298.257223563
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)

_DETECTION_COLUMNS = (
    "frame_ts_ms",
    "delay_us",
    "snr_db",
    "adsb_hex",
    "adsb_lat",
    "adsb_lon",
    "adsb_alt_baro",
    "claim_hex",
    "claim_path",
    "claim_contested",
    "claim_delay_res_us",
    "claim_doppler_res_hz",
    "claim_fix_delay_res_us",
)
_ADSB_COLUMNS = (
    "ts_ms",
    "hex",
    "lat",
    "lon",
    "alt_baro",
    "seen_ms",
    "world",
    "source",
    "source_node_id",
    "aircraft_type",
)
# Newer than the first files of either store: an old file reads without them.
_OPTIONAL_COLUMNS = frozenset(
    {
        "claim_hex",
        "claim_path",
        "claim_contested",
        "claim_delay_res_us",
        "claim_doppler_res_hz",
        "claim_fix_delay_res_us",
        "aircraft_type",
    }
)

_CELL_SECTIONS = ("by_range", "by_delay", "by_alt", "by_type")
_HISTOGRAMS = ("snr_hist", "tag_residual_hist", "claim_residual_hist")
_NUMERATOR_INDEX = {"tags": 1, "claims": 2}


@dataclass(frozen=True, slots=True)
class _Source:
    """What makes a row an opportunity, by the ``opportunities`` argument."""

    name: str
    time_column: str
    # source_node_id is a comma-joined list of ids rather than one id.
    listed: bool
    one_per_bucket: bool
    needs_frame: bool


_SOURCES = {
    "aggregator": _Source("aggregator", "seen_ms", listed=True, one_per_bucket=True, needs_frame=True),
    "node": _Source("node", "ts_ms", listed=False, one_per_bucket=False, needs_frame=False),
}


# ── Geometry ─────────────────────────────────────────────────────────────────


def _ecef(lat_deg: Any, lon_deg: Any, height_m: Any) -> np.ndarray:
    """WGS-84 earth-centred coordinates in metres, xyz on the last axis."""
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    sin_lat = np.sin(lat)
    normal = _WGS84_A / np.sqrt(1 - _WGS84_E2 * sin_lat * sin_lat)
    return np.stack(
        [
            (normal + height_m) * np.cos(lat) * np.cos(lon),
            (normal + height_m) * np.cos(lat) * np.sin(lon),
            (normal * (1 - _WGS84_E2) + height_m) * sin_lat,
        ],
        axis=-1,
    )


class _Look(NamedTuple):
    """The bistatic geometry of targets, seen from one config's true sites."""

    rt_m: np.ndarray
    rr_m: np.ndarray
    delay_us: np.ndarray
    # Height above the receiver's horizon plane: positive is elevation above 0.
    up_m: np.ndarray
    bearing_deg: np.ndarray


@dataclass(frozen=True, slots=True)
class _Site:
    """One config version's true geometry and the figures derived from it."""

    rx_lat_deg: float
    rx_lon_deg: float
    rx_height_m: float
    rx: np.ndarray
    tx: np.ndarray
    baseline_m: float
    # Rows are the east, north and up unit vectors at the receiver.
    enu: np.ndarray
    window_us: float
    # K without its two range terms; NaN when the config lacks a figure for it.
    link_db: float
    beam_azimuth_deg: float | None
    beam_width_deg: float | None

    def look(self, lat: Any, lon: Any, alt_m: Any) -> _Look:
        target = _ecef(lat, lon, alt_m)
        from_rx = target - self.rx
        rr_m = np.linalg.norm(from_rx, axis=-1)
        rt_m = np.linalg.norm(target - self.tx, axis=-1)
        local = from_rx @ self.enu.T
        return _Look(
            rt_m=rt_m,
            rr_m=rr_m,
            delay_us=(rt_m + rr_m - self.baseline_m) / 1000.0 / _C_KM_PER_US,
            up_m=local[..., 2],
            bearing_deg=np.degrees(np.arctan2(local[..., 0], local[..., 1])) % 360.0,
        )

    def known_db(self, rt_m: np.ndarray, rr_m: np.ndarray) -> np.ndarray:
        """K, the known part of the link budget; EIRP stays out, so a corrected tower needs no re-read."""
        with np.errstate(divide="ignore"):
            return self.link_db - 20 * np.log10(rt_m) - 20 * np.log10(rr_m)

    def in_declared_beam(self, bearing_deg: np.ndarray) -> np.ndarray:
        """Inside the declared wedge; everywhere is, unless azimuth and width are both declared."""
        if self.beam_azimuth_deg is None or self.beam_width_deg is None:
            return np.ones(bearing_deg.shape, dtype=bool)
        off_axis = (bearing_deg - self.beam_azimuth_deg + 180.0) % 360.0 - 180.0
        return np.abs(off_axis) <= self.beam_width_deg / 2


def _link_db(config: Mapping[str, Any]) -> float:
    fc_hz, fs_hz, cpi_s = config.get("fc_hz"), config.get("fs_hz"), config.get("cpi_s")
    if not (fc_hz and fs_hz and cpi_s):
        return math.nan
    return 20 * math.log10(_C_M_PER_S / fc_hz) + 10 * math.log10(fs_hz * cpi_s) - 30 * math.log10(4 * math.pi)


def _site(config: Mapping[str, Any]) -> _Site | None:
    """A config's geometry; None when canonical_config leaves its receiver or transmitter unplaced, (0, 0) included."""
    canonical = canonical_config(dict(config))
    if position_status(canonical) != "positioned":
        return None
    resolved = resolve_altitudes(canonical)
    rx_lat, rx_lon = float(resolved["rx_lat"]), float(resolved["rx_lon"])
    rx_height_m = float(resolved["rx_alt_ft"]) * _FT_TO_M
    rx = _ecef(rx_lat, rx_lon, rx_height_m)
    tx = _ecef(float(resolved["tx_lat"]), float(resolved["tx_lon"]), float(resolved["tx_alt_ft"]) * _FT_TO_M)
    lat, lon = math.radians(rx_lat), math.radians(rx_lon)
    enu = np.array(
        [
            [-math.sin(lon), math.cos(lon), 0.0],
            [-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)],
            [math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)],
        ]
    )
    max_range_km = resolved.get("max_range_km")
    azimuth, width = resolved.get("beam_azimuth_deg"), resolved.get("beam_width_deg")
    return _Site(
        rx_lat_deg=rx_lat,
        rx_lon_deg=rx_lon,
        rx_height_m=rx_height_m,
        rx=rx,
        tx=tx,
        baseline_m=float(np.linalg.norm(tx - rx)),
        enu=enu,
        window_us=DEFAULT_WINDOW_US if max_range_km is None else float(max_range_km) / _C_KM_PER_US,
        link_db=_link_db(resolved),
        beam_azimuth_deg=None if azimuth is None else float(azimuth),
        beam_width_deg=None if width is None else float(width),
    )


def _created_ms(value: str | datetime) -> int:
    """A config's ``created_at`` as the first whole epoch ms not before it; naive means UTC."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return -((_EPOCH - moment) // timedelta(milliseconds=1))


class _Versions:
    """A node's config rows, oldest first, answering which one was active when."""

    def __init__(self, configs: Sequence[Mapping[str, Any]]) -> None:
        ordered = sorted(configs, key=lambda config: _created_ms(config["created_at"]))
        self._created_ms = np.array([_created_ms(config["created_at"]) for config in ordered], dtype=np.int64)
        self._sites = [_site(config) for config in ordered]

    def split(self, t_ms: np.ndarray) -> Iterator[tuple[_Site, np.ndarray]]:
        """Each active version with a position and the rows it covers: the latest
        created by a row's time, else the oldest."""
        if not self._sites:
            return
        active = np.maximum(np.searchsorted(self._created_ms, t_ms, side="right") - 1, 0)
        for version in np.unique(active).tolist():
            site = self._sites[version]
            if site is not None:
                yield site, np.flatnonzero(active == version)


def config_on(configs: Sequence[Mapping[str, Any]], day: date) -> Mapping[str, Any]:
    """The config row active at the last millisecond of ``day``: the latest created by then, else the oldest."""
    end_ms = archive_reader.day_start_ms(day) + _DAY_MS - 1
    ordered = sorted(configs, key=lambda config: _created_ms(config["created_at"]))
    active = [config for config in ordered if _created_ms(config["created_at"]) <= end_ms]
    return active[-1] if active else ordered[0]


# ── Reading ──────────────────────────────────────────────────────────────────


def _partitions(root: str | Path, day: date) -> Iterator[str]:
    """The day's partition folder and the one either side of it, oldest first."""
    for offset in (-1, 0, 1):
        yield compaction.day_dir(str(root), day + timedelta(days=offset))


def _detection_files(root: str | Path, node_id: str, day: date) -> Iterator[str]:
    for partition in _partitions(root, day):
        directory = os.path.join(partition, f"node_id={node_id}")
        try:
            names = sorted(os.listdir(directory))
        except (FileNotFoundError, NotADirectoryError):
            continue
        yield from (os.path.join(directory, n) for n in names if n.endswith(".parquet") and not n.startswith("."))


def _adsb_files(root: str | Path, day: date) -> Iterator[str]:
    """The local ADS-B files that hold each row once: a usable compact and every raw part it does not list."""
    for partition in _partitions(root, day):
        names = compaction.raw_part_names(partition)
        compact = os.path.join(partition, compaction.COMPACT_NAME)
        try:
            manifest = compaction.read_footer_manifest(compact)
        except FileNotFoundError:
            manifest = None
        except OSError:
            logger.warning("performance data: reading raw parts beside unreadable %s", compact, exc_info=True)
            manifest = None
        if manifest is not None:
            yield compact
            listed = set(manifest.parts)
            names = [name for name in names if name not in listed]
        yield from (os.path.join(partition, name) for name in names)


def _may_hold(group: Any, wanted: Mapping[str, tuple[Any, Any]]) -> bool:
    """False when a row group's footer statistics rule out every row in ``wanted``'s inclusive bounds."""
    for c in range(group.num_columns):
        column = group.column(c)
        bounds = wanted.get(column.path_in_schema)
        stats = column.statistics
        if bounds is None or stats is None or not stats.has_min_max:
            continue
        if stats.max < bounds[0] or stats.min > bounds[1]:
            return False
    return True


def _row_groups(path: str, columns: Sequence[str], wanted: Mapping[str, tuple[Any, Any]]) -> Iterator[pa.Table]:
    """A file's row groups that can hold a wanted row, one at a time, projected to ``columns``.

    ParquetFile reads the one file as it is: no Hive discovery, so the
    ``node_id=`` directory cannot clash with the column of that name. A file
    the lifecycle pruned after the listing has nothing to give; one that cannot
    be read is skipped with a warning. One thread: Arrow's reader threads each
    keep allocator pages, which doubles peak memory and saves no time here.
    """
    try:
        with pq.ParquetFile(path) as parquet:
            present = set(parquet.schema_arrow.names)
            missing = set(columns) - present - _OPTIONAL_COLUMNS
            if missing:
                logger.warning("performance data: skipping %s, it has no %s", path, sorted(missing))
                return
            projected = [name for name in columns if name in present]
            metadata = parquet.metadata
            for g in range(metadata.num_row_groups):
                if _may_hold(metadata.row_group(g), wanted):
                    yield parquet.read_row_group(g, columns=projected, use_threads=False)
    except FileNotFoundError:
        return
    except (OSError, pa.ArrowException):
        logger.warning("performance data: skipping unreadable archive file %s", path, exc_info=True)


def _floats(table: pa.Table, name: str) -> np.ndarray:
    """A column as float64 with null as NaN; all NaN when the file lacks it."""
    if name not in table.column_names:
        return np.full(table.num_rows, np.nan)
    return table[name].to_numpy(zero_copy_only=False).astype(np.float64, copy=False)


def _between(column: Any, low: int, high: int) -> Any:
    """low <= column < high; a null drops out of a filter on it."""
    return pc.and_(pc.greater_equal(column, low), pc.less(column, high))


class _Interner:
    """Small integer ids for strings, one lookup per distinct value of a column, never per row."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self.names: list[str] = []

    def intern(self, name: str) -> int:
        found = self._ids.get(name)
        if found is None:
            found = self._ids[name] = len(self.names)
            self.names.append(name)
        return found

    def encode(self, column: Any) -> np.ndarray:
        """Each row's id, -1 for a null."""
        encoded = column.combine_chunks().dictionary_encode()
        lookup = [self.intern(name) for name in encoded.dictionary.to_pylist()]
        lookup.append(-1)
        rows = pc.fill_null(encoded.indices, len(lookup) - 1).to_numpy(zero_copy_only=False)
        return np.array(lookup, dtype=np.int64)[rows]


def _hex_ids(hexes: _Interner, table: pa.Table, name: str) -> np.ndarray:
    if name not in table.column_names:
        return np.full(table.num_rows, -1, dtype=np.int64)
    return hexes.encode(pc.utf8_lower(table[name]))


def _bump(histogram: dict[str, int], bins: np.ndarray) -> None:
    distinct, counts = np.unique(bins.astype(np.int64), return_counts=True)
    for value, count in zip(distinct.tolist(), counts.tolist(), strict=True):
        key = str(value)
        histogram[key] = histogram.get(key, 0) + count


def _add_snr(histogram: dict[str, int], snr_db: np.ndarray) -> None:
    _bump(histogram, np.floor(snr_db[np.isfinite(snr_db)] / SNR_BIN_DB))


# ── Detections ───────────────────────────────────────────────────────────────


def _buckets(t_ms: np.ndarray, start_ms: int) -> np.ndarray:
    """10 s buckets counted from the day's start, the bucket before the day being 0."""
    return t_ms // BUCKET_MS - start_ms // BUCKET_MS + 1


def _best_per_key(keys: np.ndarray, snr_db: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sorted distinct keys and the largest finite SNR of each, NaN when a key has none."""
    if not keys.size:
        return keys, snr_db
    order = np.argsort(keys, kind="stable")
    keys, snr_db = keys[order], snr_db[order]
    starts = np.flatnonzero(np.concatenate(([True], keys[1:] != keys[:-1])))
    return keys[starts], np.fmax.reduceat(snr_db, starts)


def _positions(sorted_keys: np.ndarray, queries: np.ndarray) -> np.ndarray:
    """Where each query sits in the sorted keys, -1 when absent."""
    if not sorted_keys.size:
        return np.full(queries.shape, -1, dtype=np.int64)
    at = np.minimum(np.searchsorted(sorted_keys, queries), sorted_keys.size - 1)
    return np.where(sorted_keys[at] == queries, at, -1)


@dataclass(slots=True)
class _Detections:
    """What a node-day's detection files reduce to, read before any ADS-B row."""

    tag_keys: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    tag_snr_db: np.ndarray = field(default_factory=lambda: np.zeros(0))
    claim_keys: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    frames: np.ndarray = field(default_factory=lambda: np.zeros(_KEY_SPAN, dtype=bool))
    any_claim_path: bool = False
    claim_columns: bool = True
    snr_hist: dict[str, int] = field(default_factory=dict)
    residual_hist: dict[str, int] = field(default_factory=dict)
    claim_residual_hist: dict[str, int] = field(default_factory=dict)


def _uncontested(table: pa.Table) -> np.ndarray:
    """Rows whose claim nobody disputes: a null is not a dispute, and a file without the column holds none."""
    if "claim_contested" not in table.column_names:
        return np.ones(table.num_rows, dtype=bool)
    return ~pc.fill_null(table["claim_contested"], False).to_numpy(zero_copy_only=False)


def _gated_claims(table: pa.Table, snr_db: np.ndarray, threshold_db: float | None) -> np.ndarray:
    """Rows whose claim is uncontested, inside the calibration gate and, with a threshold, strong enough."""
    passed = (
        (np.abs(_floats(table, "claim_delay_res_us")) <= CAL_CLAIM_DELAY_US)
        & (np.abs(_floats(table, "claim_doppler_res_hz")) <= CAL_CLAIM_DOPPLER_HZ)
        & _uncontested(table)
    )
    if threshold_db is not None:
        passed &= snr_db >= threshold_db + GATE_MARGIN_DB
    return passed


def _add_residuals(found: _Detections, table: pa.Table, rows: np.ndarray, ts_ms: np.ndarray, versions: _Versions):
    """Bin measured minus predicted delay of tagged ``rows``, from the row's fix and the true sites."""
    lat, lon = _floats(table, "adsb_lat"), _floats(table, "adsb_lon")
    alt_ft, delay_us = _floats(table, "adsb_alt_baro"), _floats(table, "delay_us")
    rows = rows[np.isfinite(lat[rows]) & np.isfinite(lon[rows]) & (alt_ft[rows] > 0) & np.isfinite(delay_us[rows])]
    for site, picked in versions.split(ts_ms[rows]):
        at = rows[picked]
        residual = delay_us[at] - site.look(lat[at], lon[at], alt_ft[at] * _FT_TO_M).delay_us
        clipped = np.clip(residual, -RESIDUAL_CLIP_US, RESIDUAL_CLIP_US)
        _bump(found.residual_hist, np.floor(clipped / RESIDUAL_BIN_US))


def _add_claim_residuals(found: _Detections, table: pa.Table, in_day: np.ndarray) -> None:
    """Bin each uncontested refreshed hold's delay against its live fix, through neither the claim gate nor the SNR's.

    Only that reading says where the node is. Any hold's ``claim_delay_res_us`` is
    against the node's own last echo, near zero on any site, and a path-2 claim's
    scatters by microseconds on a sited node too, so a spread of those follows how
    the rows split between the paths. The gate cuts residuals at its own line, which
    would cap the spread this measures. A file without the column bins nothing.
    """
    residual = _floats(table, "claim_fix_delay_res_us")
    kept = residual[in_day & np.isfinite(residual) & _uncontested(table)]
    _bump(found.claim_residual_hist, np.floor(np.clip(kept, -RESIDUAL_CLIP_US, RESIDUAL_CLIP_US) / RESIDUAL_BIN_US))


def _read_detections(
    node_id: str,
    day: date,
    root: str | Path,
    versions: _Versions,
    hexes: _Interner,
    threshold_db: float | None,
) -> _Detections:
    start_ms = archive_reader.day_start_ms(day)
    end_ms = start_ms + _DAY_MS
    low, high = start_ms - BUCKET_MS, end_ms + BUCKET_MS
    found = _Detections()
    tag_keys: list[np.ndarray] = []
    tag_snr_db: list[np.ndarray] = []
    claim_keys: list[np.ndarray] = []
    for path in _detection_files(root, node_id, day):
        for table in _row_groups(path, _DETECTION_COLUMNS, {"frame_ts_ms": (low, high - 1)}):
            table = table.filter(_between(table["frame_ts_ms"], low, high))
            if not table.num_rows:
                continue
            ts_ms = table["frame_ts_ms"].to_numpy(zero_copy_only=False)
            snr_db = _floats(table, "snr_db")
            buckets = _buckets(ts_ms, start_ms)
            in_day = (ts_ms >= start_ms) & (ts_ms < end_ms)
            found.frames[buckets] = True
            _add_snr(found.snr_hist, snr_db[in_day])
            if "claim_path" in table.column_names:
                claimed = pc.is_valid(table["claim_path"]).to_numpy(zero_copy_only=False)
                found.any_claim_path |= bool(claimed[in_day].any())
            elif in_day.any():
                found.claim_columns = False

            tag_ids = _hex_ids(hexes, table, "adsb_hex")
            tagged = tag_ids >= 0
            keys, best = _best_per_key(tag_ids[tagged] * _KEY_SPAN + buckets[tagged], snr_db[tagged])
            tag_keys.append(keys)
            tag_snr_db.append(best)
            _add_residuals(found, table, np.flatnonzero(tagged & in_day), ts_ms, versions)
            _add_claim_residuals(found, table, in_day)

            claim_ids = _hex_ids(hexes, table, "claim_hex")
            gated = (claim_ids >= 0) & _gated_claims(table, snr_db, threshold_db)
            claim_keys.append(np.unique(claim_ids[gated] * _KEY_SPAN + buckets[gated]))
    if tag_keys:
        found.tag_keys, found.tag_snr_db = _best_per_key(np.concatenate(tag_keys), np.concatenate(tag_snr_db))
        found.claim_keys = np.unique(np.concatenate(claim_keys))
    return found


def snr_histogram(node_id: str, day: date, *, archive_root: str | Path) -> dict[str, int]:
    """Counts of the node's ``snr_db`` in the day, by ``floor(snr_db / SNR_BIN_DB)``: a threshold's input."""
    histogram: dict[str, int] = {}
    if is_synthetic_node(node_id):
        return histogram
    start_ms = archive_reader.day_start_ms(day)
    end_ms = start_ms + _DAY_MS
    for path in _detection_files(archive_root, node_id, day):
        for table in _row_groups(path, ("frame_ts_ms", "snr_db"), {"frame_ts_ms": (start_ms, end_ms - 1)}):
            _add_snr(histogram, _floats(table.filter(_between(table["frame_ts_ms"], start_ms, end_ms)), "snr_db"))
    return histogram


# ── Tables ───────────────────────────────────────────────────────────────────


class _Types:
    """Each opportunity's type and class: the row's own type, else the resolver's.

    A hex goes to ``types_for`` once a day and a type to ``class_for`` once a day.
    """

    def __init__(
        self,
        hexes: _Interner,
        types_for: Callable[[Sequence[str]], list[str | None]],
        class_for: Callable[[str | None], str],
    ) -> None:
        self.names = _Interner()
        self.classes = _Interner()
        self._hexes = hexes
        self._types_for = types_for
        self._class_for = class_for
        self._type_of_hex: dict[int, int] = {}
        self._class_of_type: dict[int, int] = {}

    def resolved(self, hex_ids: np.ndarray, row_types: np.ndarray) -> np.ndarray:
        """``row_types`` with a -1 filled in from the hex, still -1 when nobody knows it."""
        missing = row_types < 0
        distinct = np.unique(hex_ids[missing])
        if not distinct.size:
            return row_types
        unasked = [h for h in distinct.tolist() if h not in self._type_of_hex]
        if unasked:
            answers = self._types_for([self._hexes.names[h] for h in unasked])
            for h, name in zip(unasked, answers, strict=True):
                self._type_of_hex[h] = -1 if name is None else self.names.intern(name)
        by_hex = np.array([self._type_of_hex[h] for h in distinct.tolist()], dtype=np.int64)
        filled = row_types.copy()
        filled[missing] = by_hex[np.searchsorted(distinct, hex_ids[missing])]
        return filled

    def class_ids(self, type_ids: np.ndarray) -> np.ndarray:
        distinct = np.unique(type_ids)
        ids = [self._class_id(t) for t in distinct.tolist()]
        return np.array(ids, dtype=np.int64)[np.searchsorted(distinct, type_ids)]

    def _class_id(self, type_id: int) -> int:
        found = self._class_of_type.get(type_id)
        if found is None:
            name = None if type_id < 0 else self.names.names[type_id]
            found = self._class_of_type[type_id] = self.classes.intern(self._class_for(name))
        return found


def _bins(values: np.ndarray, edges: Sequence[float]) -> np.ndarray:
    """The bin of each value, closed on the right as pd.cut does; -1 outside the edges."""
    bins = np.searchsorted(edges, values, side="left") - 1
    return np.where(bins < len(edges) - 1, bins, -1)


def _groups(codes: np.ndarray, *weights: np.ndarray) -> Iterable[tuple[Any, ...]]:
    """Per distinct code: the code, its row count and each weight's sum, as plain numbers."""
    distinct, inverse = np.unique(codes, return_inverse=True)
    sums = [np.bincount(inverse), *(np.bincount(inverse, weights=w) for w in weights)]
    return zip(distinct.tolist(), *(s.tolist() for s in sums), strict=True)


def _add_cell(cell: list[Any], n: int, tags: float, claims: float) -> None:
    cell[0] += n
    cell[1] += int(tags)
    cell[2] += int(claims)


class _Batch(NamedTuple):
    """Kept opportunities of one read, one entry each."""

    tag: np.ndarray
    claim: np.ndarray
    # The best SNR of the matching tag rows; 0 where no tag or no finite SNR adds to the sums.
    tag_snr_db: np.ndarray
    rr_km: np.ndarray
    delay_us: np.ndarray
    alt_ft: np.ndarray
    bearing_deg: np.ndarray
    known_key: np.ndarray
    in_fit: np.ndarray
    type_ids: np.ndarray
    class_ids: np.ndarray


class _Sections:
    """The sections of a day table as they fill, in their JSON shape."""

    def __init__(self, types: _Types) -> None:
        self._types = types
        self.opportunities = 0
        self.tags = 0
        self.claims = 0
        self.class_range_alt: dict[str, dict[str, dict[str, list[int]]]] = {}
        self.class_geometry: dict[str, dict[str, list[Any]]] = {}
        self.class_ring: dict[str, dict[str, list[int]]] = {}
        self.bearing = [[0, 0, 0] for _ in range(BEARING_BINS)]
        self.by_range: dict[str, list[int]] = {}
        self.by_delay: dict[str, list[int]] = {}
        self.by_alt: dict[str, list[int]] = {}
        self.by_type: dict[str, list[int]] = {}

    def add(self, b: _Batch) -> None:
        self.opportunities += b.tag.size
        self.tags += int(b.tag.sum())
        self.claims += int(b.claim.sum())
        self._add_flat(b)
        for class_id in np.unique(b.class_ids).tolist():
            rows = np.flatnonzero(b.class_ids == class_id)
            name = self._types.classes.names[class_id]
            self._add_grid(name, b, rows)
            self._add_geometry(name, b, rows[b.in_fit[rows]])
            self._add_ring(name, b, rows)

    def _add_flat(self, b: _Batch) -> None:
        for section, values, edges in (
            (self.by_range, b.rr_km, RANGE_EDGES_KM),
            (self.by_alt, b.alt_ft / 1000.0, ALT_EDGES_KFT),
        ):
            bins = _bins(values, edges)
            keep = bins >= 0
            for code, n, tags, claims in _groups(bins[keep], b.tag[keep], b.claim[keep]):
                _add_cell(section.setdefault(f"{edges[code]}-{edges[code + 1]}", [0, 0, 0]), n, tags, claims)

        delay_bins = np.ceil(b.delay_us / DELAY_BIN_US).astype(np.int64) - 1
        keep = delay_bins >= 0
        for code, n, tags, claims in _groups(delay_bins[keep], b.tag[keep], b.claim[keep]):
            label = f"{DELAY_BIN_US * code}-{DELAY_BIN_US * (code + 1)}"
            _add_cell(self.by_delay.setdefault(label, [0, 0, 0]), n, tags, claims)

        bearing_bins = np.floor(b.bearing_deg / BEARING_BIN_DEG).astype(np.int64) % BEARING_BINS
        for code, n, tags, claims in _groups(bearing_bins, b.tag, b.claim):
            _add_cell(self.bearing[code], n, tags, claims)

        typed = b.type_ids >= 0
        for code, n, tags, claims in _groups(b.type_ids[typed], b.tag[typed], b.claim[typed]):
            _add_cell(self.by_type.setdefault(self._types.names.names[code], [0, 0, 0]), n, tags, claims)

    def _add_grid(self, name: str, b: _Batch, rows: np.ndarray) -> None:
        range_bins = _bins(b.rr_km[rows], GRID_RANGE_EDGES_KM)
        alt_bins = _bins(b.alt_ft[rows] / 1000.0, GRID_ALT_EDGES_KFT)
        keep = (range_bins >= 0) & (alt_bins >= 0)
        columns = len(GRID_ALT_EDGES_KFT) - 1
        codes = range_bins[keep] * columns + alt_bins[keep]
        for code, n, tags, claims in _groups(codes, b.tag[rows][keep], b.claim[rows][keep]):
            r, a = divmod(code, columns)
            range_label = f"{GRID_RANGE_EDGES_KM[r]}-{GRID_RANGE_EDGES_KM[r + 1]}km"
            alt_label = f"{GRID_ALT_EDGES_KFT[a]}-{GRID_ALT_EDGES_KFT[a + 1]}kft"
            by_alt = self.class_range_alt.setdefault(name, {}).setdefault(range_label, {})
            _add_cell(by_alt.setdefault(alt_label, [0, 0, 0]), n, tags, claims)

    def _add_geometry(self, name: str, b: _Batch, rows: np.ndarray) -> None:
        snr_db = b.tag_snr_db[rows]
        for code, n, tags, claims, total, squares in _groups(
            b.known_key[rows], b.tag[rows], b.claim[rows], snr_db, snr_db * snr_db
        ):
            cell = self.class_geometry.setdefault(name, {}).setdefault(str(code), [0, 0, 0, 0.0, 0.0])
            _add_cell(cell, n, tags, claims)
            cell[3] += total
            cell[4] += squares

    def _add_ring(self, name: str, b: _Batch, rows: np.ndarray) -> None:
        alt_ft = b.alt_ft[rows]
        rows = rows[(alt_ft > RING_ALT_LOW_FT) & (alt_ft <= RING_ALT_HIGH_FT)]
        rings = np.floor(b.rr_km[rows] / RING_KM).astype(np.int64)
        for code, n, tags, claims in _groups(rings, b.tag[rows], b.claim[rows]):
            _add_cell(self.class_ring.setdefault(name, {}).setdefault(str(code), [0, 0, 0]), n, tags, claims)


@dataclass(slots=True)
class _Scan:
    """One walk over the ADS-B rows of a node-day, counting opportunities as it goes."""

    node_id: str
    source: _Source
    start_ms: int
    versions: _Versions
    in_fov: Callable[[np.ndarray, np.ndarray], np.ndarray] | None
    hexes: _Interner
    types: _Types
    detections: _Detections
    sections: _Sections
    # The (hex, bucket) keys already counted, sorted.
    counted: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))

    def _own_rows(self, table: pa.Table) -> Any:
        ids = table["source_node_id"]
        attributed = (
            pc.match_substring_regex(ids, pattern=f"(^|,){re.escape(self.node_id)}(,|$)")
            if self.source.listed
            else pc.equal(ids, self.node_id)
        )
        return functools.reduce(
            pc.and_,
            (
                pc.equal(table["world"], "real"),
                pc.equal(table["source"], self.source.name),
                attributed,
                pc.greater(table["alt_baro"], 0),
                _between(table[self.source.time_column], self.start_ms, self.start_ms + _DAY_MS),
            ),
        )

    def add(self, table: pa.Table) -> None:
        table = table.filter(self._own_rows(table))
        if not table.num_rows:
            return
        t_ms = table[self.source.time_column].to_numpy(zero_copy_only=False)
        lat, lon, alt_ft = _floats(table, "lat"), _floats(table, "lon"), _floats(table, "alt_baro")
        hex_ids = _hex_ids(self.hexes, table, "hex")
        row_types = (
            self.types.names.encode(table["aircraft_type"])
            if "aircraft_type" in table.column_names
            else np.full(table.num_rows, -1, dtype=np.int64)
        )
        for site, rows in self.versions.split(t_ms):
            self._add_rows(site, t_ms[rows], lat[rows], lon[rows], alt_ft[rows], hex_ids[rows], row_types[rows])

    def _add_rows(
        self,
        site: _Site,
        t_ms: np.ndarray,
        lat: np.ndarray,
        lon: np.ndarray,
        alt_ft: np.ndarray,
        hex_ids: np.ndarray,
        row_types: np.ndarray,
    ) -> None:
        look = site.look(lat, lon, alt_ft * _FT_TO_M)
        rr_km = look.rr_m / 1000.0
        buckets = _buckets(t_ms, self.start_ms)
        in_view = (
            site.in_declared_beam(look.bearing_deg)
            if self.in_fov is None
            else np.asarray(self.in_fov(look.bearing_deg, rr_km), dtype=bool)
        )
        keep = (look.delay_us < site.window_us) & (look.up_m > 0.0) & in_view
        if self.source.needs_frame:
            keep &= self.detections.frames[buckets]
        rows = np.flatnonzero(keep)
        keys = hex_ids[rows] * _KEY_SPAN + buckets[rows]
        if self.source.one_per_bucket:
            rows, keys = self._first_of_each_key(rows, keys)
        if not rows.size:
            return

        tag, claim, best_db = self._detected(keys)
        known_db = site.known_db(look.rt_m[rows], look.rr_m[rows])
        finite = np.isfinite(known_db)
        type_ids = self.types.resolved(hex_ids[rows], row_types[rows])
        self.sections.add(
            _Batch(
                tag=tag,
                claim=claim,
                tag_snr_db=np.where(tag & np.isfinite(best_db), best_db, 0.0),
                rr_km=rr_km[rows],
                delay_us=look.delay_us[rows],
                alt_ft=alt_ft[rows],
                bearing_deg=look.bearing_deg[rows],
                known_key=np.floor(np.where(finite, known_db, 0.0)).astype(np.int64),
                in_fit=finite & (look.delay_us[rows] < site.window_us - FIT_EDGE_MARGIN_US),
                type_ids=type_ids,
                class_ids=self.types.class_ids(type_ids),
            )
        )

    def _first_of_each_key(self, rows: np.ndarray, keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The first row of each key not counted yet, and the key marked counted."""
        distinct, first = np.unique(keys, return_index=True)
        fresh = _positions(self.counted, distinct) < 0
        self.counted = np.insert(self.counted, np.searchsorted(self.counted, distinct[fresh]), distinct[fresh])
        return rows[first[fresh]], distinct[fresh]

    def _detected(self, keys: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Whether each key has a tag and a gated claim in its bucket or either neighbour, and the best tag SNR."""
        found = self.detections
        tag = np.zeros(keys.shape, dtype=bool)
        claim = np.zeros(keys.shape, dtype=bool)
        best_db = np.full(keys.shape, np.nan)
        for shift in (-1, 0, 1):
            at = _positions(found.tag_keys, keys + shift)
            hit = at >= 0
            tag |= hit
            best_db[hit] = np.fmax(best_db[hit], found.tag_snr_db[at[hit]])
            claim |= _positions(found.claim_keys, keys + shift) >= 0
        return tag, claim, best_db


def node_day_table(
    node_id: str,
    day: date,
    *,
    configs: Sequence[Mapping[str, Any]],
    archive_root: str | Path,
    adsb_root: str | Path,
    types_for: Callable[[Sequence[str]], list[str | None]],
    class_for: Callable[[str | None], str],
    opportunities: Literal["aggregator", "node"] = "aggregator",
    threshold_db: float | None = None,
    in_fov: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
) -> dict[str, Any] | None:
    """One day's count table for a real node, JSON-safe; None for a synthetic node.

    ``configs`` are every config row of the node. ``in_fov(bearing_deg,
    range_km)`` is a learned field of view and replaces the declared one. A
    node with no ADS-B rows on disk gets a table of zeros.

    ``claim_columns`` is whether every file holding rows of the day has the
    claim columns, true with no rows. ``numerator`` is "claims" when they all do
    and a row of the day has a claim path, else "tags": on claims, the rows of a
    file from before the columns would all read as misses.
    """
    if is_synthetic_node(node_id):
        return None
    source = _SOURCES[opportunities]
    versions = _Versions(configs)
    hexes = _Interner()
    detections = _read_detections(node_id, day, archive_root, versions, hexes, threshold_db)
    types = _Types(hexes, types_for, class_for)
    sections = _Sections(types)
    start_ms = archive_reader.day_start_ms(day)
    scan = _Scan(node_id, source, start_ms, versions, in_fov, hexes, types, detections, sections)
    wanted = {
        source.time_column: (start_ms, start_ms + _DAY_MS - 1),
        "world": ("real", "real"),
        "source": (source.name, source.name),
    }
    for path in _adsb_files(adsb_root, day):
        for table in _row_groups(path, _ADSB_COLUMNS, wanted):
            scan.add(table)
    return {
        "schema": SCHEMA_VERSION,
        "node_id": node_id,
        "day": day.isoformat(),
        "denominator": source.name,
        "claim_columns": detections.claim_columns,
        "numerator": "claims" if detections.claim_columns and detections.any_claim_path else "tags",
        "gate_threshold_db": None if threshold_db is None else float(threshold_db),
        "opportunities": sections.opportunities,
        "detections_tags": sections.tags,
        "detections_claims": sections.claims,
        "class_range_alt": sections.class_range_alt,
        "class_geometry": sections.class_geometry,
        "class_ring": sections.class_ring,
        "bearing": sections.bearing,
        "by_range": sections.by_range,
        "by_delay": sections.by_delay,
        "by_alt": sections.by_alt,
        "by_type": sections.by_type,
        "snr_hist": detections.snr_hist,
        "snr_bin_db": SNR_BIN_DB,
        "tag_residual_hist": detections.residual_hist,
        "claim_residual_hist": detections.claim_residual_hist,
        "residual_bin_us": RESIDUAL_BIN_US,
    }


# ── Merging ──────────────────────────────────────────────────────────────────


def _merge_cell(section: dict[str, list[int]], label: str, cell: Sequence[int], detected: int) -> None:
    merged = section.setdefault(label, [0, 0])
    merged[0] += cell[0]
    merged[1] += cell[detected]


def _merge_day(merged: dict[str, Any], table: Mapping[str, Any], numerator: str) -> None:
    detected = _NUMERATOR_INDEX[numerator]
    merged["opportunities"] += table["opportunities"]
    merged["detections"] += table[f"detections_{numerator}"]
    merged["detections_claims"] += table["detections_claims"]
    merged["detections_tags"] += table["detections_tags"]
    for name in _CELL_SECTIONS:
        for label, cell in table[name].items():
            _merge_cell(merged[name], label, cell, detected)
    for into, cell in zip(merged["bearing"], table["bearing"], strict=True):
        into[0] += cell[0]
        into[1] += cell[detected]
    for name in _HISTOGRAMS:
        for key, count in table[name].items():
            merged[name][key] = merged[name].get(key, 0) + count
    for class_name, ranges in table["class_range_alt"].items():
        for range_label, altitudes in ranges.items():
            section = merged["class_range_alt"].setdefault(class_name, {}).setdefault(range_label, {})
            for alt_label, cell in altitudes.items():
                _merge_cell(section, alt_label, cell, detected)
    for class_name, rings in table["class_ring"].items():
        section = merged["class_ring"].setdefault(class_name, {})
        for ring, cell in rings.items():
            _merge_cell(section, ring, cell, detected)
    by_day = merged["class_geometry_by_day"].setdefault(table["day"], {})
    for class_name, keys in table["class_geometry"].items():
        section = merged["class_geometry"].setdefault(class_name, {})
        day_section = by_day.setdefault(class_name, {})
        for key, cell in keys.items():
            into = section.setdefault(key, [0, 0, 0, 0.0, 0.0])
            into[0] += cell[0]
            into[1] += cell[detected]
            into[2] += cell[1]
            into[3] += cell[3]
            into[4] += cell[4]
            day_section[key] = [cell[0], cell[detected]]


def merge_tables(
    tables: Iterable[Mapping[str, Any]], numerator: Literal["claims", "tags"] | None = None
) -> dict[str, Any]:
    """Add day tables up; a cell becomes ``[n, d]``, ``d`` counted by the window's one numerator.

    That is ``numerator`` when given, else "claims" when at least one day with
    opportunities reads claims and every such day has the claim columns, else
    "tags". Every day counts by it, so ``detections`` is always
    ``detections_<numerator>``: a day with the columns and no claim is a quiet
    day, whose zero claims are misses, and only a file without the columns sends
    a window with claims in it to tags. Raises ValueError on no tables, or on
    tables of different nodes or denominators.
    """
    days = sorted(tables, key=lambda table: table["day"])
    if not days:
        raise ValueError("no day tables to merge")
    for name in ("node_id", "denominator"):
        if len({table[name] for table in days}) > 1:
            raise ValueError(f"day tables disagree on {name}")
    active = [table for table in days if table["opportunities"]]
    any_claims = any(table["numerator"] == "claims" for table in active)
    all_columns = all(table["claim_columns"] for table in active)
    window_numerator = numerator or ("claims" if any_claims and all_columns else "tags")
    merged: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "node_id": days[0]["node_id"],
        "window": [days[0]["day"], days[-1]["day"]],
        "days": [table["day"] for table in days],
        "denominator": days[0]["denominator"],
        "numerator": window_numerator,
        "opportunities": 0,
        "detections": 0,
        "detections_claims": 0,
        "detections_tags": 0,
        "class_range_alt": {},
        "class_ring": {},
        "bearing": [[0, 0] for _ in range(BEARING_BINS)],
        "by_range": {},
        "by_delay": {},
        "by_alt": {},
        "by_type": {},
        "snr_hist": {},
        "snr_bin_db": SNR_BIN_DB,
        "tag_residual_hist": {},
        "claim_residual_hist": {},
        "residual_bin_us": RESIDUAL_BIN_US,
        "class_geometry": {},
        "class_geometry_by_day": {},
    }
    for table in days:
        _merge_day(merged, table, window_numerator)
    return merged


def _destination(lat_deg: float, lon_deg: float, ground_m: np.ndarray, bearing: np.ndarray) -> tuple[Any, Any]:
    """Where a bearing and a ground distance lead on the ellipsoid, by the Gauss mid-latitude formulas."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    d_lat = d_lon = 0.0
    for _ in range(4):
        mid = lat + d_lat / 2
        curve = 1 - _WGS84_E2 * np.sin(mid) ** 2
        meridian_m = _WGS84_A * (1 - _WGS84_E2) / curve**1.5
        normal_m = _WGS84_A / np.sqrt(curve)
        heading = bearing + d_lon * np.sin(mid) / 2
        d_lat = ground_m * np.cos(heading) / meridian_m
        d_lon = ground_m * np.sin(heading) / (normal_m * np.cos(mid))
    return np.degrees(lat + d_lat), np.degrees(lon + d_lon)


def ring_grid(
    config: Mapping[str, Any], *, ring_km: float = 5.0, max_km: float = 150.0, alt_ft: float = 12500.0
) -> dict[str, Any] | None:
    """K and delay of each (slant-range ring, bearing bin) at ``alt_ft``, for one config.

    None when the receiver, the transmitter or a figure K needs is unknown.
    Plain arrays, kept in memory only: this is where a position reaches the
    range figure, so a fit needs no coordinates.
    """
    site = _site(config)
    if site is None or math.isnan(site.link_db):
        return None
    rings = np.arange(ring_km / 2, max_km, ring_km)
    alt_m = alt_ft * _FT_TO_M
    height_km = (alt_m - site.rx_height_m) / 1000.0
    ground_km = np.sqrt(np.maximum(rings * rings - height_km * height_km, 0.0))
    bearings = np.radians((np.arange(BEARING_BINS) + 0.5) * BEARING_BIN_DEG)

    target_lat, target_lon = _destination(site.rx_lat_deg, site.rx_lon_deg, ground_km[:, None] * 1000.0, bearings)
    look = site.look(target_lat, target_lon, alt_m)
    return {
        "ring_km": rings,
        "known_db": site.known_db(look.rt_m, look.rr_m),
        "delay_us": look.delay_us,
        "window_us": site.window_us,
    }
