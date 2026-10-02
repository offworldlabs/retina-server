"""How well a set of bistatic nodes can place a target — the geometry alone.

A bistatic delay puts the target on an ellipsoid with foci at the node's
transmitter and receiver.  At a pinned altitude that is a curve on the map,
and its normal there is the horizontal part of the gradient of the bistatic
range: the sum of the two unit vectors from transmitter to target and from
receiver to target.  A fix is where the nodes' curves cross, and how sharply
they cross is all that separates a position from a smear along one curve.

For two nodes that is one crossing angle.  It is NOT a property of the
receivers: two nodes on one roof share the receiver term, so their curves
cross at half the angle their two TRANSMITTERS subtend at the target, exactly
as two nodes sharing a transmitter cross at half the angle their receivers
subtend.  Co-sited receivers on well separated towers can be a good pair, and
receivers far apart can be a bad one.  On test, 2026-10-02, the one real n=2
pair (receivers co-sited, towers 18 km apart, 981 known-lane solves) was 62%
ghosts below a 2 degree crossing and 4% between 10 and 20 degrees.

A crossing angle does not extend past two nodes, so this module reports what
it is the n=2 case of: the dilution of precision of the delay loci.  With
``g_i`` each node's horizontal gradient,

    DOP = sqrt(trace((sum_i g_i g_i^T)^-1))

is the horizontal position error per unit of independent bistatic-range error
on each node, at any node count and whatever the nodes share.  Three receivers
on one roof listening to one tower farm are as degenerate as two; three on one
roof with towers around the compass are a good fix; the number says which.  At
n=2 with gradients of magnitude g1, g2 crossing at angle theta it reduces to
``sqrt(g1^2 + g2^2) / (g1 * g2 * sin(theta))``.

Reported in km of position per microsecond of delay, the unit the delay
residual gates beside it already speak.  The same history bins by it without
regard to n: at 0.5-1 km/us the fleet's n=2, n=3 and n>=4 solves had median
errors of 0.10, 0.13 and 0.22 km.
"""

import math
from collections.abc import Iterable

from config.constants import C_KM_US, FT_TO_M

_KM_PER_DEG = math.pi * 6371.0 / 180.0

# det(G) below this fraction of trace(G)^2 is a set of parallel loci: for two
# unit gradients the ratio is sin^2(theta)/4, so this is a crossing of about
# 4e-3 degrees, far below anything a threshold on the result could care about.
_SINGULAR_RATIO = 1e-9


def _unit_to_target(east_km: float, north_km: float, up_km: float) -> tuple[float, float] | None:
    """Horizontal part of the unit vector from a focus to the target, the
    focus given by its offset FROM the target.  None when they coincide."""
    dist = math.sqrt(east_km * east_km + north_km * north_km + up_km * up_km)
    if dist < 1e-9:
        return None
    return (-east_km / dist, -north_km / dist)


def _gradient(lat: float, lon: float, alt_km: float, cfg: dict) -> tuple[float, float] | None:
    """One node's horizontal bistatic-range gradient at the target, or None
    when the config cannot place both of its foci."""
    cos_lat = math.cos(math.radians(lat))
    parts = []
    for prefix in ("tx", "rx"):
        f_lat, f_lon = cfg.get(f"{prefix}_lat"), cfg.get(f"{prefix}_lon")
        if not isinstance(f_lat, (int, float)) or not isinstance(f_lon, (int, float)):
            return None
        f_alt_ft = cfg.get(f"{prefix}_alt_ft")
        f_alt_km = float(f_alt_ft) * FT_TO_M / 1000.0 if isinstance(f_alt_ft, (int, float)) else 0.0
        unit = _unit_to_target(
            (f_lon - lon) * cos_lat * _KM_PER_DEG,
            (f_lat - lat) * _KM_PER_DEG,
            f_alt_km - alt_km,
        )
        if unit is None:
            return None
        parts.append(unit)
    return (parts[0][0] + parts[1][0], parts[0][1] + parts[1][1])


def delay_dop_km_per_us(lat: float, lon: float, alt_km: float, node_cfgs: Iterable[dict | None]) -> float | None:
    """Horizontal position error, in km, per microsecond of independent delay
    error on each of these nodes, for a target at this position and altitude.

    ``math.inf`` when the loci are parallel there (every node shares both
    foci, or the target sits where the gradients line up).  None when the
    answer is not known: fewer than two nodes, or a node whose config is
    missing or does not place its transmitter and receiver.  Callers must not
    read None as bad geometry.

    Local tangent plane around the target.  The curvature it ignores bends a
    100 km line of sight by under half a degree, against thresholds that sit
    at several degrees of crossing.
    """
    a = b = c = 0.0
    n = 0
    for cfg in node_cfgs:
        grad = _gradient(lat, lon, alt_km, cfg) if isinstance(cfg, dict) else None
        if grad is None:
            return None
        a += grad[0] * grad[0]
        b += grad[0] * grad[1]
        c += grad[1] * grad[1]
        n += 1
    if n < 2:
        return None
    trace = a + c
    det = a * c - b * b
    if det <= _SINGULAR_RATIO * trace * trace:
        return math.inf
    return math.sqrt(trace / det) * C_KM_US
