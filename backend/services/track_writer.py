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

A row is a solve with a real position, time and node count, and contributor
ids that can be read: records without them are skipped (see
write_tracks_parquet), never written with a stand-in.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa

from services.node_kind import node_kind
from services.parquet_io import write_parquet_batch

logger = logging.getLogger(__name__)

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

# The node_kind values a record can carry stamped.
_NODE_KINDS = ("real", "synthetic", "mixed")
# Row-group order within a file; None is a solve with no contributor ids.
_NODE_KIND_GROUPS = (*_NODE_KINDS, None)

_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1

# The record fields every row needs.
_REQUIRED = ("lat", "lon", "timestamp_ms", "solve_ts_ms", "n_nodes")
# The fields a record is skipped for, in the order the skip warning names
# them: the required ones, then contributing_node_ids, which may be absent (a
# solve with no contributors) but never unreadable.
_SKIPPED_FOR = (*_REQUIRED, "contributing_node_ids")


def _f(d: dict, key: str) -> float | None:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError, OverflowError):
        return None


def _i(d: dict, key: str, top: int = _INT64_MAX) -> int | None:
    """``d[key]`` as an int a signed column with maximum ``top`` holds, else None.

    inf and NaN are None too, so no value fails the batch.
    """
    v = d.get(key)
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if -top - 1 <= n <= top else None


def _truth(v) -> bool | None:
    """``v``'s truth value; None for None, or for a value that has none (a
    numpy array of several items)."""
    if v is None:
        return None
    try:
        return bool(v)
    except (TypeError, ValueError):
        return None


def _text(v) -> str | None:
    """``v`` if it is a str a string column can hold, else None.

    Parquet strings are UTF-8, so a str holding a lone surrogate (which
    json.loads gives for "\\ud800") is None too: pyarrow refuses it.
    """
    if not isinstance(v, str):
        return None
    if v.isascii():
        return v
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return v


def _node_ids(v) -> str | None:
    """contributing_node_ids as stored, or None if it cannot be read.

    A list or tuple of str is comma-joined and a str kept as is; None, an
    absent field, is "", a solve with no contributor ids. Anything else, a
    list holding anything but str included, cannot be read.
    """
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        if not all(isinstance(x, str) for x in v):
            return None
        v = ",".join(v)
    return _text(v)


def _invalid_fields(r: dict) -> list[str]:
    """The _SKIPPED_FOR fields ``r`` has no usable value for; empty for a row.

    A finite lat in [-90, 90] and lon in [-180, 180] (0.0 is a position), a
    positive timestamp_ms, solve_ts_ms and n_nodes, and contributing_node_ids
    _node_ids can read.
    """
    lat, lon = _f(r, "lat"), _f(r, "lon")
    bad = []
    # NaN fails both comparisons.
    if lat is None or not -90.0 <= lat <= 90.0:
        bad.append("lat")
    if lon is None or not -180.0 <= lon <= 180.0:
        bad.append("lon")
    for key, top in (("timestamp_ms", _INT64_MAX), ("solve_ts_ms", _INT64_MAX), ("n_nodes", _INT32_MAX)):
        n = _i(r, key, top)
        if n is None or n <= 0:
            bad.append(key)
    if _node_ids(r.get("contributing_node_ids")) is None:
        bad.append("contributing_node_ids")
    return bad


def _lane(key: str | None, r: dict) -> str | None:
    if not key:
        return None
    if key.startswith("mn-adsb-"):
        return "known"
    if key.startswith("mn-dark-"):
        follow = r.get("follow_lane")
        follows = False if follow is None else _truth(follow)
        if follows is None:
            return None
        return "follow" if follows else "dark"
    return None


def _pos_sigma_m(r: dict, key: str | None, solve_sigma_m) -> float | None:
    # solve_sigma_m converts the n_nodes and pos_sigma_km it reads with
    # conversions that raise on some values (int(inf), float(10**400)), so
    # it gets them as read here.
    read = {**r, "n_nodes": _i(r, "n_nodes", _INT32_MAX), "pos_sigma_km": _f(r, "pos_sigma_km")}
    sigma = solve_sigma_m(read, dark=not (key or "").startswith("mn-adsb-"))
    if sigma is None or not math.isfinite(sigma):
        return None
    return sigma


def _anomaly_types(v) -> str | None:
    if isinstance(v, (list, tuple, set, frozenset)):
        if not all(isinstance(x, str) for x in v):
            return None
        v = ",".join(v)
    return _text(v)


def _node_refs(v) -> str | None:
    if isinstance(v, (list, tuple)):
        if not all(x is None or isinstance(x, str) for x in v):
            return None
        v = ",".join(x or "" for x in v)
    return _text(v)


def _node_kind(stamped, contributing: str | None) -> str | None:
    if isinstance(stamped, str) and stamped in _NODE_KINDS:
        return stamped
    return node_kind(contributing)


