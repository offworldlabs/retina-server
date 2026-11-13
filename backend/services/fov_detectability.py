"""Whether a node should still see an aircraft that has just left its tracker.

The disappearance detector turns such an aircraft into negative evidence against the
node's learned field of view, but only where the node was predicted to detect it.
That prediction used to be the learned range cut alone.  With the node's performance
file, inside that range cut the measured Pd for the aircraft's class at its slant
range and altitude decides, read against the 50 % line each class's
``range_50pct_km`` is drawn at, on a bearing the node can see.  Nothing is ever
recorded beyond the range cut: the learned field of view keeps a short queue of
disappearances per bearing and shrinks to just inside the nearest one, so an event
past the limit would push nearer ones out of that queue and could reopen the bearing
with no detection behind it.  Anything the Pd reading needs and lacks (a class, a
file, an altitude, a point on the grid, a figure in the cell) falls back to the range
cut exactly.  The reason comes back with the answer, and nothing here has a side
effect, so the same question can be asked again with the other setting.
"""

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from retina_analytics.empirical_coverage import FOV_CLOSED_EPSILON_KM, EmpiricalCoverageState

from config.aircraft_classes import CLASSES, class_for_type
from config.constants import is_num
from services import aircraft_db, performance_data

# A class's 50 % range is where its Pd crosses this, so at or above it the node should see it.
PD_DETECTABLE = 0.5

_FT_TO_KM = 0.0003048


def aircraft_class_for(hex_code: str) -> str | None:
    """The class of the hex's type, ``unknown`` for a type outside the table; None without a type."""
    db = aircraft_db.current()
    if db is None:
        return None
    icao_type = db.types_for([hex_code])[0]
    return None if icao_type is None else class_for_type(icao_type)


def _grid_cell(range_km: float, alt_kft: float) -> tuple[str, str] | None:
    """The table's range and altitude labels for a point, by the table builder's own bins."""
    range_edges, alt_edges = performance_data.GRID_RANGE_EDGES_KM, performance_data.GRID_ALT_EDGES_KFT
    r = int(performance_data._bins(np.array([range_km]), range_edges)[0])
    a = int(performance_data._bins(np.array([alt_kft]), alt_edges)[0])
    if r < 0 or a < 0:
        return None
    return f"{range_edges[r]}-{range_edges[r + 1]}km", f"{alt_edges[a]}-{alt_edges[a + 1]}kft"


def _at(table: Any, range_label: str, alt_label: str) -> Any:
    row = table.get(range_label) if isinstance(table, Mapping) else None
    return row.get(alt_label) if isinstance(row, Mapping) else None


def still_detectable(
    fov: EmpiricalCoverageState,
    performance: Mapping[str, Any] | None,
    aircraft_class: str | None,
    bearing_deg: float,
    ground_km: float,
    alt_ft: Any,
    *,
    use_pd: bool,
) -> tuple[bool, str]:
    """Whether the node should still detect the aircraft here, and why.

    ``performance`` is the node's performance file, ``alt_ft`` the raw ADS-B
    ``alt_baro``.  The slant range takes the receiver at height 0, which moves it
    by well under the width of a range bin.
    """
    range_cut = fov.contains(bearing_deg, ground_km)
    if not use_pd:
        return range_cut, "off"
    if aircraft_class not in CLASSES:
        return range_cut, "no_class"
    table = performance.get("pd_by_class_range_alt") if isinstance(performance, Mapping) else None
    if not isinstance(table, Mapping):
        return range_cut, "no_performance"
    if not is_num(alt_ft):
        return range_cut, "no_altitude"
    cell = _grid_cell(math.hypot(ground_km, alt_ft * _FT_TO_KM), alt_ft / 1000.0)
    if cell is None:
        return range_cut, "out_of_grid"
    pd = _at(table.get(aircraft_class), *cell)
    if not is_num(pd) or not 0.0 <= pd <= 1.0:
        return range_cut, "no_cell"
    return range_cut and fov.limit_km(bearing_deg) > FOV_CLOSED_EPSILON_KM and pd >= PD_DETECTABLE, "pd"
