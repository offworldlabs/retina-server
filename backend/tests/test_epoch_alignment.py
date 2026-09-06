"""Measurement epoch alignment (SOLVER_EPOCH_ALIGN) — solver.align_measurement_epochs.

The solver's residual model evaluates every measurement against ONE target
state, so a solver input is implicitly a claim that its measurements were
simultaneous.  Nodes sample on independent free-running cadences, so the claim
is false by up to a frame interval, and the resulting delay error is charged to
the rms_delay gate.  These tests pin the correction, and — more importantly —
pin its SIGN against the simulator's own geometry rather than against the
derivation the helper's comment gives, since a sign error there would silently
double the very error the correction exists to remove.
"""

import pytest
from retina_simulation.world import _bistatic_delay, _bistatic_doppler

from core import state
from services.tasks import solver as solver_mod
from services.tasks.solver import align_measurement_epochs

_FC_HZ = 183e6

# One node's ENU geometry, km.  Only fc_hz is read by the helper; the rest is
# here because the simulator's delay/Doppler helpers need a real bistatic
# triangle to produce numbers whose sign means anything.
_TX_ENU = (-20.0, 5.0, 0.05)
_RX_ENU = (0.0, 0.0, 0.3)

_NODE_CFGS = {
    "node-a": {"fc_hz": _FC_HZ},
    "node-b": {"fc_hz": _FC_HZ},
    "node-c": {"FC": _FC_HZ},  # the alternate spelling the geolocator accepts
}


def _s_in(measurements, **over):
    base = {
        "initial_guess": {"lat": 34.85, "lon": -82.4, "alt_km": 9.0},
        "measurements": measurements,
        "n_nodes": len({m["node_id"] for m in measurements}),
        "timestamp_ms": 1_700_000_000_000,
    }
    base.update(over)
    return base


def _m(node_id, delay_us, doppler_hz, t_s, snr=15.0):
    return {
        "node_id": node_id,
        "delay_us": delay_us,
        "doppler_hz": doppler_hz,
        "snr": snr,
        "t_s": t_s,
    }


@pytest.fixture(autouse=True)
def _zero_counter():
    state.solver_epoch_align_skipped = 0
    yield


