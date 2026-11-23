"""The rolling evaluation: windows, state, incremental runs and aggregates.

Runs under ``python -m scripts.perf_eval`` (deploy/perf-eval/run_eval.sh,
every three hours). Everything it keeps lives under one private root:

    truth/                 the recorder's files (deploy/perf-eval/truth_recorder.py)
    registry/users.db      a copy of the node registry, out of WAL mode
    registry/events.json   a copy of the server's events log
    state/aircraft_db.npz  the aircraft type database (services/aircraft_db.py)
    state/fov/<node>/<stamp>.json
                           dated copies of each node's learned field of view
    state/windows/<start>/<node>.buckets.parquet, .encounters.parquet, .summary.json
                           one window's results per node (services/performance_truth.py)
    state/windows/<start>/WINDOW.json
                           the window's verdict: nodes scored, failed, inputs seen
    state/ledger.json      every window processed, with what and when
    out/                   latest.json, history.json, nodes/<node>.json, index.html,
                           fov/<node>.json + fov/<node>.bins.parquet (P85 reach per tier and altitude)

**Windows** are three hours, UTC-aligned. A window is complete once its end is
ARCHIVE_FLUSH_INTERVAL_S + LATE_DATA_ALLOWANCE_MS behind the clock: the
detection archive flushes hourly and the recorder's files close on the hour.
Each run scores every complete window since the evaluation started that the
ledger does not hold, and scores again the RECONCILE_WINDOWS most recent ones
it does hold, so a late flush is picked up; any older window is scored again
only when its inputs (the files that cover it, by size) have changed, or on
``--force``. Scoring a window writes each node's three files to a temporary
name and renames them into place, and the window's record last, so a run that
dies leaves either the old results or the new, never a mix, and a rerun gives
the same files: nothing is appended, nothing counted twice.

**Failure.** A node that raises is recorded as failed in WINDOW.json and the
ledger with the error, its old files (if any) are left as they were, the other
nodes' results are written, and the run exits non-zero. The aggregate marks
the window partial. A window is never marked complete with a node half-done.

**Field of view.** The server rewrites ``empirical_<node>.json`` in place, so
each run copies any changed file to state/fov with the time it was copied, and
a window is gated with the newest copy taken before the window started: the
field of view as it was learned from earlier data only. A window older than
every copy (the first runs, or a backfill) is gated with the oldest copy and
says so (``fov_after_window``), since that copy may have learned from the
window itself.

**Aggregates** add counts across windows (exact), and bootstrap over the
encounters of those windows (services/performance_truth.bootstrap_rates), for
the latest complete window, the last 24 hours, the last 7 days and everything
since the evaluation started. A node with no truth or no activity in a span
shows that status, not a rate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import shutil
import sqlite3
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from retina_analytics.empirical_coverage import FOV_CLOSED_EPSILON_KM, N_BINS, EmpiricalCoverageState

from config.aircraft_classes import class_for_type
from config.constants import ARCHIVE_FLUSH_INTERVAL_S
from services import aircraft_db
from services import performance_truth as pt

logger = logging.getLogger(__name__)

WINDOW_MS = pt.WINDOW_MS
LATE_DATA_ALLOWANCE_MS = 15 * 60_000
RECONCILE_WINDOWS = 1
DEFAULT_WORKERS = 3
# The per-bucket tables (the largest files, about 20 MB a window for the fleet)
# are kept this long for diagnosis; summaries and encounters are kept for good.
BUCKET_RETENTION_DAYS = 30
NODE_RECENT_DAYS = 7
SPANS_H = {"latest": 3, "day": 24, "week": 24 * 7}
_FOV_BIN_DEG = 360.0 / N_BINS


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def truth(self) -> Path:
        return self.root / "truth"

    @property
    def registry(self) -> Path:
        return self.root / "registry" / "users.db"

    @property
    def events(self) -> Path:
        return self.root / "registry" / "events.json"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def aircraft_db(self) -> Path:
        return self.state / "aircraft_db.npz"

    @property
    def fov(self) -> Path:
        return self.state / "fov"

    @property
    def windows(self) -> Path:
        return self.state / "windows"

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.json"

    @property
    def out(self) -> Path:
        return self.root / "out"


# ── Time ─────────────────────────────────────────────────────────────────────


def window_start(t_ms: int) -> int:
    return (t_ms // WINDOW_MS) * WINDOW_MS


def window_label(start_ms: int) -> str:
    return datetime.fromtimestamp(start_ms / 1000.0, tz=UTC).strftime("%Y-%m-%dT%H%M")


def parse_label(label: str) -> int:
    return int(datetime.strptime(label, "%Y-%m-%dT%H%M").replace(tzinfo=UTC).timestamp() * 1000)


def latest_complete_end(now_ms: int) -> int:
    """The end of the latest window whose archive flush has had time to land."""
    return window_start(now_ms - ARCHIVE_FLUSH_INTERVAL_S * 1000 - LATE_DATA_ALLOWANCE_MS)


def iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000.0, tz=UTC).isoformat(timespec="seconds")


# ── Files ────────────────────────────────────────────────────────────────────


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True, default=_json_default)
    os.replace(tmp, path)


def _json_default(value: Any) -> Any:
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


# ── Registry ─────────────────────────────────────────────────────────────────


def registry_configs(registry: Path) -> dict[str, list[dict[str, Any]]]:
    """Every node's config rows, oldest first, keyed by node id."""
    con = sqlite3.connect(f"{registry.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        cursor = con.execute("SELECT * FROM node_configs ORDER BY node_id, version")
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
        seen = dict(con.execute("SELECT node_id, last_seen_at FROM nodes").fetchall())
    finally:
        con.close()
    out: dict[str, list[dict[str, Any]]] = {}
    for config in rows:
        config["_last_seen_at"] = seen.get(config["node_id"])
        out.setdefault(config["node_id"], []).append(config)
    return out


def candidate_nodes(configs: Mapping[str, Sequence[Mapping[str, Any]]], window_from_ms: int) -> list[str]:
    """Real nodes with a placed config, seen by the server within NODE_RECENT_DAYS of the window."""
    cutoff = datetime.fromtimestamp(window_from_ms / 1000.0, tz=UTC) - timedelta(days=NODE_RECENT_DAYS)
    out = []
    for node_id, rows in configs.items():
        if node_id.startswith("synth") or not rows:
            continue
        if not any(pt.Sites([row]).versions[0].site is not None for row in rows):
            continue
        seen = rows[-1].get("_last_seen_at")
        if seen:
            try:
                seen_at = datetime.fromisoformat(str(seen)).replace(tzinfo=UTC)
                if seen_at < cutoff:
                    continue
            except ValueError:
                pass
        out.append(node_id)
    return sorted(out)


# ── Field of view snapshots ──────────────────────────────────────────────────


def snapshot_fov(fov_dir: Path, paths: Paths, nodes: Iterable[str], now_ms: int) -> None:
    """Copy each node's learned field of view into state/fov when it has changed."""
    for node_id in nodes:
        source = fov_dir / f"empirical_{node_id}.json"
        if not source.exists():
            continue
        try:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        except OSError:
            continue
        target_dir = paths.fov / node_id
        target_dir.mkdir(parents=True, exist_ok=True)
        if any(p.name.endswith(f"-{digest}.json") for p in target_dir.glob("*.json")):
            continue
        shutil.copyfile(source, target_dir / f"{now_ms}-{digest}.json")


def fov_gate(paths: Paths, node_id: str, window_from_ms: int) -> tuple[Callable | None, dict[str, Any]]:
    """The learned-FOV gate for a window from the newest copy saved before it, and how it was chosen."""
    target_dir = paths.fov / node_id
    copies = []
    for p in target_dir.glob("*.json") if target_dir.exists() else []:
        try:
            copies.append((int(p.name.split("-")[0]), p))
        except ValueError:
            continue
    if not copies:
        return None, {"fov": "none"}
    copies.sort()
    before = [c for c in copies if c[0] <= window_from_ms]
    stamp, path = before[-1] if before else copies[0]
    try:
        learned = EmpiricalCoverageState.load_from_file(str(path))
        centres = (np.arange(N_BINS) + 0.5) * _FOV_BIN_DEG
        limits_km = np.maximum([learned.limit_km(float(c)) for c in centres], FOV_CLOSED_EPSILON_KM)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        logger.warning("perf eval: %s field of view %s unreadable: %r", node_id, path.name, exc)
        return None, {"fov": "unreadable"}

    def in_fov(bearing_deg: np.ndarray, range_km: np.ndarray) -> np.ndarray:
        bins = (np.asarray(bearing_deg) % 360.0 / _FOV_BIN_DEG).astype(np.int64) % N_BINS
        return np.asarray(range_km) <= limits_km[bins]

    return in_fov, {
        "fov": "snapshot",
        "fov_saved_ms": stamp,
        "fov_after_window": not before,
        "fov_open_bins": int((limits_km > FOV_CLOSED_EPSILON_KM).sum()),
    }


# ── Inputs manifest ──────────────────────────────────────────────────────────


def inputs_manifest(
    archive_root: Path, truth_root: Path, nodes: Sequence[str], from_ms: int, to_ms: int
) -> dict[str, Any]:
    """Sizes of every file that can hold the window, so a changed input is noticed."""
    manifest: dict[str, Any] = {"truth": {}, "detections": {}}
    for path in pt.truth_files(truth_root, from_ms - 120_000, to_ms + 120_000):
        try:
            manifest["truth"][os.path.relpath(path, truth_root)] = os.path.getsize(path)
        except OSError:
            continue
    for node_id in nodes:
        sizes = {}
        for day in pt._days(from_ms - pt.BUCKET_MS, to_ms + pt.BUCKET_MS):
            for path in pt.pdata._detection_files(archive_root, node_id, day):
                try:
                    sizes[os.path.relpath(path, archive_root)] = os.path.getsize(path)
                except OSError:
                    continue
        manifest["detections"][node_id] = sizes
    return manifest


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]


