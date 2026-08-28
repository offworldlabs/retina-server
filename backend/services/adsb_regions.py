"""Query regions for external ADS-B truth.

Nodes are grouped on a fixed lattice and each group's query geometry comes from
its own members.  Grouping is what bounds the request count by geographic
spread rather than by headcount: a node joining an existing metro costs nothing.
"""

import logging
import math
from dataclasses import dataclass

from config.constants import (
    ADSB_CELL_SPACING_KM,
    ADSB_MAX_REGIONS_PER_CYCLE,
    ADSB_NODE_RANGE_MARGIN_KM,
)
from services.geo import KM_PER_DEG_LAT, R_EARTH_KM, haversine_km, km_per_deg_lon

log = logging.getLogger(__name__)

# Defines the lattice; it does not measure anything.  Every cell boundary and
# cache key moves if it moves, so it stays put and stays private.  Any actual
# distance uses services.geo.KM_PER_DEG_LAT (111.1949), which differs.
_KM_PER_DEG_LAT_NOMINAL = 111.32

_LAT_STEP_DEG = ADSB_CELL_SPACING_KM / _KM_PER_DEG_LAT_NOMINAL

# km_per_deg_lon carries the one cosine-at-a-pole floor in the codebase; this
# rescales its real-distance result back onto the nominal convention above
# rather than restating that floor here.
_NOMINAL_OVER_REAL = _KM_PER_DEG_LAT_NOMINAL / KM_PER_DEG_LAT


def _lon_step_deg(row: int) -> float:
    """Column width for a lattice row, in degrees.

    Divided by cos(row centre latitude) so a column holds its width in km
    rather than in degrees.  The cosine floor (via km_per_deg_lon) and the
    360 clamp together collapse a polar row to a single column instead of
    dividing by zero.
    """
    centre_lat = (row + 0.5) * _LAT_STEP_DEG - 90.0
    return min(360.0, ADSB_CELL_SPACING_KM / (km_per_deg_lon(centre_lat) * _NOMINAL_OVER_REAL))


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    """Lattice cell containing a position, as (row, col).

    Indices are derived from position and constants alone, so a cell id is
    stable across restarts and is safe as a cache key.
    """
    row = int(math.floor((lat + 90.0) / _LAT_STEP_DEG))
    col = int(math.floor((lon + 180.0) / _lon_step_deg(row)))
    return row, col


_KM_PER_NM = 1.852
_MAX_RADIUS_NM = 250  # adsb.lol enforces this in its schema
_MARGIN_SURPLUS = 1.0001  # so both padding solves clear margin_km rather than landing a hair under it

# Ground distance along a meridian is R_EARTH_KM times the angle in radians, so
# the latitude padding is a direct conversion and is the same at every latitude.
# Both axes of a box must be derived from this one spherical basis, the one
# haversine_km itself uses; _KM_PER_DEG_LAT_NOMINAL defines the lattice and
# measures nothing, so it is never the divisor here.  geo.offset_latlon divides
# by KM_PER_DEG_LAT, which is this same sphere and agrees to the bit, but its
# longitude half is the per-degree approximation _dlon_for_margin exists to
# avoid, so the basis is written out here for both axes rather than for one.
_DLAT_FOR_MARGIN = math.degrees(ADSB_NODE_RANGE_MARGIN_KM * _MARGIN_SURPLUS / R_EARTH_KM)


def _dlon_for_margin(lat: float, margin_km: float) -> float:
    """Longitude span, in degrees, covering *margin_km* of true ground distance at *lat*.

    Two points at the same latitude, dlon degrees apart, sit
    2*R_EARTH_KM*asin(cos(lat)*sin(radians(dlon)/2)) apart on the sphere --
    haversine_km with dlat=0, the same formula the coverage invariant is
    checked against.  This is that equation solved for dlon directly, so the
    result is exact rather than approximated.

    A parallel's own circumference shrinks toward zero approaching a pole, so
    above some latitude even the widest possible span, 180 degrees each way,
    falls short of *margin_km*; the equation then has no solution and the
    span is capped at 180 -- covering every longitude is the honest answer
    once nothing wider would help.
    """
    cos_lat = math.cos(math.radians(lat))
    if cos_lat < 1e-12:
        return 180.0
    ratio = math.sin(margin_km * _MARGIN_SURPLUS / (2 * R_EARTH_KM)) / cos_lat
    return 180.0 if ratio >= 1.0 else math.degrees(2 * math.asin(ratio))


