"""Public tracks archive: the solves the network published in a past window.

Unauthenticated, like the detection archive in routes/archive.py, and read
from the store services/track_writer.py fills. The file work is
services/tracks_store.py's and runs off the event loop.

The store holds private node ids, so a row goes out with its contributors as
services/node_refs.py publishes them, and a solve a private node contributed
to is dropped rather than redacted as the live feed does: an archived row can
be matched against the detection archive by time, which would name the node
the live feed strikes.
"""

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import orjson
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from config.constants import TRACKS_AVAILABILITY_MAX_DAYS, TRACKS_ROW_CAP, TRACKS_WINDOW_MAX_MS
from services import tracks_store
from services.publication import private_node_ids

router = APIRouter(tags=["archive"])

Fleet = Literal["real", "synthetic", "all"]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_MS = re.compile(r"-?[0-9]{1,15}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")

_FLEET_HELP = "real (the default), synthetic, or all, which adds solves with both kinds of node or none."


# ── The 200 bodies, for the API reference (the routes encode them with orjson) ──
# tests/test_tracks_archive_route.py validates real bodies against them.


class ArchiveTrackRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_ts_ms: int = Field(description="Measurement epoch the solve is for, epoch ms.")
    solve_ts_ms: int = Field(description="Server wallclock when the solve was published, epoch ms.")
    track_key: str | None = Field(description="The track the solve was stored under; null in older files.")
    lane: Literal["dark", "known", "follow"] | None
    node_kind: Literal["real", "synthetic", "mixed"] | None = Field(
        description="Which fleet the contributors belong to; null for a solve with none."
    )
    lat: float
    lon: float
    alt_m: float | None
    vel_east_ms: float | None
    vel_north_ms: float | None
    vel_up_ms: float | None
    n_nodes: int
    node_refs: list[str] = Field(description="The contributing nodes' published identities.")
    adsb_hex: str | None = Field(description="Set on solves that carried an ADS-B identity.")
    pos_sigma_m: float | None
    rms_delay_us: float | None
    rms_doppler_hz: float | None
    is_anomalous: bool | None
    anomaly_types: list[str]


class ArchiveTracks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_ms: int = Field(alias="from", description="Window start, epoch ms, inclusive.")
    to: int = Field(description="Window end, epoch ms, exclusive.")
    fleet: Fleet
    count: int = Field(description="Rows returned.")
    truncated: bool = Field(description=f"Whether more than {TRACKS_ROW_CAP:,} rows matched and the rest were cut.")
    rows: list[ArchiveTrackRow] = Field(description="Sorted by frame_ts_ms, then solve_ts_ms.")


