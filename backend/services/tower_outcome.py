"""Each real public node's archive figures, as rows for tower-finder's fleet feedback.

tower-finder ranks towers on a modelled area and corrects it with what the fleet
reports back.  Once a week, services/tasks/tower_outcome_post.py has this module
build one ``TowerOutcome(source="archive", outcome="observed")`` row per node, from
the node's performance file and the configuration versions it names, and post them.

**Who is posted.**  A node with a performance file that is not synthetic, not private
by the predicate every public read asks (services/publication.py), has an active
configuration, and a window that ended at most TOWER_OUTCOME_MAX_WINDOW_AGE_DAYS
before the post day.  A privacy policy that can't be read fails the run: reading it
as "all private" would post nothing and call that a success.  Of those, each of
these is left out, with a log line naming it and why:

- a fit reading ``rank_status == "config_suspect"``: its configured site is in doubt,
  and so is the tower position its figures would go under;
- a file without ``config_version``, written before the job recorded the version its
  window ended on, or naming a version the registry doesn't hold;
- a window measured with the receiver unplaced or more than _SAME_SITE_KM (1 km) from
  the active one's, in the version it ended on or in any version in force during it:
  a move under one tower doesn't split a window, and the row carries the active site's
  published position, so what is posted was measured within 1 km of that site.  A
  survey nudge of metres still posts;
- a window that doesn't end after the one ``posted`` holds for the node.  A node gone
  quiet keeps its last file on disk, and posting it again under a new week would count
  one window as several.

**What leaves.**  The tower, carrier and callsign are those of the version the window
ended on, not the active one: a tower changed after the window goes with the next
window.  Versions are append-only, so that row stays readable.  The receiver position
is the active version's published one, the point the map and the archive carry,
never the registry's: a row tower-finder stores is out of this deployment's hands.
S_n and each class's 50 % range go only where their bound is ``"measured"``.  Any
other bound is a floor, and the receiver would read it as a value.  tower-finder
refuses a batch whole when one value in it is outside its bounds, so a figure
outside them is left out of its row with a warning, and the rest of that row and the
batch go as built.  A position or carrier outside them leaves the node out, as its
row can't go without one.

**The wire.**  Batches of at most TOWER_OUTCOME_BATCH_SIZE, with the bearer token.
tower-finder refuses unknown keys, so one that predates ``system_constant_db`` and
``range_50pct_km`` answers 422 naming them; that batch goes once more without both,
and so does the rest of the run.  The run id names the ISO week, and tower-finder
keeps one row per node, run and tower, so a week posted twice is stored once.
"""

import logging
import math
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from config.constants import TOWER_OUTCOME_BATCH_SIZE, TOWER_OUTCOME_MAX_WINDOW_AGE_DAYS, TOWER_OUTCOME_TIMEOUT_S
from core.nodes import NodeConfig
from services import performance_store, publication
from services.geo import haversine_km
from services.node_kind import is_synthetic_node
from services.public_location import public_latlon

logger = logging.getLogger(__name__)

_ROUTE = "/api/feedback/tower-outcome"
# The keys a tower-finder from before the archive model's extension refuses.
_NEWER_KEYS = ("system_constant_db", "range_50pct_km")
# A row needs every one of these finite; the callsign may be null.
_REQUIRED_COLUMNS = ("rx_lat", "rx_lon", "tx_lat", "tx_lon", "fc_hz")
_CONFIG_COLUMNS = (*_REQUIRED_COLUMNS, "tx_callsign")
# Two receiver positions this close are one site.  A survey nudge moves a receiver by
# metres and a real move by kilometres; tower-finder pools feedback over 30 km
# (FEEDBACK_RADIUS_KM in its backend/services/tower_feedback.py).
_SAME_SITE_KM = 1.0
# tower-finder's bounds, inclusive, from TowerOutcome in tower-finder-service's
# backend/models/feedback.py: one value outside them gets the whole batch a 422.
_BOUNDS = {
    "rx_lat": (-90.0, 90.0),
    "rx_lon": (-180.0, 180.0),
    "tx_lat": (-90.0, 90.0),
    "tx_lon": (-180.0, 180.0),
    "fc_hz": (1e6, 6e9),
    "snr_median_db": (-100.0, 200.0),
    "verified_range_p85_km": (0.0, 2000.0),
    "adsb_match_rate": (0.0, 1.0),
    "hours_observed": (0.0, math.inf),
    "system_constant_db": (-300.0, 100.0),
}
# The same model's range_50pct_km: at most 16 classes, each name 1-32 characters, each range 0-2000 km.
_RANGE_BOUNDS_KM = (0.0, 2000.0)
_CLASS_NAME_CHARS = (1, 32)
_MAX_CLASSES = 16
# A refusal echoes the batch it refused, so the log keeps its head.
_BODY_LOGGED = 2000


class PostFailed(RuntimeError):
    """tower-finder didn't take a batch: a transport error, or an answer other than 2xx."""