# ── Scoring a window ─────────────────────────────────────────────────────────


@dataclass
class Settings:
    archive_root: Path
    adsb_root: Path
    fov_dir: Path
    paths: Paths
    truth_source: str = "recorder"  # recorder | aggregator | both


def window_dir(paths: Paths, start_ms: int) -> Path:
    return paths.windows / window_label(start_ms)


def load_events(paths: Paths) -> list[dict[str, Any]]:
    events = read_json(paths.events, [])
    return events if isinstance(events, list) else []


_TYPES_CACHE: dict[str, Callable[[Sequence[str]], list[str | None]]] = {}


def _unknown_types(hexes: Sequence[str]) -> list[str | None]:
    return [None] * len(hexes)


def types_for_path(path: Path | None) -> Callable[[Sequence[str]], list[str | None]]:
    """The aircraft database's lookup, loaded once per process; every type unknown without one."""
    key = str(path) if path else ""
    found = _TYPES_CACHE.get(key)
    if found is None:
        found = _unknown_types
        if path is not None:
            try:
                found = aircraft_db.load(path).types_for
            except (OSError, ValueError, aircraft_db.AircraftDbError) as exc:
                logger.warning("perf eval: no aircraft database (%s); every type is unknown", exc)
        _TYPES_CACHE[key] = found
    return found


