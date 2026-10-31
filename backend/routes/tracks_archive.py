"""Public tracks archive: the solves the network published in a past window.

Unauthenticated, like the detection archive in routes/archive.py, and read
from the store services/track_writer.py fills. The file work is
services/tracks_store.py's and runs off the event loop, on the pool of 4
threads in routes/archive_common.py that /api/data/adsb shares and that is kept
apart from the server's shared pool. A cold day of either archive holds one of
the 4 while it loads.

The store holds private node ids, so a row goes out with its contributors as
services/node_refs.py publishes them, and a solve a private node contributed
to is dropped rather than redacted as the live feed does: an archived row can
be matched against the detection archive by time, which would name the node
the live feed strikes.
"""

import re
from datetime import date
from typing import Literal

import orjson
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from config.constants import TRACKS_AVAILABILITY_MAX_DAYS, TRACKS_ROW_CAP, TRACKS_WINDOW_MAX_MS
from routes.archive_common import Fleet, in_store_pool, invalid_parameter, parse_bbox, parse_instant_ms
from services import cold_store, tracks_store
from services.publication import private_node_ids

router = APIRouter(tags=["archive"])

_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

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
    cold: bool = Field(
        default=False,
        description="Whether the hour's rows are kept only in object storage; the first read of it fetches the day.",
    )


class ArchiveTracksAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_day: str = Field(alias="from", description="First UTC day, YYYY-MM-DD.")
    to: str = Field(description="Last UTC day, YYYY-MM-DD, inclusive.")
    fleet: Fleet
    hours: list[ArchiveTracksHour] = Field(description="Hours holding any rows, ascending.")
    total: int = Field(description="Sum of count.")


# ── Parameters ───────────────────────────────────────────────────────────────


def _day(value: str, name: str) -> date:
    try:
        if not _DAY.fullmatch(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise invalid_parameter(f"{name} must be a UTC date, YYYY-MM-DD") from None


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/api/data/tracks",
    responses={
        200: {"model": ArchiveTracks, "description": "The window's published solves."},
        503: {"description": "An archived day in the window could not be loaded from cold storage."},
    },
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
    from_ms, to_ms = parse_instant_ms(from_, "from"), parse_instant_ms(to, "to")
    if to_ms <= from_ms:
        raise invalid_parameter("to must be after from")
    if to_ms - from_ms > TRACKS_WINDOW_MAX_MS:
        raise invalid_parameter(f"to - from exceeds the {TRACKS_WINDOW_MAX_MS // 60_000}-minute cap")
    box = parse_bbox(bbox) if bbox is not None else None
    # Read here, before the store, so an unknown policy is a 503 (main.py)
    # rather than a page served without it.
    private = private_node_ids()

    def _answer() -> bytes:
        return orjson.dumps(tracks_store.query_tracks(from_ms, to_ms, fleet=fleet, bbox=box, private=private))

    try:
        body = await in_store_pool(_answer)
    except cold_store.ColdStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
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
        raise invalid_parameter("to must not be before from")
    if (last - first).days + 1 > TRACKS_AVAILABILITY_MAX_DAYS:
        raise invalid_parameter(f"from..to exceeds the {TRACKS_AVAILABILITY_MAX_DAYS}-day cap")

    def _answer() -> bytes:
        return orjson.dumps(tracks_store.hourly_availability(first, last, fleet=fleet))

    body = await in_store_pool(_answer)
    return Response(content=body, media_type="application/json")
