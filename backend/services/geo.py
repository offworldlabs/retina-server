"""Geodesy for the backend: one implementation, re-exported.

The primitives live in ``retina_analytics.constants`` because that is where the
canonical spherical haversine and bearing already were, and because the only
other consumer of them is that same library.  This module re-exports them so
backend code has one import site, and adds the adapters that read a *node config
dict* — the shape backend code actually has in hand, and the shape that was
independently re-derived in four places before this existed.

What was here before: six haversines across three different formulas (two of
them flat-earth, answering 0.175% short), six bearings, five bistatic-delay
computations, and roughly fourteen inline dead-reckoning sites using four
different values for kilometres-per-degree.  A position dead-reckoned in one
module and compared against a gate in another disagreed by up to 0.3%.
"""

import math

from retina_analytics.constants import (  # noqa: F401  (re-exported surface)
    C_KM_US,
    KM_PER_DEG_LAT,
    M_PER_DEG_LAT,
    YAGI_BEAM_WIDTH_DEG,
    YAGI_MAX_RANGE_KM,
    bearing_deg,
    bistatic_delay_us,
    bistatic_differential_km,
    bistatic_max_radius_km,
    bistatic_range_limit_km,
    enu_km,
    haversine_km,
    km_per_deg_lon,
    offset_latlon,
    offset_latlon_m,
    point_in_beam,
    resolve_beam_azimuth_deg,
)
from retina_analytics.constants import (
    R_EARTH as R_EARTH_KM,
)

__all__ = [
    "C_KM_US",
    "KM_PER_DEG_LAT",
    "M_PER_DEG_LAT",
    "R_EARTH_KM",
    "YAGI_BEAM_WIDTH_DEG",
    "YAGI_MAX_RANGE_KM",
    "bearing_deg",
    "bistatic_delay_us",
    "bistatic_differential_km",
    "bistatic_max_radius_km",
    "bistatic_range_limit_km",
    "enu_km",
    "haversine_km",
    "km_per_deg_lon",
    "offset_latlon",
    "offset_latlon_m",
    "point_in_beam",
    "resolve_beam_azimuth_deg",
    "node_beam_params",
    "in_node_beam",
    "valid_latlon",
    "dr_offset_m",
]

# Below this the coordinated-turn arc and the straight line are the same
# curve to within floating-point noise, and the 1/omega radius blows up: a
# guard on the division, NOT the "is this aircraft turning" question.  That
# one is the turn-rate estimator's (services/track_filter.turn_rate applies a
# 0.2 deg/s deadband before it ever reaches here).
_DR_MIN_OMEGA_RAD_S = 1e-9


def dr_offset_m(
    vel_east_ms: float,
    vel_north_ms: float,
    omega_rad_s: float | None,
    dt_s: float,
) -> tuple[float, float]:
    """(east_m, north_m) travelled in ``dt_s`` at this velocity and turn rate.

    THE dead-reckoning displacement for the whole backend: the solver's key
    decision, its supersession test and the feed's display DR all call this,
    because an entry judged in one place and drawn in another must be moved
    by the same arithmetic (see solver._entry_dr_velocity for what happens
    when they disagree).

    ``omega_rad_s`` None or 0.0 gives the constant-velocity straight line,
    ``east = v_e * dt``.  Non-zero gives the coordinated-turn arc: the COMPASS
    heading ``theta = atan2(v_e, v_n)`` rotating at a constant ``omega``
    (positive = turning right/clockwise, the sense track_filter.turn_rate
    reports) with the speed ``|v|`` held constant, so with radius
    ``r = |v| / omega``

        east  = r * (cos(theta) - cos(theta + omega*dt))
        north = r * (sin(theta + omega*dt) - sin(theta))

    i.e. the exact integral of ``(v*sin(theta+omega*s), v*cos(theta+omega*s))``
    over s in [0, dt] — not a small-angle approximation, and reducing to the
    straight line as omega -> 0.  (The textbook form of these two, written for
    a heading measured anticlockwise FROM EAST, has the sines and cosines the
    other way round; it is the same arc, and the compass convention is used
    here so that a positive omega is a right turn everywhere in this codebase.)

    ``dt_s`` may be NEGATIVE: the formulas walk the same arc backwards, to
    where the aircraft was ``|dt|`` seconds before this fix.  Both callers in
    solver.py need that — a solve can arrive with an epoch older than the
    entry it is matched against (see _MN_ASSOC_MAX_NEG_DT_S).

    Why the arc matters: a straight-line prediction of a 3 deg/s turn at
    250 m/s is 0.9 km wrong after 12 s and 5.5 km wrong after 30 s, against a
    6-12 km association gate whose p90 occupancy is already 5.4 km.
    """
    v_e = float(vel_east_ms or 0.0)
    v_n = float(vel_north_ms or 0.0)
    dt = float(dt_s)
    omega = float(omega_rad_s or 0.0)
    if not math.isfinite(omega) or abs(omega) < _DR_MIN_OMEGA_RAD_S:
        return v_e * dt, v_n * dt
    speed = math.hypot(v_e, v_n)
    if speed == 0.0:
        return 0.0, 0.0
    theta = math.atan2(v_e, v_n)
    theta_end = theta + omega * dt
    radius = speed / omega
    return (
        radius * (math.cos(theta) - math.cos(theta_end)),
        radius * (math.sin(theta_end) - math.sin(theta)),
    )


