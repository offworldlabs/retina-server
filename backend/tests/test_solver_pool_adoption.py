"""Tests for adopting pool nodes a dark solve can vouch for.

Context (measured live, 21-min window): 72% of published dark solves are
narrower than the node pool their input was clustered out of, mean shortfall
2.27 nodes, and 390 of 481 rejected n=2 candidates had a pool of 3 or more —
a third node the round paired and the position clustering left in a separate
input.  An n=3 candidate publishes 81% of the time against 6% for n=2, so
those are the most expensive detections the pipeline discards.

solver.py's _adopt_pool_nodes runs right after the first successful solve and
before every gate: it predicts what each pool node should have measured at the
just-solved position and velocity, adopts the ones that agree within
SOLVER_ADOPT_DELAY_GATE_US / SOLVER_ADOPT_DOPPLER_GATE_HZ, re-solves wider,
and keeps the wider solve only if it passes the usual rms gate and has not
walked away from the narrow position.

The forward model is monkeypatched throughout: what is under test is the
adoption decision, not retina_analytics' bistatic geometry (which has its own
tests), and a stubbed prediction is the only way to place a pool measurement a
controlled distance from the gate edge.
"""

import time

from core import state
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0

# What the stubbed forward model claims the missing node should have seen.
PRED_DELAY_US, PRED_DOPPLER_HZ = 40.0, 12.0

_MISSING = object()


