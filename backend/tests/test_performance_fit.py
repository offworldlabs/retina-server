"""A node's published figures, from count tables generated with known parameters.

Every table comes from a latent SNR: mean ``K + sigma_k + a + T_n``, noise of the
slope, detected at or above T_n.  That gives the probit its binomial counts and
the censored regression its sums from one draw.  T_n is 5.0 throughout, a = 150.0
and s = 4.0 unless a test says otherwise, so EIRP 70.0 means S_n = 85.0.
"""

import json

import numpy as np
import pytest
from scipy.special import ndtr

from services import performance_fit as pfit

MATCHED_TOWER = {"callsign": "TEST-TV", "eirp_dbm": 70.0, "status": "matched"}
FLOOR_AT_5_DB = {"20": 2000, "21": 2000}
WIDE_BEARINGS = [[100, 50]] * 72
SITED_RESIDUALS = {"0": 40}


def geometry(rng, offsets, *, a=150.0, slope=4.0, per_cell=400, ceiling=1.0):
    """``class_geometry`` cells over a realistic span of K."""
    cells = {}
    for name, offset in offsets.items():
        cells[name] = {}
        for key in range(-170, -129):
            snr = rng.normal(key + 0.5 + offset + a + 5.0, slope, per_cell)
            tagged = snr[snr >= 5.0]
            hits = rng.binomial(tagged.size, ceiling) if ceiling < 1.0 else tagged.size
            cells[name][str(key)] = [per_cell, hits, tagged.size, float(tagged.sum()), float((tagged**2).sum())]
    return cells


def table(class_geometry=None, **fields):
    """A merged table holding only what the test gives it."""
    class_geometry = class_geometry or {}
    cells = [cell for by_key in class_geometry.values() for cell in by_key.values()]
    merged = {
        "schema": 1,
        "node_id": "node-a",
        "window": ["2026-01-01", "2026-01-07"],
        "days": [],
        "denominator": "aggregator",
        "numerator": "tags",
        "opportunities": sum(cell[0] for cell in cells),
        "detections": sum(cell[1] for cell in cells),
        "detections_claims": 0,
        "detections_tags": sum(cell[1] for cell in cells),
        "class_range_alt": {},
        "class_ring": {},
        "bearing": [[0, 0]] * 72,
        "by_range": {},
        "by_delay": {},
        "by_alt": {},
        "by_type": {},
        "snr_hist": FLOOR_AT_5_DB,
        "snr_bin_db": 0.25,
        "tag_residual_hist": {},
        "claim_residual_hist": {},
        "residual_bin_us": 0.05,
        "class_geometry": class_geometry,
        "class_geometry_by_day": {},
    }
    return merged | fields


def ring_grid(known_by_ring):
    """A grid with no bearing dependence: every ring inside a 200 us window."""
    rings = len(known_by_ring)
    return {
        "ring_km": 2.5 + 5.0 * np.arange(rings),
        "known_db": np.repeat(np.array(known_by_ring, dtype=float)[:, None], 72, axis=1),
        "delay_us": np.full((rings, 72), 50.0),
        "window_us": 200.0,
    }


def narrowbody_table(seed, **fields):
    rng = np.random.default_rng(seed)
    defaults = {"bearing": WIDE_BEARINGS, "tag_residual_hist": SITED_RESIDUALS}
    return table(geometry(rng, {"narrowbody": 0.0}, per_cell=20000), **(defaults | fields))


def curve_table(pd_at, *, keys=range(-155, -124), n=600, **fields):
    """One class holding ``pd_at(K)`` of ``n`` in each 1 dB cell, on a node its tags call sited."""
    cells = {str(key): [n, round(n * pd_at(key + 0.5)), 0, 0.0, 0.0] for key in keys}
    return table({"narrowbody": cells}, **({"bearing": WIDE_BEARINGS, "tag_residual_hist": SITED_RESIDUALS} | fields))


def walked(merged, grid, rings):
    result = pfit.fit_node(merged | {"class_ring": {"narrowbody": rings}}, grid=grid)
    return result["range_50pct_km"]["narrowbody"], result["range_50pct_bound"]["narrowbody"]


def by_day(days):
    """The merged cells of several days, and the ``[n, d]`` view the jackknife reads."""
    merged, view = {}, {}
    for index, day in enumerate(days):
        view[f"2026-01-{index + 1:02d}"] = {
            name: {key: cell[:2] for key, cell in by_key.items()} for name, by_key in day.items()
        }
        for name, by_key in day.items():
            for key, cell in by_key.items():
                held = merged.setdefault(name, {}).setdefault(key, [0, 0, 0, 0.0, 0.0])
                merged[name][key] = [a + b for a, b in zip(held, cell, strict=True)]
    return merged, view


