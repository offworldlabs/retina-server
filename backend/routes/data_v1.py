"""The keyed data API's reads: live search, lookup and track history.

Behind the data-access gate (core/data_access.py), so every call carries a key
or an administrator's identity and counts against its tier's minute. The
services read only the real fleet, for every caller. The live filters are
parsed here once, for the routes below and for the keyed streams.
"""

import math
from collections.abc import Mapping
from typing import Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from core.data_access import Principal, require_data_access
from routes.archive_common import in_store_pool, invalid_parameter, parse_bbox, parse_instant_ms
from services import cold_store, track_history
from services.live_search import LiveFilters, live_index
from services.publication import private_node_ids
from services.track_history import HISTORY_PAGE_DEFAULT, HISTORY_PAGE_MAX, HistoryQueryError


class KeyedRefusalDetail(BaseModel):
    reason: str
    message: str


class KeyedRefusal(BaseModel):
    detail: KeyedRefusalDetail


KEYED_RESPONSES: dict[int | str, dict] = {
    401: {"model": KeyedRefusal, "description": "No key was sent, or it is not valid."},
    403: {"model": KeyedRefusal, "description": "The key's owner is not entitled to the data API."},
    429: {
        "model": KeyedRefusal,
        "description": "Over the tier's requests a minute; see the Retry-After and X-RateLimit-* headers.",
    },
}

DATA_API_VERSION = "1.0.0"

DATA_API_TAGS = [
    {
        "name": "keyed data",
        "description": "Live aircraft, lookup and track history for outside clients, behind an API key.",
    },
]

# Where the contract is served: the keyed routes are on the api vhost, and their paths carry `/api/v1` themselves.
DATA_API_SERVERS = [
    {"url": "https://api.retina.fm", "description": "Production."},
    {"url": "https://staging-api.retina.fm", "description": "Staging."},
]

router = APIRouter(tags=["keyed data"], dependencies=[Depends(require_data_access)], responses=KEYED_RESPONSES)

MAX_HEXES = 1000

LIVE_FILTER_KEYS = frozenset(
    {"bbox", "lat", "lon", "radius_nm", "alt_min", "alt_max", "gs_min", "gs_max", "hex", "callsign"}
)

# The largest seeded tier's history span, so an internal caller (no tier limit) never reads an unbounded range.
INTERNAL_HISTORY_SPAN_S = 604_800


# ── The 200 bodies, for the API reference (the routes encode them with orjson) ──


class LiveAircraft(BaseModel):
    hex: str
    flight: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_baro: float | Literal["ground"] | None = None
    gs: float | None = None
    track: float | None = None
    seen: float | None = None
    position_source: str | None = None
    multinode: bool | None = None
    n_nodes: int | None = None
    pos_sigma_m: float | None = None
    node_refs: list[str] | None = None
    is_anomalous: bool | None = None


class LiveAircraftList(BaseModel):
    now: float
    aircraft: list[LiveAircraft]


class TrailPoint(BaseModel):
    ts: float
    lat: float
    lon: float
    alt_baro: float | None = None


class LookupAircraft(LiveAircraft):
    trail: list[TrailPoint]


class LookupResult(BaseModel):
    now: float
    aircraft: list[LookupAircraft]


class HistoryPosition(BaseModel):
    hex: str
    ts: float
    lat: float
    lon: float
    alt_baro: int | None = None
    gs: float | None = None
    track: float | None = None
    position_source: str
    multinode: bool
    n_nodes: int
    pos_sigma_m: float | None = None
    node_refs: list[str]
    is_anomalous: bool


class HistoryResult(BaseModel):
    aircraft: list[HistoryPosition]
    next_cursor: str | None


# ── Parameters ───────────────────────────────────────────────────────────────


def _text(values: Mapping[str, str | float], name: str) -> str | None:
    value = values.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_parameter(f"{name} must be text")
    return value