class TestPureHelper:
    def test_newest_measurement_is_the_epoch_and_is_untouched(self):
        """t0 is the newest SAMPLE time, not the input's timestamp_ms: the
        freshest node needed no correction and must not acquire one."""
        s_in = _s_in(
            [
                _m("node-a", 40.0, 100.0, 1000.0),
                _m("node-b", 50.0, -80.0, 1001.5),
                _m("node-c", 60.0, 0.0, 1002.0),
            ]
        )
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        by_id = {m["node_id"]: m for m in out["measurements"]}
        assert by_id["node-c"]["delay_us"] == 60.0
        assert meta["epoch_aligned"] is True
        assert meta["epoch_skew_s"] == pytest.approx(2.0)

    def test_each_delay_moves_by_its_own_doppler_rate(self):
        """d(delay_us)/dt = -doppler_hz * 1e6 / fc_hz, applied over that
        measurement's own gap to t0."""
        s_in = _s_in(
            [
                _m("node-a", 40.0, 100.0, 1000.0),
                _m("node-b", 50.0, -80.0, 1001.5),
                _m("node-c", 60.0, 0.0, 1002.0),
            ]
        )
        out, _ = align_measurement_epochs(s_in, _NODE_CFGS)
        by_id = {m["node_id"]: m for m in out["measurements"]}
        assert by_id["node-a"]["delay_us"] == pytest.approx(40.0 + (-100.0 * 1e6 / _FC_HZ) * 2.0)
        assert by_id["node-b"]["delay_us"] == pytest.approx(50.0 + (80.0 * 1e6 / _FC_HZ) * 0.5)

    def test_zero_doppler_measurement_is_unchanged(self):
        """A tangential target's bistatic range is stationary, so no amount of
        skew moves its delay — the rate is the only thing that can."""
        s_in = _s_in([_m("node-a", 40.0, 0.0, 1000.0), _m("node-b", 50.0, 20.0, 1004.0)])
        out, _ = align_measurement_epochs(s_in, _NODE_CFGS)
        assert out["measurements"][0]["delay_us"] == 40.0

    def test_input_is_not_mutated(self):
        """Pure: a caller must be able to drop the result and keep the
        original, which is exactly what the flag-off path does."""
        meas = [_m("node-a", 40.0, 100.0, 1000.0), _m("node-b", 50.0, -80.0, 1002.0)]
        s_in = _s_in(meas)
        out, _ = align_measurement_epochs(s_in, _NODE_CFGS)
        assert s_in["measurements"][0]["delay_us"] == 40.0
        assert s_in["measurements"] is not out["measurements"]
        assert s_in["timestamp_ms"] == 1_700_000_000_000

    def test_timestamp_ms_is_restamped_to_the_epoch(self):
        s_in = _s_in([_m("node-a", 40.0, 100.0, 1000.0), _m("node-b", 50.0, -80.0, 1002.25)])
        out, _ = align_measurement_epochs(s_in, _NODE_CFGS)
        assert out["timestamp_ms"] == 1_002_250

    def test_fc_spelled_FC_is_accepted(self):
        """Same fallback chain the geolocator uses to build its NodeSetup, so
        a node aligns on exactly the carrier its solve predicts against."""
        s_in = _s_in([_m("node-c", 40.0, 100.0, 1000.0), _m("node-b", 50.0, 0.0, 1001.0)])
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        assert meta["epoch_aligned"] is True
        assert out["measurements"][0]["delay_us"] == pytest.approx(40.0 - 100.0 * 1e6 / _FC_HZ)

    def test_single_measurement_input_is_a_no_op(self):
        s_in = _s_in([_m("node-a", 40.0, 100.0, 1000.0)])
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        assert out is s_in
        assert meta == {"epoch_aligned": False}
        assert state.solver_epoch_align_skipped == 0


class TestSkipPath:
    @pytest.mark.parametrize(
        "broken",
        [
            {"t_s": None},
            {"doppler_hz": None},
        ],
    )
    def test_missing_field_skips_the_whole_input(self, broken):
        """All-or-nothing: a partially aligned set has no marker saying which
        measurements share an epoch, so it just relocates the error."""
        good = _m("node-a", 40.0, 100.0, 1000.0)
        bad = {**_m("node-b", 50.0, -80.0, 1002.0), **broken}
        s_in = _s_in([good, bad])
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        assert out is s_in
        assert meta == {"epoch_aligned": False}
        assert state.solver_epoch_align_skipped == 1

    def test_missing_t_s_key_entirely_skips(self):
        """The pre-upgrade measurement shape: no t_s key at all."""
        untimed = {"node_id": "node-b", "delay_us": 50.0, "doppler_hz": -80.0, "snr": 9.0}
        s_in = _s_in([_m("node-a", 40.0, 100.0, 1000.0), untimed])
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        assert out is s_in
        assert meta["epoch_aligned"] is False
        assert state.solver_epoch_align_skipped == 1

    def test_unknown_node_config_skips(self):
        s_in = _s_in([_m("node-a", 40.0, 100.0, 1000.0), _m("node-zzz", 50.0, -80.0, 1002.0)])
        out, meta = align_measurement_epochs(s_in, {"node-a": {"fc_hz": _FC_HZ}})
        assert out is s_in
        assert meta["epoch_aligned"] is False
        assert state.solver_epoch_align_skipped == 1


