"""US/Canada/Australia region lookup backed by real country boundary polygons.

Replaces lat/lon bounding-box heuristics, which can't represent a border
that dips and bulges (e.g. the Great Lakes, the Maine/Quebec line).
"""

import json
import threading
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

# Ported from tower-finder-service, with one deliberate divergence: that repo
# keeps the borders under backend/data/. Here backend/data/ is runtime state and
# .dockerignore strips it from the image ("backend/data/*"), so the file would be
# missing in the container. It lives beside its only consumer instead.
_BORDERS_PATH = Path(__file__).resolve().parent / "data" / "us_canada_australia_borders.geojson"

_ADMIN_TO_SOURCE = {
    "United States of America": "us",
    "Canada": "ca",
    "Australia": "au",
}

# Single source of truth for the supported-region set and the human-facing
# rejection message, derived from _ADMIN_TO_SOURCE so the two can't drift.
SUPPORTED_REGIONS = tuple(_ADMIN_TO_SOURCE.values())
UNSUPPORTED_REGION_DETAIL = (
    f"Location is not in a supported region ({', '.join(s.upper() for s in SUPPORTED_REGIONS)})."
)

_geoms: dict[str, BaseGeometry] = {}
_load_lock = threading.Lock()


def _load_borders() -> None:
    if _geoms:  # fast path after warm-up
        return
    with _load_lock:
        if _geoms:  # re-check inside lock
            return
        with open(_BORDERS_PATH) as f:
            data = json.load(f)
        loaded: dict[str, BaseGeometry] = {}
        for feature in data["features"]:
            admin = feature["properties"].get("ADMIN")
            source = _ADMIN_TO_SOURCE.get(admin)
            if source is not None:
                loaded[source] = shape(feature["geometry"])
        # Published complete or not at all: the fast path above reads _geoms
        # without the lock, so filling it feature-by-feature would let a
        # concurrent caller see {"us"} mid-parse, skip the load, and silently
        # misclassify a Canadian point as unsupported. dict.update runs under
        # one GIL hold, so readers see the border set whole or empty.
        _geoms.update(loaded)


def warm_borders() -> None:
    """Parse the borders now, so no request pays for it.

    Otherwise the first classify_region() call does it — ~1.5s of synchronous
    JSON + shapely work on a 5 MB file, on the event loop, inside a request
    handler, stalling every other request on that worker once per process.
    Call this from application startup, where blocking costs nothing.

    Idempotent, and not required: classify_region() still loads on demand if
    this was never called (tests, scripts, a bare import).
    """
    _load_borders()


def classify_region(lat: float, lon: float) -> str | None:
    """Return "us", "ca", "au", or None if the point falls in none of them."""
    _load_borders()
    point = Point(lon, lat)  # GeoJSON order is (lon, lat)
    for source, geom in _geoms.items():
        if geom.covers(point):  # covers() includes boundary points; contains() excludes them
            return source
    return None
