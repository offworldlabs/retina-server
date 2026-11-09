"""Stored match of each node's configured tower against tower-finder's licence records.

A node's figures carry the power of its tower, and a later job takes that power out
of them.  tower-finder has no lookup by callsign, only ``GET /api/towers``, a radius
search, so the match is made here.  ``refresh`` asks once per tower position and
keeps the result in one JSON file: per tower a status, the licence's callsign,
frequency and EIRP, and the last day tower-finder answered for it.

The key is the tower as configured (callsign, frequency, position), never the node.
A node's tower changes often and the fit needs the power of the tower in force on
each day, so an entry never moves between towers: a new tower cannot inherit the old
one's match, and a superseded config row still resolves.  Entries are never deleted,
and a failed lookup leaves them as they were.

Runs in a job's child process, so this module imports neither ``main`` nor
``core.state``, and the config rows and every setting arrive as arguments.
"""

import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import httpx

from config.constants import is_num

logger = logging.getLogger(__name__)

_SEARCH_RADIUS_KM = 30
_SEARCH_LIMIT = 200
_MAX_FREQUENCY_OFFSET_MHZ = 3.5
# The FCC side takes up to 30 s per state, fanned out 4 at a time.
_TIMEOUT_S = 120.0
# A hung service must not eat the job's time limit.
_MAX_CONSECUTIVE_FAILURES = 3
_USER_AGENT = "retina-server/1.0 (+https://github.com/offworldlabs/retina-server)"

_CALLSIGN_SUFFIX_RE = re.compile(r"-(?:TV|DT|D|LD|LP|CD|CA|FM|HD)$")
_DEFAULT_EIRP_FM_DBM = 50.0
_DEFAULT_EIRP_DBM = 60.0
_ENTRY_FIELDS = frozenset({"callsign", "frequency_mhz", "eirp_dbm", "status", "date"})


class _LookupFailed(Exception):
    """tower-finder gave no usable answer for a position; the message names why, and no tower."""


def _now() -> datetime:
    return datetime.now(UTC)


def _finite(value: object) -> bool:
    # bool is an int to is_num, and a true is not a number here.
    return is_num(value) and not isinstance(value, bool)


def _normalise(callsign: object) -> str:
    """The bare callsign, the same on both sides of the match; "" where there is none."""
    tokens = callsign.upper().split() if isinstance(callsign, str) else []
    return _CALLSIGN_SUFFIX_RE.sub("", tokens[0]) if tokens else ""


def _fixed(value: object, decimals: int) -> str:
    return f"{value:.{decimals}f}" if _finite(value) else ""


def _tower_parts(config: Mapping) -> tuple[str, str, str, str]:
    """Callsign, MHz, latitude and longitude as the key writes them, "" for a missing part."""
    fc_hz = config.get("fc_hz")
    return (
        _normalise(config.get("tx_callsign")),
        _fixed(fc_hz / 1e6, 3) if _finite(fc_hz) else "",
        _fixed(config.get("tx_lat"), 4),
        _fixed(config.get("tx_lon"), 4),
    )


def tower_key(config: Mapping) -> str:
    """What says two config rows name the same tower, and what an entry is stored under."""
    return "|".join(_tower_parts(config))


def _callsigns(record: dict) -> set[str]:
    """Every callsign on a row: a shared transmitter keeps the partner's only in ``shared_callsigns``."""
    shared = record.get("shared_callsigns")
    return {_normalise(name) for name in (record.get("callsign"), *(shared if isinstance(shared, list) else ()))}


def match(records: list[dict], callsign: str | None, fc_hz: float) -> tuple[dict | None, str]:
    """The licence row behind a configured tower, and the status saying how far it can be trusted."""
    wanted = _normalise(callsign)
    candidates = [
        row
        for row in records
        if wanted
        and row.get("licence_type")
        and row.get("measured") is not True
        and _finite(row.get("frequency_mhz"))
        and wanted in _callsigns(row)
    ]
    if not candidates:
        return None, "not_found"

    def closeness(row: dict) -> tuple[float, float]:
        distance_km = row.get("distance_km")
        return abs(row["frequency_mhz"] - fc_hz / 1e6), distance_km if _finite(distance_km) else math.inf

    best = min(candidates, key=closeness)
    return best, "matched" if closeness(best)[0] <= _MAX_FREQUENCY_OFFSET_MHZ else "frequency_mismatch"


def _licensed_eirp(record: dict) -> float | None:
    eirp_dbm = record.get("eirp_dbm")
    # The route fills these in, unflagged, for a record with no power on file: a made-up power must not reach the fit.
    default_dbm = _DEFAULT_EIRP_FM_DBM if record.get("band") == "FM" else _DEFAULT_EIRP_DBM
    return float(eirp_dbm) if _finite(eirp_dbm) and eirp_dbm != default_dbm else None


def _entry(status: str, day: str, record: dict | None = None) -> dict:
    entry = {"callsign": None, "frequency_mhz": None, "eirp_dbm": None, "status": status, "date": day}
    if record is not None:
        entry["callsign"] = record.get("callsign") or None
        entry["frequency_mhz"] = float(record["frequency_mhz"])
    if status == "matched":
        entry["eirp_dbm"] = _licensed_eirp(record)
    return entry