def week_label(day: date) -> str:
    """The ISO week ``day`` falls in, as ``2026-W46``."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _number(value: Any) -> float | None:
    """A plain float, or None for anything that isn't a finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _configs(node_ids: list[str]) -> tuple[dict[str, Mapping[str, Any]], dict[str, list[Mapping[str, Any]]]]:
    """Each node's active configuration, and each node's versions, oldest first.

    Read with the sync engine services/node_sites.py uses.
    """
    columns = [getattr(NodeConfig, name) for name in _CONFIG_COLUMNS]
    with publication._sync_engine().connect() as conn:
        rows = conn.execute(
            select(NodeConfig.node_id, NodeConfig.version, NodeConfig.created_at, NodeConfig.superseded_at, *columns)
            .where(NodeConfig.node_id.in_(node_ids))
            .order_by(NodeConfig.version)
        ).all()
    # Ascending, so the newest wins should a node ever hold two active, as node_config_store.active_config chooses.
    active = {row.node_id: row._mapping for row in rows if row.superseded_at is None}
    versions: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        versions.setdefault(row.node_id, []).append(row._mapping)
    return active, versions


def _window(doc: Mapping[str, Any]) -> tuple[date, date] | None:
    """The window's first and last day, or None when the file's dates don't parse."""
    try:
        return date.fromisoformat(doc["window"][0]), date.fromisoformat(doc["window"][1])
    except ValueError:
        return None


def _position(config: Mapping[str, Any]) -> tuple[float, float] | None:
    """The receiver's (lat, lon), or None for a receiver not placed."""
    lat, lon = _number(config["rx_lat"]), _number(config["rx_lon"])
    return None if lat is None or lon is None else (lat, lon)


def _measured(doc: Mapping[str, Any]) -> dict[str, Any]:
    """S_n and the per-class ranges whose bound is ``"measured"``; the keys go only with a value."""
    out: dict[str, Any] = {}
    constant = _number(doc.get("system_constant_db"))
    if doc.get("system_constant_bound") == "measured" and constant is not None:
        out["system_constant_db"] = constant
    ranges, bounds = doc.get("range_50pct_km"), doc.get("range_50pct_bound")
    if isinstance(ranges, dict) and isinstance(bounds, dict):
        measured = {name: _number(ranges.get(name)) for name, bound in bounds.items() if bound == "measured"}
        if measured := {name: value for name, value in measured.items() if value is not None}:
            out["range_50pct_km"] = measured
    return out