class TestSignAgainstSimulatorGeometry:
    """The sign check, run against the simulator's own delay/Doppler model.

    A target is flown in a straight line and sampled at two times using
    _bistatic_delay / _bistatic_doppler.  The older sample plus the correction
    must land on the newer sample's true delay — which is a statement about the
    sign of the Doppler-to-delay-rate conversion that no amount of algebra in a
    comment can substitute for.
    """

    _POS0 = (10.0, 15.0, 9.0)  # km ENU
    _DT_S = 2.0

    @staticmethod
    def _truth(vel_kms, dt_s):
        pos0 = TestSignAgainstSimulatorGeometry._POS0
        pos1 = tuple(pos0[i] + vel_kms[i] * dt_s for i in range(3))
        return (
            _bistatic_delay(pos0, _TX_ENU, _RX_ENU),
            _bistatic_delay(pos1, _TX_ENU, _RX_ENU),
            _bistatic_doppler(pos0, vel_kms, _TX_ENU, _RX_ENU, _FC_HZ),
        )

    @pytest.mark.parametrize(
        "vel_kms",
        [
            (-0.20, -0.15, 0.0),  # inbound: bistatic range shrinking
            (0.20, 0.15, 0.0),  # outbound: bistatic range growing
            (0.05, -0.24, 0.01),  # mostly crossing, with a climb
        ],
    )
    def test_alignment_moves_the_stale_delay_toward_the_truth(self, vel_kms):
        delay0, delay1, doppler0 = self._truth(vel_kms, self._DT_S)

        # node-a sampled _DT_S seconds ago; node-b is the newest sample and
        # therefore defines t0.  node-b's own numbers are irrelevant to the
        # assertion — it is only here to set the epoch.
        s_in = _s_in(
            [
                _m("node-a", delay0, doppler0, 1000.0),
                _m("node-b", 77.0, 0.0, 1000.0 + self._DT_S),
            ]
        )
        out, meta = align_measurement_epochs(s_in, _NODE_CFGS)
        assert meta["epoch_aligned"] is True
        aligned = out["measurements"][0]["delay_us"]

        err_before = abs(delay0 - delay1)
        err_after = abs(aligned - delay1)
        # A wrong sign would double err_before rather than shrink it, so the
        # margin here is the sign test.  Over 2 s of straight-line flight the
        # first-order term dominates; the residual is the trajectory's
        # curvature in bistatic range, not a modelling disagreement.
        assert err_after < err_before * 0.2
        assert err_before > 0.05  # the case would prove nothing otherwise

    def test_correction_and_truth_share_a_sign(self):
        """Stated directly, so a failure says 'the sign is wrong' rather than
        'the error did not shrink enough'."""
        vel_kms = (-0.20, -0.15, 0.0)
        delay0, delay1, doppler0 = self._truth(vel_kms, self._DT_S)
        s_in = _s_in(
            [
                _m("node-a", delay0, doppler0, 1000.0),
                _m("node-b", 77.0, 0.0, 1000.0 + self._DT_S),
            ]
        )
        out, _ = align_measurement_epochs(s_in, _NODE_CFGS)
        correction = out["measurements"][0]["delay_us"] - delay0
        assert correction * (delay1 - delay0) > 0


class TestProcessSolverItemWiring:
    """The flag, and that the aligned numbers are what the solve actually sees."""

    @staticmethod
    def _item():
        s_in = _s_in(
            [
                _m("node-a", 40.0, 300.0, 1000.0),
                _m("node-b", 50.0, 0.0, 1004.0),
            ]
        )
        return (s_in, _NODE_CFGS, None)

    def test_flag_off_leaves_the_input_untouched(self, monkeypatch):
        monkeypatch.setattr(state, "SOLVER_EPOCH_ALIGN", False)
        seen = {}

        def _solve(s_in, node_cfgs):
            seen["delays"] = [m["delay_us"] for m in s_in["measurements"]]
            return None

        solver_mod._process_solver_item(self._item(), _solve)
        assert seen["delays"] == [40.0, 50.0]

    def test_flag_on_hands_the_solver_aligned_delays(self, monkeypatch):
        monkeypatch.setattr(state, "SOLVER_EPOCH_ALIGN", True)
        seen = {}

        def _solve(s_in, node_cfgs):
            seen["delays"] = [m["delay_us"] for m in s_in["measurements"]]
            return None

        solver_mod._process_solver_item(self._item(), _solve)
        assert seen["delays"][0] == pytest.approx(40.0 + (-300.0 * 1e6 / _FC_HZ) * 4.0)
        assert seen["delays"][1] == 50.0
