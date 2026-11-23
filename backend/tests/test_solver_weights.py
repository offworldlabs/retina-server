"""Solver weights from node performance: which measurements get one, and how much."""

import asyncio

import pytest
from sqlalchemy import insert

from config import constants
from core.nodes import NodeConfig
from services import node_sites, performance_fit, performance_store, publication, solver_weights
from tests.ownership_helpers import register_node_row

_GUESS = {"lat": 40.0, "lon": -100.0, "alt_km": 9.0}
# Every receiver below sees the guess 20-40 km away, at 29.5 kft.
_CELL = ("20-40km", "20-45kft")


def _perf_doc(
    node_id, system_constant_db, threshold_db, eirp_dbm, rows, *, cell=_CELL, bound="measured", rank_status=None
):
    """A published file; ``rows`` maps a class to its (Pd, evidence) in ``cell``, every other cell empty."""
    ranges, alts = performance_fit.RANGE_LABELS, performance_fit.ALT_LABELS
    pd = {name: {r: dict.fromkeys(alts) for r in ranges} for name in rows}
    evidence = {name: {r: dict.fromkeys(alts, 0) for r in ranges} for name in rows}
    for name, (share, n) in rows.items():
        pd[name][cell[0]][cell[1]], evidence[name][cell[0]][cell[1]] = share, n
    return {
        "schema": 1,
        "node_id": node_id,
        "window": ["2026-01-01", "2026-01-28"],
        "system_constant_db": system_constant_db,
        "system_constant_bound": bound,
        "threshold_db": threshold_db,
        "pd_by_class_range_alt": pd,
        "evidence_by_class_range_alt": evidence,
        "tower": {"callsign": "TEST-TV", "eirp_dbm": eirp_dbm, "status": "matched"},
        "rank_status": rank_status,
    }


def _node_cfg(rx, tx, fc_hz=195e6, fs_hz=2e6):
    """A solver config as node_pipeline builds it, from the receiver's and the transmitter's (lat, lon, alt_ft): no CPI."""
    ends = dict(zip(("rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"), (*rx, *tx)))
    wire = {**ends, "fc_hz": fc_hz, "fs_hz": fs_hz, "beam_width_deg": None, "beam_azimuth_deg": None}
    return {**wire, "max_range_km": 60.0, "doppler_min": -300, "doppler_max": 300, "min_doppler": 15}


def _solver_input(*node_snr):
    measurements = [
        {"node_id": nid, "track_id": f"trk-{nid}", "delay_us": 40.0, "doppler_hz": -20.0, "snr": snr, "t_s": 0.0}
        for nid, snr in node_snr
    ]
    return {"initial_guess": dict(_GUESS), "measurements": measurements, "n_nodes": len(node_snr), "timestamp_ms": 0}


_CONFIGS = {
    "node-a": _node_cfg((40.25, -100.0, 1000.0), (40.3, -99.4, 1500.0)),
    "node-b": _node_cfg((39.75, -100.1, 600.0), (39.6, -100.5, 1200.0), fc_hz=200e6, fs_hz=2.4e6),
}
_CPI_S = {"node-a": 0.5, "node-b": 0.25}
_FILES = {
    "node-a": _perf_doc(
        "node-a", 98.0, 6.0, 70.0, {"narrowbody": (0.8, 100), "widebody": (0.75, 40), "light_GA": (None, 12)}
    ),
    "node-b": _perf_doc(
        "node-b", 92.0, 8.0, 68.0, {"narrowbody": (0.6, 50), "unknown": (0.4, 35), "turboprop": (None, 20)}
    ),
}
# K = 20log(c/fc) + 10log(fs*cpi) - 30log(4pi) - 20log(R_t) - 20log(R_r), at the guess, cpi from _CPI_S:
# node-a: 30.7594 - 20log(61658 m) - 20log(29109 m) = -154.3208 dB, SNR_exp = K + 70 + 98 = 13.6792 dB (over T_n 6);
#   Pd = (80 + 30 + 1) / (100 + 40 + 2) = 111/142, light_GA's null left out; raw = 1.36792 * sqrt(111/142) = 1.209420
# node-b: 28.3210 - 20log(62339 m) - 20log(30375 m) = -157.2246 dB, SNR_exp = 2.7754 dB, floored at T_n 8;
#   Pd = (30 + 14 + 1) / (50 + 35 + 2) = 45/87; raw = 0.8 * sqrt(45/87) = 0.575356
# Today's 1.8 + 1.2 = 3.0 re-split: w = raw * 3.0 / 1.784776.
_WEIGHTS = [2.0328939330179203, 0.96710606698208]