@dataclass(frozen=True)
class Box:
    """One OpenSky bounding box, in the coordinate range a request must stay inside."""

    lamin: float
    lamax: float
    lomin: float
    lomax: float

    def sq_deg(self) -> float:
        return (self.lamax - self.lamin) * (self.lomax - self.lomin)

    def opensky_credits(self) -> int:
        """OpenSky charges per request by bounding-box area."""
        area = self.sq_deg()
        if area <= 25.0:
            return 1
        if area <= 100.0:
            return 2
        if area <= 400.0:
            return 3
        return 4


def _credits(boxes: tuple[Box, ...]) -> int:
    """What a region's boxes cost, one request apiece."""
    return sum(box.opensky_credits() for box in boxes)


@dataclass(frozen=True)
class Region:
    """One query region: a lattice cell's nodes plus the geometry covering them.

    Provider-agnostic on purpose.  adsb.lol takes a point and a radius,
    OpenSky takes bounding boxes, and each must cover every member plus its
    detection range.  Covering that ground is the whole of what they share:
    a band straddling the antimeridian is two boxes and a padding that crosses
    a pole is the whole parallel, while the radius stays tight either way, so
    sq_deg() and radius_nm are not in proportion.
    """

    name: str
    row: int
    col: int
    lat: float
    lon: float
    radius_nm: int
    boxes: tuple[Box, ...]
    n_nodes: int

    def as_area(self) -> dict:
        """The `{name, lat, lon, radius_nm}` dict AdsbLolClient already takes."""
        return {"name": self.name, "lat": self.lat, "lon": self.lon, "radius_nm": self.radius_nm}

    def sq_deg(self) -> float:
        """Total area asked for, across every box."""
        return sum(box.sq_deg() for box in self.boxes)

    def opensky_credits(self) -> int:
        """Credits for a whole region: charged per request, so summed per box."""
        return _credits(self.boxes)


def _region_from_members(row: int, col: int, members: list[tuple[float, float]]) -> Region:
    """Geometry covering every member plus its detection range.

    Derived from the members rather than from the cell bounds: a cell is a
    worst-case container, and sizing every query to it would mean a 433 km
    radius where a real metro needs 150.  It is also what keeps each OpenSky
    box inside the 1-credit band.
    """
    name = f"r{row}c{col}"
    lats = [lat for lat, _ in members]
    lons = [lon for _, lon in members]
    centre_lat = (min(lats) + max(lats)) / 2.0
    centre_lon = (min(lons) + max(lons)) / 2.0

    reach_km = max(haversine_km(centre_lat, centre_lon, lat, lon) for lat, lon in members)
    raw_radius_nm = math.ceil((reach_km + ADSB_NODE_RANGE_MARGIN_KM) / _KM_PER_NM)
    if raw_radius_nm > _MAX_RADIUS_NM:
        # Should not happen at the 400 km cell spacing; if it does, the query
        # silently stops covering a member's detection range once clamped.
        log.warning(
            "region %s: radius %d nm exceeds the %d nm schema cap; clamping, coverage guarantee breached",
            name,
            raw_radius_nm,
            _MAX_RADIUS_NM,
        )
    radius_nm = min(_MAX_RADIUS_NM, raw_radius_nm)

    # cos(lat) is smallest, so the required dlon is largest, at whichever bbox
    # edge sits furthest from the equator.  Sizing dlon from the bbox midpoint
    # instead would under-cover that edge, since cos(lat) is not constant
    # across a cell's ~3.6 degree latitude band.
    extreme_lat = max((min(lats), max(lats)), key=abs)
    dlon = _dlon_for_margin(extreme_lat, ADSB_NODE_RANGE_MARGIN_KM)

    # Latitude cannot wrap, so a margin that would cross a pole just reaches it.
    lamin_raw = min(lats) - _DLAT_FOR_MARGIN
    lamax_raw = max(lats) + _DLAT_FOR_MARGIN
    lamin = max(-90.0, lamin_raw)
    lamax = min(90.0, lamax_raw)

    # Padding that crosses a pole comes down the far side at every longitude, so
    # this clamp firing must itself force the full sweep below.  A tight
    # longitude band beside a clamped latitude band excludes ground the member
    # genuinely reaches, and does so silently.
    crosses_pole = lamin_raw < -90.0 or lamax_raw > 90.0

    # Longitude wraps, so an out-of-range edge does not mean "past the map" the
    # way it does for latitude -- it means the padded band crosses the
    # antimeridian.  A box cannot express that, so the band is asked for as one
    # box each side of +-180; an out-of-range edge would be silently
    # misinterpreted instead.
    lomin_raw = min(lons) - dlon
    lomax_raw = max(lons) + dlon
    sweep = (Box(lamin, lamax, -180.0, 180.0),)  # the whole parallel, both branches below
    if crosses_pole or lomax_raw - lomin_raw >= 360.0:
        # Every longitude is genuinely reachable: over the pole, or because the
        # padding alone (dlon saturated at 180 for a pole-adjacent member) spans
        # the globe.  The whole parallel is then the honest request, and no
        # split of it covers less ground.
        boxes = sweep
    elif lomin_raw < -180.0 or lomax_raw > 180.0:
        # The piece against -180 and the piece against +180.  Their union is the
        # padded band exactly, so sweeping the parallel is a superset and stands
        # in only where it is strictly cheaper: two wide boxes can cost more
        # than one, but on equal credits the split wins, covering the same
        # members over less ground and so offering the matcher fewer irrelevant
        # candidates.
        if lomax_raw > 180.0:
            lower, upper = (-180.0, lomax_raw - 360.0), (lomin_raw, 180.0)
        else:
            lower, upper = (-180.0, lomax_raw), (lomin_raw + 360.0, 180.0)
        split = (Box(lamin, lamax, *lower), Box(lamin, lamax, *upper))
        boxes = sweep if _credits(sweep) < _credits(split) else split
    else:
        boxes = (Box(lamin, lamax, lomin_raw, lomax_raw),)

    return Region(
        name=name,
        row=row,
        col=col,
        lat=centre_lat,
        lon=centre_lon,
        radius_nm=radius_nm,
        boxes=boxes,
        n_nodes=len(members),
    )