def _fetch(client: httpx.Client, url: str, lat: str, lon: str) -> list[dict] | None:
    """The records around a position, None where tower-finder refuses the region.

    Raises _LookupFailed for anything else.  The timeout goes on the call so that an
    injected client has it too.
    """
    params = {"lat": lat, "lon": lon, "radius_km": _SEARCH_RADIUS_KM, "limit": _SEARCH_LIMIT}
    try:
        resp = client.get(f"{url.rstrip('/')}/api/towers", params=params, timeout=_TIMEOUT_S)
        body = resp.json() if resp.status_code in (200, 422) else None
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        raise _LookupFailed(type(exc).__name__) from exc
    if not isinstance(body, dict):
        raise _LookupFailed(f"unexpected answer, HTTP {resp.status_code}")
    # The refusal's wording differs between deployments, and FastAPI's own validation 422 has a list here.
    if resp.status_code == 422 and isinstance(body.get("detail"), str):
        return None
    records = body.get("towers")
    if resp.status_code == 200 and isinstance(records, list) and all(isinstance(row, dict) for row in records):
        return records
    raise _LookupFailed(f"unexpected answer, HTTP {resp.status_code}")


def _store(towers: dict, key: str, config: dict, records: list[dict] | None, day: str) -> None:
    """One tower's entry from one answer; ``records`` None is a region refusal."""
    if records is None:
        towers[key] = _entry("unsupported_region", day)
        return
    record, status = match(records, config.get("tx_callsign"), config["fc_hz"])
    # tower-finder skips a state the FCC didn't answer and still answers 200: a previous match or frequency mismatch
    # may be a gap, not a lost licence, so it is kept.
    if status == "not_found" and towers.get(key, {}).get("status") in ("matched", "frequency_mismatch"):
        logger.warning("tower match: a known tower is missing from an answer, keeping its entry")
        return
    towers[key] = _entry(status, day, record)


def _ask(client: httpx.Client, url: str, positions: dict, towers: dict, interval_s: float) -> None:
    """Every position in turn, until tower-finder has failed too many times running."""
    failures = 0
    for index, ((lat, lon), rows) in enumerate(positions.items()):
        if failures == _MAX_CONSECUTIVE_FAILURES:
            logger.warning("tower match: stopped asking, %d positions keep what they have", len(positions) - index)
            return
        if index:
            time.sleep(interval_s)
        try:
            records = _fetch(client, url, lat, lon)
        except _LookupFailed as exc:
            failures += 1
            logger.warning("tower match: lookup failed: %s", exc)
            continue
        failures = 0
        day = _now().date().isoformat()
        for key, config in rows.items():
            _store(towers, key, config, records, day)


def _write(path: Path, doc: dict) -> None:
    """``doc`` at ``path``, swapped in whole so that a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            json.dump(doc, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def refresh(
    configs: Iterable[dict],
    *,
    url: str,
    path: str | os.PathLike,
    session: httpx.Client | None = None,
    interval_s: float = 1.0,
) -> dict:
    """Bring the stored match up to date for ``configs``; the document it wrote.

    One request per tower position, ``interval_s`` apart.  The file is written even
    when every lookup failed, and an entry absent from ``configs`` stays in it.  An
    OSError reading the stored document raises, with nothing asked and nothing
    written: writing would drop every entry the read didn't see.
    """
    towers = _read(path)["towers"]
    positions: dict[tuple[str, str], dict[str, dict]] = {}
    for config in configs:
        callsign, fc_mhz, lat, lon = _tower_parts(config)
        if callsign and fc_mhz and lat and lon:
            positions.setdefault((lat, lon), {})[tower_key(config)] = config
        else:
            towers[tower_key(config)] = _entry("not_found", _now().date().isoformat())

    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    with httpx.Client(headers=headers) if session is None else nullcontext(session) as client:
        _ask(client, url, positions, towers, interval_s)

    doc = {"refreshed_at": _now().isoformat(), "towers": towers}
    _write(Path(path), doc)
    return doc


def _well_formed(doc: object) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get("towers"), dict):
        return False
    if "refreshed_at" not in doc or not isinstance(doc["refreshed_at"], str | None):
        return False
    return all(isinstance(entry, dict) and entry.keys() >= _ENTRY_FIELDS for entry in doc["towers"].values())


def _empty() -> dict:
    return {"refreshed_at": None, "towers": {}}


def _read(path: str | os.PathLike) -> dict:
    """The stored document; an empty one when there is none or it can't be used.

    An OSError other than a missing file raises.  It says nothing about the file,
    which may well hold every entry, while bad JSON or the wrong shape can't heal:
    the file is swapped in whole.
    """
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return _empty()
    except (ValueError, RecursionError) as exc:
        logger.warning("tower match: cannot read %s: %s", path, type(exc).__name__)
        return _empty()
    if not _well_formed(doc):
        logger.warning("tower match: %s has the wrong shape", path)
        return _empty()
    return doc


def load(path: str | os.PathLike) -> dict:
    """The stored document, or an empty one whatever went wrong: a reader has nothing to protect."""
    try:
        return _read(path)
    except OSError as exc:
        logger.warning("tower match: cannot read %s: %s", path, type(exc).__name__)
        return _empty()


def lookup(doc: dict, config: Mapping) -> dict | None:
    """The stored entry for one config row, current or superseded; None where nothing is known yet."""
    return doc["towers"].get(tower_key(config))