def score_node(task: Mapping[str, Any]) -> dict[str, Any]:
    """Score one node over one window and write its three files; returns the window record's entry for it.

    Runs in the parent or in a worker process (score_one_window), so it takes
    plain data: the settings, the node's config rows, the truth already cut to
    the node's regions, the events that name the node, the clock, and the
    aircraft database's path. A failure is returned, never raised: one node's
    error must not stop the others, and its old files are left as they were.
    """
    settings: Settings = task["settings"]
    node_id: str = task["node_id"]
    start_ms: int = task["start_ms"]
    end_ms = start_ms + WINDOW_MS
    paths = settings.paths
    directory = window_dir(paths, start_ms)
    try:
        node_truth: pt.TruthRows = task["truth"]
        if settings.truth_source in ("aggregator", "both"):
            aggregator = pt.truth_from_adsb_store(
                settings.adsb_root, node_id, start_ms - pt.TRUTH_MAX_GAP_MS, end_ms + pt.TRUTH_MAX_GAP_MS
            )
            node_truth = pt.merge_truth([node_truth, aggregator]) if settings.truth_source == "both" else aggregator
        sites = pt.Sites(task["configs"])
        detections = pt.load_detections(settings.archive_root, node_id, start_ms - pt.BUCKET_MS, end_ms + pt.BUCKET_MS)
        gate, fov_info = fov_gate(paths, node_id, start_ms)
        inputs = pt.WindowInputs(
            node_id=node_id,
            from_ms=start_ms,
            to_ms=end_ms,
            sites=sites,
            truth=node_truth,
            detections=detections,
            offline=pt.offline_spans(task["events"], node_id, end_ms),
            in_learned_fov=gate,
            types_for=types_for_path(task.get("aircraft_db")),
            class_for=class_for_type,
        )
        result = pt.score_window(inputs)
        summary = result.summary
        summary["fov"] = fov_info
        summary["legacy_node_rate"] = pt.legacy_node_rate(
            settings.adsb_root, node_id, sites, detections, start_ms, end_ms
        )
        summary["processed_ms"] = task["now_ms"]
        summary["provisional"] = bool(task.get("provisional"))
        summary["config_versions"] = sorted(
            {int(v) for v in result.buckets["config_version"].to_pylist() if v is not None and v >= 0}
        )
        write_parquet(directory / f"{node_id}.buckets.parquet", result.buckets)
        write_parquet(directory / f"{node_id}.encounters.parquet", result.encounters)
        write_json(directory / f"{node_id}.summary.json", summary)
        return {
            "status": summary["status"]["code"],
            "flags": summary["status"]["flags"],
            "opportunities": summary["gates"]["geometric"]["n"],
        }
    except Exception as exc:  # noqa: BLE001 - one node's failure must not stop the others
        logger.exception("perf eval: %s failed on %s", node_id, window_label(start_ms))
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-2000:],
        }


def _events_for(events: Sequence[Mapping[str, Any]], node_id: str) -> list[Mapping[str, Any]]:
    return [e for e in events if (e.get("meta") or {}).get("node_id") == node_id]