def test_fit_recovers_the_system_constant_and_slope():
    rng = np.random.default_rng(1)
    offsets = {"narrowbody": 0.0, "widebody": 0.77, "regional_jet": 0.26, "military_transport": 1.14}
    cells, view = by_day([geometry(rng, offsets) for _ in range(3)])
    merged = table(cells, class_geometry_by_day=view, tag_residual_hist=SITED_RESIDUALS)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER)

    assert result["threshold_db"] == 5.0
    assert result["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert pfit.tobit_check(merged)["probit_slope_db"] == pytest.approx(4.0, abs=0.3)
    assert result["rank_status"] is None


def test_system_constant_stays_when_the_numerator_changes():
    merged = table(geometry(np.random.default_rng(1), {"narrowbody": 0.0}), tag_residual_hist=SITED_RESIDUALS)
    tags = pfit.fit_node(merged, tower=MATCHED_TOWER)
    claims = pfit.fit_node(merged | {"numerator": "claims"}, tower=MATCHED_TOWER)

    assert tags["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert claims["system_constant_db"] == tags["system_constant_db"]
    assert claims["threshold_db"] == 5.0


def test_a_fits_own_system_constant_needs_the_transition_in_the_data():
    def node(a, ceiling=1.0, slope=5.0):
        # Evidence in the bins from -155 to -119 dB, so at a slope of 5 dB S_n stands for a 50 % point from -160 to -114.
        cells = {}
        for key in range(-155, -119):
            n = 4000 if key < -140 else 1500
            cells[str(key)] = [n, round(n * ceiling * float(ndtr((key + 0.5 + a) / slope))), 0, 0.0, 0.0]
        rings = {"narrowbody": {"0": [40, 40]}}
        return table({"narrowbody": cells}, bearing=WIDE_BEARINGS, class_ring=rings, tag_residual_hist=SITED_RESIDUALS)

    assert pfit.fit_node(node(157.0), tower=MATCHED_TOWER)["system_constant_db"] == pytest.approx(92.0, abs=0.1)
    assert pfit.fit_node(node(159.7), tower=MATCHED_TOWER)["system_constant_db"] == pytest.approx(94.7, abs=0.1)

    # Past one slope the cells show too little of the fall: at 2 dB the margin is 2 dB, not 5, and 93.0 reads their edge.
    assert pfit.fit_node(node(158.0, slope=2.0), tower=MATCHED_TOWER)["system_constant_db"] == 86.0

    # A 1 % ceiling on a node that never crosses 0.5, past the margin: nothing but the edge either.
    grid = ring_grid([-140.0])
    grid["delay_us"][0, :38] = 250.0
    ceiling = node(162.0, ceiling=0.99)
    result = pfit.fit_node(ceiling, tower=MATCHED_TOWER, grid=grid)
    assert result["system_constant_db"] == 86.0
    assert result["pd_ceiling"] == 0.99
    assert result["range_50pct_km"]["narrowbody"] == 2.5
    assert result["range_50pct_bound"]["narrowbody"] == "window"
    assert result["pd_by_bearing"][0] == 0.5
    assert pfit.fit_node(ceiling)["rank_status"] == "tower_unknown"

    assert pfit.fit_node(node(108.0), tower=MATCHED_TOWER)["system_constant_db"] is None


def test_no_tower_leaves_the_rest_of_the_file_standing():
    grid_cells = {
        "narrowbody": {"0-20km": {"0-5kft": [40, 30], "5-10kft": [29, 29]}},
        "unknown": {"20-40km": {"10-20kft": [50, 25]}},
    }
    bearing = [[30, 15], [29, 29]] + WIDE_BEARINGS[2:]
    rings = {"narrowbody": {"0": [50, 50]}}
    merged = narrowbody_table(2, bearing=bearing, class_range_alt=grid_cells, class_ring=rings)

    result = pfit.fit_node(merged, grid=ring_grid([-146.0]))

    assert result["system_constant_db"] is None
    assert result["system_constant_sigma_db"] is None
    assert result["tower"] == {"callsign": None, "eirp_dbm": None, "status": None}
    assert result["rank_status"] == "tower_unknown"
    assert result["threshold_db"] == 5.0
    assert result["range_50pct_km"]["narrowbody"] == 2.5
    assert result["range_50pct_bound"]["narrowbody"] == "evidence"
    assert result["range_50pct_km"]["widebody"] is None
    assert result["pd_by_bearing"][:3] == [0.5, None, 0.5]
    assert result["pd_by_class_range_alt"]["narrowbody"]["0-20km"] == {
        "0-5kft": 0.75,
        "5-10kft": None,
        "10-20kft": None,
        "20-45kft": None,
    }
    assert result["evidence_by_class_range_alt"]["narrowbody"]["0-20km"]["5-10kft"] == 29
    assert result["pd_by_class_range_alt"]["unknown"]["20-40km"]["10-20kft"] == 0.5
    assert result["classes_with_evidence"] == ["narrowbody"]


def test_range_figure_says_what_stopped_it():
    rings = {"narrowbody": {str(ring): [40, 40] for ring in range(4)}}
    merged = narrowbody_table(3, class_ring=rings, bearing=[[29, 0]] + WIDE_BEARINGS[1:])

    # K + a of +5, +3, +1, -1 dB: Pd crosses 0.5 half way between 12.5 and 17.5 km.
    # Pd 1 at a bearing short of evidence, and at one past the window, would pull it out.
    grid = ring_grid([-145.0, -147.0, -149.0, -151.0])
    grid["known_db"][:, :2] = -100.0
    grid["delay_us"][:, 1] = 250.0
    measured = pfit.fit_node(merged, grid=grid)
    assert measured["range_50pct_bound"]["narrowbody"] == "measured"
    assert measured["range_50pct_km"]["narrowbody"] == pytest.approx(15.0, abs=0.1)

    already_under = pfit.fit_node(merged, grid=ring_grid([-155.0] * 4))
    assert already_under["range_50pct_km"]["narrowbody"] == 0.0
    assert already_under["range_50pct_bound"]["narrowbody"] == "measured"

    # Evidence that starts further out, already under 0.5, measured nothing.
    started_late = pfit.fit_node(merged | {"class_ring": {"narrowbody": {"2": [40, 40]}}}, grid=ring_grid([-155.0] * 4))
    assert started_late["range_50pct_km"]["narrowbody"] is None
    assert started_late["range_50pct_bound"]["narrowbody"] == "evidence"

    # No crossing.  37 of 71 usable bearings past the window is more than half.
    grid = ring_grid([-140.0] * 4)
    grid["delay_us"][3, :38] = 250.0
    windowed = pfit.fit_node(merged, grid=grid)
    assert windowed["range_50pct_km"]["narrowbody"] == 17.5
    assert windowed["range_50pct_bound"]["narrowbody"] == "window"

    # No usable bearing left inside the window on the last ring: what it measured stands.
    grid = ring_grid([-140.0] * 4)
    grid["delay_us"][3] = 250.0
    stopped = pfit.fit_node(merged, grid=grid)
    assert stopped["range_50pct_km"]["narrowbody"] == 17.5
    assert stopped["range_50pct_bound"]["narrowbody"] == "window"

    # 35 of 71 is not, and a thin ring neither ends the walk, however low, nor carries it.
    thin = {"narrowbody": {"1": [40, 40], "2": [40, 40], "3": [29, 0], "4": [40, 40], "5": [29, 29]}}
    grid = ring_grid([-140.0] * 6)
    grid["delay_us"][4, :36] = 250.0
    short = pfit.fit_node(merged | {"class_ring": thin}, grid=grid)
    assert short["range_50pct_km"]["narrowbody"] == 22.5
    assert short["range_50pct_bound"]["narrowbody"] == "evidence"


def test_the_walk_stops_where_the_rings_measure_the_fall():
    # K + a of +10 dB on every ring, so the curve alone stays near 1, and past 12.5 km the window cuts off most bearings.
    merged = narrowbody_table(3)
    grid = ring_grid([-140.0] * 5)
    grid["delay_us"][3:, :38] = 250.0

    # 0.7 at 7.5 km and 0.3 at 12.5 km, the last ring with evidence.
    assert walked(merged, grid, {"0": [100, 90], "1": [100, 70], "2": [100, 30]}) == (10.0, "measured")

    # 0.4 on 30 opportunities, outweighed by the 300 beyond it: the fall is between 12.5 and 17.5 km.
    noisy = {"0": [100, 90], "1": [30, 12], "2": [300, 270], "3": [100, 20], "4": [100, 10]}
    assert walked(merged, grid, noisy) == (pytest.approx(15.2, abs=0.05), "measured")


def test_a_class_plateau_replaces_the_nodes_ceiling_in_the_walk():
    merged = narrowbody_table(3)

    # The node tops out at 1.  K + a of +10, +10, +4, 0 dB: 0.7 * Phi reads 0.589 at 12.5 km and 0.35 at 17.5 km.
    grid = ring_grid([-140.0, -140.0, -146.0, -150.0])
    assert walked(merged, grid, dict.fromkeys("0123", [100, 70])) == (pytest.approx(14.4, abs=0.1), "measured")

    # A class that tops out under half crosses nowhere: not even at 0 km.
    assert walked(merged, ring_grid([-140.0] * 4), dict.fromkeys("0123", [100, 40])) == (None, "evidence")


def test_a_fit_not_located_measures_no_range():
    # A flat 0.52 with a knee in two bins locates no transition, so the ring where it falls measures nothing.
    cells = {str(key): [120, 62, 0, 0.0, 0.0] for key in range(-152, -120)}
    cells["-152"][1], cells["-151"][1] = 50, 56
    rings = {"narrowbody": {"0": [40, 21], "1": [40, 21], "2": [40, 10]}}
    merged = table({"narrowbody": cells}, bearing=WIDE_BEARINGS, class_ring=rings, tag_residual_hist=SITED_RESIDUALS)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER, grid=ring_grid([-140.0, -146.0, -151.5]))

    assert result["range_50pct_km"]["narrowbody"] == 7.5
    assert result["range_50pct_bound"]["narrowbody"] == "evidence"


def test_jackknife_measures_day_to_day_scatter():
    rng = np.random.default_rng(4)
    shifts = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5)
    days = [geometry(rng, {"narrowbody": 0.0}, a=150.0 + shift, per_cell=4000) for shift in shifts]
    cells, view = by_day(days)

    result = pfit.fit_node(table(cells, class_geometry_by_day=view), tower=MATCHED_TOWER)

    # The standard error of a seven-day mean: sd 1.0801 over sqrt(7).
    assert result["system_constant_sigma_db"] == pytest.approx(0.408, abs=0.05)

    # The same with a ceiling, refitted with each day left out: held at 1 it reads 0.22.
    rng = np.random.default_rng(35)
    capped = [geometry(rng, {"narrowbody": 0.0}, a=150.0 + shift, per_cell=4000, ceiling=0.6) for shift in shifts]
    capped_cells, capped_view = by_day(capped)
    capped_result = pfit.fit_node(table(capped_cells, class_geometry_by_day=capped_view), tower=MATCHED_TOWER)
    assert capped_result["system_constant_sigma_db"] == pytest.approx(0.408, abs=0.05)

    one_day = {"2026-01-01": view["2026-01-01"]}
    alone = pfit.fit_node(table(cells, class_geometry_by_day=one_day), tower=MATCHED_TOWER)
    assert alone["system_constant_db"] is not None
    assert alone["system_constant_sigma_db"] is None

    # The rest can't be fitted without the first day: no figure, not a spread of 0 over the others.
    all_detected = {"narrowbody": {str(key): [400, 400, 400, 0.0, 0.0] for key in range(-140, -130)}}
    cells, view = by_day([days[0], all_detected, all_detected, all_detected])
    carried = pfit.fit_node(table(cells, class_geometry_by_day=view), tower=MATCHED_TOWER)
    assert carried["system_constant_db"] is not None
    assert carried["system_constant_sigma_db"] is None


