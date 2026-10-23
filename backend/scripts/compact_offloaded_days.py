"""Compact the archive days whose raw parts now live only in object storage.

It works on one compacted store per run: tracks by default, or the ADS-B
snapshot store with --store adsb. The hourly sweep (services/compaction.py)
compacts the raw parts it finds on local disk. On a host with an object store,
the archive lifecycle had already uploaded and deleted the raw parts of every
day older than ARCHIVE_RETENTION_DAYS before compaction shipped: those days
have no compact, and a day that was only partly local at that moment got a
compact of its local parts alone. This command finishes them from the store, day by day:

- a day with no local compact gets one built from the store's raw parts (and
  the store's compact, if it has one), uploaded as compact.parquet and then
  compact.json beside the parts; then the local compact.json is written, so the
  server serves the day through its read-through cache (services/cold_store.py);
- a day whose local compact lacks some of the store's raw parts gets them
  merged in, and its local compact is replaced; the lifecycle uploads it on
  its next cycle;
- a day whose compact already lists every raw part in the store is done; if
  only its local compact.json is missing, it is restored from the store's copy.

Run it from backend/ with the server's environment (its object-store settings,
and what the app needs to import when --root is left out), dry run first:

    .venv/bin/python -m scripts.compact_offloaded_days --dry-run
    .venv/bin/python -m scripts.compact_offloaded_days [--from YYYY-MM-DD] [--to YYYY-MM-DD]
    .venv/bin/python -m scripts.compact_offloaded_days --sidecars-only
    .venv/bin/python -m scripts.compact_offloaded_days --store adsb --dry-run

On a deployed host, in the server's container, which has both:
``docker compose exec -w /app/backend server python3 -m
scripts.compact_offloaded_days --dry-run``. Each day prints one line, its
outcome (done, restored, compacted, skipped, failed) with its compact's parts,
rows and bytes; the exit status is 0 when no day failed, 1 when one did or when
the store lists no day at all (an empty store, or a listing that failed, which
cold_store logs and reports as empty), and 2 when no object store is configured.

It never deletes or overwrites a raw part, in the store or locally, and
deletes no local file but its own tmp files. It never touches a day that has
local raw parts the sweep has not compacted yet, or a day that is not complete.
It never uploads a local compact (the lifecycle does), and it never marks a
part superseded before the compact holding its rows is uploaded or installed.

Idempotent: a done day costs no download and no upload, so a second run only
prints "done", and a run cut short is finished by the next one. A raw part that
cannot be read is listed as unreadable, and retried only when its day is
compacted again.

Safe while the server runs: a local compact is installed, and a local
compact.json written, under the lock the hourly sweep takes, and only if the
day's local files are still what this command read before compacting;
otherwise the day fails with "changed while compacting, run again". Raw parts
are downloaded into a fresh directory per day under --work-dir, which must be
outside the store root, and which is always removed.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date

from services import cold_store, compaction
from services.compaction import COMPACT_NAME, SIDECAR_NAME, TMP_PREFIX, CompactionError, Manifest, Store

logger = logging.getLogger("compact_offloaded_days")

# A key after the store's prefix: year=YYYY/month=MM/day=DD/<name>.
_KEY = re.compile(r"year=([0-9]{4})/month=([0-9]{2})/day=([0-9]{2})/([^/]+)")
# A raw part's name, as the writers publish it and compaction.raw_part_names lists it.
_PART = re.compile(r"part-[^/\0]*\.parquet")
_HASH_CHUNK = 1 << 20
_OUTCOMES = ("compacted", "restored", "done", "would compact", "would restore", "skipped", "failed")


class _DayFailed(Exception):
    """The day is left as it was locally; the message says why."""


@dataclass(frozen=True)
class Outcome:
    """What became of one day; ``manifest`` is its compact's, when it has one."""

    status: str
    why: str = ""
    manifest: Manifest | None = None
    # The compact's bytes, when the manifest (a footer's) does not carry them.
    size: int | None = None


@dataclass(frozen=True)
class _Local:
    """A day partition's local files, as read before anything is downloaded."""

    # The local compact's footer manifest and file size; None when there is no local compact.
    compact: Manifest | None
    compact_size: int | None
    # The local compact.json; None when there is none.
    sidecar: Manifest | None
    # Local raw parts the local manifest lists neither as compacted nor as unreadable.
    unlisted: tuple[str, ...]


@dataclass(frozen=True)
class _Run:
    store: Store
    root: str
    work_dir: str
    jobs: int
    dry_run: bool
    sidecars_only: bool
    now_ms: int


# ── The store's days ─────────────────────────────────────────────────────────