def _stub_result(node_ids, rms_delay, lat=LAT, lon=LON, **overrides):
    """A solve_multinode-shaped success dict for the given contributing nodes."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 9000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 150.0,
        "vel_north": -60.0,
        "rms_delay": rms_delay,
        "rms_doppler": 5.0,
        "n_nodes": len(node_ids),
        "n_measurements": len(node_ids),
        "contributing_node_ids": list(node_ids),
    }
    result.update(overrides)
    return result


def _s_in(node_ids, pool_measurements=None, pool_n_nodes=None, **overrides):
    """A dark bottom-up solver input, optionally carrying a pool.

    Shape matches what InterNodeAssociator._solver_input emits — measurements
    with t_s, per-node track ids, and the pool the cluster came out of.
    """
    s_in = {
        "initial_guess": {"lat": LAT, "lon": LON, "alt_km": 9.0},
        "measurements": [
            {"node_id": nid, "delay_us": 10.0, "doppler_hz": 1.0, "snr": 15.0, "t_s": 1.0} for nid in node_ids
        ],
        "n_nodes": len(node_ids),
        "timestamp_ms": int(time.time() * 1000),
        "adsb_hex": None,
        "track_ids": [f"t-{nid}" for nid in node_ids],
        "track_ids_by_node": {nid: [f"t-{nid}"] for nid in node_ids},
        "chi2_per_dof": 1.0,
        "n_epochs": 8,
        "pool_n_nodes": pool_n_nodes if pool_n_nodes is not None else len(node_ids),
        "pool_node_ids": sorted(node_ids + [m["node_id"] for m in (pool_measurements or [])]),
        "pool_measurements": pool_measurements,
        "pool_conflicts": 0,
    }
    s_in.update(overrides)
    return s_in


def _pool_meas(node_id="pc", delay_us=PRED_DELAY_US, doppler_hz=PRED_DOPPLER_HZ):
    return {
        "node_id": node_id,
        "track_id": f"t-{node_id}",
        "delay_us": delay_us,
        "doppler_hz": doppler_hz,
        "snr": 12.0,
        "t_s": 1.0,
    }


def _stub_solve_fn(table: dict):
    """solve_fn keyed on the frozenset of node_ids in s_in["measurements"].

    Same idiom as test_solver_trimming: _solve_best_altitude calls solve_fn
    once per altitude layer, so the answer must depend only on which nodes are
    present.  An unregistered node set is a solve solve_multinode rejects.
    """

    def fn(s_in, node_cfgs):
        nodes = frozenset(m["node_id"] for m in s_in["measurements"])
        base = table.get(nodes, _MISSING)
        if base is _MISSING:
            return {"success": False}
        if base is None:
            return None
        return dict(base)

    return fn


class _AdoptTestBase:
    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()

    def _arm(self, monkeypatch, node_ids=("pc",), delay=PRED_DELAY_US, doppler=PRED_DOPPLER_HZ):
        """Register geometry for the pool nodes and stub the forward model.

        A node with no registered geometry is never adopted (the abstention
        rule), so the registry entry has to exist even though its contents are
        never read once predict_observation is stubbed.
        """
        for nid in node_ids:
            state.node_associator.node_geometries[nid] = object()
        monkeypatch.setattr(solver_mod, "predict_observation", lambda geo, *a, **kw: (delay, doppler))

    def _run(self, s_in, solve_fn, cfgs=None):
        return solver_mod._process_solver_item((dict(s_in), cfgs or {}, time.time()), solve_fn)

    def _last_record(self):
        assert state.mlat_solve_history
        return state.mlat_solve_history[-1]


class TestAdoptWidens(_AdoptTestBase):
    """An n=2 candidate whose third node agrees becomes an n=3 solve."""

    def test_agreeing_pool_node_is_adopted_and_resolved_wider(self, monkeypatch):
        self._arm(monkeypatch)
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.1),
        }
        result = self._run(
            _s_in(["n1", "n2"], pool_measurements=[_pool_meas()], pool_n_nodes=3),
            _stub_solve_fn(table),
        )

        assert result is not None and result["success"]
        assert result["n_nodes"] == 3
        assert state.solver_adopt_eligible == 1
        assert state.solver_adopt_widened == 1
        assert state.solver_adopt_nodes_added == 1
        assert state.solver_adopt_rejected == 0
        rec = self._last_record()
        assert rec["adopt_meta"]["outcome"] == "widened"
        assert rec["adopt_meta"]["adopted_node_ids"] == ["pc"]
        assert rec["adopt_meta"]["pool_n"] == 3
        assert rec["n_nodes_pre_adopt"] == 2
        assert rec["n_nodes"] == 3

    def test_adopted_node_track_joins_the_provenance(self, monkeypatch):
        """The published solve must name the tracklet it was widened with.

        Otherwise the third node's measurement is in the fit but its track is
        not in track_ids, and nothing downstream (supersession, claiming) can
        tell that the tracklet has been consumed.
        """
        self._arm(monkeypatch)
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.1),
        }
        self._run(_s_in(["n1", "n2"], pool_measurements=[_pool_meas()], pool_n_nodes=3), _stub_solve_fn(table))
        rec = self._last_record()
        assert "t-pc" in rec["track_ids"]


class TestAdoptDeclines(_AdoptTestBase):
    def test_pool_node_outside_the_delay_gate_is_not_adopted(self, monkeypatch):
        """40 µs from prediction is a different aircraft, not a wider solve.

        The gate is 6.0 µs — an n=2 dark solve sits ~2 km from truth and 2 km
        of range error is ~6.7 µs of bistatic delay, so anything past that is
        not explained by the narrow solve's own error budget.
        """
        self._arm(monkeypatch)
        table = {frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5)}
        result = self._run(
            _s_in(
                ["n1", "n2"],
                pool_measurements=[_pool_meas(delay_us=PRED_DELAY_US + 40.0)],
                pool_n_nodes=3,
            ),
            _stub_solve_fn(table),
        )

        assert result is not None and result["n_nodes"] == 2
        assert state.solver_adopt_eligible == 1
        assert state.solver_adopt_widened == 0
        assert state.solver_adopt_rejected == 1
        rec = self._last_record()
        assert rec["adopt_meta"]["outcome"] == "none_passed"
        assert rec["adopt_meta"]["candidates"] == 1
        assert rec["adopt_meta"]["adopted_node_ids"] == []
        assert rec["n_nodes"] == 2

    def test_pool_node_outside_the_doppler_gate_is_not_adopted(self, monkeypatch):
        """Delay agreement alone is not enough — a node on the same delay
        ellipse but moving wrongly is a different target on that ellipse."""
        self._arm(monkeypatch)
        table = {frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5)}
        result = self._run(
            _s_in(
                ["n1", "n2"],
                pool_measurements=[_pool_meas(doppler_hz=PRED_DOPPLER_HZ + 400.0)],
                pool_n_nodes=3,
            ),
            _stub_solve_fn(table),
        )

        assert result["n_nodes"] == 2
        assert state.solver_adopt_widened == 0
        assert self._last_record()["adopt_meta"]["outcome"] == "none_passed"

    def test_wide_solve_failing_rms_keeps_the_narrow_one(self, monkeypatch):
        """Agreement at the prediction is a claim; the joint fit is the test.

        With a single adopted node there is no cheap second chance to take —
        dropping it is just the original solve — so the widening is abandoned
        and the narrow result is what the gates below see.
        """
        self._arm(monkeypatch)
        narrow = _stub_result(["n1", "n2"], rms_delay=1.5)
        table = {
            frozenset({"n1", "n2"}): narrow,
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=11.0),
        }
        result = self._run(
            _s_in(["n1", "n2"], pool_measurements=[_pool_meas()], pool_n_nodes=3),
            _stub_solve_fn(table),
        )

        assert result is not None and result["n_nodes"] == 2
        assert result["rms_delay"] == 1.5
        assert state.solver_adopt_widened == 0
        assert state.solver_adopt_rejected == 1
        rec = self._last_record()
        assert rec["adopt_meta"]["outcome"] == "rejected_rms"
        assert rec["adopt_meta"]["adopted_node_ids"] == ["pc"]
        assert rec["n_nodes_pre_adopt"] == 2

    def test_wide_solve_that_walks_away_is_refused(self, monkeypatch):
        """A widened solve 60 km from the narrow one is a different geometry
        the adopted node dragged the fit into, not the same aircraft solved
        better."""
        self._arm(monkeypatch)
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.0, lat=LAT + 0.55),
        }
        result = self._run(
            _s_in(["n1", "n2"], pool_measurements=[_pool_meas()], pool_n_nodes=3),
            _stub_solve_fn(table),
        )

        assert result["n_nodes"] == 2
        assert abs(result["lat"] - LAT) < 1e-9
        assert state.solver_adopt_widened == 0
        assert self._last_record()["adopt_meta"]["outcome"] == "rejected_jump"

    def test_second_chance_drops_the_worst_adopted_node(self, monkeypatch):
        """Two adopted nodes, one contaminated: the joint rms is a sum over
        both, so retrying once without the worst residual recovers a widening
        the blanket rms gate would have thrown away whole."""
        self._arm(monkeypatch, node_ids=("pc", "pd"))
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc", "pd"}): _stub_result(
                ["n1", "n2", "pc", "pd"],
                rms_delay=9.0,
                per_node_delay_res_us={"n1": 0.4, "n2": 0.4, "pc": 0.5, "pd": 17.0},
            ),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.2),
        }
        result = self._run(
            _s_in(["n1", "n2"], pool_measurements=[_pool_meas("pc"), _pool_meas("pd")], pool_n_nodes=4),
            _stub_solve_fn(table),
        )

        assert result["n_nodes"] == 3
        assert state.solver_adopt_widened == 1
        assert state.solver_adopt_nodes_added == 1
        rec = self._last_record()
        assert rec["adopt_meta"]["outcome"] == "widened"
        assert rec["adopt_meta"]["adopted_node_ids"] == ["pc"]
        assert rec["adopt_meta"]["dropped_node_id"] == "pd"


class TestAdoptScope(_AdoptTestBase):
    def test_anchored_input_is_skipped(self, monkeypatch):
        """An anchored input has a claimed identity and never went through the
        clustering the pool describes, so there is nothing to widen from."""
        self._arm(monkeypatch)
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.0),
        }
        result = self._run(
            _s_in(
                ["n1", "n2"],
                pool_measurements=[_pool_meas()],
                pool_n_nodes=3,
                anchor_key="mn-dark-abc123",
            ),
            _stub_solve_fn(table),
        )

        assert result["n_nodes"] == 2
        assert state.solver_adopt_eligible == 0
        assert "adopt_meta" not in self._last_record()

    def test_input_already_as_wide_as_its_pool_is_skipped(self, monkeypatch):
        """No shortfall, nothing to adopt — and no eligible count either, so
        the hit rate is read against candidates that actually had one."""
        self._arm(monkeypatch)
        table = {frozenset({"n1", "n2", "n3"}): _stub_result(["n1", "n2", "n3"], rms_delay=1.2)}
        result = self._run(_s_in(["n1", "n2", "n3"], pool_measurements=[]), _stub_solve_fn(table))

        assert result["n_nodes"] == 3
        assert state.solver_adopt_eligible == 0
        assert "adopt_meta" not in self._last_record()

    def test_kill_switch_disables_the_stage(self, monkeypatch):
        self._arm(monkeypatch)
        monkeypatch.setattr(solver_mod, "_ADOPT_POOL_ENABLED", False)
        table = {
            frozenset({"n1", "n2"}): _stub_result(["n1", "n2"], rms_delay=1.5),
            frozenset({"n1", "n2", "pc"}): _stub_result(["n1", "n2", "pc"], rms_delay=1.0),
        }
        result = self._run(
            _s_in(["n1", "n2"], pool_measurements=[_pool_meas()], pool_n_nodes=3),
            _stub_solve_fn(table),
        )

        assert result["n_nodes"] == 2
        assert state.solver_adopt_eligible == 0