def test_fleet_fit_recovers_class_offsets_with_narrowbody_pinned():
    rng = np.random.default_rng(5)
    truth = {"narrowbody": 0.0, "widebody": 2.0, "bizjet": -1.5, "light_GA": -3.0}
    nodes = {
        "node-a": table(geometry(rng, truth, a=146.0, slope=3.0)),
        "node-b": table(geometry(rng, truth, a=150.0, slope=4.0)),
        "node-c": table(geometry(rng, truth, a=155.0, slope=6.0)),
        "node-small": table(geometry(rng, truth | {"widebody": 9.0}, a=150.0), opportunities=4999),
    }

    result = pfit.fit_fleet(nodes)

    offsets = result["class_offsets_db"]
    assert list(offsets) == list(pfit.DEFAULT_CLASS_OFFSETS_DB)
    assert offsets["narrowbody"] == 0.0
    assert offsets["widebody"] == pytest.approx(2.0, abs=0.3)
    assert offsets["bizjet"] == pytest.approx(-1.5, abs=0.3)
    assert offsets["light_GA"] == pytest.approx(-3.0, abs=0.3)
    assert offsets["helicopter"] == -0.70
    assert result["nodes"] == 3
    assert result["opportunities"] == 3 * 4 * 41 * 400
    assert "node-" not in json.dumps(result)

    assert pfit.fit_fleet({"node-small": nodes["node-small"]}) is None
    no_anchor = table(geometry(rng, {"widebody": 2.0}))
    assert pfit.fit_fleet({"node-a": no_anchor}) is None