def _number(values: Mapping[str, str | float], name: str) -> float | None:
    value = values.get(name)
    if value is None:
        return None
    try:
        number = math.nan if isinstance(value, bool) else float(value)
    except ValueError:
        number = math.nan
    if not math.isfinite(number):
        raise invalid_parameter(f"{name} must be a finite number")
    return number


def _in_range(number: float | None, name: str, low: float, high: float) -> None:
    if number is not None and not low <= number <= high:
        raise invalid_parameter(f"{name} must be from {low:g} to {high:g}")


def _ordered(low: float | None, high: float | None, low_name: str, high_name: str) -> None:
    if low is not None and high is not None and low > high:
        raise invalid_parameter(f"{low_name} must not be above {high_name}")


def _hex_tokens(text: str) -> list[str]:
    tokens = [token for part in text.split(",") if (token := part.strip())]
    if not tokens:
        raise invalid_parameter("hex must name at least one aircraft")
    if len(tokens) > MAX_HEXES:
        raise invalid_parameter(f"hex may name at most {MAX_HEXES} aircraft")
    return tokens


def parse_live_filters(values: Mapping[str, str | float]) -> LiveFilters:
    """The live filters the keyed routes and streams take, from their parameters by name.

    Keys outside LIVE_FILTER_KEYS are ignored.  A refusal is a 422 naming the
    parameter and the rule, never the value.
    """
    box_text = _text(values, "bbox")
    bbox = None if box_text is None else parse_bbox(box_text)
    lat, lon, radius_nm = (_number(values, name) for name in ("lat", "lon", "radius_nm"))
    if len({value is None for value in (lat, lon, radius_nm)}) > 1:
        raise invalid_parameter("lat, lon and radius_nm go together")
    _in_range(lat, "lat", -90, 90)
    _in_range(lon, "lon", -180, 180)
    if radius_nm is not None and not radius_nm > 0:
        raise invalid_parameter("radius_nm must be greater than 0")
    alt_min, alt_max, gs_min, gs_max = (_number(values, name) for name in ("alt_min", "alt_max", "gs_min", "gs_max"))
    _ordered(alt_min, alt_max, "alt_min", "alt_max")
    _ordered(gs_min, gs_max, "gs_min", "gs_max")
    hex_text, callsign = _text(values, "hex"), _text(values, "callsign")
    if callsign is not None and not callsign.strip():
        raise invalid_parameter("callsign must not be empty")
    try:
        return LiveFilters(
            bbox=bbox,
            center=None if lat is None or lon is None else (lat, lon),
            radius_nm=radius_nm,
            alt_min_ft=alt_min,
            alt_max_ft=alt_max,
            gs_min_kt=gs_min,
            gs_max_kt=gs_max,
            hexes=None if hex_text is None else frozenset(_hex_tokens(hex_text)),
            callsign_prefix=callsign,
        )
    except ValueError as exc:
        raise invalid_parameter(str(exc)) from None


def live_filters_query(
    bbox: str | None = Query(
        None, description="south,west,north,east in degrees; west > east crosses the antimeridian."
    ),
    lat: float | None = Query(None, description="Centre latitude, degrees, -90 to 90; goes with lon and radius_nm."),
    lon: float | None = Query(None, description="Centre longitude, degrees, -180 to 180; goes with lat and radius_nm."),
    radius_nm: float | None = Query(None, description="Radius around lat, lon in nautical miles, above 0."),
    alt_min: float | None = Query(None, description="Lowest barometric altitude, feet, inclusive."),
    alt_max: float | None = Query(None, description="Highest barometric altitude, feet, inclusive."),
    gs_min: float | None = Query(None, description="Lowest ground speed, knots, inclusive."),
    gs_max: float | None = Query(None, description="Highest ground speed, knots, inclusive."),
    hex: str | None = Query(
        None, description=f"Comma-separated ICAO hex codes, at most {MAX_HEXES}; case-insensitive."
    ),
    callsign: str | None = Query(None, description="Callsign prefix, case-insensitive."),
) -> LiveFilters:
    given = {
        "bbox": bbox,
        "lat": lat,
        "lon": lon,
        "radius_nm": radius_nm,
        "alt_min": alt_min,
        "alt_max": alt_max,
        "gs_min": gs_min,
        "gs_max": gs_max,
        "hex": hex,
        "callsign": callsign,
    }
    return parse_live_filters({key: value for key, value in given.items() if value is not None})


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/api/v1/aircraft",
    responses={200: {"model": LiveAircraftList, "description": "The live aircraft matching every filter given."}},
)
def live_aircraft(filters: LiveFilters = Depends(live_filters_query)) -> Response:
    """The real fleet's live aircraft, as readsb rows, narrowed by the filters; none given is the whole feed."""
    index = live_index()
    return Response(
        content=orjson.dumps({"now": index.now, "aircraft": index.search(filters)}), media_type="application/json"
    )


