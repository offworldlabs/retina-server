"""Archive lifecycle management: offload old detections to R2, prune local disk.

Runs every hour as a background task. The lifecycle is:
  1. Files older than OFFLOAD_AGE_DAYS → upload to R2 under "archive/" prefix.
     On success, a `<file>.uploaded` sentinel is written next to the file.
  2. Files older than RETENTION_DAYS that have a sentinel → delete from disk.
     Files without a sentinel are NEVER deleted, even past retention — this
     guarantees no local copy is removed before the R2 upload is confirmed.
  3. Empty date directories are cleaned up.

R2 retains uploaded files indefinitely. The default config keeps local files
for 3 days after creation (RETENTION_DAYS = 3); upload happens at 1 day, so
the 2-day buffer covers transient R2 outages and gives ~48 retry attempts.

A root whose prefix names a compacted store (compaction.store_for_prefix:
tracks/ is compaction.TRACKS and adsb/ is compaction.ADSB; archive/ has none)
adds rules, each applied with that store's own settings:
  0. Every cycle first compacts each such root's complete day partitions
     (the day's UTC end plus the store's settle margin has passed), on every
     host, with an object store or not: compaction.sweep() writes
     day=DD/compact.parquet and its sidecar day=DD/compact.json. A sweep that
     fails is logged, and the other root's sweep and the rest of the cycle
     still run.
  - compact.parquet is uploaded with no age gate, once its size and sha256
    match the sidecar's, then compact.json, then its sentinel is written
    holding that sha256. It counts as uploaded only while the sentinel holds
    the sidecar's sha256, so a compact a later sweep rewrites is uploaded
    again. Once its day ended more than the store's retention ago
    (TRACKS_COMPACT_RETENTION_DAYS for tracks, ADSB_COMPACT_RETENTION_DAYS for
    adsb; 0 keeps it forever), it is uploaded, and no raw part is left in its
    partition, it is deleted locally. Until then it stays, cycle after cycle,
    which keeps the day readable from local disk and stops superseded parts
    from piling up beside a deleted compact: the parts leave first, on their
    own schedule, and the compact on a later cycle. Its sentinel and
    compact.json are never deleted; the sentinel goes on recording that the
    object store holds the compact the sidecar describes, so a superseded
    part left over later can still be shown safe and deleted.
  - Every other .parquet is a raw part: uploaded as above, but deleted only
    once it is also superseded (listed in its day's sidecar) and that day's
    compact is safe: on disk at the sidecar's size, or uploaded (its sentinel
    holds the sidecar's sha256, the compact on local disk or not).
"""

import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from config.constants import (
    ARCHIVE_OFFLOAD_AGE_DAYS,
    ARCHIVE_RETENTION_DAYS,
)
from services import compaction

logger = logging.getLogger(__name__)

_COVERAGE_DIR = Path(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))) / "coverage_data"
_ARCHIVE_DIR = _COVERAGE_DIR / "archive"
# Track-solver Parquet output (services.tasks.track_archive).  Hive-partitioned
# as tracks/year=YYYY/month=MM/day=DD/part-*.parquet — a different layout from
# the detection archive, and historically NOT swept by this lifecycle: the
# old iterator only walked _ARCHIVE_DIR's year/month/day/node tree, so track
# Parquet accumulated forever (observed: 67 GB filling a 77 GB prod disk).
_TRACKS_DIR = _COVERAGE_DIR / "tracks"
# ADS-B snapshot raw parts, same Hive layout as tracks (services.adsb_writer).
_ADSB_DIR = _COVERAGE_DIR / "adsb"


def _archive_roots():
    """Local dir → R2 prefix pairs swept every cycle.

    Built fresh from the module globals (not frozen at import) so tests that
    monkeypatch _ARCHIVE_DIR / _TRACKS_DIR / _ADSB_DIR to temp paths take effect.
    """
    return [
        (_ARCHIVE_DIR, "archive"),
        (_TRACKS_DIR, "tracks"),
        (_ADSB_DIR, "adsb"),
    ]