def test_fleet_fit_holds_a_class_the_data_cannot_place():
    rng = np.random.default_rng(26)
    busy = {"narrowbody": 0.0, "widebody": 2.0}
    node_a = geometry(rng, busy, a=146.0, slope=3.0, per_cell=2000)
    node_b = geometry(rng, busy, a=155.0, slope=3.0, per_cell=2000)

    # Past the floors, deep in node b's tail: the fit stops on float noise over the node fit's gate.
    tail = {str(key): [200, 192, 192, 0.0, 0.0] for key in range(-135, -129)}
    stands = pfit.fit_fleet({"node-a": table(node_a), "node-b": table(node_b | {"helicopter": tail})})
    assert stands is not None
    assert stands["class_offsets_db"]["widebody"] == pytest.approx(2.0, abs=0.3)

    thin = {"-165": [1, 1, 1, 0.0, 0.0], "-135": [1, 0, 0, 0.0, 0.0]}
    held = pfit.fit_fleet({"node-a": table(node_a), "node-b": table(node_b | {"helicopter": thin})})
    assert held["class_offsets_db"]["helicopter"] == -0.70

    # All detected on one node and all missed on another pool past the floors, and link nothing.
    all_detected = {str(key): [300, 300, 300, 0.0, 0.0] for key in range(-140, -129)}
    all_missed = {str(key): [300, 0, 0, 0.0, 0.0] for key in range(-170, -159)}
    linked_by_one_outcome = {
        "node-x": table({"widebody": node_a["widebody"]}),
        "node-y": table({"narrowbody": node_b["narrowbody"], "widebody": all_detected}),
        "node-z": table({"narrowbody": node_a["narrowbody"], "widebody": all_missed}),
    }
    unlinked = pfit.fit_fleet(linked_by_one_outcome)
    assert unlinked["class_offsets_db"]["widebody"] == 0.77
    assert unlinked["nodes"] == 2

    # A held class links nothing either, or two opportunities would set the busy class behind them.
    linked_by_a_thin_class = {
        "node-x": table({"narrowbody": node_a["narrowbody"], "helicopter": thin}),
        "node-y": table({"widebody": node_b["widebody"], "helicopter": thin}),
    }
    bridged = pfit.fit_fleet(linked_by_a_thin_class)
    assert bridged["class_offsets_db"]["widebody"] == 0.77
    assert bridged["nodes"] == 1