def _config_rows(request, cpi_by_node, configs=_CONFIGS):
    """Give each node an active config row: its solver config's geometry with this CPI, as node_sites reads it."""
    request.addfinalizer(node_sites._reset_for_tests)
    pipeline_defaults = ("doppler_min", "doppler_max", "min_doppler")
    for node_id, cpi_s in cpi_by_node.items():
        asyncio.run(register_node_row(node_id))
        row = {key: value for key, value in configs[node_id].items() if key not in pipeline_defaults}
        row |= {"cpi_s": cpi_s, "delay_tolerance_us": 2.0, "doppler_tolerance_hz": 5.0}
        with publication._sync_engine().begin() as conn:
            conn.execute(insert(NodeConfig).values(node_id=node_id, version=1, **row))
    node_sites._reset_for_tests()


def test_figured_nodes_share_todays_emphasis_by_model_snr_and_pooled_pd(request):
    _config_rows(request, _CPI_S)
    s_in = _solver_input(("node-a", 18.0), ("node-b", 12.0))
    assert solver_weights.measurement_weights(s_in, _CONFIGS, _FILES) == pytest.approx(_WEIGHTS, rel=1e-9)


def test_k_takes_the_cpi_from_the_nodes_active_config_row(monkeypatch, request):
    """Without a row a node keeps today's weight, even when its solver config carries a CPI."""
    monkeypatch.setattr(constants, "SOLVER_PERFORMANCE_WEIGHTS", True)
    monkeypatch.setattr(performance_store, "_files", performance_store._Files(dict(_FILES), {}))
    s_in = _solver_input(("node-a", 18.0), ("node-b", 12.0))
    _config_rows(request, {"node-a": 0.5})
    # node-b has no row, which leaves node-a figured alone: nothing is attached.
    assert solver_weights.with_weights(s_in, _CONFIGS) is s_in
    assert solver_weights.with_weights(s_in, {nid: {**cfg, "cpi_s": 0.25} for nid, cfg in _CONFIGS.items()}) is s_in


def test_nodes_without_usable_figures_keep_todays_weight(request):
    others = ("node-c", "node-d", "node-e", "node-f")
    configs = {**_CONFIGS, **dict.fromkeys(others, _CONFIGS["node-a"])}
    _config_rows(request, {**_CPI_S, **dict.fromkeys(others, 0.5)}, configs)
    rows = {"narrowbody": (0.8, 100)}
    files = {
        **_FILES,
        "node-d": _perf_doc("node-d", 98.0, 6.0, 70.0, rows, bound="at_least"),
        "node-e": _perf_doc("node-e", 98.0, 6.0, 70.0, rows, rank_status="config_suspect"),
        "node-f": _perf_doc("node-f", 98.0, 6.0, 70.0, rows, cell=("40-60km", "20-45kft")),
    }
    s_in = _solver_input(("node-a", 18.0), *((nid, 15.0) for nid in others), ("node-b", 12.0))
    expected = [_WEIGHTS[0], None, None, None, None, _WEIGHTS[1]]
    assert solver_weights.measurement_weights(s_in, configs, files) == pytest.approx(expected, rel=1e-9)


def test_a_lone_figured_measurement_attaches_nothing(monkeypatch, request):
    _config_rows(request, _CPI_S)
    monkeypatch.setattr(constants, "SOLVER_PERFORMANCE_WEIGHTS", True)
    monkeypatch.setattr(performance_store, "_files", performance_store._Files(dict(_FILES), {}))
    s_in = _solver_input(("node-a", 18.0), ("node-c", 15.0))
    assert solver_weights.measurement_weights(s_in, _CONFIGS, _FILES) == [None, None]
    assert solver_weights.with_weights(s_in, _CONFIGS) is s_in