def _left_out(
    doc: Mapping[str, Any],
    site: Mapping[str, Any],
    fitted_on: Mapping[str, Any] | None,
    in_force: Iterable[Mapping[str, Any]],
    end: datetime,
    posted_end: datetime | None,
) -> str | None:
    """Why a node with a placed active receiver and a recent window isn't posted, or None when it is.

    ``in_force`` is every version of the node in force at some point of the window.
    """
    if doc.get("rank_status") == "config_suspect":
        return "its fit reads its configured site as suspect"
    version = doc.get("config_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return "its performance file names no config_version"
    if fitted_on is None:
        return f"its window's config version {version} is not in the registry"
    here = _position(site)
    for config in (fitted_on, *in_force):
        there = _position(config)
        if here is None or there is None or haversine_km(*there, *here) > _SAME_SITE_KM:
            return (
                f"its window was measured with config version {config['version']}, "
                f"whose receiver isn't within {_SAME_SITE_KM:g} km of the active one's"
            )
    if posted_end is not None and end <= posted_end:
        return f"its window ending {end.isoformat()} was posted already"
    return None


def _in_bounds(node_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """``row`` without the figures tower-finder's bounds refuse; None when a column it needs is out."""
    for name, (low, high) in _BOUNDS.items():
        value = row.get(name)
        if value is None or low <= value <= high:
            continue
        if name in _REQUIRED_COLUMNS:
            logger.warning("tower outcome: %s not posted, %s=%r is outside tower-finder's bounds", node_id, name, value)
            return None
        logger.warning("tower outcome: %s %s=%r is outside tower-finder's bounds, left out", node_id, name, value)
        del row[name]
    ranges = row.pop("range_50pct_km", None) or {}
    (low, high), (shortest, longest) = _RANGE_BOUNDS_KM, _CLASS_NAME_CHARS
    kept = {name: km for name, km in ranges.items() if shortest <= len(name) <= longest and low <= km <= high}
    if outside := {name: km for name, km in ranges.items() if name not in kept}:
        logger.warning(
            "tower outcome: %s range_50pct_km %r is outside tower-finder's bounds, left out", node_id, outside
        )
    if len(kept) > _MAX_CLASSES:
        logger.warning("tower outcome: %s range_50pct_km has over %d classes, left out", node_id, _MAX_CLASSES)
    elif kept:
        row["range_50pct_km"] = kept
    return row


def _row(node_id: str, doc: Mapping[str, Any], config: Mapping[str, Any], run_id: str, end: datetime) -> dict[str, Any]:
    rx_lat, rx_lon = public_latlon(float(config["rx_lat"]), float(config["rx_lon"]), node_id)
    opportunities, detections = _number(doc.get("opportunities")), _number(doc.get("detections"))
    return {
        "node_id": node_id,
        "rx_lat": float(rx_lat),
        "rx_lon": float(rx_lon),
        "tx_lat": float(config["tx_lat"]),
        "tx_lon": float(config["tx_lon"]),
        "fc_hz": float(config["fc_hz"]),
        "callsign": config["tx_callsign"],
        "source": "archive",
        "outcome": "observed",
        "run_id": run_id,
        "snr_median_db": _number(doc.get("snr_median_db")),
        "verified_range_p85_km": _number(doc.get("verified_range_p85_km")),
        "hours_observed": _number(doc.get("hours_observed")),
        "adsb_match_rate": detections / opportunities if opportunities and detections is not None else None,
        "observed_at": end.isoformat(),
        **_measured(doc),
    }


def build_rows(post_day: date, posted: Mapping[str, datetime]) -> list[dict[str, Any]]:
    """The week's rows, one per node posted, by node id; ``posted`` is the window end each node was last posted with.

    Raises ``publication.PublicationUnavailable`` before a first read of the privacy
    policy, and whatever the configuration read raises.  Sync database reads
    throughout, so it runs off the event loop.
    """
    docs = performance_store.all()
    candidates = sorted(node_id for node_id in docs if not is_synthetic_node(node_id))
    if not candidates:
        return []
    private = publication.private_node_ids()
    candidates = [node_id for node_id in candidates if node_id not in private]
    active, versions = _configs(candidates) if candidates else ({}, {})
    oldest = post_day - timedelta(days=TOWER_OUTCOME_MAX_WINDOW_AGE_DAYS)
    run_id = f"archive-{week_label(post_day)}"
    rows = []
    for node_id in candidates:
        doc, site, window = docs[node_id], active.get(node_id), _window(docs[node_id])
        if site is None or _position(site) is None or window is None or window[1] < oldest:
            continue
        # From midnight UTC of the window's first day to midnight UTC after its last.
        start = datetime.combine(window[0], time(0), tzinfo=UTC)
        end = datetime.combine(window[1] + timedelta(days=1), time(0), tzinfo=UTC)
        history = versions.get(node_id, [])
        version = doc.get("config_version")
        fitted_on = next((config for config in history if config["version"] == version), None)
        in_force = [
            config
            for config in history
            if config["created_at"] < end and (config["superseded_at"] is None or config["superseded_at"] > start)
        ]
        reason = _left_out(doc, site, fitted_on, in_force, end, posted.get(node_id))
        if reason is not None:
            logger.info("tower outcome: %s not posted, %s", node_id, reason)
            continue
        # The tower of the version the window ended on, at the active receiver, which
        # every receiver the window was measured at is within _SAME_SITE_KM of.
        config = {**fitted_on, "rx_lat": site["rx_lat"], "rx_lon": site["rx_lon"]}
        if any(_number(config[name]) is None for name in _REQUIRED_COLUMNS):
            continue
        if (row := _in_bounds(node_id, _row(node_id, doc, config, run_id, end))) is not None:
            rows.append(row)
    return rows


def _without_newer(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key not in _NEWER_KEYS} for row in rows]


def _refuses_newer(resp: httpx.Response) -> bool:
    """Whether a 422 names one of the newer keys as a field the receiver doesn't know."""
    try:
        body = resp.json()
    except (ValueError, RecursionError):
        return False
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, list) and any(
        isinstance(error, dict)
        and error.get("type") == "extra_forbidden"
        and isinstance(error.get("loc"), list)
        and bool(error["loc"])
        and error["loc"][-1] in _NEWER_KEYS
        for error in detail
    )


def _send(client: httpx.Client, url: str, token: str, batch: list[dict[str, Any]]) -> httpx.Response:
    """One batch.  The timeout goes on the call so that an injected client has it too."""
    try:
        return client.post(
            url, json=batch, headers={"Authorization": f"Bearer {token}"}, timeout=TOWER_OUTCOME_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        raise PostFailed(f"{type(exc).__name__}: {exc}") from exc


def post(rows: list[dict[str, Any]], *, url: str, token: str, client: httpx.Client | None = None) -> None:
    """Every row to tower-finder, in batches; returns once each batch was answered 2xx, else raises PostFailed."""
    endpoint = f"{url.rstrip('/')}{_ROUTE}"
    older = False
    with httpx.Client(timeout=TOWER_OUTCOME_TIMEOUT_S) if client is None else nullcontext(client) as session:
        for start in range(0, len(rows), TOWER_OUTCOME_BATCH_SIZE):
            batch = rows[start : start + TOWER_OUTCOME_BATCH_SIZE]
            resp = _send(session, endpoint, token, _without_newer(batch) if older else batch)
            if resp.status_code == 422 and not older and _refuses_newer(resp):
                logger.warning("tower outcome: tower-finder refuses %s, sending this run without them", _NEWER_KEYS)
                older = True
                resp = _send(session, endpoint, token, _without_newer(batch))
            if not resp.is_success:
                raise PostFailed(f"HTTP {resp.status_code}: {resp.text[:_BODY_LOGGED]}")
