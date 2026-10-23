"""Names for the ADS-B snapshot archive's raw parts, and their writer.

Layout:

    adsb/year=YYYY/month=MM/day=DD/part-HHMMSS-<uuid32>.parquet

The partition is the UTC day the batch was flushed, and HHMMSS is the flush
time. Files are written through services/parquet_io.write_parquet_batch as one
row group, rows sorted by ts_ms, then hex, source and world.

One row is one fix of one aircraft in one snapshot of the server's ADS-B
picture. An aircraft can have several rows in one snapshot (in world "real",
one per public node that heard it, and each ADSBHub fix written for it); a
reader serves one of them. The columns:
  ts_ms           the snapshot's server wallclock
  hex             lowercase ICAO address
  lat, lon        position, degrees
  alt_baro        barometric altitude, feet; 0 on the ground
  gs, track       ground speed in knots, track in degrees
  flight          callsign, stripped; null when none
  seen_ms         the fix's capture time (a node's own clock for a node fix)
  world           "real" or "sim"
  source          "node" (a receiver's own ADS-B, including the simulator's
                  fleet) or "aggregator" (ADSBHub via adsb.retina.fm)
  source_node_id  who the fix is attributed to; never served

source_node_id, by source:
  node        The id of the node whose own receiver reported the fix,
              publishable when the row was written (not private, and with a
              public identity). In world "real" each such node holding a fresh
              fix of the aircraft has a row of its own; in world "sim" only
              the node of the freshest fix does (services/tasks/adsb_snapshot.py
              says why). Null only for the simulator's own fleet, which
              reports under no node and only in world "sim"; a world "real"
              node row always carries an id. A reader serves the row only
              while that node is publishable.
  aggregator  The ids, sorted and comma-joined, of the real nodes the fix is
              attributed to: each one's lattice cell was polled and its
              answer held the aircraft, and the fix lies within the clip
              radius of the node's PUBLISHED position
              (services/tasks/adsb_snapshot.py explains the radius). Never
              null or empty: a fix no node is attributed to is not written.
              The ids may include nodes that were private when the row was
              written; a reader serves the row only while at least one
              listed id is publishable.

The writer refuses a row that breaks either rule rather than writing it with
a null id, so a null always means the simulator's fleet.
"""

import logging
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeGuard

import pyarrow as pa

from services.parquet_io import write_parquet_batch

logger = logging.getLogger(__name__)

ADSB_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "coverage_data",
    "adsb",
)

SCHEMA = pa.schema(
    [
        pa.field("ts_ms", pa.int64(), nullable=False),
        pa.field("hex", pa.string(), nullable=False),
        pa.field("lat", pa.float64(), nullable=False),
        pa.field("lon", pa.float64(), nullable=False),
        pa.field("alt_baro", pa.int32()),
        pa.field("gs", pa.float64()),
        pa.field("track", pa.float64()),
        pa.field("flight", pa.string()),
        pa.field("seen_ms", pa.int64(), nullable=False),
        pa.field("world", pa.string(), nullable=False),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_node_id", pa.string()),
    ]
)

WORLDS = ("real", "sim")
SOURCES = ("node", "aggregator")

_INT32_MAX = 2**31 - 1
_INT64_MAX = 2**63 - 1


def _is_int(v) -> TypeGuard[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_float(v) -> TypeGuard[float]:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _text(v) -> str | None:
    """v when it is a non-empty str Parquet can hold (valid UTF-8), else None."""
    if not isinstance(v, str) or not v:
        return None
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return v


def _invalid_fields(row: dict) -> list[str]:
    """The required fields a row fails on; empty when it can be written."""
    bad = []
    for key in ("ts_ms", "seen_ms"):
        if not (_is_int(row.get(key)) and 0 < row[key] <= _INT64_MAX):
            bad.append(key)
    if _text(row.get("hex")) is None:
        bad.append("hex")
    lat, lon = row.get("lat"), row.get("lon")
    if not (_is_float(lat) and -90.0 <= lat <= 90.0):
        bad.append("lat")
    if not (_is_float(lon) and -180.0 <= lon <= 180.0):
        bad.append("lon")
    world, source = row.get("world"), row.get("source")
    if world not in WORLDS:
        bad.append("world")
    if source not in SOURCES:
        bad.append("source")
    node_id = row.get("source_node_id")
    # Null is the simulator's fleet and nothing else: see the module docstring.
    may_be_null = source == "node" and world == "sim"
    if not (_text(node_id) is not None or (node_id is None and may_be_null)):
        bad.append("source_node_id")
    return bad


def _columns(rows: list[dict]) -> dict[str, list]:
    """SCHEMA's columns from valid rows; an optional value the column cannot hold is null."""
    cols: dict[str, list] = {name: [] for name in SCHEMA.names}
    for r in rows:
        alt = r.get("alt_baro")
        cols["ts_ms"].append(r["ts_ms"])
        cols["hex"].append(r["hex"])
        cols["lat"].append(float(r["lat"]))
        cols["lon"].append(float(r["lon"]))
        cols["alt_baro"].append(alt if _is_int(alt) and -_INT32_MAX - 1 <= alt <= _INT32_MAX else None)
        cols["gs"].append(float(r["gs"]) if _is_float(r.get("gs")) else None)
        cols["track"].append(float(r["track"]) if _is_float(r.get("track")) else None)
        cols["flight"].append(_text(r.get("flight")))
        cols["seen_ms"].append(r["seen_ms"])
        cols["world"].append(r["world"])
        cols["source"].append(r["source"])
        cols["source_node_id"].append(r.get("source_node_id"))
    return cols


def write_adsb_parquet(
    *,
    rows: list[dict],
    base_dir: str | Path,
    write_ts: datetime | None = None,
) -> str | None:
    """Write snapshot rows as one raw part; return its key relative to base_dir.

    A row missing a required column, or holding a value its column cannot
    take, is skipped, with one warning per call naming how many and which
    fields failed; the rest of the batch is written, so no row can wedge the
    flush that retries a failed batch. Among the optional columns (alt_baro,
    gs, track, flight) such a value is written null instead.

    The partition is write_ts's UTC day (now when omitted). Returns
    ``year=YYYY/month=MM/day=DD/part-HHMMSS-<uuid32>.parquet``, or None,
    writing no file, when no row is valid.
    """
    kept = []
    failed: Counter[str] = Counter()
    for r in rows:
        bad = _invalid_fields(r) if isinstance(r, dict) else ["row"]
        if bad:
            failed.update(bad)
        else:
            kept.append(r)
    if len(kept) < len(rows):
        logger.warning(
            "ADS-B archive: skipped %d of %d rows (%s)",
            len(rows) - len(kept),
            len(rows),
            ", ".join(f"{key}={n}" for key, n in sorted(failed.items())),
        )
    if not kept:
        return None

    kept.sort(key=lambda r: (r["ts_ms"], r["hex"], r["source"], r["world"]))
    table = pa.table(_columns(kept), schema=SCHEMA)

    if write_ts is None:
        write_ts = datetime.now(timezone.utc)
    elif write_ts.tzinfo is None:
        write_ts = write_ts.replace(tzinfo=timezone.utc)
    else:
        write_ts = write_ts.astimezone(timezone.utc)
    partition = f"year={write_ts:%Y}/month={write_ts:%m}/day={write_ts:%d}"
    filename = write_parquet_batch(table, Path(base_dir) / partition, write_ts, row_group_sizes=[table.num_rows])
    return f"{partition}/{filename}"