def test_tobit_agrees_with_the_probit_offset():
    rng = np.random.default_rng(6)
    merged = table(geometry(rng, {"narrowbody": 0.0, "widebody": 0.77}))

    result = pfit.tobit_check(merged)

    assert result["offset_db"] == pytest.approx(155.0, abs=0.3)
    assert result["sigma_db"] == pytest.approx(4.0, abs=0.3)
    assert result["threshold_db"] == 5.0
    assert result["probit_offset_db"] == pytest.approx(155.0, abs=0.3)
    assert result["probit_slope_db"] == pytest.approx(4.0, abs=0.3)

    claims = pfit.tobit_check(table(geometry(rng, {"narrowbody": 0.0}), numerator="claims"))
    assert claims["offset_db"] == pytest.approx(155.0, abs=0.3)
    assert claims["probit_offset_db"] == pytest.approx(155.0, abs=0.3)


def test_threshold_and_residual_from_small_histograms():
    # Half of the 1000 peak is 500: reached in bin 21, 100 of the 400 up from bin 20's centre.
    hist = {"18": 50, "20": 400, "21": 800, "22": 1000, "23": 600}
    assert pfit.threshold_db(hist) == 5.1875
    assert pfit.threshold_db({"20": 999}) is None

    # Residuals -0.075, 0.025, 0.325, 0.525 us, ten each: an even count, so the
    # median is 0.175 and the deviations' median sits between 0.15 and 0.25.
    even = {"-2": 10, "0": 10, "6": 10, "10": 10}
    assert pfit.fit_node(table(tag_residual_hist=even))["tag_delay_residual_mad_us"] == 0.2
    assert pfit.fit_node(table(tag_residual_hist={"0": 29}))["tag_delay_residual_mad_us"] is None


def test_mis_sited_node_is_flagged_before_it_reads_as_low_evidence():
    # Tags 3 us either side of the prediction, and too few opportunities to fit.
    mis_sited = table(opportunities=200, tag_residual_hist={"-61": 20, "60": 20})

    result = pfit.fit_node(mis_sited)

    assert result["tag_delay_residual_mad_us"] == 3.025
    assert result["rank_status"] == "config_suspect"
    assert pfit.fit_node(mis_sited | {"opportunities": 0})["rank_status"] == "config_suspect"

    # A fit, a tower and the opportunities, but too few tags to judge the site by.
    unjudged = narrowbody_table(7, tag_residual_hist={"60": 29})
    assert pfit.fit_node(unjudged, tower=MATCHED_TOWER)["rank_status"] == "low_evidence"