# Caps to prevent runaway work in a single cycle
_MAX_UPLOAD_PER_CYCLE = 500
_MAX_DELETE_PER_CYCLE = 2000

# Suffix appended to a file's path to mark it as confirmed-uploaded to R2.
# Presence of this sentinel is the sole gate for local deletion.
_UPLOADED_SUFFIX = ".uploaded"


_HASH_CHUNK = 1 << 20


def _sentinel_path(file: Path) -> Path:
    return file.with_name(file.name + _UPLOADED_SUFFIX)


@dataclass
class _Partition:
    """A day partition of a compacted store, as this cycle read it."""

    # Its sidecar; None when there is none or it cannot be read.
    manifest: compaction.Manifest | None
    # The object store holds the compact the sidecar describes: its sentinel
    # holds the sidecar's sha256 (kept when the local compact is deleted), or
    # this cycle uploaded it.
    compact_uploaded: bool

    def keeps_rows_of(self, part: Path) -> bool:
        """True when ``part`` is superseded and the compact holding its rows is on disk at the sidecar's size, or uploaded."""
        if self.manifest is None or part.name not in self.manifest.parts:
            return False
        if self.compact_uploaded:
            return True
        try:
            return (part.parent / compaction.COMPACT_NAME).stat().st_size == self.manifest.size
        except OSError:
            return False


class _Partitions(dict[Path, _Partition]):
    """Each day directory's sidecar and compact sentinel, read once per cycle, on first use."""

    def __missing__(self, directory: Path) -> _Partition:
        manifest = compaction.read_sidecar(str(directory))
        uploaded = manifest is not None and _sentinel_holds(directory / compaction.COMPACT_NAME, manifest.sha256)
        partition = self[directory] = _Partition(manifest, uploaded)
        return partition


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _sentinel_holds(compact: Path, sha256: str | None) -> bool:
    """True when the compact's sentinel holds exactly ``sha256``."""
    if sha256 is None:
        return False
    try:
        with open(_sentinel_path(compact), "rb") as f:
            return f.read(len(sha256) + 1) == sha256.encode()
    except OSError:
        return False


def _matches_sidecar(compact: Path, manifest: compaction.Manifest) -> bool:
    """True when the compact on disk has the sidecar's size and sha256; hashes only once the size agrees."""
    try:
        return compact.stat().st_size == manifest.size and _sha256(compact) == manifest.sha256
    except OSError:
        return False