def score_one_window(
    settings: Settings,
    start_ms: int,
    nodes: Sequence[str],
    configs: Mapping[str, Sequence[Mapping[str, Any]]],
    events: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    now_ms: int,
    *,
    workers: int = 1,
    aircraft_db_path: Path | None = None,
    provisional: bool = False,
) -> dict[str, Any]:
    """Score every node over one window, nodes in parallel across ``workers`` processes; returns the window record.

    The truth is read once and cut per node before it is handed to a worker, so
    each process carries only its node's fixes. With ``provisional`` the window
    has not closed (or its archive flush has not landed): the record says so, and
    the ledger keeps it due until it is scored complete.
    """
    end_ms = start_ms + WINDOW_MS
    started = time.monotonic()
    paths = settings.paths
    directory = window_dir(paths, start_ms)
    directory.mkdir(parents=True, exist_ok=True)
    truth = pt.TruthRows.empty()
    if settings.truth_source in ("recorder", "both"):
        # The control reads the trajectories up to the largest shift later, so the truth span covers it.
        reach = max(pt.BACKGROUND_SHIFTS_MS, default=0)
        truth = pt.load_truth(paths.truth, start_ms - pt.TRUTH_MAX_GAP_MS, end_ms + reach + pt.TRUTH_MAX_GAP_MS)
    record: dict[str, Any] = {
        "window": window_label(start_ms),
        "from_ms": start_ms,
        "to_ms": end_ms,
        "processed_ms": now_ms,
        "provisional": provisional,
        "inputs_digest": manifest_digest(manifest),
        "truth_source": settings.truth_source,
        "truth_regions": {name: int(times.size) for name, times in truth.polls.items()},
        "workers": max(1, min(workers, len(nodes))),
        "nodes": {},
    }
    tasks = [
        {
            "settings": settings,
            "start_ms": start_ms,
            "node_id": node_id,
            "configs": list(configs[node_id]),
            "truth": truth.for_node(node_id) if truth.regions else truth,
            "events": _events_for(events, node_id),
            "now_ms": now_ms,
            "aircraft_db": aircraft_db_path,
            "provisional": provisional,
        }
        for node_id in nodes
    ]
    if record["workers"] > 1:
        # Spawned, not forked: the parent already runs Arrow's and BLAS's threads, and a fork
        # of a threaded process can deadlock in the child.
        with ProcessPoolExecutor(
            max_workers=record["workers"], mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            entries = list(pool.map(score_node, tasks))
    else:
        entries = [score_node(task) for task in tasks]
    record["nodes"] = dict(zip(nodes, entries, strict=True))
    failed = [node_id for node_id, entry in record["nodes"].items() if entry["status"] == "failed"]
    record["failed"] = failed
    record["status"] = "partial" if failed else ("provisional" if provisional else "ok")
    record["duration_s"] = round(time.monotonic() - started, 1)
    write_json(directory / "WINDOW.json", record)
    return record


# ── The ledger and a run ─────────────────────────────────────────────────────


def load_ledger(paths: Paths) -> dict[str, Any]:
    ledger = read_json(paths.ledger, None)
    if not isinstance(ledger, dict) or "windows" not in ledger:
        ledger = {"schema": 1, "windows": {}, "evaluation_start_ms": None}
    return ledger


def windows_due(
    ledger: Mapping[str, Any],
    manifests: Callable[[int], Mapping[str, Any]],
    first_ms: int,
    last_end_ms: int,
    *,
    force: bool = False,
    only: Sequence[int] | None = None,
) -> list[tuple[int, str]]:
    """Window starts to score this run, each with why: new, complete, retry, reconcile, changed or forced."""
    due = []
    starts = list(range(window_start(first_ms), last_end_ms, WINDOW_MS)) if last_end_ms > first_ms else []
    if only is not None:
        starts = [s for s in only if s < last_end_ms or force]
    recent = set(starts[-RECONCILE_WINDOWS:]) if only is None else set()
    held = ledger.get("windows", {})
    for start in starts:
        label = window_label(start)
        entry = held.get(label)
        if force or entry is None:
            due.append((start, "forced" if force and entry is not None else "new"))
        elif entry.get("status") == "provisional":
            due.append((start, "complete"))
        elif entry.get("status") != "ok":
            due.append((start, "retry"))
        elif start in recent:
            due.append((start, "reconcile"))
        elif entry.get("inputs_digest") != manifest_digest(manifests(start)):
            due.append((start, "changed"))
    return due


def run(
    settings: Settings,
    *,
    now_ms: int | None = None,
    nodes: Sequence[str] | None = None,
    backfill_from_ms: int | None = None,
    only_windows: Sequence[int] | None = None,
    force: bool = False,
    max_windows: int = 16,
    aggregate: bool = True,
    workers: int = 1,
    provisional: bool = True,
) -> dict[str, Any]:
    """One run: score what is due, the open window provisionally, record it, aggregate and render.

    Returns the run report. ``workers`` processes score a window's nodes in
    parallel. With ``provisional`` every window that has started but is not
    complete yet (the open one, and the one before it until its archive flush
    has had time to land) is scored on what has arrived and recorded as
    provisional, so the dashboard shows the last hours within the hour; the
    window is scored again, complete, once it closes.
    """
    now_ms = now_ms or int(time.time() * 1000)
    paths = settings.paths
    paths.state.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger(paths)
    if ledger.get("evaluation_start_ms") is None:
        ledger["evaluation_start_ms"] = backfill_from_ms or window_start(now_ms)
    if backfill_from_ms is not None and backfill_from_ms < ledger["evaluation_start_ms"]:
        ledger["evaluation_start_ms"] = window_start(backfill_from_ms)
    first_ms = ledger["evaluation_start_ms"]
    last_end = latest_complete_end(now_ms)

    configs = registry_configs(paths.registry)
    events = load_events(paths)
    aircraft_db_path = paths.aircraft_db if paths.aircraft_db.exists() else None
    if aircraft_db_path is None:
        logger.warning("perf eval: no aircraft database at %s; every type is unknown", paths.aircraft_db)

    def nodes_for(start: int) -> list[str]:
        chosen = candidate_nodes(configs, start)
        if nodes:
            chosen = [n for n in chosen if n in set(nodes)] + [n for n in nodes if n in configs and n not in chosen]
        return sorted(set(chosen))

    def manifest_for(start: int) -> dict[str, Any]:
        return inputs_manifest(settings.archive_root, paths.truth, nodes_for(start), start, start + WINDOW_MS)

    snapshot_fov(settings.fov_dir, paths, nodes_for(window_start(now_ms)), now_ms)
    due = windows_due(ledger, manifest_for, first_ms, last_end, force=force, only=only_windows)
    if len(due) > max_windows:
        # Oldest first, so a backfill advances; the recent ones are reconciled next run.
        due = due[:max_windows]
    if provisional and only_windows is None:
        # Every window that has started and is not complete: the open one, and the one before it
        # while its last archive flush is still pending.
        first_open = max(last_end, window_start(first_ms))
        due += [
            (start, "provisional") for start in range(first_open, window_start(now_ms) + 1, WINDOW_MS) if start < now_ms
        ]
    report: dict[str, Any] = {"now_ms": now_ms, "latest_complete_end_ms": last_end, "scored": [], "failed": []}
    for start, reason in due:
        chosen = nodes_for(start)
        manifest = manifest_for(start)
        complete = start + WINDOW_MS <= last_end
        logger.info("perf eval: scoring %s (%s) for %d node(s)", window_label(start), reason, len(chosen))
        record = score_one_window(
            settings,
            start,
            chosen,
            configs,
            events,
            manifest,
            now_ms,
            workers=workers,
            aircraft_db_path=aircraft_db_path,
            provisional=not complete,
        )
        ledger["windows"][record["window"]] = {
            "status": record["status"],
            "processed_ms": now_ms,
            "inputs_digest": record["inputs_digest"],
            "reason": reason,
            "nodes": {n: v["status"] for n, v in record["nodes"].items()},
            "failed": record["failed"],
            "duration_s": record["duration_s"],
        }
        write_json(paths.ledger, ledger)
        report["scored"].append(
            {"window": record["window"], "reason": reason, "status": record["status"], "nodes": record["nodes"]}
        )
        if record["failed"]:
            report["failed"].append({"window": record["window"], "nodes": record["failed"]})
    ledger["last_run_ms"] = now_ms
    ledger["last_run"] = {"scored": [s["window"] for s in report["scored"]], "failed": report["failed"]}
    write_json(paths.ledger, ledger)
    report["pruned_bucket_files"] = prune_bucket_tables(paths, now_ms)
    if aggregate:
        report["aggregate"] = build_outputs(paths, now_ms, configs)
    return report


def prune_bucket_tables(paths: Paths, now_ms: int, keep_days: int = BUCKET_RETENTION_DAYS) -> int:
    """Delete the per-bucket tables of windows older than keep_days; summaries and encounters stay."""
    cutoff = now_ms - keep_days * 86_400_000
    removed = 0
    if not paths.windows.exists():
        return 0
    for directory in paths.windows.iterdir():
        try:
            start = parse_label(directory.name)
        except ValueError:
            continue
        if start >= cutoff:
            continue
        for path in directory.glob("*.buckets.parquet"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


# ── Aggregates ───────────────────────────────────────────────────────────────


def _add_counts(into: dict[str, Any], cell: Mapping[str, Any]) -> None:
    for key, value in cell.items():
        if isinstance(value, int | float) and not isinstance(value, bool):
            into[key] = into.get(key, 0) + value


def _merge_cells(
    into: dict[str, dict[str, dict[str, Any]]], cells: Mapping[str, Mapping[str, Mapping[str, Any]]]
) -> None:
    for dim, section in cells.items():
        target = into.setdefault(dim, {})
        for label, cell in section.items():
            _add_counts(target.setdefault(label, {}), cell)


def _summary_paths(paths: Paths, node_id: str, starts: Iterable[int]) -> list[tuple[int, Path]]:
    out = []
    for start in starts:
        p = window_dir(paths, start) / f"{node_id}.summary.json"
        if p.exists():
            out.append((start, p))
    return out


def ok_windows(ledger: Mapping[str, Any], *, provisional: bool = False) -> list[int]:
    """Starts of every window the ledger holds complete (ok or partial), or, with ``provisional``, those scored provisionally."""
    wanted = ("provisional",) if provisional else ("ok", "partial")
    starts = []
    for label, entry in ledger.get("windows", {}).items():
        if entry.get("status") in wanted:
            try:
                starts.append(parse_label(label))
            except ValueError:
                continue
    return sorted(starts)


def _figure(cell: Mapping[str, Any], numerator: str) -> float | None:
    value = pt.rate(cell, numerator)
    return None if value is None else round(value, 4)


def _rates(cell: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "n": cell.get("n", 0),
        "encounters": cell.get("encounters", 0),
        "raw_pm1": _figure(cell, "raw_pm1"),
        "raw": _figure(cell, "raw"),
        "validated": _figure(cell, "validated"),
        "validated_tagged": _figure(cell, "validated_tagged"),
        "matched_any": _figure(cell, "matched_any"),
        "ambiguous": _figure(cell, "ambiguous"),
        "refuted_share": (round(cell["refuted"] / cell["raw"], 4) if cell.get("raw") else None),
        "background": _figure(cell, "background"),
        "net": _figure(cell, "net"),
        "snr_n": cell.get("snr_n", 0),
        "snr_mean_db": (round(cell["snr_sum_db"] / cell["snr_n"], 2) if cell.get("snr_n") else None),
    }


def aggregate_node(paths: Paths, node_id: str, starts: Sequence[int]) -> dict[str, Any] | None:
    """Exact sums over the windows' summaries and an encounter bootstrap over their encounter tables."""
    found = _summary_paths(paths, node_id, starts)
    if not found:
        return None
    gates: dict[str, dict[str, Any]] = {g: {} for g in pt.GATES}
    cells: dict[str, dict[str, dict[str, Any]]] = {g: {} for g in pt.GATES}
    statuses: dict[str, int] = {}
    flags: dict[str, int] = {}
    diag_sum: dict[str, Any] = {}
    legacy = {"n": 0, "detected": 0}
    windows_in = 0
    truth_share, active_share = [], []
    for _, path in found:
        summary = read_json(path)
        if not summary:
            continue
        windows_in += 1
        for g in pt.GATES:
            _add_counts(gates[g], summary["gates"][g])
            _merge_cells(cells[g], summary["cells"][g])
        code = summary["status"]["code"]
        statuses[code] = statuses.get(code, 0) + 1
        for f in summary["status"]["flags"]:
            flags[f] = flags.get(f, 0) + 1
        truth_share.append(summary["status"]["truth_share"])
        active_share.append(summary["status"]["active_share"])
        for key in (
            "buckets",
            "active_buckets",
            "truth_buckets",
            "frames",
            "detections",
            "tagged_detections",
            "tags_in_truth",
            "tags_fitting_truth",
            "tags_unknown_to_truth",
            "matched_detections",
            "ambiguous_detections",
            "truth_fixes",
            "truth_polls",
        ):
            diag_sum[key] = diag_sum.get(key, 0) + summary["diagnostics"].get(key, 0)
        hist = summary["diagnostics"].get("snr_hist")  # absent from windows scored before SNR was kept
        if hist:
            merged = diag_sum.setdefault("snr_hist", {"edges_db": hist["edges_db"]})
            for key in ("all", "matched", "tagged"):
                merged[key] = [a + b for a, b in zip(merged.get(key) or [0] * len(hist[key]), hist[key], strict=True)]
        leg = summary.get("legacy_node_rate") or {}
        legacy["n"] += leg.get("n", 0)
        legacy["detected"] += leg.get("detected", 0)
    tables = [
        pq.read_table(window_dir(paths, s) / f"{node_id}.encounters.parquet")
        for s, _ in found
        if (window_dir(paths, s) / f"{node_id}.encounters.parquet").exists()
    ]
    encounters = pa.concat_tables([t for t in tables if t.num_rows]) if any(t.num_rows for t in tables) else None
    boot = pt.bootstrap_from_encounters(encounters) if encounters is not None else {}
    latency = pt.latency_summary(encounters) if encounters is not None else {}
    if "snr_hist" in diag_sum:
        hist = diag_sum["snr_hist"]
        diag_sum["snr_median_db"] = {
            key: pt.hist_quantile(hist["edges_db"], hist[key], 0.5) for key in ("all", "matched", "tagged")
        }
    geo = gates["geometric"]
    n = geo.get("n", 0)
    enc = boot.get("validated", {}) or {}
    status = status_for_span(statuses, n, enc.get("encounters", 0), windows_in)
    return {
        "node_id": node_id,
        "windows": windows_in,
        "status": status,
        "flags": flags,
        "statuses": statuses,
        "truth_share": round(float(np.mean(truth_share)), 3) if truth_share else None,
        "active_share": round(float(np.mean(active_share)), 3) if active_share else None,
        "gates": {g: _rates(gates[g]) | {"aircraft": gates[g].get("aircraft", 0)} for g in pt.GATES},
        "bootstrap": boot,
        "latency_s": latency,
        "cells": {
            g: {dim: {label: _rates(cell) for label, cell in section.items()} for dim, section in cells[g].items()}
            for g in pt.GATES
        },
        "diagnostics": diag_sum,
        "legacy_node_rate": legacy | {"pd": (round(legacy["detected"] / legacy["n"], 4) if legacy["n"] else None)},
    }


def status_for_span(statuses: Mapping[str, int], n: int, encounters: int, windows: int) -> str:
    if not windows:
        return "no_data"
    if statuses.get("no_truth", 0) == windows:
        return "no_truth"
    if statuses.get("node_inactive", 0) + statuses.get("no_truth", 0) == windows:
        return "node_inactive"
    if n < pt.MIN_OPPORTUNITIES or encounters < pt.MIN_ENCOUNTERS:
        return "insufficient"
    return "ok"


def history_rows(paths: Paths, node_id: str, starts: Sequence[int]) -> list[dict[str, Any]]:
    """One compact row per window for the trend views."""
    rows = []
    for start, path in _summary_paths(paths, node_id, starts):
        summary = read_json(path)
        if not summary:
            continue
        geo = summary["gates"]["geometric"]
        boot = summary.get("bootstrap") or {}
        val = boot.get("validated") or {}
        classes = summary["cells"]["geometric"].get("class", {})
        rows.append(
            {
                "window": window_label(start),
                "from_ms": start,
                "status": summary["status"]["code"],
                "flags": summary["status"]["flags"],
                "truth_share": summary["status"]["truth_share"],
                "active_share": summary["status"]["active_share"],
                "n": geo["n"],
                "encounters": geo["encounters"],
                "aircraft": geo["aircraft"],
                "raw_pm1": _figure(geo, "raw_pm1"),
                "raw": _figure(geo, "raw"),
                "validated": _figure(geo, "validated"),
                "validated_lo": val.get("lo"),
                "validated_hi": val.get("hi"),
                "background": _figure(geo, "background"),
                "net": _figure(geo, "net"),
                "refuted_share": (round(geo["refuted"] / geo["raw"], 4) if geo.get("raw") else None),
                "declared_n": summary["gates"]["declared"]["n"],
                "empirical_n": summary["gates"]["empirical"]["n"],
                "empirical_validated": _figure(summary["gates"]["empirical"], "validated"),
                "legacy": (summary.get("legacy_node_rate") or {}).get("pd"),
                "legacy_n": (summary.get("legacy_node_rate") or {}).get("n", 0),
                "class_mix": {label: cell["n"] for label, cell in classes.items()},
                "delay_residual_mad_us": (summary["diagnostics"].get("delay_residual_us") or {}).get("mad"),
                "fov_after_window": (summary.get("fov") or {}).get("fov_after_window"),
            }
        )
    return rows


def build_outputs(
    paths: Paths, now_ms: int, configs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None
) -> dict[str, Any]:
    """Write out/latest.json, out/history.json, out/nodes/<node>.json, out/fov/<node>.* and out/index.html."""
    from services import performance_truth_fov as fov
    from services import performance_truth_html as html

    ledger = load_ledger(paths)
    starts = ok_windows(ledger)
    provisional = ok_windows(ledger, provisional=True)
    paths.out.mkdir(parents=True, exist_ok=True)
    (paths.out / "nodes").mkdir(exist_ok=True)
    (paths.out / "fov").mkdir(exist_ok=True)
    node_ids: set[str] = set()
    for entry in ledger.get("windows", {}).values():
        node_ids.update(entry.get("nodes", {}).keys())
    latest_end = (starts[-1] + WINDOW_MS) if starts else None
    # The day, week and cumulative spans hold complete windows only; the latest span is the
    # newest window scored, provisional when the open one has been.
    spans: dict[str, list[int]] = {"cumulative": starts}
    for name, hours in SPANS_H.items():
        spans[name] = [s for s in starts if latest_end is not None and s >= latest_end - hours * 3_600_000]
    newest = max(starts + provisional) if (starts or provisional) else None
    if newest is not None:
        spans["latest"] = [newest]
    latest_is_provisional = newest is not None and newest in provisional
    all_starts = sorted(set(starts) | set(provisional))
    callsigns = {}
    if configs:
        for node_id, rows in configs.items():
            callsigns[node_id] = (rows[-1].get("tx_callsign") or "?", rows[-1].get("fc_hz"))
    nodes_out: dict[str, Any] = {}
    history: dict[str, Any] = {}
    fov_docs: dict[str, Any] = {}
    for node_id in sorted(node_ids):
        per_span = {name: aggregate_node(paths, node_id, chosen) for name, chosen in spans.items()}
        if per_span.get("latest"):
            per_span["latest"]["provisional"] = latest_is_provisional
        history[node_id] = history_rows(paths, node_id, all_starts)
        for row in history[node_id]:
            row["provisional"] = row["from_ms"] in provisional
        # The P85 geometries use every bucket table still on hand (complete and provisional
        # windows alike, within BUCKET_RETENTION_DAYS): a geometry wants volume, not recency.
        with_tables = [s for s in all_starts if (window_dir(paths, s) / f"{node_id}.buckets.parquet").exists()]
        fov_doc = fov.node_document(
            node_id,
            [window_dir(paths, s) / f"{node_id}.buckets.parquet" for s in with_tables],
            [window_label(s) for s in with_tables],
            includes_provisional=any(s in provisional for s in with_tables),
            now_ms=now_ms,
        )
        write_json(paths.out / "fov" / f"{node_id}.json", fov_doc)
        write_parquet(paths.out / "fov" / f"{node_id}.bins.parquet", fov.bins_table(node_id, fov_doc["maps"]))
        fov_docs[node_id] = fov_doc
        node_doc = {
            "node_id": node_id,
            "tower": callsigns.get(node_id),
            "spans": per_span,
            "history": history[node_id],
            "fov": fov.overview(fov_doc),
            "generated_ms": now_ms,
        }
        write_json(paths.out / "nodes" / f"{node_id}.json", node_doc)
        nodes_out[node_id] = {
            "tower": callsigns.get(node_id),
            "spans": {k: _overview(v) for k, v in per_span.items()},
            "fov": node_doc["fov"],
        }
    latest = {
        "schema": 1,
        "generated_ms": now_ms,
        "generated": iso(now_ms),
        "evaluation_start_ms": ledger.get("evaluation_start_ms"),
        "latest_window_end_ms": latest_end,
        "latest_window_end": iso(latest_end) if latest_end else None,
        "latest_window_start_ms": newest,
        "latest_window_provisional": latest_is_provisional,
        "provisional_windows": [window_label(s) for s in provisional],
        "windows_processed": len(starts),
        "windows_partial": [label for label, e in ledger.get("windows", {}).items() if e.get("status") == "partial"],
        "last_run": ledger.get("last_run"),
        "spans": {
            name: {"windows": len(chosen), "from_ms": (chosen[0] if chosen else None)} for name, chosen in spans.items()
        },
        "nodes": nodes_out,
        "method": {
            "validate_delay_us": pt.VALIDATE_DELAY_US,
            "validate_doppler_hz": pt.VALIDATE_DOPPLER_HZ,
            "background_shifts_s": [s // 1000 for s in pt.BACKGROUND_SHIFTS_MS],
            "active_window_s": pt.ACTIVE_WINDOW_MS // 1000,
            "truth_max_gap_s": pt.TRUTH_MAX_GAP_MS // 1000,
            "bucket_s": pt.BUCKET_MS // 1000,
            "min_opportunities": pt.MIN_OPPORTUNITIES,
            "min_encounters": pt.MIN_ENCOUNTERS,
        },
    }
    write_json(paths.out / "latest.json", latest)
    write_json(paths.out / "history.json", {"generated_ms": now_ms, "nodes": history})
    page = html.render(latest, {n: read_json(paths.out / "nodes" / f"{n}.json") for n in sorted(node_ids)}, fov_docs)
    tmp = paths.out / "index.html.tmp"
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, paths.out / "index.html")
    return {"nodes": len(node_ids), "windows": len(starts), "latest_window_end": latest["latest_window_end"]}


def _overview(agg: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not agg:
        return None
    geo = agg["gates"]["geometric"]
    boot = agg.get("bootstrap") or {}
    return {
        "status": agg["status"],
        "provisional": bool(agg.get("provisional")),
        "windows": agg["windows"],
        "n": geo["n"],
        "encounters": geo["encounters"],
        "aircraft": geo.get("aircraft", 0),
        "raw_pm1": geo["raw_pm1"],
        "validated": geo["validated"],
        "validated_ci": [(boot.get("validated") or {}).get("lo"), (boot.get("validated") or {}).get("hi")],
        "background": geo["background"],
        "net": geo["net"],
        "refuted_share": geo["refuted_share"],
        "truth_share": agg["truth_share"],
        "active_share": agg["active_share"],
        "flags": agg["flags"],
        "legacy": agg["legacy_node_rate"]["pd"],
    }