def valid_latlon(lat, lon) -> bool:
    """True when both coordinates are usable.

    None (absent) and the (0, 0) broken-config sentinel are invalid, but a
    legitimate 0.0 on a single axis is not.  The widespread
    ``not lat or not lon`` form this replaces silently dropped anything on
    the equator or the prime meridian.
    """
    return lat is not None and lon is not None and (bool(lat) or bool(lon))


def node_beam_params(node_cfg: dict) -> dict:
    """Pull a node's detection-area geometry out of its config dict.

    One place where the defaults live, because the callers disagreed:
    ``solver.py`` read ``beam_width_deg or 41`` while ``manager.py`` read
    ``.get("beam_width_deg", YAGI_BEAM_WIDTH_DEG)``.  Those differ for a config
    carrying ``beam_width_deg: 0`` — the first gives 41°, the second gives a
    node that can see nothing.  The forgiving form wins: a zero beam width is
    always a missing value, never a real antenna.

    ``beam_azimuth_deg`` is None when the node declares no aim *and* has no TX
    to derive broadside from; callers should then skip the bearing test rather
    than invent a direction.
    """
    rx_lat = float(node_cfg.get("rx_lat") or node_cfg.get("lat") or 0)
    rx_lon = float(node_cfg.get("rx_lon") or node_cfg.get("lon") or 0)
    tx_lat = node_cfg.get("tx_lat")
    tx_lon = node_cfg.get("tx_lon")

    # A usable explicit azimuth wins outright. A present-but-unusable one
    # (NaN/inf/non-numeric) must resolve exactly as if it were absent, so its
    # usability is checked locally rather than by handing
    # resolve_beam_azimuth_deg a transmitter position: that helper always
    # returns a bearing toward whichever tx it is given, and a fabricated
    # (0.0, 0.0) stand-in for "no real tx here" silently pointed the fallback
    # at Null Island instead of this node's actual baseline (or, correctly,
    # nowhere at all when there is no baseline to point along).
    explicit_az = node_cfg.get("beam_azimuth_deg")
    try:
        explicit_az = float(explicit_az) if explicit_az is not None else None
    except (TypeError, ValueError):
        explicit_az = None
    if explicit_az is not None and not math.isfinite(explicit_az):
        explicit_az = None

    if explicit_az is not None:
        beam_az = explicit_az
    elif tx_lat and tx_lon:
        beam_az = (bearing_deg(rx_lat, rx_lon, float(tx_lat), float(tx_lon)) + 90.0) % 360.0
    else:
        beam_az = None

    # bool(float("nan")) is True, so a bare `or YAGI_BEAM_WIDTH_DEG` truthiness
    # fallback (kept above for the null/zero cases it already handles
    # correctly) lets a NaN or infinite width straight through. Every
    # downstream comparison against it is then False (NaN comparisons always
    # are), which fails the beam-membership test *open* instead of closed.
    beam_width_deg = float(node_cfg.get("beam_width_deg") or YAGI_BEAM_WIDTH_DEG)
    if not math.isfinite(beam_width_deg):
        beam_width_deg = YAGI_BEAM_WIDTH_DEG

    max_range_km = float(node_cfg.get("max_range_km") or YAGI_MAX_RANGE_KM)
    if not math.isfinite(max_range_km):
        max_range_km = YAGI_MAX_RANGE_KM

    # Cast to float rather than passing the config value through raw: a
    # numeric string ("0") is truthy where the equivalent float (0.0) is
    # falsy, which flips which branch point_in_beam takes. Preserve None
    # (absent/not configured); treat a non-finite result the same as
    # None -- "no limit configured", not a limit of zero.
    max_bistatic_range_km = node_cfg.get("max_bistatic_range_km")
    if max_bistatic_range_km is not None:
        max_bistatic_range_km = float(max_bistatic_range_km)
        if not math.isfinite(max_bistatic_range_km):
            max_bistatic_range_km = None

    return {
        "rx_lat": rx_lat,
        "rx_lon": rx_lon,
        "tx_lat": float(tx_lat) if tx_lat else None,
        "tx_lon": float(tx_lon) if tx_lon else None,
        "beam_azimuth_deg": beam_az,
        "beam_width_deg": beam_width_deg,
        "max_range_km": max_range_km,
        "max_bistatic_range_km": max_bistatic_range_km,
    }


def in_node_beam(lat: float, lon: float, node_cfg: dict) -> bool:
    """Is (lat, lon) inside this node's detection area?"""
    return point_in_beam(lat, lon, **node_beam_params(node_cfg))
