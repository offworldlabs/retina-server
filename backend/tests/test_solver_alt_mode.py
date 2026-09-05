"""SOLVER_ALT_MODE: how the n>=3 solve gets its altitude.

sweep (the default) calls the LM once per fixed altitude layer and keeps the
lowest rms_delay — six process-pool round trips, and an altitude quantised to
a ladder 2 km wide, which puts up to 1 km of error into the residual the
reject gate reads.  free makes ONE call to the geolocator's multi-start
helper, which solves altitude as a sixth unknown from three start layers.

These tests are about the routing, not the physics: the geolocator's own
suite (tests/test_free_altitude.py there) measures what the free solve
actually fits.  What matters here is that the default is byte-identical to
the sweep, that free spends one call and not six, that trimming re-solves
under the same mode, and that both modes leave enough on the history record
to be compared live.
"""

import time

import pytest

from core import state
from services import frame_processor
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0


def _s_in(node_ids, alt_km=9.0, **overrides):
    s_in = {
        "initial_guess": {"lat": LAT, "lon": LON, "alt_km": alt_km},
        "measurements": [{"node_id": nid, "delay_us": 10.0, "doppler_hz": 1.0, "snr": 15.0} for nid in node_ids],
        "n_nodes": len(node_ids),
        "timestamp_ms": int(time.time() * 1000),
    }
    s_in.update(overrides)
    return s_in


def _stub_result(node_ids, rms_delay=0.5, **overrides):
    result = {
        "success": True,
        "lat": LAT,
        "lon": LON,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": rms_delay,
        "rms_doppler": 5.0,
        "n_nodes": len(node_ids),
        "n_measurements": len(node_ids),
        "contributing_node_ids": list(node_ids),
    }
    result.update(overrides)
    return result


class _Recorder:
    """A solve_fn / multistart_fn that records every call it is given."""

    def __init__(self, result_for):
        self.calls: list[tuple] = []
        self._result_for = result_for

    def __call__(self, s_in, node_cfgs, *rest):
        self.calls.append((s_in, node_cfgs, rest))
        nodes = tuple(m["node_id"] for m in s_in["measurements"])
        return self._result_for(nodes, s_in, *rest)


class _AltModeBase:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()


class TestFreeAltStarts:
    """The three starts handed to the multi-start helper."""

    @pytest.mark.parametrize(
        "alt_km,expected",
        [
            (9.0, [7.0, 9.0, 11.0]),
            (7.0, [5.0, 7.0, 9.0]),
            (8.2, [7.0, 9.0, 11.0]),
            # Clamped at the ends: the ladder's first and last layers still get
            # three starts, not one or two.
            (1.5, [1.5, 3.0, 5.0]),
            (0.4, [1.5, 3.0, 5.0]),
            (11.0, [7.0, 9.0, 11.0]),
            (40.0, [7.0, 9.0, 11.0]),
        ],
    )
    def test_window_around_the_nearest_layer(self, alt_km, expected):
        starts = solver_mod._free_alt_starts(alt_km, solver_mod._SOLVER_ALT_LAYERS_KM)
        assert starts == expected
        assert len(starts) == solver_mod._FREE_ALT_N_STARTS

    def test_an_adsb_altitude_in_the_ladder_is_a_start(self):
        """_solve_best_altitude splices an ADS-B altitude into the layers, and
        the window is taken over that spliced list — otherwise the one exact
        altitude available would never be started from."""
        layers = sorted(set(solver_mod._SOLVER_ALT_LAYERS_KM + [8.4]))
        assert solver_mod._free_alt_starts(8.4, layers) == [7.0, 8.4, 9.0]

    def test_no_layers_gives_no_starts(self):
        assert solver_mod._free_alt_starts(9.0, []) == []


class TestSweepIsTheDefault(_AltModeBase):
    def test_sweep_calls_the_lm_once_per_layer_and_never_the_multistart(self):
        nodes = ["n1", "n2", "n3"]
        solve = _Recorder(lambda n, s, *r: _stub_result(n))
        multistart = _Recorder(lambda n, s, *r: pytest.fail("multistart called in sweep mode"))

        result = solver_mod._solve_best_altitude(_s_in(nodes), {}, solve, multistart)

        assert result is not None and result["success"]
        assert len(solve.calls) == len(solver_mod._SOLVER_ALT_LAYERS_KM)
        assert [c[0]["initial_guess"]["alt_km"] for c in solve.calls] == solver_mod._SOLVER_ALT_LAYERS_KM
        assert multistart.calls == []
        assert state.SOLVER_ALT_MODE == "sweep"


