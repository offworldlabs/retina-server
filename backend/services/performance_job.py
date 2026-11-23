"""The daily node performance run: yesterday's archive in, every node's published files out.

``run_daily`` reads the archive one node at a time and keeps what it learns in small
files under ``data_dir``:

    performance/days/<YYYY-MM-DD>/<node_id>.json   a node's count table for one UTC day
    performance/class_table.json                   the fleet's class offsets, refitted monthly
    performance/system_constant.json               the fleet's system constant model, refitted daily
    performance/last_run.json                      written last: how a run that reached it ended
    tower_match.json                               services/tower_match.py
    performance_<node_id>.json                     the node's figures for the trailing window
    performance_<node_id>.owner.json               the same from the whole days of the owner's
                                                   tenure, where it starts inside the window

A day table with opportunities is final: it is never read again, which keeps a run to
about one day of reading, and it outlives the detection files it came from.  A table
without any is a quiet day or a day whose ADS-B couldn't be read, and the two look the
same, so each run reads that day again while it can, and only a table with
opportunities takes its place.  A later run can only add to what is known, never turn
a real day into zeros.

A day is read only while all of its detection files are surely on local disk.  With an
object store the archive lifecycle deletes uploaded files past its retention, and a
table read from what is left would stand as final, so past that point a day keeps what
it has, a table or none.

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

import fcntl
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
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
    performance_settings,
)
from services import aircraft_db, performance_predict, tower_match
from services import performance_data as pdata
from services import performance_fit as pfit
from services.node_kind import is_synthetic_node

logger = logging.getLogger(__name__)

_DAYS_DIR = "performance/days"
_CLASS_TABLE = "performance/class_table.json"
_LOCK_FILE = "performance/.job.lock"
# A tmp file this much older than now is a killed run's; a younger one may be a live writer's.
_STALE_TMP_S = 3600
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
    """``doc`` at ``path``, swapped in whole.

    A process killed here leaves its dotted tmp, which the first sweep an hour or more later removes.
    """
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


def towers_on(configs: Configs, day: date) -> set[str]:
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
    budget_s: float,
) -> dict:
    """The stored tower match, refreshed once a day in at most ``budget_s``.

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
    return tower_match.refresh(rows, url=url, path=path, session=session, budget_s=budget_s)


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

    A day the archive lifecycle may have taken detection files from by now
    (performance_data.detections_may_have_left_disk) is not read: a day with no table
    gets none and a table already there stays.  So does a day with no SNR rows, whose
    files are gone or whose node was silent.  Rows with no opportunity among them
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
        if pdata.detections_may_have_left_disk(day, time.time()):
            logger.info(
                "performance job: %s, %s: its detections may have left local disk, no table is written", node_id, day
            )
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
        towers = towers_on(configs, day)
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
        if towers_on(configs, day) != end_tower:
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
        doc = pfit.fit_node(
            pdata.merge_tables(tables[day] for day in days),
            class_offsets=class_offsets,
            tower=tower_match.tower_for(end_config, towers),
            grid=pdata.ring_grid(end_config),
        )
        # The tower-finder post reads the tower and site this window was measured on from this version.
        return {**doc, "config_version": int(end_config["version"])}

    _write_json(data_dir / f"performance_{node_id}.json", fitted(window))
    owner_path = data_dir / f"performance_{node_id}.owner.json"
    owner_days = _owner_days(window, tenure)
    if owner_days:
        _write_json(owner_path, fitted(owner_days))
    else:
        owner_path.unlink(missing_ok=True)


def _sweep(data_dir: Path, keep: set[str], today: date) -> None:
    """Remove the files of every node outside ``keep``, tmp files a killed run left, and tables past their term.

    A tmp file is a killed run's once it is an hour old by the wall clock; a younger one
    may be a live writer's and stays.
    """
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
        with suppress(FileNotFoundError):
            if time.time() - path.stat().st_mtime > _STALE_TMP_S:
                path.unlink()
    for folder in days_dir.glob("*"):
        try:
            age_days = (today - date.fromisoformat(folder.name)).days
        except ValueError:
            continue
        if age_days > PERFORMANCE_TABLES_KEPT_DAYS:
            shutil.rmtree(folder, ignore_errors=True)