def is_position_absent(lat, lon) -> bool:
    """Whether a coordinate pair means "no position was given" rather than a place.

    An absent coordinate defaults to 0, so the pair lands in the Gulf of Guinea,
    which no node and no aircraft occupies.  Only the exact pair reads as
    absence: the equator and the prime meridian are each perfectly good
    coordinates on their own.  This is the convention retina_analytics applies
    in _has_receiver_position, and every backend site must agree with it.

    A bool is never the sentinel even though `bool` is an `int` subclass and
    `False == 0.0`: a node reporting a boolean is sending malformed config,
    not declaring "no position", and `is_usable` is the predicate that must
    see it in order to count and log it as such.
    """
    if isinstance(lat, bool) or isinstance(lon, bool):
        return False
    return lat == 0.0 and lon == 0.0


def is_usable(lat, lon) -> bool:
    """Whether a value pair is a finite, in-range coordinate.

    Every caller that turns a pair into geometry or into a haversine distance
    reaches here unvalidated -- node config via a free-form dict that
    json.loads happily parses `NaN` into, a trust-score sample's
    self-reported position via a JSON body with no type checking -- so
    anything that is not a finite in-range coordinate is dropped rather than
    allowed to raise or to be measured as if it were a real place.  Paired
    with `is_position_absent`, which must run first: that sentinel is itself
    a pair this function accepts.
    """
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def regions_for_nodes(positions: list[tuple[float, float]]) -> list[Region]:
    """Query regions covering every positioned node.

    Ordered busiest first so the cap sheds the least-populated regions, and
    tie-broken on cell index so the selection is deterministic.
    """
    groups: dict[tuple[int, int], list[tuple[float, float]]] = {}
    unusable: list[tuple] = []
    for lat, lon in positions:
        if is_position_absent(lat, lon):
            continue
        if not is_usable(lat, lon):
            unusable.append((lat, lon))
            continue
        groups.setdefault(cell_of(lat, lon), []).append((lat, lon))

    if unusable:
        log.warning(
            "ADS-B regions: dropped %d unusable node position(s), e.g. %s",
            len(unusable),
            unusable[:3],
        )

    regions = [_region_from_members(row, col, members) for (row, col), members in groups.items()]
    regions.sort(key=lambda r: (-r.n_nodes, r.row, r.col))

    if len(regions) > ADSB_MAX_REGIONS_PER_CYCLE:
        dropped = regions[ADSB_MAX_REGIONS_PER_CYCLE:]
        log.warning(
            "ADS-B region cap: querying %d of %d regions, dropped %s",
            ADSB_MAX_REGIONS_PER_CYCLE,
            len(regions),
            ", ".join(f"{r.name} ({r.n_nodes} nodes)" for r in dropped),
        )
        regions = regions[:ADSB_MAX_REGIONS_PER_CYCLE]
    return regions