def test_results_are_plain_json_with_or_without_data():
    rng = np.random.default_rng(8)
    cells, view = by_day([geometry(rng, {"narrowbody": 0.0}) for _ in range(3)])
    merged = table(
        cells,
        class_geometry_by_day=view,
        bearing=WIDE_BEARINGS,
        class_ring={"narrowbody": {"0": [50, 50]}},
        class_range_alt={"narrowbody": {"0-20km": {"0-5kft": [40, 30]}}},
        tag_residual_hist={"0": 40},
    )
    tower = {"callsign": "TEST-TV", "eirp_dbm": np.float64(70.0), "status": "matched"}

    result = pfit.fit_node(merged, tower=tower, grid=ring_grid([np.float64(-140.0)]))

    figures = (
        result["system_constant_db"],
        result["system_constant_sigma_db"],
        result["pd_ceiling"],
        result["threshold_db"],
        result["tag_delay_residual_mad_us"],
        result["range_50pct_km"]["narrowbody"],
        result["pd_by_bearing"][0],
        result["pd_by_class_range_alt"]["narrowbody"]["0-20km"]["0-5kft"],
        result["tower"]["eirp_dbm"],
    )
    assert [type(figure) for figure in figures] == [float] * 9
    assert type(result["evidence_by_class_range_alt"]["narrowbody"]["0-20km"]["0-5kft"]) is int
    json.dumps(result, allow_nan=False)
    json.dumps(pfit.tobit_check(merged), allow_nan=False)

    empty = pfit.fit_node(table(snr_hist={}))
    assert empty["rank_status"] == "no_opportunities"
    assert empty["threshold_db"] is None
    assert empty["system_constant_db"] is None
    assert empty["pd_ceiling"] is None
    assert empty["pd_by_class_range_alt"] == {}
    assert empty["pd_by_bearing"] == [None] * 72
    assert empty["range_50pct_km"] == dict.fromkeys(pfit.CLASSES)
    assert empty["range_50pct_bound"] == dict.fromkeys(pfit.CLASSES, "evidence")
    json.dumps(empty, allow_nan=False)
    tobit_fields = ("offset_db", "sigma_db", "threshold_db", "probit_offset_db", "probit_slope_db")
    assert pfit.tobit_check(table(snr_hist={})) == dict.fromkeys(tobit_fields)


def test_fit_recovers_a_ceiling_with_the_system_constant_and_slope():
    rng = np.random.default_rng(31)
    cells = geometry(rng, {"narrowbody": 0.0}, per_cell=4000, ceiling=0.95)
    merged = table(cells, tag_residual_hist=SITED_RESIDUALS)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER)

    assert result["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert pfit.tobit_check(merged)["probit_slope_db"] == pytest.approx(4.0, abs=0.3)
    assert result["pd_ceiling"] == pytest.approx(0.95, abs=0.02)
    assert result["rank_status"] is None


def test_fit_stays_at_no_ceiling_when_the_curve_reaches_one():
    rng = np.random.default_rng(1)
    offsets = {"narrowbody": 0.0, "widebody": 0.77, "regional_jet": 0.26, "military_transport": 1.14}
    merged = table(geometry(rng, offsets), tag_residual_hist=SITED_RESIDUALS)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER)

    assert result["pd_ceiling"] == 1.0
    assert result["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert pfit.tobit_check(merged)["probit_slope_db"] == pytest.approx(4.0, abs=0.3)

    # The same curve with a real node's scatter: a free ceiling reads 0.99 in it, on a ratio of 40 over 15 times binomial.
    rng = np.random.default_rng(89)
    cells = {}
    for key in range(-170, -129):
        pd = min(max(float(ndtr((key + 0.5 + 150.0) / 4.0)), 1e-3), 0.998)
        cells[str(key)] = [400, int(rng.binomial(400, rng.beta(pd * 19, (1 - pd) * 19))), 0, 0.0, 0.0]
    scattered = table({"narrowbody": cells}, tag_residual_hist=SITED_RESIDUALS)
    assert pfit.fit_node(scattered, tower=MATCHED_TOWER)["pd_ceiling"] == 1.0


def test_a_curve_that_runs_off_the_top_invents_no_ceiling():
    # 50 % at -150 dB, and three level bins at the top end: a free ceiling takes them for a plateau at 0.53.
    keys = range(-170, -147)
    cells = {str(key): [600, round(600 * float(ndtr((key + 0.5 + 150.0) / 5.0))), 0, 0.0, 0.0] for key in keys}
    for key in ("-150", "-149", "-148"):
        cells[key][1] = cells["-151"][1]

    result = pfit.fit_node(table({"narrowbody": cells}, tag_residual_hist=SITED_RESIDUALS), tower=MATCHED_TOWER)

    assert result["pd_ceiling"] == 1.0


def test_a_flat_curve_locates_no_transition():
    # A tenth of what the node could see, whatever K is: two thin bins at the low end are scatter, not a fall.
    cells = {str(key): [120, 12, 0, 0.0, 0.0] for key in range(-152, -120)}
    cells["-152"][1], cells["-151"][1] = 6, 9
    merged = table({"narrowbody": cells}, tag_residual_hist=SITED_RESIDUALS)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER)

    assert result["system_constant_db"] is None
    assert result["pd_ceiling"] is None
    assert result["rank_status"] == "low_evidence"


