"""One-shot backfill: rewrite rx_lat/rx_lon in already-archived Parquet to the published position.

Usage:
    NODE_FUZZ_SALT=<the production salt> \\
    NODE_FUZZ_MIN_KM=<the live min> NODE_FUZZ_MAX_KM=<the live max> \\
    PYTHONPATH=. python3 scripts/backfill_archive_fuzz.py [--prefix archive/]
                                                          [--limit N]
                                                          [--dry-run]
                                                          [--force]
                                                          [--fuzzed-since-ms MS]

Why this exists
---------------
services/parquet_writer.py writes the PUBLISHED receiver coordinate into every
new archive file.  Files written before that are still sitting in R2 with the
true one, and /api/data/archive hands them to anyone who asks — a Parquet file
pins a receiver forever and, unlike a live feed, cannot be taken back once
someone has a copy.  This walks the existing archive and moves them.

Only ``rx_lat`` and ``rx_lon`` change.  ``tx_*`` are licensed broadcast towers
and stay true; ``delay_us``, ``doppler_hz``, ``snr_db`` and the ``adsb_*``
columns are measurements and stay exactly as recorded — this is the same
serialization-boundary rewrite public_location.py describes, applied late.

The salt
--------
Refuses to run unless NODE_FUZZ_SALT is set explicitly.  ``public_location``
falls back to a salt generated and persisted in the runtime directory, which is
per-deployment: run this on a machine without the production salt and every row
would be displaced by an offset that matches nothing on the live map, so the
archive and the map would disagree about where the same node is — which is
worse than not running it, and is not undoable.  Also refuses when
NODE_FUZZ_MODE is off, where public_latlon is pass-through and the whole run
would be an expensive no-op.

Idempotence
-----------
Applying the offset twice displaces the node twice, so this must never rewrite
a file it has already rewritten.  The gate is the Parquet schema metadata
``retina.rx_frame=published`` (services/parquet_writer.RX_FRAME_KEY), which is
a fact carried inside the file itself: parquet_writer stamps it on every new
write, this script stamps it on every rewrite, and any stamped file is skipped.
Nothing here depends on the run log, on key naming, or on comparing coordinates
against a node config that may since have moved.

The one hole is the deploy gap: files written after the fuzz landed but before
the stamp landed carry a published rx and no stamp, and are indistinguishable
from pre-fuzz files by inspection.  ``--fuzzed-since-ms`` closes it — pass the
epoch-milliseconds of the fuzz deploy and any file whose rows were ingested at
or after it is skipped.  A file's ``ingest_ts_ms`` column is stamped by the
writer at flush time, so it is the right clock to compare against.  Establish
that timestamp before the first production run; it cannot be recovered
afterwards.

The bounds
----------
Pass the live server's NODE_FUZZ_MIN_KM and NODE_FUZZ_MAX_KM as well.  They are
part of the HMAC message, not merely the scale of its result, so a run under
the defaults against a deployment serving some other donut writes a frame that
matches nothing on the map — the same failure mode as the wrong salt, and just
as permanent.  There is no guard for it here: this script cannot see the
server's environment, and a wrong pair is indistinguishable from a deliberate
one.  Read them off the running deployment before starting.

``--force`` overrides BOTH guards and rewrites unconditionally.  It exists for
a re-run against a fresh copy of the archive, and displaces every node twice on
anything else.  There is no undo.
"""

from __future__ import annotations

import argparse
import logging
import sys

import pyarrow as pa
import pyarrow.parquet as pq

from config.constants import node_fuzz_salt
from services import r2_client
from services.parquet_writer import RX_FRAME_KEY, RX_FRAME_PUBLISHED, is_rx_published
from services.public_location import fuzz_enabled, public_latlon

logger = logging.getLogger("backfill_fuzz")


def needs_fuzzing(table: pa.Table, fuzzed_since_ms: int | None) -> bool:
    """Whether this table still carries true receiver coordinates.

    False when the file is stamped as published, and false when every row was
    ingested at or after ``fuzzed_since_ms`` — see the module docstring on the
    deploy gap.  ``max`` rather than ``min`` of the ingest column: a file is
    written in one flush, so its rows share an ingest stamp, and taking the
    latest is the reading that errs towards skipping.
    """
    if is_rx_published(table.schema):
        return False
    if fuzzed_since_ms is None:
        return True
    if "ingest_ts_ms" not in table.schema.names:
        return True
    stamps = [v for v in table.column("ingest_ts_ms").to_pylist() if v is not None]
    if not stamps:
        return True
    return max(stamps) < fuzzed_since_ms


