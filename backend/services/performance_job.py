"""The daily node performance run: yesterday's archive in, every node's published files out.

``run_daily`` reads the archive one node at a time and keeps what it learns in small
files under ``data_dir``:

    performance/days/<YYYY-MM-DD>/<node_id>.json   a node's count table for one UTC day
    performance/class_table.json                   the fleet's class offsets, refitted monthly
    performance/last_run.json                      written last: how a run that reached it ended
    tower_match.json                               services/tower_match.py
    performance_<node_id>.json                     the node's figures for the trailing window
    performance_<node_id>.owner.json               the same from the whole days of the owner's
                                                   tenure, where it starts inside the window

A day table with opportunities is final: it is never read again, which keeps a run to
about one day of reading, and it outlives the detection files it came from.  A table
without any is a quiet day or a day whose ADS-B couldn't be read, and the two look the
same, so each run reads that day again while its detection rows last, and only a table
with opportunities takes its place.  A later run can only add to what is known, never
turn a real day into zeros.

Every file is swapped in whole, so a killed run leaves each one as it found it and
the next run fills in what is missing.

A window is whole days on one tower.  ``fit_node`` takes one tower and the figures
carry its power, so a day that saw another tower, or two, stays out of the node's
file, and the fleet's fit takes a node's days as one entry per tower for the same
reason.

This runs in a child process, away from the frame threads (scripts/node_performance.py
--daily, started by services/tasks/performance_refresh.py).  So it imports neither
``main`` nor ``core.state``, reads the registry once, read-only, and writes no SQLite.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from retina_analytics.empirical_coverage import FOV_CLOSED_EPSILON_KM, N_BINS, EmpiricalCoverageState

from config.aircraft_classes import class_for_type
from config.constants import (
    PERFORMANCE_LAST_RUN_FILE,
    PERFORMANCE_REFIT_INTERVAL_DAYS,
    PERFORMANCE_TABLES_KEPT_DAYS,
    PERFORMANCE_WINDOW_DAYS,
)
from services import aircraft_db, tower_match
from services import performance_data as pdata
from services import performance_fit as pfit
from services.node_kind import is_synthetic_node

logger = logging.getLogger(__name__)

_DAYS_DIR = "performance/days"
_CLASS_TABLE = "performance/class_table.json"
# As services/performance_store reads them back: an id's file name has to give the id again.
_PUBLISHED_RE = re.compile(r"performance_(.+?)(\.owner)?\.json")
_PLAIN_FILE_NAME_RE = re.compile(r"[^./\\\0][^/\\\0]*")
_FOV_BIN_DEG = 360.0 / N_BINS

Configs = list[dict[str, Any]]
Tables = Mapping[date, Mapping[str, Any]]


def _moment(value: str | datetime) -> datetime:
    """A stored timestamp, aware: SQLite keeps UTC's wall clock with no offset (core/timestamps.py)."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _days_before(today: date, count: int) -> list[date]:
    """The ``count`` days ending yesterday, oldest first; today is still being written."""
    return [today - timedelta(days=back) for back in range(count, 0, -1)]


