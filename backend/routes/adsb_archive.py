"""Public ADS-B archive: the known aircraft of a past window.

Unauthenticated, like the tracks archive in routes/tracks_archive.py, and read
from the snapshots services/adsb_writer.py names. The file work is
services/adsb_store.py's and runs off the event loop, on the tracks archive's
thread pool: a day kept only in object storage downloads while its readers
wait, which must not hold the loop's shared executor.

Every archived fix is attributed to the node that reported it or, for an
ADSBHub fix, to the nodes whose published coverage holds it. A node's own fix
is dropped while that node is private or, for a real node, has no published
identity; an ADSBHub fix once every node it is attributed to is. No
attribution goes out with the rest: the aircraft a node hears would otherwise
trace where it is. Rows carry no node id, though their timing can tell public
reporters apart; nothing a withheld node reported is served.
"""

import orjson
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from config.constants import ADSB_ROW_CAP, ADSB_WINDOW_MAX_MS
from routes.tracks_archive import Fleet, _bbox, _in_store_pool, _instant_ms, _invalid
from services import adsb_store, cold_store
from services.known_claiming import KNOWN_CLAIM_MAX_FIX_AGE_S
from services.publication import private_node_ids

router = APIRouter(tags=["archive"])

_FLEET_HELP = (
    "real (the default): real-world traffic from node receivers and ADSBHub; synthetic: the simulated fleet; all: both."
)


# ── The 200 body, for the API reference (the route encodes it with orjson) ──
# tests/test_adsb_archive_route.py validates a real body against it.


class ArchiveAdsbRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_ms: int = Field(description="When the server's ADS-B picture was snapshotted, epoch ms.")
    hex: str = Field(description="ICAO address, lowercase.")
    lat: float
    lon: float
    alt_baro: int | None = Field(description="Barometric altitude, feet.")
    gs: float | None = Field(description="Ground speed, knots.")
    track: float | None = Field(description="Track, degrees.")
    flight: str | None = Field(description="Callsign.")
    seen_ms: int = Field(
        description=(
            "When the fix was received, epoch ms, rounded down to the whole second and never after ts_ms. A fix is "
            f"kept while at most {KNOWN_CLAIM_MAX_FIX_AGE_S:g} s old, so the position can be that much older than "
            "the snapshot."
        )
    )


class ArchiveAdsb(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_ms: int = Field(alias="from", description="Window start, epoch ms, inclusive.")
    to: int = Field(description="Window end, epoch ms, exclusive.")
    fleet: Fleet
    count: int = Field(description="Rows returned.")
    truncated: bool = Field(description=f"Whether more than {ADSB_ROW_CAP:,} rows matched and the rest were cut.")
    rows: list[ArchiveAdsbRow] = Field(description="One per aircraft and snapshot, sorted by ts_ms, then hex.")


@router.get(
    "/api/data/adsb",
    responses={
        200: {"model": ArchiveAdsb, "description": "The window's known aircraft."},
        503: {"description": "An archived day in the window could not be loaded from cold storage."},
    },
)
async def archived_adsb(
    from_: str = Query(..., alias="from", description="Window start: epoch ms, or ISO-8601 (UTC when no offset)."),
    to: str = Query(
        ..., description=f"Window end, exclusive; at most {ADSB_WINDOW_MAX_MS // 60_000} minutes after from."
    ),
    bbox: str | None = Query(None, description="south,west,north,east; west > east crosses the antimeridian."),
    fleet: Fleet = Query("real", description=_FLEET_HELP),
) -> Response:
    """The ADS-B traffic the server knew of with from <= snapshot time < to.

    The server's ADS-B picture, from node receivers and ADSBHub, snapshotted
    every 10 seconds: one row per aircraft per snapshot, sorted by ts_ms, then
    hex, with the freshest fix that may be published and when it was received
    (seen_ms). A node's own fix is left out when that node is registered
    private or has no published identity, and an ADSBHub fix when every node
    whose published coverage holds it is. Past the row cap the answer is cut
    and truncated is true, so a client narrows the window.
    """
    from_ms, to_ms = _instant_ms(from_, "from"), _instant_ms(to, "to")
    if to_ms <= from_ms:
        raise _invalid("to must be after from")
    if to_ms - from_ms > ADSB_WINDOW_MAX_MS:
        raise _invalid(f"to - from exceeds the {ADSB_WINDOW_MAX_MS // 60_000}-minute cap")
    box = _bbox(bbox) if bbox is not None else None
    # Read here, before the store, so an unknown policy is a 503 (main.py)
    # rather than a page served without it.
    private = private_node_ids()

    def _answer() -> bytes:
        return orjson.dumps(adsb_store.query_adsb(from_ms, to_ms, fleet=fleet, bbox=box, private=private))

    try:
        body = await _in_store_pool(_answer)
    except cold_store.ColdStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return Response(content=body, media_type="application/json")
