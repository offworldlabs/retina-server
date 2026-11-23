"""The public map's node source.

/api/radar/analytics and /api/radar/nodes need credentials, so the anonymous
map reads its markers and coverage from here instead: every published real
node, with only the fields the map draws.

Endpoints
---------
GET /api/map/nodes  – Real nodes: status, location, detection area, coverage
"""

import orjson
from fastapi import APIRouter
from fastapi.responses import Response

from core import state

router = APIRouter(tags=["radar"])

_RX = ("lat", "lon", "location_uncertainty_km", "alt")
_TX = ("lat", "lon", "alt")
_AREA = ("beam_azimuth_deg", "beam_width_deg", "max_range_km", "max_bistatic_range_km")
_COVERAGE = ("polygon", "n_points", "polygon_source")

# The last pair of sources and the answer built from them.
_last: tuple[bytes, bytes, bytes] | None = None


def _pick(source: dict, keys: tuple[str, ...]) -> dict:
    return {key: source[key] for key in keys if key in source}


def _detection_area(area: object) -> dict | None:
    if not isinstance(area, dict):
        return None
    out = _pick(area, _AREA)
    for side, keys in (("rx", _RX), ("tx", _TX)):
        if isinstance(area.get(side), dict):
            out[side] = _pick(area[side], keys)
    return out


def _empirical_coverage(coverage: object) -> dict | None:
    return _pick(coverage, _COVERAGE) if isinstance(coverage, dict) else None


def _build(listing: bytes, analytics: bytes) -> bytes:
    drawn = orjson.loads(analytics).get("nodes", {})
    nodes = {}
    for ref, entry in orjson.loads(listing).get("nodes", {}).items():
        if entry.get("is_synthetic") is not False:
            continue
        detail = drawn.get(ref) or {}
        nodes[ref] = {
            "node_ref": ref,
            "status": entry.get("status"),
            "is_synthetic": False,
            "location": entry.get("location"),
            "detection_area": _detection_area(detail.get("detection_area")),
            "empirical_coverage": _empirical_coverage(detail.get("empirical_coverage")),
        }
    return orjson.dumps({"nodes": nodes})


@router.get("/api/map/nodes")
async def map_nodes():
    """Published real nodes as the map draws them; open to anyone."""
    global _last
    listing, analytics = state.latest_nodes_bytes, state.latest_analytics_real_bytes
    if _last is None or _last[0] is not listing or _last[1] is not analytics:
        _last = (listing, analytics, _build(listing, analytics))
    return Response(content=_last[2], media_type="application/json")