def _write_sentinel(compact: Path, sha256: str) -> None:
    """Record that the object store holds the compact with ``sha256``: a tmp file renamed over the sentinel."""
    sentinel = _sentinel_path(compact)
    tmp = sentinel.with_name(f".{sentinel.name}-{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(sha256)
        os.replace(tmp, sentinel)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _past_compact_retention(directory: Path, store: compaction.Store, now: float) -> bool:
    """True when the store keeps compacts for a limited time and the partition's day ended more than that long ago."""
    days = store.retention_days()
    day = compaction.day_of_dir(str(directory))
    if days <= 0 or day is None:
        return False
    try:
        after = day + timedelta(days=1)
    except OverflowError:
        return False
    return datetime(after.year, after.month, after.day, tzinfo=UTC).timestamp() < now - days * 86400


def _delete_compact(compact: Path, root_dir: Path) -> bool:
    """Delete a local compact, keeping its sentinel; whether it did.

    Under the compaction lock, so no sweep rewrites the compact meanwhile, and
    only while its partition has no raw part left (that keeps the day readable
    from local disk and stops superseded parts from piling up) and the file, a
    fresh read of the sidecar and the sentinel all name the same sha256: the
    object store then holds exactly the bytes deleted. The partition is
    listed before the compact is hashed, so a compact kept for its parts costs
    no hashing, cycle after cycle.
    """
    with compaction.lock(str(root_dir)):
        if compaction.raw_part_names(str(compact.parent)):
            return False
        manifest = compaction.read_sidecar(str(compact.parent))
        if manifest is None or not _sentinel_holds(compact, manifest.sha256):
            return False
        if not _matches_sidecar(compact, manifest):
            return False
        compact.unlink()
        return True


def _offload_compact(
    compact: Path,
    r2_key: str,
    sidecar_key: str,
    root_dir: Path,
    store: compaction.Store,
    partition: _Partition,
    stats: dict,
    now: float,
    use_r2: bool,
    r2_upload: Callable[..., bool],
) -> None:
    """Upload a compact the object store does not hold yet; past the store's retention, delete it locally once its raw parts are gone."""
    manifest = partition.manifest
    if use_r2 and not partition.compact_uploaded and stats["uploaded"] < _MAX_UPLOAD_PER_CYCLE:
        if manifest is None or manifest.sha256 is None or not _matches_sidecar(compact, manifest):
            # The next sweep rewrites a sidecar that disagrees with its compact.
            logger.warning("Archive lifecycle: %s does not match its sidecar; not uploading it this cycle", compact)
            stats["skipped"] += 1
        elif r2_upload(r2_key, str(compact), content_type=compaction.PARQUET_CONTENT_TYPE) and r2_upload(
            sidecar_key, str(compact.with_name(compaction.SIDECAR_NAME)), content_type="application/json"
        ):
            try:
                _write_sentinel(compact, manifest.sha256)
                partition.compact_uploaded = True
            except OSError:
                logger.warning("Could not write sentinel %s", _sentinel_path(compact))
            stats["uploaded"] += 1
        else:
            stats["errors"] += 1

    if partition.compact_uploaded and _past_compact_retention(compact.parent, store, now):
        try:
            if _delete_compact(compact, root_dir):
                stats["deleted"] += 1
            else:
                stats["skipped"] += 1
        except OSError:
            stats["errors"] += 1


def run_archive_lifecycle() -> dict:
    """Synchronous — run in a thread executor.

    Returns summary dict: {uploaded, deleted, errors, skipped, compacted}.
    """
    from services.r2_client import is_enabled as r2_enabled
    from services.r2_client import upload_file as r2_upload

    stats = {"uploaded": 0, "deleted": 0, "errors": 0, "skipped": 0, "compacted": 0}

    # Phase 0: compact the complete days of every compacted store, on every host.
    stores: dict[str, compaction.Store] = {}
    for root_dir, prefix in _archive_roots():
        store = compaction.store_for_prefix(prefix)
        if store is None:
            continue
        stores[prefix] = store
        try:
            stats["compacted"] += compaction.sweep(store, str(root_dir))["compacted"]
        except Exception:
            logger.exception("Archive lifecycle: compacting %s failed", root_dir)

    now = time.time()
    offload_cutoff = now - (ARCHIVE_OFFLOAD_AGE_DAYS * 86400)
    # ARCHIVE_RETENTION_DAYS <= 0 disables local deletion (R2 keeps forever).
    deletion_enabled = ARCHIVE_RETENTION_DAYS > 0
    delete_cutoff = now - (ARCHIVE_RETENTION_DAYS * 86400) if deletion_enabled else 0.0

    use_r2 = r2_enabled()
    partitions = _Partitions()

    for json_file, root_dir, r2_prefix in _iter_archive_files():
        if stats["uploaded"] + stats["deleted"] >= _MAX_DELETE_PER_CYCLE:
            stats["skipped"] += 1
            continue

        try:
            mtime = json_file.stat().st_mtime
        except OSError:
            continue

        rel = json_file.relative_to(root_dir)
        r2_key = f"{r2_prefix}/{rel}"
        store = stores.get(r2_prefix)
        if store is not None and json_file.name == compaction.COMPACT_NAME:
            sidecar_key = f"{r2_prefix}/{rel.with_name(compaction.SIDECAR_NAME)}"
            partition = partitions[json_file.parent]
            _offload_compact(json_file, r2_key, sidecar_key, root_dir, store, partition, stats, now, use_r2, r2_upload)
            continue
        sentinel = _sentinel_path(json_file)
        already_uploaded = sentinel.exists()

        # Phase 1: Upload to R2 if old enough and not yet uploaded
        if use_r2 and mtime < offload_cutoff and not already_uploaded and stats["uploaded"] < _MAX_UPLOAD_PER_CYCLE:
            if r2_upload(r2_key, str(json_file)):
                # Mark as uploaded — local deletion is only allowed once this
                # sentinel exists, so an upload failure can never cascade into
                # data loss on the next cycle.
                try:
                    sentinel.touch()
                    already_uploaded = True
                except OSError:
                    logger.warning("Could not write sentinel %s", sentinel)
                stats["uploaded"] += 1
            else:
                stats["errors"] += 1

        # Phase 2: Delete from local disk if past retention AND confirmed in R2,
        # and, in a compacted store, once the part's rows are also in a safe compact.
        if (
            deletion_enabled
            and mtime < delete_cutoff
            and already_uploaded
            and (store is None or partitions[json_file.parent].keeps_rows_of(json_file))
        ):
            try:
                json_file.unlink()
                # Drop the sentinel too so empty-dir pruning can collapse the tree.
                try:
                    sentinel.unlink(missing_ok=True)
                except OSError:
                    pass
                stats["deleted"] += 1
            except OSError:
                stats["errors"] += 1

    # Phase 3: Clean up empty directories (bottom-up) in every root.
    for root_dir, _prefix in _archive_roots():
        _prune_empty_dirs(root_dir)

    if stats["uploaded"] or stats["deleted"] or stats["compacted"]:
        logger.info(
            "Archive lifecycle: uploaded=%d deleted=%d errors=%d skipped=%d compacted=%d",
            stats["uploaded"],
            stats["deleted"],
            stats["errors"],
            stats["skipped"],
            stats["compacted"],
        )

    return stats


def _iter_archive_files():
    """Yield (file, root_dir, r2_prefix) for every archived file, oldest-ish first.

    Sweeps all archive roots:
      - archive/  detection JSON/Parquet under year/month/day/node/
      - tracks/   solver Parquet under Hive year=…/month=…/day=… partitions
      - adsb/     ADS-B snapshot Parquet, same Hive partitions as tracks/
    The tracks/ and adsb/ roots are globbed recursively because their layout
    (and any future partition scheme) differs from the detection archive's
    fixed depth.
    """
    # Detection archive: fixed year/month/day/node tree, walked in order.
    if _ARCHIVE_DIR.exists():
        try:
            for year_dir in sorted(_ARCHIVE_DIR.iterdir()):
                if not year_dir.is_dir():
                    continue
                for month_dir in sorted(year_dir.iterdir()):
                    if not month_dir.is_dir():
                        continue
                    for day_dir in sorted(month_dir.iterdir()):
                        if not day_dir.is_dir():
                            continue
                        for node_dir in sorted(day_dir.iterdir()):
                            if not node_dir.is_dir():
                                continue
                            files = [
                                *node_dir.glob("*.parquet"),
                                *node_dir.glob("*.json"),
                            ]
                            for f in sorted(files):
                                yield f, _ARCHIVE_DIR, "archive"
        except OSError:
            logger.debug("Error iterating archive directory", exc_info=True)

    # Track-solver and ADS-B snapshot Parquet: recursive glob, skip our own
    # .uploaded sentinels.
    for root, prefix in ((_TRACKS_DIR, "tracks"), (_ADSB_DIR, "adsb")):
        if root.exists():
            try:
                for f in sorted(root.glob("**/*.parquet")):
                    yield f, root, prefix
            except OSError:
                logger.debug("Error iterating %s directory", prefix, exc_info=True)


def _prune_empty_dirs(base: Path):
    """Remove empty leaf directories bottom-up."""
    try:
        for dirpath, dirnames, filenames in os.walk(str(base), topdown=False):
            if dirpath == str(base):
                continue
            if not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    except OSError:
        pass