# ── System constant model ────────────────────────────────────────────────────

# tower-finder's classify_band edges in MHz, both ends inclusive: its backend/services/tower_ranking.py
# and backend/config/tower_config.json.
_BANDS_MHZ = (("FM", 87.8, 108.0), ("VHF", 174.0, 216.0), ("UHF", 470.0, 608.0))
_TEXT_TERMS = ("tower_polarisation", "sdr_model", "antenna_model", "antenna_polarisation")
_NUMBER_TERMS = ("gain_reduction_ref_db", "gain_reduction_surv_db", "antenna_gain_dbi", "feedline_loss_db")


def _number(value: Any) -> float | None:
    """``value`` as a float where it is a finite number, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:  # an int no float holds, which a JSON file can carry
        return None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _text(value: Any) -> str | None:
    """A string column stripped, None when nothing is left; its case is the node's."""
    return (value.strip() or None) if isinstance(value, str) else None


def _flag(value: Any) -> str | None:
    """A Boolean column as SQLite gives it back, 0 or 1, as the fit's "false" or "true"."""
    if value not in (0, 1):
        return None
    return "true" if value else "false"


def _band(config: Mapping[str, Any]) -> str | None:
    """The band tower-finder matched the tower in, else the one ``fc_hz`` falls in, as its classify_band reads it."""
    named = _text(config.get("tower_band"))
    if named is not None:
        return named.upper()
    fc_hz = _number(config.get("fc_hz"))
    if fc_hz is not None:
        for band, low_mhz, high_mhz in _BANDS_MHZ:
            if low_mhz <= fc_hz / 1e6 <= high_mhz:
                return band
    return None


def _model_row(node_id: str, doc: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any] | None:
    """The fit's row for a node file, from the config row its window ended on; None unless S_n is fit to enter.

    It enters measured, never "at least", which is censored, on a matched tower's EIRP,
    on the fleet's floor of opportunities, and not suspect: a suspect node's geometry,
    hence its K, is wrong.
    """
    tower = doc.get("tower")
    matched = isinstance(tower, dict) and tower.get("status") == "matched"
    eirp_dbm = _number(tower.get("eirp_dbm")) if matched else None
    system_constant_db = _number(doc.get("system_constant_db"))
    opportunities = _number(doc.get("opportunities"))
    if (
        system_constant_db is None
        or eirp_dbm is None
        or doc.get("system_constant_bound") != "measured"
        or opportunities is None
        or opportunities < pfit.MIN_FLEET_OPPORTUNITIES
        or doc.get("rank_status") == "config_suspect"
    ):
        return None
    fs_hz, cpi_s = _positive(config.get("fs_hz")), _positive(config.get("cpi_s"))
    lna_state = _number(config.get("lna_state"))
    described = {
        "band": _band(config),
        "tower_eirp_dbm": eirp_dbm,
        "tower_antenna_height_m": _positive(config.get("tower_antenna_height_m")),
        "baseline_km": pdata.baseline_km(config),
        "lna_state": None if lna_state is None else str(int(lna_state)),
        **{name: _text(config.get(name)) for name in _TEXT_TERMS},
        **{name: _flag(config.get(name)) for name in performance_predict.FLAG_TERMS},
        **{name: _number(config.get(name)) for name in _NUMBER_TERMS},
    }
    return {
        "node_id": node_id,
        "system_constant_db": system_constant_db,
        "sigma_db": _number(doc.get("system_constant_sigma_db")),
        "threshold_db": _number(doc.get("threshold_db")),
        "processing_gain_db": None if fs_hz is None or cpi_s is None else 10 * math.log10(fs_hz * cpi_s),
        "window": doc["window"],
        "descriptors": {term.name: described.get(term.name) for term in performance_predict.TERMS},
    }