def remote_days(store: Store) -> dict[date, set[str]]:
    """The object names in each day partition of the store; keys of any other shape are ignored."""
    prefix = f"{store.prefix}/"
    days: dict[date, set[str]] = {}
    for key in cold_store.list_keys(prefix):
        match = _KEY.fullmatch(key.removeprefix(prefix)) if key.startswith(prefix) else None
        if match is None:
            continue
        try:
            day = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        days.setdefault(day, set()).add(match[4])
    return days


def _checked(store: Store, day: date, manifest: Manifest | None, what: str) -> Manifest:
    if manifest is None:
        raise _DayFailed(f"{what} carries no manifest")
    if (manifest.store, manifest.day) != (store.name, day.isoformat()):
        raise _DayFailed(f"{what} is a manifest of {manifest.store} {manifest.day}")
    return manifest


def _read_local(store: Store, day: date, directory: str) -> _Local:
    compact_path = os.path.join(directory, COMPACT_NAME)
    footer = size = None
    if os.path.lexists(compact_path):
        try:
            footer = compaction.read_footer_manifest(compact_path)
            size = os.stat(compact_path).st_size
        except OSError as exc:
            raise _DayFailed(f"the local {COMPACT_NAME} cannot be read: {exc}") from exc
        footer = _checked(store, day, footer, f"the local {COMPACT_NAME}")
    sidecar = compaction.read_sidecar(directory)
    if footer is None:
        if sidecar is None and os.path.lexists(os.path.join(directory, SIDECAR_NAME)):
            raise _DayFailed(f"the local {SIDECAR_NAME} cannot be read")
        if sidecar is not None:
            _checked(store, day, sidecar, f"the local {SIDECAR_NAME}")
    listed = footer if footer is not None else sidecar
    known = set(listed.parts) | set(listed.unreadable) if listed is not None else set()
    unlisted = tuple(name for name in compaction.raw_part_names(directory) if name not in known)
    return _Local(footer, size, sidecar, unlisted)


def _stored_sidecar(store: Store, day: date) -> Manifest:
    key = compaction.object_key(store, day, SIDECAR_NAME)
    data = cold_store.download_bytes(key)
    if data is None:
        raise _DayFailed(f"could not download {key}")
    try:
        manifest = Manifest.from_json(data)
    except ValueError as exc:
        raise _DayFailed(f"{key} is malformed: {exc}") from exc
    if manifest.size is None:
        raise _DayFailed(f"{key} carries no size and sha256")
    return _checked(store, day, manifest, key)


# ── One day ──────────────────────────────────────────────────────────────────


def process_day(run: _Run, day: date, names: set[str]) -> Outcome:
    """Bring one day up to date with the store; _DayFailed when it cannot be."""
    store = run.store
    if not compaction.is_complete(store, day, run.now_ms):
        return Outcome("skipped", "the day is not complete yet")
    directory = compaction.day_dir(run.root, day)
    local = _read_local(store, day, directory)
    if local.unlisted:
        return Outcome("skipped", f"pending the hourly compaction of {len(local.unlisted)} local raw parts")

    manifest = local.compact if local.compact is not None else local.sidecar
    from_store = manifest is None and SIDECAR_NAME in names
    if from_store:
        manifest = _stored_sidecar(store, day)
    if manifest is not None and local.compact is None and COMPACT_NAME not in names:
        raise _DayFailed(f"its {SIDECAR_NAME} describes a {COMPACT_NAME} the store does not hold")
    parts = sorted(name for name in names if _PART.fullmatch(name))

    if manifest is not None and set(parts) <= set(manifest.parts) | set(manifest.unreadable):
        if not from_store:
            return Outcome("done", manifest=manifest, size=local.compact_size)
        if run.dry_run:
            return Outcome("would restore", manifest=manifest)
        _restore_sidecar(run.root, directory, manifest)
        return Outcome("restored", manifest=manifest)

    if run.sidecars_only:
        return Outcome("skipped", "it needs compacting, which --sidecars-only leaves", manifest, local.compact_size)
    if manifest is None and not parts:
        if COMPACT_NAME in names:
            raise _DayFailed(f"the store holds its {COMPACT_NAME} but no {SIDECAR_NAME} and no raw part")
        return Outcome("skipped", "the store holds no raw part of it")
    if run.dry_run:
        missing = [name for name in parts if manifest is None or name not in manifest.parts]
        return Outcome("would compact", f"raw parts to download: {len(missing)}", manifest, local.compact_size)
    return Outcome("compacted", manifest=_compact(run, day, directory, local, manifest, parts))


