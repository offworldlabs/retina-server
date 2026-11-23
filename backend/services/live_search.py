"""Live search and lookup over the real-only public aircraft feed, in readsb rows.

The keyed v1 routes and streams read live aircraft from here.  The source is
``state.latest_real_aircraft_json_bytes`` and nothing else: by the time the
flush (services/tasks/aircraft_flush.py, ``_flush_once``) stores those bytes,
the private-node redaction (services/publication.py), the real-only filter
(``filter_payload_to_nodes``) and the identity substitution
(services/node_refs.py) have all run on them.  So this module applies no
privacy or fleet filter of its own, and a tightening of any of those helpers
reaches the keyed routes with no edit here.  Reading ``state.latest_aircraft_json``
or ``state.track_histories_public`` instead would step around all three.

The bytes are parsed once per flush.  ``live_index`` rebuilds only when the
bytes object on state is replaced, so every request and every stream tick
inside one flush shares one parse.

Rows follow readsb/tar1090's ``aircraft.json``: a field whose value is unknown
is omitted rather than sent as null, and values keep the units the feed
already publishes (alt_baro ft, gs kt, track deg, seen s).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

import orjson

from core import state
from services.geo import haversine_km
from services.tracks_store import BBox

# The most trail points a lookup returns, oldest dropped first.  The API's own
# limit, deliberately not tied to the store's TRACK_HISTORY_MAX: a larger store
# must not silently widen what a key is served.
TRAIL_MAX_POINTS = 60

_KM_PER_NM = 1.852

# Entry types whose builder writes an ADS-B callsign into `flight`: the claimed
# ADS-B entry (services/aircraft_feed.py, from state.adsb_aircraft).  The other
# builders write placeholders there: a single-node track its ADS-B hex or ""
# (services/track_gates.py), a multinode solve "MN<n>N"
# (services/aircraft_feed.py).  A whitelist, so an entry type added later
# carries no callsign until someone checks where its `flight` comes from.
_CALLSIGN_TYPES = frozenset({"adsb_icao"})

# Optional readsb fields that pass through as published, omitted when unknown.
_PASSTHROUGH = ("alt_baro", "gs", "track", "seen", "position_source")


@dataclass(frozen=True, slots=True)
class LiveFilters:
    """One live search.  A None field is no filter; the fields AND together, and bounds are inclusive.

    ``bbox`` with west > east crosses the antimeridian.  ``center`` (lat, lon)
    and ``radius_nm`` come together, and the radius is a great-circle distance.
    ``hexes`` match case-insensitively and an empty set matches nothing.
    ``callsign_prefix`` matches the start of a real ADS-B callsign,
    case-insensitively.  Both are normalised here, once, rather than per entry.
    An aircraft with no position never matches a box or a radius, one with no
    altitude or ground speed never matches that range, and one with no real
    callsign never matches a prefix.
    """

    bbox: BBox | None = None
    center: tuple[float, float] | None = None
    radius_nm: float | None = None
    alt_min_ft: float | None = None
    alt_max_ft: float | None = None
    gs_min_kt: float | None = None
    gs_max_kt: float | None = None
    hexes: frozenset[str] | None = None
    callsign_prefix: str | None = None

    def __post_init__(self) -> None:
        if (self.center is None) != (self.radius_nm is None):
            raise ValueError("center and radius_nm must be given together")
        if self.radius_nm is not None and not self.radius_nm > 0:
            raise ValueError("radius_nm must be greater than 0")
        if self.hexes is not None:
            object.__setattr__(self, "hexes", frozenset(h.strip().lower() for h in self.hexes))
        if self.callsign_prefix is not None:
            object.__setattr__(self, "callsign_prefix", self.callsign_prefix.strip().upper())


@dataclass(frozen=True, slots=True)
class _Entry:
    """One aircraft: its readsb row, and the values the filters compare."""

    row: dict[str, Any]
    lat: float | None
    lon: float | None
    alt_ft: float | None
    gs_kt: float | None
    # The real callsign upper-cased, or None when the entry has none.
    callsign: str | None
    # recent_positions as published: [lat, lon, alt_ft, ts], oldest first.
    recent: list


class LiveIndex:
    """One parse of the real-only feed: its ``now`` and every aircraft as a readsb row."""

    __slots__ = ("_entries", "now")

    def __init__(self, source: bytes) -> None:
        payload = orjson.loads(source)
        self.now: float = float(payload.get("now") or 0)
        self._entries = tuple(e for ac in payload.get("aircraft") or () if (e := _entry(ac)) is not None)

    def search(self, filters: LiveFilters) -> list[dict[str, Any]]:
        """The rows matching every filter, in feed order.

        Each row is a new dict the caller may extend; the ``node_refs`` list
        inside it is the index's own and is read-only.
        """
        return [dict(e.row) for e in self._entries if _matches(e, filters)]

    def lookup(self, *, hex: str | None = None, callsign: str | None = None) -> list[dict[str, Any]]:
        """Current state and live trail for one hex or one callsign; exactly one of them.

        ``hex`` matches case-insensitively, ``callsign`` the whole real
        callsign, case-insensitively, both stripped.  A list, because nothing
        stops two entries in one frame from sharing a hex.  Each result is the
        search row plus ``trail``: the entry's published ``recent_positions``
        as ``{ts, lat, lon, alt_baro}`` objects, ``ts`` in epoch seconds,
        oldest first, the last TRAIL_MAX_POINTS of them.  An entry type that
        publishes no trail (a claimed ADS-B fix) gets an empty one.
        """
        if hex is not None and callsign is None:
            key = hex.strip().lower()
            found = [e for e in self._entries if e.row["hex"] == key]
        elif callsign is not None and hex is None:
            key = callsign.strip().upper()
            found = [e for e in self._entries if e.callsign == key]
        else:
            raise ValueError("lookup takes exactly one of hex and callsign")
        return [{**e.row, "trail": _trail(e.recent)} for e in found]


_lock = threading.Lock()
# The bytes object the current index was built from, held so that `is` stays a
# valid test: a live reference cannot be freed and its address reused.
_built: tuple[bytes, LiveIndex] | None = None


def live_index() -> LiveIndex:
    """The index of the real-only feed as the last flush stored it.

    Rebuilt only when ``state.latest_real_aircraft_json_bytes`` is a different
    object, which happens once per flush; callers within one flush get the same
    index.  The rebuild is under a lock, so concurrent callers parse once.
    """
    global _built
    built = _built
    if built is None or built[0] is not state.latest_real_aircraft_json_bytes:
        with _lock:
            source = state.latest_real_aircraft_json_bytes
            built = _built
            if built is None or built[0] is not source:
                built = _built = (source, LiveIndex(source))
    return built[1]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _entry(ac: Any) -> _Entry | None:
    """One feed entry as a readsb row; None for an entry without a hex, which no row can name."""
    if not isinstance(ac, dict) or not isinstance(ac.get("hex"), str) or not ac["hex"]:
        return None
    row: dict[str, Any] = {"hex": ac["hex"].lower()}
    flight = ac.get("flight")
    callsign = flight.strip() if ac.get("type") in _CALLSIGN_TYPES and isinstance(flight, str) else ""
    if callsign:
        row["flight"] = callsign
    lat, lon = _number(ac.get("lat")), _number(ac.get("lon"))
    if lat is None or lon is None:
        lat = lon = None
    else:
        row["lat"], row["lon"] = ac["lat"], ac["lon"]
    for key in _PASSTHROUGH:
        if ac.get(key) is not None:
            row[key] = ac[key]
    refs = list(ac.get("contributing_node_refs") or ([ac["node_ref"]] if ac.get("node_ref") else []))
    row["multinode"] = bool(ac.get("multinode"))
    row["n_nodes"] = ac["n_nodes"] if ac.get("n_nodes") is not None else len(refs)
    if ac.get("pos_sigma_m") is not None:
        row["pos_sigma_m"] = ac["pos_sigma_m"]
    row["node_refs"] = refs
    row["is_anomalous"] = bool(ac.get("is_anomalous"))
    alt = ac.get("alt_baro")
    return _Entry(
        row=row,
        lat=lat,
        lon=lon,
        # readsb's on-ground sentinel, read as 0 ft the way state.adsb_derived_fields reads it.
        alt_ft=0.0 if alt == "ground" else _number(alt),
        gs_kt=_number(ac.get("gs")),
        callsign=callsign.upper() or None,
        recent=ac.get("recent_positions") or [],
    )


def _within(value: float | None, low: float | None, high: float | None) -> bool:
    if low is None and high is None:
        return True
    return value is not None and (low is None or low <= value) and (high is None or value <= high)


def _in_box(lat: float, lon: float, box: BBox) -> bool:
    """tracks_store's box rule (``_select``): west > east crosses the antimeridian."""
    if not box.south <= lat <= box.north:
        return False
    if box.west <= box.east:
        return box.west <= lon <= box.east
    return lon >= box.west or lon <= box.east