class ArchiveTracksHour(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_ms: int = Field(description="The hour's start, epoch ms.")
    count: int = Field(description="Rows of the fleet asked for.")
    unclassified: int = Field(description="Rows in files that do not record their fleet; 0 under fleet=all.")


class ArchiveTracksAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_day: str = Field(alias="from", description="First UTC day, YYYY-MM-DD.")
    to: str = Field(description="Last UTC day, YYYY-MM-DD, inclusive.")
    fleet: Fleet
    hours: list[ArchiveTracksHour] = Field(description="Hours holding any rows, ascending.")
    total: int = Field(description="Sum of count.")


# ── Parameters ───────────────────────────────────────────────────────────────


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _instant_ms(value: str, name: str) -> int:
    """Epoch ms from integer ms or ISO-8601 (Z or an offset; none means UTC; a date is its midnight)."""
    text = value.strip()
    try:
        if _EPOCH_MS.fullmatch(text):
            moment = _EPOCH + timedelta(milliseconds=int(text))
        else:
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
        # Rounded up: the first whole millisecond at or after the instant.
        return -((_EPOCH - moment) // timedelta(milliseconds=1))
    except (ValueError, OverflowError):
        raise _invalid(f"{name} must be epoch milliseconds or an ISO-8601 time") from None


def _bbox(value: str) -> tracks_store.BBox:
    parts = value.split(",")
    message = "bbox must be south,west,north,east in degrees, with south <= north"
    if len(parts) != 4 or not all(_NUMBER.fullmatch(part.strip()) for part in parts):
        raise _invalid(message)
    south, west, north, east = (float(part) for part in parts)
    # NaN and infinities fail these comparisons too.
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        raise _invalid(message)
    return tracks_store.BBox(south, west, north, east)


def _day(value: str, name: str) -> date:
    try:
        if not _DAY.fullmatch(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise _invalid(f"{name} must be a UTC date, YYYY-MM-DD") from None


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/api/data/tracks",
    responses={200: {"model": ArchiveTracks, "description": "The window's published solves."}},
)
async def archived_tracks(
    from_: str = Query(..., alias="from", description="Window start: epoch ms, or ISO-8601 (UTC when no offset)."),
    to: str = Query(
        ..., description=f"Window end, exclusive; at most {TRACKS_WINDOW_MAX_MS // 60_000} minutes after from."
    ),
    bbox: str | None = Query(None, description="south,west,north,east; west > east crosses the antimeridian."),
    fleet: Fleet = Query("real", description=_FLEET_HELP),
) -> Response:
    """The solves the network published with from <= solve time < to.

    One row per published solve, sorted by frame_ts_ms, then solve_ts_ms.
    Each row names its contributors by their published node refs. A solve a
    node registered private contributed to is left out, as is one whose
    contributor has no published identity. Past the row cap the answer is cut
    and truncated is true, so a client narrows the window.
    """
    from_ms, to_ms = _instant_ms(from_, "from"), _instant_ms(to, "to")
    if to_ms <= from_ms:
        raise _invalid("to must be after from")
    if to_ms - from_ms > TRACKS_WINDOW_MAX_MS:
        raise _invalid(f"to - from exceeds the {TRACKS_WINDOW_MAX_MS // 60_000}-minute cap")
    box = _bbox(bbox) if bbox is not None else None
    # Read here, before the store, so an unknown policy is a 503 (main.py)
    # rather than a page served without it.
    private = private_node_ids()

    def _answer() -> bytes:
        return orjson.dumps(tracks_store.query_tracks(from_ms, to_ms, fleet=fleet, bbox=box, private=private))

    body = await asyncio.get_running_loop().run_in_executor(None, _answer)
    return Response(content=body, media_type="application/json")


@router.get(
    "/api/data/tracks/availability",
    responses={200: {"model": ArchiveTracksAvailability, "description": "Rows per hour."}},
)
async def archived_tracks_availability(
    from_: str = Query(..., alias="from", description="First UTC day, YYYY-MM-DD."),
    to: str = Query(
        ...,
        description=f"Last UTC day, YYYY-MM-DD, inclusive; from..to spans at most {TRACKS_AVAILABILITY_MAX_DAYS} days.",
    ),
    fleet: Fleet = Query("real", description=_FLEET_HELP),
) -> Response:
    """How many solves the archive holds per hour, for a timeline.

    Counted from the archive's file metadata, never its rows, by the hour of
    each batch's earliest solve. Files written before solves recorded their
    fleet are counted as unclassified under fleet=real or synthetic, and in
    count under all. These are network-wide totals: they include solves that
    /api/data/tracks leaves out because a private node contributed to them.
    """
    first, last = _day(from_, "from"), _day(to, "to")
    if last < first:
        raise _invalid("to must not be before from")
    if (last - first).days + 1 > TRACKS_AVAILABILITY_MAX_DAYS:
        raise _invalid(f"from..to exceeds the {TRACKS_AVAILABILITY_MAX_DAYS}-day cap")

    def _answer() -> bytes:
        return orjson.dumps(tracks_store.hourly_availability(first, last, fleet=fleet))

    body = await asyncio.get_running_loop().run_in_executor(None, _answer)
    return Response(content=body, media_type="application/json")