def _model_rows(data_dir: Path, nodes: Mapping[str, Configs]) -> list[dict[str, Any]]:
    """The fit's rows, one per registry node whose file, as it stands now, is fit to enter, in node order.

    A file that can't be read, or holds no window of two days, is logged and its node stays out.
    """
    rows = []
    for node_id in sorted(nodes):
        path = data_dir / f"performance_{node_id}.json"
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
            window = doc.get("window")
            if not isinstance(window, list) or [type(day) for day in window] != [str, str]:
                raise ValueError("no window of two days")
        except FileNotFoundError:
            continue  # nothing in the window, or never published
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning("performance job: %s can't be read, its node stays out of the model: %r", path.name, exc)
            continue
        version = doc.get("config_version")
        config = next((row for row in nodes[node_id] if row["version"] == version), None)
        if config is None:
            logger.warning(
                "performance job: %s has no config row of version %r, it stays out of the model", node_id, version
            )
            continue
        row = _model_row(node_id, doc, config)
        if row is not None:
            rows.append(row)
    return rows


def _write_model(data_dir: Path, nodes: Mapping[str, Configs], class_offsets: Mapping[str, float], today: date) -> None:
    """The fleet's system constant model, fitted again on the node files as they now stand.

    Anything that raises here is logged and the last model stands: the node files are
    published by then, and the marker still tells the task so.
    """
    try:
        rows = _model_rows(data_dir, nodes)
        _write_json(
            data_dir / performance_predict.MODEL_FILE, performance_predict.model_file(rows, class_offsets, today)
        )
    except Exception:
        logger.exception("performance job: the system constant model failed, the last one stands")


@contextmanager
def _run_lock(data_dir: Path) -> Iterator[None]:
    """Hold an exclusive flock on ``performance/.job.lock``, waiting while another run holds it.

    The kernel lets go when the holder's process ends, however it ends, so a killed run
    leaves only the file and never blocks the next.  Not re-entrant: a second hold in
    the same process waits forever.
    """
    path = data_dir / _LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("performance job: waiting for another run to end, it holds %s", path)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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
    After the sweep the fleet's system constant model is fitted on the node files as
    they stand; a fit that raises is logged, leaves the last model, and stops nothing.

    Runs on one ``data_dir`` take turns: each holds ``performance/.job.lock`` from before
    it reads the registry to after its marker, and one that finds it held, such as a
    manual run beside the scheduled child, says so in the log and waits, then takes its
    day from the clock.
    """
    data_dir = Path(data_dir)
    with _run_lock(data_dir):
        settings = performance_settings()
        if settings.tower_budget_s >= settings.timeout_s:
            logger.warning(
                "performance job: PERFORMANCE_JOB_TOWER_BUDGET_S=%d is not below PERFORMANCE_JOB_TIMEOUT_S=%d, "
                "so a slow tower-finder can use the run's whole time",
                settings.tower_budget_s,
                settings.timeout_s,
            )
        today = today or datetime.now(UTC).date()
        days = _days_before(today, PERFORMANCE_WINDOW_DAYS)
        nodes, tenures = _read_registry(registry)
        towers = _tower_document(
            nodes,
            days[0],
            today,
            url=tower_url,
            path=data_dir / "tower_match.json",
            session=session,
            budget_s=settings.tower_budget_s,
        )
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
            lambda node_id, configs: _add_day_tables(
                node_id, configs, days, data_dir, archive_root, adsb_root, types_for
            )
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
        _write_model(data_dir, nodes, class_offsets, today)
        marker = {"finished_at": datetime.now(UTC).isoformat(), "nodes": len(nodes), "failed": len(failed)}
        _write_json(data_dir / PERFORMANCE_LAST_RUN_FILE, marker)
        return marker