def fuzz_rx_columns(table: pa.Table) -> pa.Table:
    """A copy of ``table`` whose rx_lat/rx_lon are the published coordinates.

    Pure: no I/O, no globals beyond the deterministic offset public_location
    memoises, so the transform is testable without touching R2.  Per row rather
    than per file because ``node_id`` is a column — a file is written per node
    today, but the schema does not promise that and a mixed file must not be
    displaced by one node's offset.

    A row with a null rx is left null: there is nothing to move, and inventing
    a coordinate for it would be worse than the gap.  The result is stamped
    published, which is what makes a second run over the same file a no-op.

    Small memo on (node_id, lat, lon) because a per-detection archive repeats
    the frame's constant rx on every row: a 200k-row file is a handful of
    distinct receivers, not 200k trig evaluations.
    """
    node_ids = table.column("node_id").to_pylist()
    lats = table.column("rx_lat").to_pylist()
    lons = table.column("rx_lon").to_pylist()

    memo: dict[tuple, tuple] = {}
    out_lat: list[float | None] = []
    out_lon: list[float | None] = []
    for nid, lat, lon in zip(node_ids, lats, lons, strict=True):
        if lat is None or lon is None:
            out_lat.append(lat)
            out_lon.append(lon)
            continue
        key = (nid, lat, lon)
        moved = memo.get(key)
        if moved is None:
            moved = memo[key] = public_latlon(lat, lon, nid)
        out_lat.append(moved[0])
        out_lon.append(moved[1])

    out = table.set_column(table.schema.get_field_index("rx_lat"), "rx_lat", pa.array(out_lat, pa.float64()))
    out = out.set_column(out.schema.get_field_index("rx_lon"), "rx_lon", pa.array(out_lon, pa.float64()))
    return out.replace_schema_metadata({**(table.schema.metadata or {}), RX_FRAME_KEY: RX_FRAME_PUBLISHED})


def transform_parquet_bytes(raw: bytes, fuzzed_since_ms: int | None = None, force: bool = False) -> bytes | None:
    """One file's bytes in, the rewritten bytes out, or None if it needs no rewrite.

    The other half of the pure core: everything above the R2 calls in ``run``
    happens here, so the whole transform can be exercised on a buffer.
    """
    table = pq.read_table(pa.BufferReader(raw))
    if not force and not needs_fuzzing(table, fuzzed_since_ms):
        return None
    sink = pa.BufferOutputStream()
    # Same codec and level as services/parquet_writer, so a rewritten file is
    # the same kind of object as a freshly written one.
    pq.write_table(fuzz_rx_columns(table), sink, compression="zstd", compression_level=3)
    return sink.getvalue().to_pybytes()


def run(
    prefix: str = "archive/",
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    fuzzed_since_ms: int | None = None,
) -> dict:
    if not node_fuzz_salt():
        logger.error(
            "NODE_FUZZ_SALT is not set. Without it public_location falls back to this machine's "
            "persisted salt and the archive would be displaced by offsets the live map does not "
            "use. Set the production salt explicitly and re-run."
        )
        return {"error": "no_salt"}
    if not fuzz_enabled():
        logger.error("NODE_FUZZ_MODE is off; public_latlon is pass-through and this run would do nothing.")
        return {"error": "fuzz_disabled"}
    if not r2_client.is_enabled():
        logger.error("R2 is not configured; aborting.")
        return {"error": "r2_disabled"}
    if force:
        logger.warning("--force: skip checks are off, every listed file will be displaced again. No undo.")

    stats = {"scanned": 0, "rewritten": 0, "skipped": 0, "errors": 0}

    keys = [k for k in r2_client.list_keys(prefix) if k.endswith(".parquet")]
    if limit:
        keys = keys[:limit]
    logger.info("Found %d Parquet keys under %s", len(keys), prefix)

    for key in keys:
        stats["scanned"] += 1
        try:
            raw = r2_client.download_bytes(key)
            if not raw:
                stats["errors"] += 1
                continue
            rewritten = transform_parquet_bytes(raw, fuzzed_since_ms=fuzzed_since_ms, force=force)
            if rewritten is None:
                stats["skipped"] += 1
                continue
            if dry_run:
                logger.info("DRY: %s (%d -> %d bytes)", key, len(raw), len(rewritten))
            else:
                # Back to the SAME key: the archive index, the download route
                # and anyone holding a link all address files by key, so a new
                # key would leave the true-coordinate file exactly where it
                # already is.
                ok = r2_client.upload_bytes(key, rewritten, content_type="application/octet-stream")
                if not ok:
                    stats["errors"] += 1
                    continue
            stats["rewritten"] += 1
            if stats["scanned"] % 100 == 0:
                logger.info("Progress: %s", stats)
        except Exception:
            logger.exception("Failed to rewrite %s", key)
            stats["errors"] += 1

    logger.info("Archive fuzz backfill done: %s", stats)
    return stats


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--prefix", default="archive/")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--fuzzed-since-ms",
        type=int,
        default=None,
        help="Skip files whose rows were ingested at or after this epoch-ms — the fuzz deploy time.",
    )
    args = p.parse_args()

    stats = run(
        prefix=args.prefix,
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        fuzzed_since_ms=args.fuzzed_since_ms,
    )
    if stats.get("error"):
        sys.exit(2)


if __name__ == "__main__":
    main()