def _matches(e: _Entry, f: LiveFilters) -> bool:
    if f.hexes is not None and e.row["hex"] not in f.hexes:
        return False
    if f.callsign_prefix is not None and (e.callsign is None or not e.callsign.startswith(f.callsign_prefix)):
        return False
    if not (_within(e.alt_ft, f.alt_min_ft, f.alt_max_ft) and _within(e.gs_kt, f.gs_min_kt, f.gs_max_kt)):
        return False
    if f.bbox is None and f.center is None:
        return True
    if e.lat is None or e.lon is None:
        return False
    if f.bbox is not None and not _in_box(e.lat, e.lon, f.bbox):
        return False
    if f.center is not None and f.radius_nm is not None:
        return haversine_km(f.center[0], f.center[1], e.lat, e.lon) <= f.radius_nm * _KM_PER_NM
    return True


def _trail(recent: list) -> list[dict[str, Any]]:
    """Published [lat, lon, alt_ft, ts] points as readsb-named objects, alt_baro omitted when null."""
    trail = []
    for point in recent[-TRAIL_MAX_POINTS:]:
        if not isinstance(point, list) or len(point) < 4:
            continue
        lat, lon, alt, ts = point[:4]
        out = {"ts": ts, "lat": lat, "lon": lon}
        if alt is not None:
            out["alt_baro"] = alt
        trail.append(out)
    return trail