def _write_json(path: Path, doc: Mapping[str, Any]) -> None:
    """``doc`` at ``path``, swapped in whole.  A process killed here leaves its dotted tmp for the sweep."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ── Registry ─────────────────────────────────────────────────────────────────


def _tenure(claim: Mapping[str, Any]) -> datetime | None:
    """When the node's owner took it: core.auth.tenure_start's rule, on the row as stored.

    Not that function: core.users won't import without the app's JWT secret, and
    this module is also loaded by the offline script, which has none.
    """
    theirs = claim["owned_since"] is not None and claim["owned_since_user_id"] == claim["user_id"]
    start = claim["owned_since"] if theirs else claim["updated_at"]
    return None if start is None else _moment(start)


def _read_registry(path: str | Path) -> tuple[dict[str, Configs], dict[str, datetime | None]]:
    """Every config row of each real node, oldest version first, and when each owned node's owner took it."""
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM node_configs ORDER BY node_id, version").fetchall()
        claims = connection.execute(
            "SELECT node_id, user_id, owned_since, owned_since_user_id, updated_at "
            "FROM node_claims WHERE user_id IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()
    nodes: dict[str, Configs] = {}
    for row in rows:
        node_id = row["node_id"]
        if is_synthetic_node(node_id):
            continue
        if _PLAIN_FILE_NAME_RE.fullmatch(node_id) is None or node_id.endswith(".owner"):
            logger.warning("performance job: skipping node id %r, it can't name a file", node_id)
            continue
        nodes.setdefault(node_id, []).append(dict(row))
    return nodes, {claim["node_id"]: _tenure(claim) for claim in claims}


def _towers_on(configs: Configs, day: date) -> set[str]:
    """The tower of each config row in force during ``day``: the one active as it starts, and any created in it."""
    start, end = _midnight(day), _midnight(day + timedelta(days=1))
    ordered = sorted(configs, key=lambda row: _moment(row["created_at"]))
    active = [row for row in ordered if _moment(row["created_at"]) <= start][-1:] or ordered[:1]
    created = [row for row in ordered if start < _moment(row["created_at"]) < end]
    return {tower_match.tower_key(row) for row in (*active, *created)}


def _tower_document(
    nodes: Mapping[str, Configs],
    first_day: date,
    today: date,
    *,
    url: str,
    path: Path,
    session: httpx.Client | None,
) -> dict:
    """The stored tower match, refreshed once a day.

    A window can end on a row superseded since, so those rows are looked up too.  A
    failed read reads as never refreshed, and ``refresh`` raises on it, which stops
    the run before any node's file is written against an empty match.
    """
    doc = tower_match.load(path)
    if (doc["refreshed_at"] or "").startswith(today.isoformat()):
        return doc
    start = _midnight(first_day)
    rows = [
        row
        for configs in nodes.values()
        for row in configs
        if row["superseded_at"] is None or _moment(row["superseded_at"]) >= start
    ]
    return tower_match.refresh(rows, url=url, path=path, session=session)


# ── Day tables ───────────────────────────────────────────────────────────────


def _table_path(data_dir: Path, day: date, node_id: str) -> Path:
    return data_dir / _DAYS_DIR / day.isoformat() / f"{node_id}.json"


def _day_tables(data_dir: Path, node_id: str, days: Iterable[date]) -> dict[date, dict[str, Any]]:
    """The node's tables among ``days``, keyed by day, so a merge can't pass one twice."""
    tables = {}
    for day in days:
        try:
            with open(_table_path(data_dir, day, node_id), encoding="utf-8") as f:
                tables[day] = json.load(f)
        except FileNotFoundError:
            continue
    return tables


def _learned_fov(data_dir: Path, node_id: str) -> Callable[[np.ndarray, np.ndarray], np.ndarray] | None:
    """The node's learned field of view as ``in_fov(bearing_deg, range_km)``; None leaves the declared wedge.

    What the app's analytics manager last saved, read here and never written. Every
    bin shares the limit at its centre, so this is ``EmpiricalCoverageState.contains``
    in one lookup, floor included.
    """
    path = data_dir / f"empirical_{node_id}.json"
    try:
        learned = EmpiricalCoverageState.load_from_file(str(path))
        centres = (np.arange(N_BINS) + 0.5) * _FOV_BIN_DEG
        limits_km = np.maximum([learned.limit_km(float(centre)) for centre in centres], FOV_CLOSED_EPSILON_KM)
    except FileNotFoundError:
        logger.info("performance job: %s has no learned field of view, using the declared one", node_id)
        return None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        logger.warning(
            "performance job: %s's learned field of view won't load, using the declared one: %r", node_id, exc
        )
        return None

    def in_fov(bearing_deg: np.ndarray, range_km: np.ndarray) -> np.ndarray:
        bins = (np.asarray(bearing_deg) % 360.0 / _FOV_BIN_DEG).astype(np.int64) % N_BINS
        return np.asarray(range_km) <= limits_km[bins]

    return in_fov


def _gate_threshold_db(tables: Tables, day: date, hist: Mapping[str, int]) -> float | None:
    """T_n for ``day``'s claim gate: its ``hist`` plus the other days' histograms, so a day read again counts once."""
    merged = dict(hist)
    for other, table in tables.items():
        if other != day:
            for key, count in table["snr_hist"].items():
                merged[key] = merged.get(key, 0) + count
    return pfit.threshold_db(merged)


def _add_day_tables(
    node_id: str,
    configs: Configs,
    days: Sequence[date],
    data_dir: Path,
    archive_root: str | Path,
    adsb_root: str | Path,
    types_for: Callable[[Sequence[str]], list[str | None]],
) -> None:
    """Write the tables ``days`` lack and read again the ones without opportunities, oldest first.

    No SNR rows means the files are gone or the node was silent: a day with no table
    gets none and a table already there stays.  Rows with no opportunity among them
    are a quiet day, which is how a node comes to read ``no_opportunities``, or ADS-B
    that couldn't be read, so that table is written, and it stands until a later read
    finds opportunities.  A read that lost only some of the ADS-B files has them, so
    its table is final like any other.
    """
    tables = _day_tables(data_dir, node_id, days)
    in_fov = _learned_fov(data_dir, node_id)
    for day in days:
        stored = tables.get(day)
        if stored is not None and stored["opportunities"]:
            continue
        hist = pdata.snr_histogram(node_id, day, archive_root=archive_root)
        if not hist:
            continue
        table = pdata.node_day_table(
            node_id,
            day,
            configs=configs,
            archive_root=archive_root,
            adsb_root=adsb_root,
            types_for=types_for,
            class_for=class_for_type,
            opportunities="aggregator",
            threshold_db=_gate_threshold_db(tables, day, hist),
            in_fov=in_fov,
        )
        if table is not None and (stored is None or table["opportunities"]):
            _write_json(_table_path(data_dir, day, node_id), table)
            tables[day] = table


def _types_for() -> Callable[[Sequence[str]], list[str | None]]:
    database = aircraft_db.current()
    if database is not None:
        return database.types_for
    logger.warning("performance job: no aircraft database, a row's own type is all there is")
    return lambda hexes: [None] * len(hexes)


# ── Class table ──────────────────────────────────────────────────────────────


def _tower_runs(configs: Configs, tables: Tables) -> list[list[date]]:
    """``tables``' days, oldest first, in runs on one tower; a day on two towers belongs to none."""
    runs: list[list[date]] = []
    current = None
    for day in sorted(tables):
        towers = _towers_on(configs, day)
        tower = towers.pop() if len(towers) == 1 else None
        if tower is not None:
            if tower != current:
                runs.append([])
            runs[-1].append(day)
        current = tower
    return runs


def _read_class_table(path: Path) -> dict[str, Any] | None:
    """The stored table, None when there is none or it can't be used.  A failed read raises and stops the run."""
    try:
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
        date.fromisoformat(table["fitted_on"])
        if isinstance(table["class_offsets_db"], dict):
            return table
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        pass
    logger.warning("performance job: %s can't be used, fitting a new one", path)
    return None


def _class_offsets(
    each_node: Callable[[Callable[[str, Configs], None]], None], data_dir: Path, today: date
) -> Mapping[str, float]:
    """The class offsets for this run's fits, refitted when there is no table or it has aged out.

    A failed refit keeps what there was, and with nothing written the next run tries
    again.  Each tower run is an entry of its own, so ``fit_fleet`` can't blend two
    towers' powers into one ``a``.
    """
    path = data_dir / _CLASS_TABLE
    stored = _read_class_table(path)
    if stored is not None:
        age_days = (today - date.fromisoformat(stored["fitted_on"])).days
        if age_days < PERFORMANCE_REFIT_INTERVAL_DAYS:
            return stored["class_offsets_db"]

    runs: dict[str, dict[str, Any]] = {}

    def add_runs(node_id: str, configs: Configs) -> None:
        tables = _day_tables(data_dir, node_id, _days_before(today, PERFORMANCE_REFIT_INTERVAL_DAYS))
        for run in _tower_runs(configs, tables):
            merged = pdata.merge_tables(tables[day] for day in run)
            if pfit.fit_node(merged)["rank_status"] != "config_suspect":
                runs[f"{node_id}@{run[0]}"] = merged

    each_node(add_runs)
    fitted = pfit.fit_fleet(runs)
    if fitted is None:
        logger.info("performance job: nothing anchors a class table yet")
        return pfit.DEFAULT_CLASS_OFFSETS_DB if stored is None else stored["class_offsets_db"]
    _write_json(path, {**fitted, "fitted_on": today.isoformat()})
    return fitted["class_offsets_db"]


# ── Published files ──────────────────────────────────────────────────────────


def _window_days(configs: Configs, tables: Tables) -> list[date]:
    """The days of the node's file, oldest first: whole days on its last day's tower, back to the first that isn't.

    Empty when the last day itself saw two towers: the next run has a whole day.
    """
    days = sorted(tables, reverse=True)
    end_tower = {tower_match.tower_key(pdata.config_on(configs, days[0]))}
    window: list[date] = []
    for day in days:
        if _towers_on(configs, day) != end_tower:
            break
        window.insert(0, day)
    return window


def _owner_days(window: Sequence[date], tenure: datetime | None) -> list[date]:
    """The whole days of the window that are the owner's, where those are some of it but not all."""
    if tenure is None:
        return []
    held = [day for day in window if _midnight(day) >= tenure]
    return held if len(held) < len(window) else []


def _publish(
    node_id: str,
    configs: Configs,
    tables: Tables,
    data_dir: Path,
    towers: dict,
    class_offsets: Mapping[str, float],
    tenure: datetime | None,
) -> None:
    window = _window_days(configs, tables)
    if not window:
        logger.info("performance job: %s changed tower on its last day, its files stand", node_id)
        return
    end_config = pdata.config_on(configs, window[-1])

    def fitted(days: Iterable[date]) -> dict[str, Any]:
        return pfit.fit_node(
            pdata.merge_tables(tables[day] for day in days),
            class_offsets=class_offsets,
            tower=tower_match.lookup(towers, end_config),
            grid=pdata.ring_grid(end_config),
        )

    _write_json(data_dir / f"performance_{node_id}.json", fitted(window))
    owner_path = data_dir / f"performance_{node_id}.owner.json"
    owner_days = _owner_days(window, tenure)
    if owner_days:
        _write_json(owner_path, fitted(owner_days))
    else:
        owner_path.unlink(missing_ok=True)


def _sweep(data_dir: Path, keep: set[str], today: date) -> None:
    """Remove the files of every node outside ``keep``, tmp files a killed run left, and tables past their term."""
    for path in data_dir.glob("performance_*.json"):
        found = _PUBLISHED_RE.fullmatch(path.name)
        if found is not None and found[1] not in keep:
            logger.info("performance job: removing %s, its node has nothing in the window", path.name)
            path.unlink(missing_ok=True)
    days_dir = data_dir / _DAYS_DIR
    for path in (
        *data_dir.glob(".performance_*.tmp"),
        *data_dir.glob(".tower_match.json.*.tmp"),
        *days_dir.parent.glob(".*.tmp"),
        *days_dir.glob("*/.*.tmp"),
    ):
        path.unlink(missing_ok=True)
    for folder in days_dir.glob("*"):
        try:
            age_days = (today - date.fromisoformat(folder.name)).days
        except ValueError:
            continue
        if age_days > PERFORMANCE_TABLES_KEPT_DAYS:
            shutil.rmtree(folder, ignore_errors=True)


def run_daily(
    *,
    archive_root: str | Path,
    adsb_root: str | Path,
    registry: str | Path,
    data_dir: str | Path,
    tower_url: str,
    today: date | None = None,
    session: httpx.Client | None = None,
) -> dict[str, Any]:
    """One run, to the marker it writes last and returns: when it ended, nodes seen, nodes that raised.

    A node that raises is logged and counted, keeps the files it has, and the run
    goes on.  Anything else that raises stops the run short of its marker, and so
    does the end of a run where a node raised and no other came through with a table:
    the marker is what tells the task a run went well, so it would stamp health over
    a run that published nothing.  Nothing is swept then, so the last good files stand.
    Nodes that are only silent raise nothing, and a run of them ends at its marker.
    """
    today = today or datetime.now(UTC).date()
    data_dir = Path(data_dir)
    days = _days_before(today, PERFORMANCE_WINDOW_DAYS)
    nodes, tenures = _read_registry(registry)
    towers = _tower_document(nodes, days[0], today, url=tower_url, path=data_dir / "tower_match.json", session=session)
    failed: set[str] = set()

    def each_node(step: Callable[[str, Configs], None]) -> None:
        for node_id, configs in nodes.items():
            if node_id not in failed:
                try:
                    step(node_id, configs)
                except Exception:
                    logger.exception("performance job: node %s failed, its files stand", node_id)
                    failed.add(node_id)

    types_for = _types_for()
    each_node(
        lambda node_id, configs: _add_day_tables(node_id, configs, days, data_dir, archive_root, adsb_root, types_for)
    )
    class_offsets = _class_offsets(each_node, data_dir, today)
    with_tables: set[str] = set()

    def publish(node_id: str, configs: Configs) -> None:
        tables = _day_tables(data_dir, node_id, days)
        if tables:
            with_tables.add(node_id)
            _publish(node_id, configs, tables, data_dir, towers, class_offsets, tenures.get(node_id))

    each_node(publish)
    if failed:
        logger.warning("performance job: %d of %d nodes failed, their files stand", len(failed), len(nodes))
        if with_tables <= failed:
            raise RuntimeError("performance job: a node failed and no other came through")
    _sweep(data_dir, with_tables | failed, today)
    marker = {"finished_at": datetime.now(UTC).isoformat(), "nodes": len(nodes), "failed": len(failed)}
    _write_json(data_dir / PERFORMANCE_LAST_RUN_FILE, marker)
    return marker
