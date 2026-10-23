"""Public detections archive: what the network's nodes detected in a past window.

Unauthenticated, like the detection archive in routes/archive.py, and read from
the same store, published the way that archive publishes it: through its public
scope (services/archive_access.public_scope), so a node registered private
contributes nothing, every other node goes out under its published identity,
and the receiver position is the published one the writer stamped into the
file. The file work is services/detections_store.py's and runs off the event
loop.

The window is capped at a few minutes: a node writes about three detections a
second, so it is the replayed map's arcs layer around one moment, not a bulk
export, which the Data Explorer's file downloads already are.
"""

import asyncio

import orjson
from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from config.constants import DETECTIONS_ROW_CAP, DETECTIONS_WINDOW_MAX_MS
from routes.tracks_archive import Fleet, _bbox, _instant_ms, _invalid
from services import detections_store
from services.archive_access import public_scope

router = APIRouter(tags=["archive"])

_FLEET_HELP = "real (the default), synthetic, or all: the fleet of the node that made the detection, by its id."
_WINDOW_MINUTES = DETECTIONS_WINDOW_MAX_MS // 60_000


# ── The 200 body, for the API reference (the route encodes it with orjson) ──
# tests/test_detections_archive_route.py validates real bodies against it.


class ArchiveDetectionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ref: str = Field(description="The detecting node's published identity.")
    frame_ts_ms: int = Field(description="The frame's time as the node stamped it, epoch ms.")
    delay_us: float | None = Field(description="Bistatic delay, microseconds.")
    doppler_hz: float | None = Field(description="Bistatic Doppler shift, Hz.")
    snr_db: float | None = Field(description="Signal-to-noise ratio, dB.")
    adsb_hex: str | None = Field(description="The ADS-B aircraft the detection was matched to when it was taken.")
    rx_lat: float | None = Field(description="The receiver's published position, as the live map shows it.")
    rx_lon: float | None
    tx_lat: float | None = Field(description="The transmitter's position, a licensed broadcast tower.")
    tx_lon: float | None


class ArchiveDetections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_ms: int = Field(alias="from", description="Window start, epoch ms, inclusive.")
    to: int = Field(description="Window end, epoch ms, exclusive.")
    fleet: Fleet
    count: int = Field(description="Rows returned.")
    truncated: bool = Field(description=f"Whether more than {DETECTIONS_ROW_CAP:,} rows matched and the rest were cut.")
    local: bool = Field(
        description="False when the server may already have deleted some of the window's detections from its "
        "own disk, past how long it keeps them there, so rows may be missing here; the object store keeps them."
    )
    pending: bool = Field(
        description="True when the server may not have archived all of the window yet: detections are written "
        "once an hour, so for about the last hour rows may be missing now and appear in a later answer."
    )
    rows: list[ArchiveDetectionRow] = Field(description="Sorted by frame_ts_ms, then node_ref, then delay_us.")


# ── Route ────────────────────────────────────────────────────────────────────


@router.get(
    "/api/data/detections",
    responses={200: {"model": ArchiveDetections, "description": "The window's published detections."}},
)
async def archived_detections(
    from_: str = Query(..., alias="from", description="Window start: epoch ms, or ISO-8601 (UTC when no offset)."),
    to: str = Query(..., description=f"Window end, exclusive; at most {_WINDOW_MINUTES} minutes after from."),
    bbox: str | None = Query(
        None,
        description="south,west,north,east; west > east crosses the antimeridian. "
        "A detection is inside when its receiver or its transmitter is.",
    ),
    fleet: Fleet = Query("real", description=_FLEET_HELP),
) -> Response:
    """The detections the network archived with from <= frame time < to.

    One row per detection, sorted by frame_ts_ms, then node_ref, then
    delay_us. A node registered private contributes nothing, nor does one with
    no published identity, and no row names a node's private id. The receiver
    position is the published one; files archived before the archive recorded
    which position it holds contribute nothing. Past the row cap the answer is
    cut and truncated is true, so a client narrows the window. Detections are
    archived hourly: a window within about the last hour is answered with
    what is archived so far and pending true, so a client asks again later.
    """
    from_ms, to_ms = _instant_ms(from_, "from"), _instant_ms(to, "to")
    if to_ms <= from_ms:
        raise _invalid("to must be after from")
    if to_ms - from_ms > DETECTIONS_WINDOW_MAX_MS:
        raise _invalid(f"to - from exceeds the cap of {_WINDOW_MINUTES} minutes")
    box = _bbox(bbox) if bbox is not None else None
    # Built here, before the store, so an unknown policy is a 503 (main.py)
    # rather than a window served without it, and so each request reads the
    # private set afresh whatever the store has cached.
    scope = public_scope()

    def _answer() -> bytes:
        return orjson.dumps(detections_store.query_detections(from_ms, to_ms, fleet=fleet, bbox=box, scope=scope))

    body = await asyncio.get_running_loop().run_in_executor(None, _answer)
    return Response(content=body, media_type="application/json")