@router.get(
    "/api/v1/lookup",
    responses={200: {"model": LookupResult, "description": "The aircraft's current state and live trail, if live."}},
)
def live_lookup(
    hex: str | None = Query(None, description="One ICAO hex code, case-insensitive."),
    callsign: str | None = Query(None, description="One whole callsign, case-insensitive."),
) -> Response:
    """Current state and live trail of one aircraft, by hex or by callsign; an aircraft not live gives an empty list."""
    hex, callsign = (hex or "").strip(), (callsign or "").strip()
    if bool(hex) == bool(callsign):
        raise invalid_parameter("give exactly one of hex and callsign")
    index = live_index()
    found = index.lookup(hex=hex or None, callsign=callsign or None)
    return Response(content=orjson.dumps({"now": index.now, "aircraft": found}), media_type="application/json")


@router.get(
    "/api/v1/history",
    responses={
        200: {"model": HistoryResult, "description": "One page of positions, oldest first."},
        503: {"description": "An archived hour in the window could not be loaded from cold storage."},
    },
)
async def aircraft_history(
    start: str = Query(..., description="Window start: epoch ms, or ISO-8601 (UTC when no offset)."),
    end: str = Query(..., description="Window end, exclusive: epoch ms, or ISO-8601."),
    hex: str | None = Query(None, description=f"Comma-separated ICAO hex codes, at most {MAX_HEXES}."),
    bbox: str | None = Query(
        None, description="south,west,north,east in degrees; west > east crosses the antimeridian."
    ),
    limit: int = Query(HISTORY_PAGE_DEFAULT, ge=1, le=HISTORY_PAGE_MAX, description="Positions in the page."),
    cursor: str | None = Query(None, description="The previous page's next_cursor, for the same window and filters."),
    principal: Principal = Depends(require_data_access),
) -> Response:
    """The real fleet's recorded positions in the window, a page at a time; give a hex, a bbox or both.

    The window may span at most the caller's tier allows. next_cursor is null on the last page.
    """
    start_ms, end_ms = parse_instant_ms(start, "start"), parse_instant_ms(end, "end")
    hexes = None if hex is None else _hex_tokens(hex)
    box = None if bbox is None else parse_bbox(bbox)
    max_span_s = principal.limits.max_history_span_s
    # Read here, before the store, so an unknown policy is a 503 (main.py) rather than a page served without it.
    private = private_node_ids()

    def _answer() -> bytes:
        page = track_history.query_history(
            start_ms,
            end_ms,
            max_span_s=INTERNAL_HISTORY_SPAN_S if max_span_s is None else max_span_s,
            private=private,
            hexes=hexes,
            bbox=box,
            limit=limit,
            cursor=cursor,
        )
        return orjson.dumps({"aircraft": page.rows, "next_cursor": page.next_cursor})

    try:
        body = await in_store_pool(_answer)
    except HistoryQueryError as exc:
        raise invalid_parameter(str(exc)) from None
    except cold_store.ColdStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(content=body, media_type="application/json")