class TestFreeMode(_AltModeBase):
    def setup_method(self):
        super().setup_method()
        self._saved_mode = state.SOLVER_ALT_MODE
        state.SOLVER_ALT_MODE = "free"

    def teardown_method(self):
        state.SOLVER_ALT_MODE = self._saved_mode
        super().teardown_method()

    def test_one_multistart_call_with_three_starts(self):
        nodes = ["n1", "n2", "n3"]
        solve = _Recorder(lambda n, s, *r: pytest.fail("sweep ran in free mode"))
        multistart = _Recorder(lambda n, s, *r: _stub_result(n, altitude_mode="free", rms_by_start=[1.2, 0.4, 0.9]))

        result = solver_mod._solve_best_altitude(_s_in(nodes), {}, solve, multistart)

        assert result is not None and result["success"]
        assert solve.calls == []
        assert len(multistart.calls) == 1
        (_, _, rest) = multistart.calls[0]
        assert rest == ([7.0, 9.0, 11.0],)

    def test_n2_keeps_the_sweep(self):
        """Altitude is unobservable at n=2 — the free path is not entered even
        with the mode on."""
        nodes = ["n1", "n2"]
        solve = _Recorder(lambda n, s, *r: _stub_result(n))
        multistart = _Recorder(lambda n, s, *r: pytest.fail("free path taken at n=2"))

        result = solver_mod._solve_best_altitude(_s_in(nodes), {}, solve, multistart)

        assert result is not None
        assert len(solve.calls) == len(solver_mod._SOLVER_ALT_LAYERS_KM)
        assert multistart.calls == []

    def test_a_failed_multistart_is_a_failed_solve(self):
        """No silent fall back to the sweep: three starts producing nothing is
        the same verdict as every layer producing nothing."""
        solve = _Recorder(lambda n, s, *r: pytest.fail("swept after a failed multistart"))
        multistart = _Recorder(lambda n, s, *r: None)

        assert solver_mod._solve_best_altitude(_s_in(["n1", "n2", "n3"]), {}, solve, multistart) is None

    def test_history_carries_the_mode_and_the_per_start_residuals(self):
        nodes = ["n1", "n2", "n3"]
        multistart = _Recorder(
            lambda n, s, *r: _stub_result(
                n, altitude_mode="free", rms_by_start=[1.2345, None, 0.4321], alt_starts_km=[5.0, 7.0, 9.0]
            )
        )
        solver_mod._process_solver_item(
            (_s_in(nodes), {}, time.time()),
            lambda s, c: pytest.fail("sweep ran in free mode"),
            multistart_fn=multistart,
        )

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["altitude_mode"] == "free"
        assert rec["alt_starts_km"] == [5.0, 7.0, 9.0]
        assert rec["alt_start_rms_us"] == [1.234, None, 0.432]

    def test_z_saturation_reaches_the_history(self):
        nodes = ["n1", "n2", "n3"]
        multistart = _Recorder(lambda n, s, *r: _stub_result(n, altitude_mode="free", z_saturated=True))
        solver_mod._process_solver_item((_s_in(nodes), {}, time.time()), lambda s, c: None, multistart_fn=multistart)
        assert state.mlat_solve_history[-1]["z_saturated"] is True

    def test_trimming_re_solves_through_the_multistart(self):
        """A trim round must use the mode its first solve used, or the rms it
        compares against the previous round is a different quantity."""
        full = ["n1", "n2", "n3", "n4", "bad"]
        trimmed = ["n1", "n2", "n3", "n4"]

        def _result(nodes, s_in, *rest):
            if "bad" in nodes:
                return _stub_result(
                    nodes,
                    rms_delay=8.0,
                    altitude_mode="free",
                    per_node_delay_res_us={n: (12.0 if n == "bad" else 0.5) for n in nodes},
                )
            return _stub_result(
                nodes,
                rms_delay=0.8,
                altitude_mode="free",
                per_node_delay_res_us={n: 0.3 for n in nodes},
            )

        multistart = _Recorder(_result)
        result = solver_mod._process_solver_item(
            (_s_in(full), {}, time.time()),
            lambda s, c: pytest.fail("sweep ran during a free-mode trim"),
            multistart_fn=multistart,
        )

        assert result is not None and result["n_nodes"] == 4
        assert sorted(m["node_id"] for m in multistart.calls[-1][0]["measurements"]) == trimmed
        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["trimmed_node_ids"] == ["bad"]
        assert rec["altitude_mode"] == "free"

    def test_an_unrecognised_mode_would_sweep(self):
        """The flag degrades to the inert mode, like its siblings — asserted on
        the resolution rule rather than by re-importing core.state."""
        state.SOLVER_ALT_MODE = "definitely-not-a-mode"
        solve = _Recorder(lambda n, s, *r: _stub_result(n))
        multistart = _Recorder(lambda n, s, *r: pytest.fail("free path taken for a bad mode"))
        assert solver_mod._solve_best_altitude(_s_in(["n1", "n2", "n3"]), {}, solve, multistart)
        assert len(solve.calls) == len(solver_mod._SOLVER_ALT_LAYERS_KM)


class TestConfigsForSolverInput:
    """Only the configs a candidate can reach are queued with it.

    The pool is a spawn pool, so whatever is queued is pickled and shipped on
    every solve — 58 fleet configs against a candidate's 2-8 measurements.
    """

    _FLEET = {f"n{i}": {"rx_lat": 35.0 + i, "rx_lon": -82.0} for i in range(8)}

    def test_restricted_to_the_measurement_nodes(self):
        s_in = _s_in(["n1", "n3", "n5"])
        cfgs = frame_processor.configs_for_solver_input(self._FLEET, s_in)
        assert sorted(cfgs) == ["n1", "n3", "n5"]
        assert cfgs["n3"] is self._FLEET["n3"]

    def test_unknown_measurement_nodes_are_simply_absent(self):
        """A measurement from a node with no config is the case
        solve_multinode already handles by skipping it — not an error here."""
        cfgs = frame_processor.configs_for_solver_input(self._FLEET, _s_in(["n1", "ghost"]))
        assert sorted(cfgs) == ["n1"]

    def test_no_measurements_gives_nothing(self):
        assert frame_processor.configs_for_solver_input(self._FLEET, {"measurements": []}) == {}
        assert frame_processor.configs_for_solver_input(self._FLEET, {}) == {}