def _flatten(records: list[dict]) -> dict[str, list]:
    """Column lists for ``records``, in SCHEMA order.

    A value is what the record holds, or null; nothing is filled in, and no
    value raises. write_tracks_parquet passes only records _invalid_fields
    accepts.

    Each value is read as its column's type or is null. A number is null
    when it does not convert or does not fit its column (_f, _i). A string
    column takes only a str Parquet can hold (_text: not bytes, not a number,
    not a lone surrogate), and a list joined into one only str items;
    adsb_hex, target_class and track_key take a non-empty one.
    contributing_node_ids is a list or tuple of str comma-joined, a str as
    is, or "" for none, and null for anything else (_node_ids):
    write_tracks_parquet skips such a record rather than write it.

    The appended columns, from the record the solver archived:

    - track_key: the state.multinode_tracks key, stamped on the archive copy
      at both append sites (solver._process_solver_item, known_lane._publish).
    - lane: "known" for an mn-adsb-* key; for an mn-dark-* key, "follow" when
      the record carries follow_lane (stamped by the solver when its input
      came from the dark-follow lane), else "dark"; null for any other key or
      none, or when follow_lane has no truth value. vel_source is not needed:
      the key prefix separates known from dark, and the follow marker
      separates follow from dark.
    - pos_sigma_m: services.solve_uncertainty.solve_sigma_m, with the dark
      floors for every key that is not mn-adsb-* (no key takes the dark,
      wider, floors), as solve_history computes it. Null when the record has
      no n_nodes or the value is not finite.
    - is_anomalous: null when the record has none, or a value with no truth
      value, else its truth value.
    - anomaly_types: the list, tuple or set comma-joined in its own order, or
      a str as is. Null means not recorded, "" means recorded with no types.
    - node_kind: the record's node_kind when it is "real", "synthetic" or
      "mixed", which both append sites stamp from the contributors'
      handshake verdict. Otherwise (a record from an older producer or a
      tool, or any other value) by id prefix: services.node_kind.node_kind of
      contributing_node_ids as stored, the string a reader splits, so an id
      holding a comma is classified by its parts: "real" if none is
      synthetic, "synthetic" if all are, "mixed" otherwise, null with no ids.
      The archive reader falls back on the prefix rule for files written
      before this column existed.
    - solve_count: the key's solve count, null when absent.
    - contributing_node_refs: what each contributor was published as when the
      solve was archived (services.node_refs.cached_owner_identity, stamped at
      both append sites by services.tasks.solver.stamp_archive_contributors),
      comma-joined in contributing_node_ids order. An empty slot (a None or
      "" item) is a contributor with no published identity then, resolved
      when the row is read. Null when the record carries none, or a list or
      tuple holding anything but str and None.
    """
    # Imported here, not at module level: solve_uncertainty pulls in
    # track_filter and core.state, and importing this module must stay light
    # enough for tools that write the archive outside the app.
    from services.solve_uncertainty import solve_sigma_m

    cols: dict[str, list] = {f.name: [] for f in SCHEMA}
    for r in records:
        contributing = _node_ids(r.get("contributing_node_ids"))
        key = _text(r.get("track_key")) or None
        cols["solve_ts_ms"].append(_i(r, "solve_ts_ms"))
        cols["frame_ts_ms"].append(_i(r, "timestamp_ms"))
        cols["lat"].append(_f(r, "lat"))
        cols["lon"].append(_f(r, "lon"))
        cols["alt_m"].append(_f(r, "alt_m"))
        cols["vel_east_ms"].append(_f(r, "vel_east"))
        cols["vel_north_ms"].append(_f(r, "vel_north"))
        cols["vel_up_ms"].append(_f(r, "vel_up"))
        cols["n_nodes"].append(_i(r, "n_nodes", _INT32_MAX))
        cols["contributing_node_ids"].append(contributing)
        cols["adsb_hex"].append(_text(r.get("adsb_hex")) or None)
        cols["rms_delay_us"].append(_f(r, "rms_delay"))
        cols["rms_doppler_hz"].append(_f(r, "rms_doppler"))
        cols["target_class"].append(_text(r.get("target_class")) or None)
        cols["track_key"].append(key)
        cols["lane"].append(_lane(key, r))
        cols["pos_sigma_m"].append(_pos_sigma_m(r, key, solve_sigma_m))
        cols["is_anomalous"].append(_truth(r.get("is_anomalous")))
        cols["anomaly_types"].append(_anomaly_types(r.get("anomaly_types")))
        cols["node_kind"].append(_node_kind(r.get("node_kind"), contributing))
        cols["solve_count"].append(_i(r, "solve_count", _INT32_MAX))
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

    No value in a record fails the batch, which the flush would retry
    forever. A record is skipped (_invalid_fields), with one warning per call
    naming how many and which fields failed, when:

    - lat, lon, timestamp_ms, solve_ts_ms or n_nodes is absent, does not
      convert, or is out of range (a finite lat in [-90, 90] and lon in
      [-180, 180], positive times and node count, each fitting its column);
    - contributing_node_ids is not None (or absent: a solve with no
      contributors), a str, or a list or tuple of str, or is text that is
      not valid UTF-8 (_node_ids). The reader drops a row with a private
      contributor by these ids, so a row is never written with null or
      partial ones.

    Any other value its column cannot hold is written null (_flatten says
    how each column reads its value).

    One row group per node_kind present, in _NODE_KIND_GROUPS order. Returns
    the relative key (Hive-partitioned path), or None, writing no file, when
    no record is valid.
    """
    kept = []
    failed: Counter[str] = Counter()
    for r in records:
        bad = _invalid_fields(r)
        if bad:
            failed.update(bad)
        else:
            kept.append(r)
    if len(kept) < len(records):
        logger.warning(
            "track archive: skipped %d of %d records without a valid position, time or node count (%s)",
            len(records) - len(kept),
            len(records),
            ", ".join(f"{key}={failed[key]}" for key in _SKIPPED_FOR if failed[key]),
        )
    if not kept:
        return None

    write_ts = write_ts or datetime.now(timezone.utc)
    table, group_sizes = _group_by_node_kind(pa.table(_flatten(kept), schema=SCHEMA))

    partition = f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}"
    filename = write_parquet_batch(table, Path(base_dir) / partition, write_ts, row_group_sizes=group_sizes)
    return f"{partition}/{filename}"
