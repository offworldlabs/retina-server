"""Parquet writer for multi-node solver track outputs.

Tracks are the *product* of the system — the lat/lon/alt/velocity solutions
emitted by the multi-node solver — as opposed to the raw detections that fed
them. We persist them in their own Parquet stream so the dataset captures
what the system actually output at a given solver version, not just what we
*could* re-derive from raw frames.

Layout:

    tracks/year=YYYY/month=MM/day=DD/part-HHMMSS-<batch>.parquet

Schema is one row per track update, similar in spirit to the detections schema.
Columns are only ever appended, so older files read with the newer columns null.
Each file holds one row group per node_kind ("real", "synthetic", "mixed", then
unknown), so a reader can count or skip a fleet from the footer alone.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from services.node_kind import node_kind
from services.parquet_io import write_parquet_batch

SCHEMA = pa.schema(
    [
        ("solve_ts_ms", pa.int64()),
        ("frame_ts_ms", pa.int64()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("alt_m", pa.float64()),
        ("vel_east_ms", pa.float64()),
        ("vel_north_ms", pa.float64()),
        ("vel_up_ms", pa.float64()),
        ("n_nodes", pa.int32()),
        ("contributing_node_ids", pa.string()),
        ("adsb_hex", pa.string()),
        ("rms_delay_us", pa.float64()),
        ("rms_doppler_hz", pa.float64()),
        ("target_class", pa.string()),
        ("track_key", pa.string()),
        ("lane", pa.string()),
        ("pos_sigma_m", pa.float64()),
        ("is_anomalous", pa.bool_()),
        ("anomaly_types", pa.string()),
        ("node_kind", pa.string()),
        ("solve_count", pa.int32()),
        ("contributing_node_refs", pa.string()),
    ]
)

# Row-group order within a file; None is a solve with no contributor ids.
_NODE_KIND_GROUPS = ("real", "synthetic", "mixed", None)


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(d: dict, key: str) -> int | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lane(key: str | None, r: dict) -> str | None:
    if not key:
        return None
    if key.startswith("mn-adsb-"):
        return "known"
    if key.startswith("mn-dark-"):
        return "follow" if r.get("follow_lane") else "dark"
    return None


def _pos_sigma_m(r: dict, key: str | None, solve_sigma_m) -> float | None:
    sigma = solve_sigma_m(r, dark=not (key or "").startswith("mn-adsb-"))
    if sigma is None or not math.isfinite(sigma):
        return None
    return sigma


def _anomaly_types(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        return ",".join(str(x) for x in v)
    return None


def _node_refs(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ",".join("" if x is None else str(x) for x in v)
    return None


def _flatten(records: list[dict]) -> dict[str, list]:
    """Column lists for ``records``, in SCHEMA order.

    The appended columns, from the record the solver archived:

    - track_key: the state.multinode_tracks key, stamped on the archive copy
      at both append sites (solver._process_solver_item, known_lane._publish).
    - lane: "known" for an mn-adsb-* key; for an mn-dark-* key, "follow" when
      the record carries follow_lane (stamped by the solver when its input
      came from the dark-follow lane), else "dark"; null for any other key or
      none. vel_source is not needed: the key prefix separates known from
      dark, and the follow marker separates follow from dark.
    - pos_sigma_m: services.solve_uncertainty.solve_sigma_m, with the dark
      floors for every key that is not mn-adsb-* (no key takes the dark,
      wider, floors), as solve_history computes it. Null when the record has
      no n_nodes or the value is not finite.
    - is_anomalous: null when the record has none, else its truth value.
    - anomaly_types: the list comma-joined in its own order. Null means not
      recorded, "" means recorded with no types.
    - node_kind: services.node_kind.node_kind of contributing_node_ids as
      stored, the string a reader splits, so an id holding a comma is
      classified by its parts: "real" if none is synthetic, "synthetic" if
      all are, "mixed" otherwise, null with no ids. The archive reader falls
      back on the same classifier for files written before this column
      existed.
    - solve_count: the key's solve count, null when absent.
    - contributing_node_refs: what each contributor was published as when the
      solve was archived (services.node_refs.owner_identity, stamped at both
      append sites), comma-joined in contributing_node_ids order. An empty
      slot is a contributor with no published identity. Null when the record
      carries none.
    """
    # Imported here, not at module level: solve_uncertainty pulls in
    # track_filter and core.state, and importing this module must stay light
    # enough for tools that write the archive outside the app.
    from services.solve_uncertainty import solve_sigma_m

    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for r in records:
        contributing = r.get("contributing_node_ids") or []
        if isinstance(contributing, list):
            contributing = ",".join(str(x) for x in contributing)
        key = r.get("track_key") or None
        is_anomalous = r.get("is_anomalous")
        anomaly_types = r.get("anomaly_types")
        cols["solve_ts_ms"].append(_i(r, "solve_ts_ms") or 0)
        cols["frame_ts_ms"].append(_i(r, "timestamp_ms") or 0)
        cols["lat"].append(_f(r, "lat") or 0.0)
        cols["lon"].append(_f(r, "lon") or 0.0)
        cols["alt_m"].append(_f(r, "alt_m"))
        cols["vel_east_ms"].append(_f(r, "vel_east"))
        cols["vel_north_ms"].append(_f(r, "vel_north"))
        cols["vel_up_ms"].append(_f(r, "vel_up"))
        cols["n_nodes"].append(_i(r, "n_nodes") or 0)
        cols["contributing_node_ids"].append(contributing)
        cols["adsb_hex"].append(r.get("adsb_hex") or None)
        cols["rms_delay_us"].append(_f(r, "rms_delay"))
        cols["rms_doppler_hz"].append(_f(r, "rms_doppler"))
        cols["target_class"].append(r.get("target_class") or None)
        cols["track_key"].append(key)
        cols["lane"].append(_lane(key, r))
        cols["pos_sigma_m"].append(_pos_sigma_m(r, key, solve_sigma_m))
        cols["is_anomalous"].append(None if is_anomalous is None else bool(is_anomalous))
        cols["anomaly_types"].append(None if anomaly_types is None else _anomaly_types(anomaly_types))
        cols["node_kind"].append(node_kind(contributing))
        cols["solve_count"].append(_i(r, "solve_count"))
        cols["contributing_node_refs"].append(_node_refs(r.get("contributing_node_refs")))
    return cols


def _group_by_node_kind(table: pa.Table) -> tuple[pa.Table, list[int]]:
    """``table`` reordered into _NODE_KIND_GROUPS order, and each group's length.

    Rows keep their order within a group.
    """
    rows: dict[str | None, list[int]] = {kind: [] for kind in _NODE_KIND_GROUPS}
    for i, kind in enumerate(table.column("node_kind").to_pylist()):
        rows[kind].append(i)
    order = [i for kind in _NODE_KIND_GROUPS for i in rows[kind]]
    return table.take(order), [len(rows[kind]) for kind in _NODE_KIND_GROUPS if rows[kind]]


def write_tracks_parquet(
    *,
    records: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
) -> str | None:
    """Write a list of track records as a single Parquet file.

    One row group per node_kind present, in _NODE_KIND_GROUPS order. Returns
    the relative key (Hive-partitioned path) or None when ``records`` is
    empty.
    """
    if not records:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    cols = _flatten(records)
    if not cols["solve_ts_ms"]:
        return None

    table, group_sizes = _group_by_node_kind(pa.table(cols, schema=SCHEMA))

    partition = f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}"
    filename = write_parquet_batch(table, Path(base_dir) / partition, write_ts, row_group_sizes=group_sizes)
    return f"{partition}/{filename}"