def _compact(
    run: _Run, day: date, directory: str, local: _Local, manifest: Manifest | None, parts: list[str]
) -> Manifest:
    """Build the day's compact from its previous one and the store's raw parts it lacks, then install or upload it."""
    store = run.store
    work = tempfile.mkdtemp(prefix=f"{store.name}-{day.isoformat()}-", dir=run.work_dir)
    try:
        previous = None
        holds = manifest
        if local.compact is not None:
            previous = os.path.join(directory, COMPACT_NAME)
        elif manifest is not None:
            previous = os.path.join(work, "previous.parquet")
            key = compaction.object_key(store, day, COMPACT_NAME)
            if not cold_store.download_to(key, previous):
                raise _DayFailed(f"could not download {key}")
            holds = _stored_compact_manifest(store, day, previous, manifest)
        missing = [name for name in parts if holds is None or name not in holds.parts]
        sources = _download(store, day, missing, os.path.join(work, "parts"), run.jobs)
        built_path = os.path.join(work, f"{TMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            built = compaction.build(store, day, sources, built_path, previous=previous)
        except CompactionError as exc:
            raise _DayFailed(str(exc)) from exc
        if local.compact is not None:
            _install(run.root, directory, built_path, built, local.compact)
        else:
            _upload(store, run.root, day, directory, built_path, built, local.sidecar)
        return built
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _stored_compact_manifest(store: Store, day: date, path: str, described: Manifest) -> Manifest:
    """The footer manifest of the store's compact, downloaded to ``path``: what it really holds."""
    try:
        footer = compaction.read_footer_manifest(path)
    except OSError as exc:
        raise _DayFailed(f"the store's {COMPACT_NAME} cannot be read: {exc}") from exc
    footer = _checked(store, day, footer, f"the store's {COMPACT_NAME}")
    if footer != replace(described, size=None, sha256=None):
        # An upload cut short between the compact and its compact.json; the
        # compact's own footer says which parts it holds.
        logger.warning(
            "%s %s: %s does not describe the stored compact; merging by its footer", store.name, day, SIDECAR_NAME
        )
    return footer


def _download(store: Store, day: date, names: list[str], directory: str, jobs: int) -> list[str]:
    """Download raw parts into ``directory`` under their own names, ``jobs`` at a time; their paths."""
    os.makedirs(directory, exist_ok=True)
    paths = [os.path.join(directory, name) for name in names]
    keys = [compaction.object_key(store, day, name) for name in names]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        stored = list(pool.map(cold_store.download_to, keys, paths))
    failed = [name for name, ok in zip(names, stored, strict=True) if not ok]
    if failed:
        raise _DayFailed(f"could not download {len(failed)} of {len(names)} raw parts, {failed[0]} first")
    return paths


def _install(root: str, directory: str, built_path: str, built: Manifest, seen: Manifest) -> None:
    """Replace the local compact with the built one, under the compaction lock, if it is still the one ``seen``."""
    with compaction.lock(root):
        try:
            current = compaction.read_footer_manifest(os.path.join(directory, COMPACT_NAME))
        except OSError:
            current = None
        if current != seen:
            raise _DayFailed("the local compact changed while compacting, run again")
        # Copied into the partition first, so the rename stays on one filesystem.
        tmp = os.path.join(directory, f"{TMP_PREFIX}{uuid.uuid4().hex}.tmp")
        try:
            with open(built_path, "rb") as source, open(tmp, "xb") as target:
                shutil.copyfileobj(source, target, _HASH_CHUNK)
                target.flush()
                os.fsync(target.fileno())
            if _sha256(tmp) != built.sha256:
                raise _DayFailed(f"the copy of the built compact in {directory} is not what was built")
            compaction.install(directory, tmp, built)
        except CompactionError as exc:
            raise _DayFailed(str(exc)) from exc
        finally:
            _unlink_quietly(tmp)


def _upload(
    store: Store, root: str, day: date, directory: str, built_path: str, built: Manifest, seen: Manifest | None
) -> None:
    """Upload the compact, then its compact.json, then write the local compact.json under the compaction lock."""
    key = compaction.object_key(store, day, COMPACT_NAME)
    if not cold_store.upload_file(key, built_path, compaction.PARQUET_CONTENT_TYPE):
        raise _DayFailed(f"could not upload {key}; nothing written locally")
    key = compaction.object_key(store, day, SIDECAR_NAME)
    if not cold_store.upload_bytes(key, built.to_json(), "application/json"):
        raise _DayFailed(f"could not upload {key}; nothing written locally")
    with compaction.lock(root):
        sidecar_path = os.path.join(directory, SIDECAR_NAME)
        unchanged = not os.path.lexists(os.path.join(directory, COMPACT_NAME)) and (
            compaction.read_sidecar(directory) == seen if seen is not None else not os.path.lexists(sidecar_path)
        )
        if not unchanged:
            raise _DayFailed("the local day changed while compacting, run again")
        os.makedirs(directory, exist_ok=True)
        compaction.write_sidecar(directory, built)


def _restore_sidecar(root: str, directory: str, manifest: Manifest) -> None:
    """Write the store's compact.json locally, under the compaction lock, if the day still has no local manifest."""
    with compaction.lock(root):
        if any(os.path.lexists(os.path.join(directory, name)) for name in (COMPACT_NAME, SIDECAR_NAME)):
            raise _DayFailed("the local day changed while restoring, run again")
        os.makedirs(directory, exist_ok=True)
        compaction.write_sidecar(directory, manifest)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("cannot remove %s", path, exc_info=True)


# ── The command ──────────────────────────────────────────────────────────────


def _line(day: date, outcome: Outcome) -> str:
    status = f"{outcome.status}: {outcome.why}" if outcome.why else outcome.status
    manifest = outcome.manifest
    if manifest is None:
        return f"{day.isoformat()}  {status}  parts=- rows=- bytes=-"
    size = outcome.size if outcome.size is not None else manifest.size
    line = f"{day.isoformat()}  {status}  parts={len(manifest.parts)} rows={manifest.rows} bytes={size}"
    if manifest.unreadable:
        line += f"  unreadable={','.join(manifest.unreadable)}"
    return line


def _iso_day(text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is None or day.isoformat() != text:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date")
    return day


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Compact the archive days whose raw parts live only in object storage.",
        epilog="See the module docstring of scripts/compact_offloaded_days.py for what it never does.",
    )
    ap.add_argument(
        "--store",
        choices=[store.name for store in compaction.STORES],
        default=compaction.TRACKS.name,
        help=f"the archive store to compact (default: {compaction.TRACKS.name})",
    )
    ap.add_argument("--root", help="the store's local root (default: the one the server writes)")
    ap.add_argument("--from", dest="first", type=_iso_day, metavar="YYYY-MM-DD", help="the first day to process")
    ap.add_argument("--to", dest="last", type=_iso_day, metavar="YYYY-MM-DD", help="the last day to process")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; change nothing, locally or in the store")
    ap.add_argument(
        "--sidecars-only",
        action="store_true",
        help="only restore missing local compact.json files from the store; download no raw part, upload nothing",
    )
    ap.add_argument(
        "--work-dir",
        help="where each day's raw parts are downloaded, outside the store root (default: the system temp dir)",
    )
    ap.add_argument("--jobs", type=_positive, default=8, help="parallel downloads per day (default: 8)")
    args = ap.parse_args(argv)

    store = compaction.store_named(args.store)
    assert store is not None
    if args.first is not None and args.last is not None and args.first > args.last:
        ap.error("--from is after --to")
    if not cold_store.enabled():
        logger.error("No object store is configured; there is nothing to compact from.")
        return 2
    try:
        # The store's default root is the app's: finding it imports the app's
        # task modules, which need the server's environment.
        root = os.path.abspath(args.root or store.root())
    except Exception as exc:
        ap.error(f"cannot find the {store.name} root without the server's environment ({exc}); pass --root")
    work_dir = os.path.abspath(args.work_dir or tempfile.gettempdir())
    if not os.path.isdir(work_dir):
        ap.error(f"--work-dir {work_dir} is not a directory")
    if _inside(work_dir, root):
        ap.error(f"--work-dir {work_dir} is inside the store root {root}")

    days = remote_days(store)
    if not days:
        print(
            f"the store listed no {store.name} days under {store.prefix}/ (an empty store or a failed listing; see the log)",
            flush=True,
        )
        return 1
    selected = sorted(
        day for day in days if (args.first is None or day >= args.first) and (args.last is None or day <= args.last)
    )
    logger.info("%s: %d days in the store, %d selected; local root %s", store.name, len(days), len(selected), root)
    run = _Run(store, root, work_dir, args.jobs, args.dry_run, args.sidecars_only, int(time.time() * 1000))
    counts: Counter[str] = Counter()
    for day in selected:
        try:
            outcome = process_day(run, day, days[day])
        except _DayFailed as exc:
            logger.warning("%s %s failed: %s", store.name, day, exc)
            outcome = Outcome("failed", str(exc))
        except Exception as exc:
            logger.exception("%s %s failed", store.name, day)
            outcome = Outcome("failed", f"{type(exc).__name__}: {exc}")
        counts[outcome.status] += 1
        print(_line(day, outcome), flush=True)
    tally = ", ".join(f"{counts[status]} {status}" for status in _OUTCOMES if counts[status] or status == "failed")
    print(f"{store.name}: {len(selected)} days{' (dry run)' if args.dry_run else ''}: {tally}", flush=True)
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
