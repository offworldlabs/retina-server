"""Detection reach per aircraft tier and altitude band: P85 geometries from the truth-scored buckets.

The learned field of view in ``retina_analytics.empirical_coverage`` is one polygon per
node, learned from the node's own detections of whatever happened to fly past. This
module draws several per node from the independent evaluation's bucket tables
(``services.performance_truth``): one per aircraft tier and pressure-altitude band, so a
widebody at cruise and a light aircraft in the pattern get their own reach.

Per node, tier and altitude band, and per ``FOV_BINS`` bearing bin from the receiver:

* ``p85_det_km``: the 85th percentile of the receiver ground range of the opportunities
  the node detected (``validated`` in the bucket table: a detection whose delay and
  Doppler fit the aircraft's independently predicted geometry, tag or no tag). This is
  the P85 geometry, the same statistic the learned field of view uses, drawn from
  validated detections instead of tags, under the geometric gate (nothing removed
  from the denominator for coverage);
* ``p95_det_km``: the 95th percentile, the statistic the learned field of view extends
  its reach with (before its 1.25 margin);
* ``p85_opp_km``: the same percentile over every opportunity the truth offered in the
  bin, detected or not. Where ``p85_det_km`` reaches it, the traffic ran out before the
  radar did and the P85 is a lower bound on reach (``traffic_limited``);
* ``pd``: the bin's validated detection probability, so the geometry can be read
  beside the rate it summarises.

A map is drawn only where its detections beat chance: the bucket tables carry the
control opportunities (trajectories shifted in time, column ``shift_s``), and a tier
and band whose validated rate is under ``BACKGROUND_FACTOR`` times its own control rate,
or within ``BACKGROUND_MARGIN`` of it, is reported with its counts but no geometry
(``drawn`` false): a node that associates by chance would otherwise draw the traffic's
extent as its reach. A bin needs ``MIN_BIN_DETECTIONS`` validated detections from
``MIN_BIN_AIRCRAFT`` distinct aircraft before it is drawn. A bin short of that but holding a detection of
its own pools its two neighbours (``pooled``), as the learned field of view does; a bin
still short, or with no detection of its own, is closed (``null``), not zero. Everything here is receiver-relative (bearing, range) and holds no
position, so it can be published where the learned state, which embeds the receiver's
true position and raw aircraft positions, cannot.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from services import performance_data as pdata
from services import performance_truth as pt

logger = logging.getLogger(__name__)

FOV_BINS = 36
BIN_DEG = 360.0 / FOV_BINS
P_REACH = 85
P_FAR = 95
MIN_BIN_DETECTIONS = 10
MIN_BIN_AIRCRAFT = 2
TRAFFIC_LIMITED_RATIO = 0.9
BACKGROUND_FACTOR = 3.0
BACKGROUND_MARGIN = 0.02
ALT_BANDS_KFT: tuple[float, ...] = tuple(pdata.GRID_ALT_EDGES_KFT)
ALL = "all"

# Aircraft classes (services.performance_data) grouped into radar-cross-section tiers.
# A class in no tier (unknown) counts in the "all" tier only.
TIERS: dict[str, tuple[str, ...]] = {
    "heavy": ("widebody", "military_transport"),
    "airliner": ("narrowbody", "regional_jet"),
    "business": ("bizjet", "turboprop"),
    "light": ("light_GA", "helicopter"),
}
TIER_ORDER = (ALL, *TIERS)

_COLUMNS = ("hex", "class", "alt_ft", "ground_km", "bearing_deg", "validated", "opportunity", "geometric", "shift_s")


def band_labels() -> list[str]:
    return [ALL, *(f"{lo:g}-{hi:g} kft" for lo, hi in zip(ALT_BANDS_KFT, ALT_BANDS_KFT[1:], strict=False))]


def tier_of(class_names: Sequence[str | None]) -> np.ndarray:
    """The tier of each class name, "" when the class belongs to none."""
    lookup = {cls: tier for tier, classes in TIERS.items() for cls in classes}
    return np.array([lookup.get(c or "", "") for c in class_names], dtype=object)


def band_of(alt_ft: np.ndarray) -> np.ndarray:
    """Index of the altitude band holding each altitude, -1 outside the bands."""
    return pt._bins(np.asarray(alt_ft, dtype=float), tuple(e * 1000.0 for e in ALT_BANDS_KFT))


def bearing_bin(bearing_deg: np.ndarray) -> np.ndarray:
    return (np.asarray(bearing_deg, dtype=float) % 360.0 / BIN_DEG).astype(np.int64) % FOV_BINS


def load_opportunities(files: Iterable[Path]) -> pa.Table:
    """Every opportunity under the geometric gate from the bucket tables given: the real ones
    (``shift_s`` 0) and the control ones (shifted trajectories) the background is read from."""
    pieces = []
    for path in files:
        try:
            t = pq.read_table(path, columns=list(_COLUMNS))
        except (OSError, pa.ArrowException) as exc:
            logger.warning("perf fov: %s unreadable: %r", path, exc)
            continue
        keep = t["opportunity"].to_numpy(zero_copy_only=False) & t["geometric"].to_numpy(zero_copy_only=False)
        pieces.append(t.filter(pa.array(keep)))
    if not pieces:
        return pa.table({c: pa.array([], _COLUMNS_TYPES[c]) for c in _COLUMNS})
    return pa.concat_tables(pieces, promote_options="default")


_COLUMNS_TYPES = {
    "hex": pa.string(),
    "class": pa.string(),
    "alt_ft": pa.float64(),
    "ground_km": pa.float64(),
    "bearing_deg": pa.float64(),
    "validated": pa.bool_(),
    "opportunity": pa.bool_(),
    "geometric": pa.bool_(),
    "shift_s": pa.int64(),
}


def _pct(values: np.ndarray, pct: float) -> float | None:
    return float(np.percentile(values, pct)) if values.size else None


def bin_stats(
    ground_km: np.ndarray, bearing_deg: np.ndarray, detected: np.ndarray, hexes: np.ndarray
) -> list[dict[str, Any]]:
    """Per bearing bin: counts, Pd, the P85/P95 detection reach and the traffic reach."""
    ground_km = np.asarray(ground_km, dtype=float)
    detected = np.asarray(detected, dtype=bool)
    hexes = np.asarray(hexes, dtype=object)
    bins = bearing_bin(bearing_deg)
    members = [np.flatnonzero(bins == i) for i in range(FOV_BINS)]
    rows = []
    for i in range(FOV_BINS):
        own = members[i]
        own_det = own[detected[own]]
        n_opp, n_det = int(own.size), int(own_det.size)
        n_ac = int(np.unique(hexes[own_det]).size) if n_det else 0
        pooled = False
        chosen, chosen_det = own, own_det
        if n_det < MIN_BIN_DETECTIONS or n_ac < MIN_BIN_AIRCRAFT:
            pooled = True
            chosen = np.concatenate([members[(i - 1) % FOV_BINS], own, members[(i + 1) % FOV_BINS]])
            chosen_det = chosen[detected[chosen]]
        pooled_ac = int(np.unique(hexes[chosen_det]).size) if chosen_det.size else 0
        # A pooled bin still needs a detection of its own: neighbours may vouch for its
        # reach, not for its existence.
        open_ = n_det > 0 and chosen_det.size >= MIN_BIN_DETECTIONS and pooled_ac >= MIN_BIN_AIRCRAFT
        p85_det = _pct(ground_km[chosen_det], P_REACH) if open_ else None
        p95_det = _pct(ground_km[chosen_det], P_FAR) if open_ else None
        p85_opp = _pct(ground_km[chosen], P_REACH) if chosen.size else None
        rows.append(
            {
                "bearing_deg": i * BIN_DEG,
                "n_opp": n_opp,
                "n_det": n_det,
                "n_aircraft": n_ac,
                "pd": (n_det / n_opp) if n_opp else None,
                "p85_det_km": p85_det,
                "p95_det_km": p95_det,
                "p85_opp_km": p85_opp,
                "pooled": pooled and open_,
                "traffic_limited": bool(
                    open_ and p85_opp is not None and p85_det is not None and p85_det >= TRAFFIC_LIMITED_RATIO * p85_opp
                ),
            }
        )
    return rows


def beats_background(pd: float | None, background: float | None) -> bool:
    """Whether a validated rate is detection rather than chance association."""
    if pd is None:
        return False
    if background is None:
        return True
    return pd >= BACKGROUND_FACTOR * background and pd - background >= BACKGROUND_MARGIN


def stratum_map(table: pa.Table, tier: str, band: str) -> dict[str, Any]:
    """One P85 geometry: the bins of the opportunities in this tier and altitude band.

    ``table`` holds real and control opportunities; the geometry is drawn from the real
    ones, the background read from the control ones, and ``limits_km`` is all null when
    the real rate does not beat the background."""
    real = table.filter(pa.array(table["shift_s"].to_numpy() == 0))
    control = table.filter(pa.array(table["shift_s"].to_numpy() != 0))
    rows = bin_stats(
        real["ground_km"].to_numpy(zero_copy_only=False),
        real["bearing_deg"].to_numpy(zero_copy_only=False),
        real["validated"].to_numpy(zero_copy_only=False),
        np.asarray(real["hex"].to_pylist(), dtype=object),
    )
    detected = real["validated"].to_numpy(zero_copy_only=False)
    n_det = int(detected.sum())
    hexes = np.asarray(real["hex"].to_pylist(), dtype=object)
    pd = (n_det / real.num_rows) if real.num_rows else None
    background = (
        float(control["validated"].to_numpy(zero_copy_only=False).sum() / control.num_rows)
        if control.num_rows
        else None
    )
    drawn = beats_background(pd, background)
    if not drawn:
        for r in rows:
            r["p85_det_km"] = r["p95_det_km"] = None
            r["pooled"] = r["traffic_limited"] = False
    limits = [r["p85_det_km"] for r in rows]
    return {
        "tier": tier,
        "alt_band": band,
        "n_opp": int(real.num_rows),
        "n_det": n_det,
        "n_aircraft": int(np.unique(hexes[detected]).size) if n_det else 0,
        "pd": pd,
        "background": background,
        "background_n": int(control.num_rows),
        "net_pd": (pd - background) if (pd is not None and background is not None) else pd,
        "drawn": drawn,
        "open_bins": sum(1 for v in limits if v is not None),
        "pooled_bins": sum(1 for r in rows if r["pooled"]),
        "traffic_limited_bins": sum(1 for r in rows if r["traffic_limited"]),
        "limits_km": limits,
        "bins": rows,
    }


def stratum_maps(table: pa.Table) -> list[dict[str, Any]]:
    """Every tier x altitude band of the opportunities given, "all" first on both axes."""
    tiers = tier_of(table["class"].to_pylist())
    bands = band_of(table["alt_ft"].to_numpy(zero_copy_only=False))
    labels = band_labels()
    maps = []
    for tier in TIER_ORDER:
        in_tier = np.ones(table.num_rows, dtype=bool) if tier == ALL else tiers == tier
        for b, band in enumerate(labels):
            in_band = np.ones(table.num_rows, dtype=bool) if band == ALL else bands == b - 1
            picked = table.filter(pa.array(in_tier & in_band))
            maps.append(stratum_map(picked, tier, band))
    return maps


def polygon_km(limits_km: Sequence[float | None]) -> list[tuple[float, float]]:
    """The geometry as (bearing_deg, range_km) vertices at bin centres, closed bins at 0 km."""
    return [((i + 0.5) * BIN_DEG, float(v) if v is not None else 0.0) for i, v in enumerate(limits_km)]


def bins_table(node_id: str, maps: Sequence[Mapping[str, Any]]) -> pa.Table:
    """Every bin of every map as one flat table, for analysis beside the JSON."""
    columns: dict[str, list[Any]] = {
        "node_id": [],
        "tier": [],
        "alt_band": [],
        "bearing_deg": [],
        "n_opp": [],
        "n_det": [],
        "n_aircraft": [],
        "pd": [],
        "p85_det_km": [],
        "p95_det_km": [],
        "p85_opp_km": [],
        "pooled": [],
        "traffic_limited": [],
    }
    for m in maps:
        for r in m["bins"]:
            columns["node_id"].append(node_id)
            columns["tier"].append(m["tier"])
            columns["alt_band"].append(m["alt_band"])
            for key in list(columns)[3:]:
                columns[key].append(r[key])
    return pa.table(
        {
            "node_id": pa.array(columns["node_id"], pa.string()),
            "tier": pa.array(columns["tier"], pa.string()),
            "alt_band": pa.array(columns["alt_band"], pa.string()),
            "bearing_deg": pa.array(columns["bearing_deg"], pa.float64()),
            "n_opp": pa.array(columns["n_opp"], pa.int64()),
            "n_det": pa.array(columns["n_det"], pa.int64()),
            "n_aircraft": pa.array(columns["n_aircraft"], pa.int64()),
            "pd": pa.array(columns["pd"], pa.float64()),
            "p85_det_km": pa.array(columns["p85_det_km"], pa.float64()),
            "p95_det_km": pa.array(columns["p95_det_km"], pa.float64()),
            "p85_opp_km": pa.array(columns["p85_opp_km"], pa.float64()),
            "pooled": pa.array(columns["pooled"], pa.bool_()),
            "traffic_limited": pa.array(columns["traffic_limited"], pa.bool_()),
        }
    )


def node_document(
    node_id: str, files: Sequence[Path], windows: Sequence[str], *, includes_provisional: bool, now_ms: int
) -> dict[str, Any]:
    """The node's P85 geometries over the bucket tables given."""
    table = load_opportunities(files)
    maps = stratum_maps(table) if table.num_rows else []
    real = table.filter(pa.array(table["shift_s"].to_numpy() == 0)) if table.num_rows else table
    return {
        "schema": 1,
        "node_id": node_id,
        "generated_ms": now_ms,
        "windows": list(windows),
        "includes_provisional": includes_provisional,
        "bin_deg": BIN_DEG,
        "percentile": P_REACH,
        "far_percentile": P_FAR,
        "min_bin_detections": MIN_BIN_DETECTIONS,
        "min_bin_aircraft": MIN_BIN_AIRCRAFT,
        "alt_bands_kft": list(ALT_BANDS_KFT),
        "tiers": {tier: list(classes) for tier, classes in TIERS.items()},
        "background_factor": BACKGROUND_FACTOR,
        "background_margin": BACKGROUND_MARGIN,
        "n_opp": int(real.num_rows),
        "n_det": int(real["validated"].to_numpy(zero_copy_only=False).sum()) if real.num_rows else 0,
        "maps": maps,
    }


def overview(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The small part of a node's document that belongs beside its rates."""
    return {
        "n_opp": doc["n_opp"],
        "n_det": doc["n_det"],
        "windows": len(doc["windows"]),
        "maps_drawn": sum(1 for m in doc["maps"] if m["drawn"] and m["open_bins"]),
        "maps_chance_level": sum(1 for m in doc["maps"] if m["n_det"] and not m["drawn"]),
        "maps": {
            f"{m['tier']}|{m['alt_band']}": {"n_det": m["n_det"], "pd": m["pd"], "open_bins": m["open_bins"]}
            for m in doc["maps"]
            if m["drawn"] and m["open_bins"]
        },
    }
