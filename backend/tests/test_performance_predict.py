"""A node's system constant predicted from its descriptors, on fleets with known figures.

Every node has sigma 1.0 dB unless a test says otherwise.
"""

import json
from datetime import date

import numpy as np
import orjson
import pytest

from services import performance_predict as pp

TODAY = date(2026, 11, 11)
CLASS_OFFSETS = {"widebody": 0.77, "narrowbody": 0.0, "helicopter": -0.7}


def node(node_id, system_constant_db, *, sigma_db=1.0, threshold_db=None, **descriptors):
    return {
        "node_id": node_id,
        "system_constant_db": system_constant_db,
        "sigma_db": sigma_db,
        "threshold_db": threshold_db,
        "processing_gain_db": None,
        "window": ["2026-11-04", "2026-11-10"],
        "descriptors": descriptors,
    }


def test_generated_fleet_recovers_band_levels_and_tau():
    # VHF 78 dB, UHF 66 dB, tau 3 dB, sigma 0.5-1.5 dB, 25 nodes a band.
    rng = np.random.default_rng(20261111)
    rows = []
    for index in range(50):
        band = "VHF" if index % 2 else "UHF"
        sigma = float(rng.uniform(0.5, 1.5))
        measured = (78.0 if band == "VHF" else 66.0) + rng.normal(0.0, 3.0) + rng.normal(0.0, sigma)
        rows.append(node(f"ret0f{index:06x}", float(measured), sigma_db=sigma, band=band))

    calibration = pp.fit(rows)

    # About 3 standard errors: a band's mean over 25 nodes 0.64 dB (plus the 6 dB prior's 0.2 dB pull), tau 0.34 dB.
    assert pp.predict(calibration, {"band": "VHF"})[0] == pytest.approx(78.0, abs=2.0)
    assert pp.predict(calibration, {"band": "UHF"})[0] == pytest.approx(66.0, abs=2.0)
    assert calibration["tau_db"] == pytest.approx(3.0, abs=1.0)


def test_term_without_support_is_dropped_with_its_reason():
    rows = [
        node("tt-node-02", 78.0, band="VHF", tower_antenna_height_m=300.0, baseline_km=20.0, tower_polarisation="H"),
        node("tt-node-10", 76.0, band="VHF", tower_antenna_height_m=150.0, baseline_km=20.0, tower_polarisation="H"),
        node("tt-node-17", 74.0, band="VHF", baseline_km=20.0, tower_polarisation="V"),
        node("tt-node-06", 66.0, band="UHF", baseline_km=20.0),
        node("tt-node-14", 64.0, band="UHF"),
    ]

    calibration = pp.fit(rows)

    assert [term["name"] for term in calibration["terms"]] == ["band"]
    assert calibration["dropped_terms"]["tower_antenna_height_m"] == "carried by 2 fitted nodes, needs 3"
    assert calibration["dropped_terms"]["baseline_km"] == "one value across its 4 carriers"
    assert calibration["dropped_terms"]["tower_polarisation"] == "needs two levels held by 2 nodes each"
    assert calibration["dropped_terms"]["sdr_model"] == "no fitted node carries it"


def test_node_without_descriptors_is_predicted_the_fleet_average():
    rows = [
        node("tt-node-04", 80.0, band="VHF", tower_eirp_dbm=82.0),
        node("tt-node-12", 76.0, band="VHF", tower_eirp_dbm=79.0),
        node("tt-node-20", 74.0, band="VHF", tower_eirp_dbm=75.0),
        node("tt-node-21", 71.0, band="VHF"),
        node("tt-node-22", 62.0, band="UHF", tower_eirp_dbm=70.0),
        node("tt-node-23", 57.0, band="UHF", tower_eirp_dbm=68.0),
    ]

    calibration = pp.fit(rows)

    assert [term["name"] for term in calibration["terms"]] == ["band", "tower_eirp_dbm"]
    assert pp.predict(calibration, {})[0] == pytest.approx(70.0, abs=1e-6)


def test_left_out_node_is_predicted_by_a_fit_without_it():
    held = node("tt-node-24", 80.0, tower_antenna_height_m=300.0)
    others = [
        node("tt-node-25", 75.0, tower_antenna_height_m=100.0),
        node("tt-node-01", 65.0, tower_antenna_height_m=30.0),
        node("tt-node-08", 70.0),
        node("tt-node-15", 72.0),
    ]
    with_it = pp.fit([held, *others])
    assert pp.predict(with_it, held["descriptors"])[0] != pytest.approx(70.5, abs=0.5)

    entry = next(row for row in pp.validate([*others, held])["loo"] if row["node_id"] == "tt-node-24")

    # Without it the height has two carriers and drops, leaving the mean of the other four.
    without_it = pp.fit(others)
    assert entry["measured_db"] == 80.0
    assert entry["predicted_db"] == pytest.approx(70.5, abs=1e-6)
    assert entry["prediction_sigma_db"] == round(pp.predict(without_it, held["descriptors"])[1], 6)
    assert entry["tau_db"] == without_it["tau_db"]