def test_range_figure_walks_the_curve_under_its_ceiling():
    def node(ceiling):
        rng = np.random.default_rng(32)
        rings = {"narrowbody": {str(ring): [40, 40] for ring in range(4)}}
        cells = geometry(rng, {"narrowbody": 0.0}, per_cell=20000, ceiling=ceiling)
        return table(cells, class_ring=rings, bearing=WIDE_BEARINGS, tag_residual_hist=SITED_RESIDUALS)

    # K + a of +5, +3, +1, -1 dB at a slope of 4 dB: 0.8 * Phi reads 0.619 at 7.5 km and 0.479 at 12.5 km.
    grid = ring_grid([-145.0, -147.0, -149.0, -151.0])
    capped = pfit.fit_node(node(0.8), grid=grid)
    assert capped["range_50pct_km"]["narrowbody"] == pytest.approx(11.75, abs=0.15)
    assert capped["range_50pct_bound"]["narrowbody"] == "measured"

    # A ceiling of 0.4 crosses 0.5 nowhere: a located curve, and no range to give.
    low = pfit.fit_node(node(0.4), tower=MATCHED_TOWER, grid=grid)
    assert low["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert low["range_50pct_km"]["narrowbody"] is None
    assert low["range_50pct_bound"]["narrowbody"] == "evidence"


def test_fleet_fit_reads_a_class_plateau_as_a_plateau():
    rng = np.random.default_rng(33)

    def node(a, slope, narrowbody, widebody, bizjet):
        classes = (("narrowbody", 0.0, narrowbody), ("widebody", 2.0, widebody), ("bizjet", -2.5, bizjet))
        cells = [geometry(rng, {name: offset}, a=a, slope=slope, ceiling=c) for name, offset, c in classes]
        return table(cells[0] | cells[1] | cells[2])

    # Bizjets top out far under narrowbodies, by a different share on each node.
    nodes = {"node-a": node(150.0, 2.5, 0.95, 0.9, 0.8), "node-b": node(153.0, 3.0, 0.8, 0.9, 0.5)}

    offsets = pfit.fit_fleet(nodes)["class_offsets_db"]

    assert offsets["widebody"] == pytest.approx(2.0, abs=0.3)
    assert offsets["bizjet"] == pytest.approx(-2.5, abs=0.3)


def test_a_node_with_no_transition_stays_out_of_the_fleet_fit():
    rng = np.random.default_rng(34)
    truth = {"narrowbody": 0.0, "widebody": 2.0, "bizjet": -2.5}
    # A slow rise far under its 50 % point, its classes level at equal K: their offsets don't show in it.
    slow = {
        str(key): [60000, round(60000 * float(ndtr((key + 0.5 + 133.0) / 29.0))), 0, 0.0, 0.0]
        for key in range(-162, -144)
    }
    nodes = {
        "node-a": table(geometry(rng, truth, a=150.0, slope=3.0)),
        "node-b": table(geometry(rng, truth, a=153.0, slope=4.0)),
        "node-slow": table(dict.fromkeys(truth, slow)),
    }

    result = pfit.fit_fleet(nodes)

    assert result["nodes"] == 2
    assert result["class_offsets_db"]["widebody"] == pytest.approx(2.0, abs=0.3)


def test_a_node_stronger_than_its_window_reads_at_least_the_top_of_its_weakest_band():
    result = pfit.fit_node(curve_table(lambda known: 0.9), tower=MATCHED_TOWER)

    # The weakest band is the bins of -155 to -152 dB, so K tops out at -151: 151 - EIRP 70 + T_n 5.
    assert result["system_constant_db"] == 86.0
    assert result["system_constant_bound"] == "at_least"
    assert (result["system_constant_sigma_db"], result["pd_ceiling"], result["rank_status"]) == (None, 0.9, None)


def test_a_located_fit_past_its_weakest_cell_keeps_its_value_at_least():
    def node(a):
        return curve_table(lambda known: float(ndtr((known + a) / 5.0)), keys=range(-155, -119), n=1500)

    past = pfit.fit_node(node(157.0), tower=MATCHED_TOWER)
    assert (past["system_constant_db"], past["system_constant_bound"]) == (pytest.approx(92.0, abs=0.1), "at_least")

    inside = pfit.fit_node(node(150.0), tower=MATCHED_TOWER)
    assert (inside["system_constant_db"], inside["system_constant_bound"]) == (pytest.approx(85.0, abs=0.1), "measured")

    no_tower = pfit.fit_node(node(150.0))
    assert (no_tower["system_constant_db"], no_tower["system_constant_bound"]) == (None, None)


def test_a_weak_flat_node_and_a_fall_that_does_not_locate_stay_out():
    # Under 0.5 everywhere, and a 0.9 plateau that stops dead two cells up: a fall too sharp to give a slope.
    for merged in (curve_table(lambda known: 0.45), curve_table(lambda known: 0.9 if known > -153.0 else 0.0)):
        result = pfit.fit_node(merged, tower=MATCHED_TOWER)

        assert (result["system_constant_db"], result["system_constant_bound"]) == (None, None)
        assert (result["pd_ceiling"], result["rank_status"]) == (None, "low_evidence")


def test_an_at_least_node_walks_its_rings_alone():
    flat = curve_table(lambda known: 0.9)
    grid = ring_grid([-140.0] * 3)
    grid["delay_us"][2, :38] = 250.0

    assert walked(flat, grid, dict.fromkeys("012", [40, 36])) == (12.5, "window")

    # A ring that falls ends the walk where the last one held, and that is no crossing.
    falling = {"0": [40, 36], "1": [40, 36], "2": [40, 8]}
    assert walked(flat, ring_grid([-140.0] * 3), falling) == (7.5, "evidence")

    # So for a located fit past its weakest cell, whose crossing would have read 10.4 km.
    past = curve_table(lambda known: float(ndtr((known + 157.0) / 5.0)), keys=range(-155, -119), n=1500)
    assert walked(past, ring_grid([-140.0] * 3), falling) == (7.5, "evidence")


def test_a_located_ceiling_under_half_is_not_ranked_and_says_why():
    def capped(ceiling):
        return curve_table(lambda known: ceiling * float(ndtr((known + 150.0) / 4.0)), keys=range(-170, -129), n=20000)

    low = capped(0.4)
    result = pfit.fit_node(low, tower=MATCHED_TOWER)

    assert result["pd_ceiling"] == pytest.approx(0.4, abs=0.01)
    assert result["system_constant_db"] == pytest.approx(85.0, abs=0.3)
    assert result["rank_status"] == "low_ceiling"

    assert pfit.fit_node(low)["rank_status"] == "low_ceiling"
    assert pfit.fit_node(low | {"opportunities": 999}, tower=MATCHED_TOWER)["rank_status"] == "low_evidence"

    at_half = pfit.fit_node(capped(0.5), tower=MATCHED_TOWER)
    assert (at_half["pd_ceiling"], at_half["rank_status"]) == (0.5, None)


def test_a_node_with_no_tags_reads_its_residual_from_its_claims():
    no_tags = curve_table(lambda known: 0.9, tag_residual_hist={"0": 29})

    # Residuals -0.075 and 0.125 us, twenty each.
    sited = pfit.fit_node(no_tags | {"claim_residual_hist": {"-2": 20, "2": 20}}, tower=MATCHED_TOWER)
    assert sited["tag_delay_residual_mad_us"] == 0.1
    assert sited["delay_residual_source"] == "claims"
    assert sited["rank_status"] is None

    mis_sited = pfit.fit_node(no_tags | {"claim_residual_hist": {"-61": 20, "60": 20}}, tower=MATCHED_TOWER)
    assert mis_sited["tag_delay_residual_mad_us"] == 3.025
    assert mis_sited["rank_status"] == "config_suspect"

    # Enough tags stand whatever the claims say, and too few of both is no figure.
    tagged = pfit.fit_node(no_tags | {"tag_residual_hist": SITED_RESIDUALS, "claim_residual_hist": {"60": 40}})
    assert (tagged["tag_delay_residual_mad_us"], tagged["delay_residual_source"]) == (0.0, "tags")
    unjudged = pfit.fit_node(no_tags | {"claim_residual_hist": {"0": 29}})
    assert (unjudged["tag_delay_residual_mad_us"], unjudged["delay_residual_source"]) == (None, None)


def test_a_slow_rise_reads_at_least_the_top_of_the_band_that_first_reads_half():
    # Too slow for the slope's bounds, so nothing locates.  S_n is 146.4 - EIRP 70 + T_n 5 = 81.4.
    result = pfit.fit_node(curve_table(lambda known: float(ndtr((known + 146.4) / 35.0))), tower=MATCHED_TOWER)

    # The weakest cells read 0.41, over half the plateau of 0.66 and under 0.5.  The band first reading 0.5 is
    # the bins of -148 to -145 dB, so K tops out at -144, where the weakest cells would have read 88.0.
    assert (result["system_constant_db"], result["system_constant_bound"]) == (79.0, "at_least")
    assert (result["pd_ceiling"], result["rank_status"]) == (0.6583, None)


def test_a_located_fit_past_its_strongest_cell_is_not_ranked():
    # 50 % at -117 dB, 2 dB over the top of the strongest cell, which reads 0.31.
    merged = curve_table(lambda known: float(ndtr((known + 117.0) / 5.0)), keys=range(-155, -119), n=1500)

    result = pfit.fit_node(merged, tower=MATCHED_TOWER)

    # Only a located fit reads "measured": 117 - EIRP 70 + T_n 5.
    assert (result["system_constant_db"], result["system_constant_bound"]) == (pytest.approx(52.0, abs=0.1), "measured")
    assert (result["pd_ceiling"], result["rank_status"]) == (1.0, "low_ceiling")
