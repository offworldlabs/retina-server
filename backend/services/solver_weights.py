"""Solver weights from each node's measured performance.

A measurement weighs ``snr_weight(snr)`` today.  For a node with figures (a measured
system constant S_n, its SNR floor T_n, its tower's EIRP, and a Pd where the solve
looks), the weight reads instead how well the node should see the solve's initial
guess: the SNR the performance fit models there, ``K + EIRP + S_n`` with K the fit's
own geometry term on the node's CPI as its active config row holds it (the column the
fit's K reads; the solver's node config never carries one), floored at T_n because
the detection exists, through the same emphasis curve, times the square root of the
node's Pd in the guess's range and altitude cell, every class pooled since the solver
doesn't know what the target is.

Those factors only re-split the emphasis: over the figured measurements of one call
they are scaled to today's total, so the Huber scale and the balance against nodes
without figures stay where they were.  A node without figures keeps today's weight,
as does every measurement when fewer than two are figured or the setting is off.
The weights are computed in the app process, where the performance store lives, and
travel to the solver on the measurement dicts.
"""

import logging
import math
from collections.abc import Mapping
from numbers import Integral, Real
from typing import Any

import numpy as np
from retina_geolocator.multinode_solver import snr_weight

from config import constants
from services import node_sites, performance_data, performance_fit, performance_store

logger = logging.getLogger(__name__)

_Point = tuple[float, float, float]


def _number(value: Any) -> float | None:
    """A finite real number as a float, or None for anything else; a bool is not a number."""
    if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _node_id(measurement: Any) -> str | None:
    node_id = measurement.get("node_id") if isinstance(measurement, Mapping) else None
    return node_id if isinstance(node_id, str) else None


def _point(solver_input: Mapping[str, Any]) -> _Point | None:
    """The initial guess as (lat, lon, alt_km), or None when any of them isn't a number."""
    guess = solver_input.get("initial_guess")
    if not isinstance(guess, Mapping):
        return None
    lat, lon, alt_km = (_number(guess.get(key)) for key in ("lat", "lon", "alt_km"))
    if lat is None or lon is None or alt_km is None:
        return None
    return lat, lon, alt_km


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


def _pooled_pd(doc: Mapping[str, Any], cell: tuple[str, str]) -> float | None:
    """Pd in one cell over every class row with a figure there, by Laplace's rule; None under the evidence floor."""
    pd_rows, evidence_rows = doc.get("pd_by_class_range_alt"), doc.get("evidence_by_class_range_alt")
    if not isinstance(pd_rows, Mapping) or not isinstance(evidence_rows, Mapping):
        return None
    detected = opportunities = 0
    for name, table in pd_rows.items():
        pd, n = _number(_at(table, *cell)), _at(evidence_rows.get(name), *cell)
        # A published Pd is a share in [0, 1] of n >= 0 opportunities; anything else carries no count.
        if pd is None or not 0.0 <= pd <= 1.0 or not isinstance(n, Integral) or isinstance(n, bool) or n < 0:
            continue
        detected += round(pd * int(n))
        opportunities += int(n)
    if opportunities < performance_fit.EVIDENCE_FLOOR:
        return None
    return (detected + 1) / (opportunities + 2)


def _node_factor(doc: Any, config: Any, node_id: str, point: _Point) -> float | None:
    """The node's raw weight at the point, or None when it has no figures there."""
    if not performance_store._usable(doc, node_id):
        return None
    if doc.get("system_constant_bound") != "measured" or doc.get("rank_status") == "config_suspect":
        return None
    system_constant, threshold = _number(doc.get("system_constant_db")), _number(doc.get("threshold_db"))
    eirp = _number(doc["tower"].get("eirp_dbm"))
    if system_constant is None or threshold is None or eirp is None or not isinstance(config, Mapping):
        return None
    # Read only for a node with figures, so a solve without any never touches the registry.
    cpi_s = _number(node_sites.tx_and_cpi(node_id)[1])
    if cpi_s is None or cpi_s <= 0:
        return None
    try:
        site = performance_data._site({**config, "cpi_s": cpi_s})
    except (TypeError, ValueError, ArithmeticError):
        return None
    if site is None:
        return None
    lat, lon, alt_km = point
    look = site.look(lat, lon, alt_km * 1000.0)
    known_db = float(site.known_db(look.rt_m, look.rr_m))
    if not math.isfinite(known_db):
        return None
    cell = _grid_cell(float(look.rr_m) / 1000.0, alt_km * 1000.0 / performance_data._FT_TO_M / 1000.0)
    pd = None if cell is None else _pooled_pd(doc, cell)
    if pd is None:
        return None
    snr_expected = known_db + eirp + system_constant
    return snr_weight(max(snr_expected, threshold)) * math.sqrt(pd)


def measurement_weights(
    solver_input: Mapping[str, Any],
    node_configs: Mapping[str, Any],
    performance: Mapping[str, Any],
) -> list[float | None]:
    """One weight per measurement, in order; None where the measurement keeps today's.

    ``performance`` maps a node id to its published file.  A node's CPI is read from its
    active config row through ``node_sites``, never from ``node_configs``.
    """
    measurements = solver_input.get("measurements") or []
    weights: list[float | None] = [None] * len(measurements)
    point = _point(solver_input)
    if point is None:
        return weights
    factors: dict[str, float | None] = {}
    figured: list[tuple[int, float, float]] = []
    for index, measurement in enumerate(measurements):
        node_id = _node_id(measurement)
        if node_id is None:
            continue
        snr = measurement.get("snr", 0)  # as the solver reads it
        if _number(snr) is None:
            continue
        if node_id not in factors:
            factors[node_id] = _node_factor(performance.get(node_id), node_configs.get(node_id), node_id, point)
        factor = factors[node_id]
        if factor is not None:
            figured.append((index, factor, snr_weight(snr)))
    # One figured measurement alone would only get today's weight back, re-rounded.
    if len(figured) < 2:
        return weights
    total_today = sum(today for _, _, today in figured)
    total_raw = sum(raw for _, raw, _ in figured)
    for index, raw, _ in figured:
        weights[index] = raw * total_today / total_raw
    return weights


def with_weights(solver_input: dict[str, Any], node_configs: Mapping[str, Any]) -> dict[str, Any]:
    """``solver_input`` with a ``weight`` on each figured measurement, or the same object when none is.

    Never raises: a failure here must not block a solve the unweighted input could still produce.
    """
    if not constants.SOLVER_PERFORMANCE_WEIGHTS:
        return solver_input
    try:
        measurements = solver_input.get("measurements") or []
        node_ids = {node_id for node_id in map(_node_id, measurements) if node_id is not None}
        performance = {node_id: doc for node_id in node_ids if (doc := performance_store.get(node_id)) is not None}
        if not performance:
            return solver_input
        weights = measurement_weights(solver_input, node_configs, performance)
        if all(weight is None for weight in weights):
            return solver_input
        weighted = [m if w is None else {**m, "weight": w} for m, w in zip(measurements, weights, strict=True)]
        return {**solver_input, "measurements": weighted}
    except Exception:
        logger.exception("Solver performance weights failed, solving on the SNR weights")
        return solver_input