def test_fewer_than_four_nodes_publish_no_model():
    rows = [node("tt-node-03", 78.0, band="VHF"), node("tt-node-05", 66.0, band="UHF"), node("tt-node-07", 70.0)]

    assert pp.model_file(rows, CLASS_OFFSETS, TODAY) == {
        "schema": 1,
        "status": "too_few_nodes",
        "fitted_on": "2026-11-11",
        "nodes": 3,
        "calibration": None,
        "validation": None,
    }


def test_row_order_leaves_the_model_file_byte_identical():
    rows = [
        node("tt-node-09", 79.5, sigma_db=0.8, threshold_db=6.0, band="VHF", tower_eirp_dbm=81.0),
        node("tt-node-11", 74.0, sigma_db=None, threshold_db=5.5, band="VHF", tower_eirp_dbm=77.0),
        node("tt-node-13", 71.5, sigma_db=1.6, band="VHF", tower_antenna_height_m=250.0),
        node("tt-node-16", 63.0, sigma_db=1.1, threshold_db=6.5, band="UHF", tower_eirp_dbm=72.0),
        node("tt-node-18", 58.5, sigma_db=0.6, band="UHF", tower_antenna_height_m=90.0),
        node("tt-node-19", 66.0, sigma_db=1.3, band="UHF", tower_antenna_height_m=400.0),
    ]
    shuffled = [rows[index] for index in (3, 0, 5, 1, 4, 2)]

    doc = pp.model_file(rows, CLASS_OFFSETS, TODAY)

    assert doc["status"] == "fitted"
    assert json.dumps(doc, allow_nan=False) == json.dumps(
        pp.model_file(shuffled, CLASS_OFFSETS, TODAY), allow_nan=False
    )
    assert orjson.loads(orjson.dumps(doc)) == doc
    # A bool is no figure: a sigma of True and a threshold of False read as none at all.
    flagged = [rows[0], {**rows[1], "sigma_db": True}, {**rows[2], "threshold_db": False}, *rows[3:]]
    assert pp.model_file(flagged, CLASS_OFFSETS, TODAY) == doc


def test_a_descriptor_the_node_does_not_send_widens_its_band_by_the_fleet_s_spread():
    # Literals worked out apart from the module, from this fit's published numbers.
    fleet = (
        ("tt-node-01", 80.2, "VHF", 82.0, "1", "true"),
        ("tt-node-02", 75.1, "VHF", 78.5, "2", "false"),
        ("tt-node-03", 78.4, "VHF", 80.0, "1", "false"),
        ("tt-node-04", 73.0, "VHF", 76.0, "3", "true"),
        ("tt-node-05", 67.3, "UHF", 81.0, "2", "true"),
        ("tt-node-06", 62.8, "UHF", 77.5, "3", "false"),
        ("tt-node-07", 66.0, "UHF", 79.0, "1", "true"),
        ("tt-node-08", 64.1, "UHF", 74.5, "2", "false"),
    )
    rows = [node(n, s, band=b, tower_eirp_dbm=e, lna_state=lna, local_adsb_receiver=a) for n, s, b, e, lna, a in fleet]
    calibration = pp.fit(rows)
    known = {"band": "VHF", "tower_eirp_dbm": 80.0, "lna_state": "3", "local_adsb_receiver": "true"}

    def sigma(**changed):
        return pp.predict(calibration, {**known, **changed})[1]

    # The sample SD (n - 1) of the eight EIRPs about their mean, 78.5625 dBm.
    assert next(term for term in calibration["terms"] if term["name"] == "tower_eirp_dbm")["spread"] == 2.513357
    assert sigma(tower_eirp_dbm=None) == pytest.approx(1.925229, abs=1e-6)
    assert sigma(tower_eirp_dbm=None) > sigma()
    assert sigma(tower_eirp_dbm="80") == sigma(tower_eirp_dbm=None)
    assert sigma(tower_eirp_dbm=10**400) == sigma(tower_eirp_dbm=None)
    assert sigma(lna_state=10**5000) == sigma(lna_state=None)
    assert sigma(lna_state=None) == pytest.approx(1.722798, abs=1e-6)
    # A band the fleet never held adds the band's prior, (6 dB)^2, to what a missing band adds.
    assert sigma(band="FM") == pytest.approx(8.129308, abs=1e-6)
    assert sigma(band="FM") ** 2 - sigma(band=None) ** 2 == pytest.approx(36.0)
    assert pp.predict(calibration, {**known, "lna_state": 3, "local_adsb_receiver": True}) == pp.predict(
        calibration, known
    )


def test_a_fit_with_tau_on_a_bound_publishes_no_calibration():
    flat = [node(f"tt-node-{index:02d}", 70.0) for index in range(1, 6)]
    wide = [node(f"tt-node-{index:02d}", 70.0 + 100.0 * (-1) ** index * index) for index in range(1, 6)]
    for rows, tau in ((flat, 0.1), (wide, 40.0)):
        assert pp.fit(rows)["tau_db"] == tau
        doc = pp.model_file(rows, CLASS_OFFSETS, TODAY)
        assert (doc["status"], doc["calibration"], doc["validation"]["of"]) == ("tau_at_bound", None, 5)
