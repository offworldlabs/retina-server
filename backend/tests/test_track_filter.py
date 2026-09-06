"""Unit tests for services.track_filter — the per-track-key Kalman filter
that replaced _ewma_smooth_track as the default multinode display smoother.

Covers:
- TRACK_SMOOTHER mode dispatch (off / ewma / kf / unknown-falls-back-to-kf)
- KF lifecycle basics: first-solve passthrough, duplicate/out-of-order
  passthrough, gap re-init, innovation-gate re-init, drop_key/reset
- Covariance weighting (tight cov trusts the measurement more than loose)
  and ADS-B velocity preference over the solved CV fit
- The Joseph covariance update's PSD invariant, and the sqrt clamp that
  backs it up when a covariance arrives already poisoned
- TRACK_KF_R_SOURCE: the flat vs calibrated (solve_uncertainty) base term
- TRACK_KF_OUTLIER_MODE: holding an established dark track against an
  outlier solve, the streak that ends a hold, and the guards that keep the
  hold off young tracks, small innovations and the ADS-B lane — plus the
  solver-side publish/history/counter side of one
- RMSE reduction on a seeded synthetic constant-velocity track
- Exact agreement with a Stone-Soup reference Kalman filter (skipped if
  stonesoup is not installed — nothing else in this file depends on it)
- _enu_offset_m as the exact inverse of services.geo.offset_latlon_m
"""

import datetime
import math
import os
import time

import numpy as np
import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from services import solve_uncertainty, track_filter  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402
from services.tasks import solver as solver_mod  # noqa: E402


def make_result(lat, lon, ts_ms, vel_east=None, vel_north=None, cov_en_km2=None):
    """Build a solver-result-shaped dict — the same shape solver.py hands to
    the smoother.  vel_east/vel_north/cov_en_km2 are omitted entirely unless
    given, matching the solver's own conditional fields (vel_east/vel_north
    are only set on an adopted CV fit; cov_en_km2 is only set when the solver
    could compute one)."""
    result = {"success": True, "lat": lat, "lon": lon, "timestamp_ms": ts_ms}
    if vel_east is not None:
        result["vel_east"] = vel_east
    if vel_north is not None:
        result["vel_north"] = vel_north
    if cov_en_km2 is not None:
        result["cov_en_km2"] = cov_en_km2
    return result


class TestModes:
    """TRACK_SMOOTHER selects the smoothing strategy: off / ewma / kf, and
    any unrecognised value falls back to kf."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_off_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "off")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "off-key", None)
        assert out is result  # same object — no copy, no new keys
        assert "smoother" not in out
        assert "kf_pos_sigma_m" not in out

    def test_ewma_delegates_to_supplied_fn(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "ewma")
        calls = []

        def fake_ewma(result, track_key, adsb_hex):
            calls.append((track_key, adsb_hex))
            out = dict(result)
            out["smoother"] = "ewma-fake"
            return out

        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "ewma-key", "abc123", ewma_fn=fake_ewma)

        assert calls == [("ewma-key", "abc123")]
        assert out["smoother"] == "ewma-fake"

    def test_ewma_without_fn_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "ewma")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "ewma-noop", None, ewma_fn=None)
        assert out is result

    def test_unknown_value_falls_back_to_kf(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "some-nonsense-value")
        key = "unknown-mode-key"
        r1 = make_result(35.0, -82.0, 1_000)
        out1 = track_filter.smooth_solve(r1, key, None)
        assert "smoother" not in out1  # first-ever solve on the kf path: raw

        r2 = make_result(35.001, -82.0, 21_000)
        out2 = track_filter.smooth_solve(r2, key, None)
        assert out2["smoother"] == "kf"  # proves the kf branch, not off/ewma, ran


class TestKFBasics:
    """Lifecycle behaviour of the KF path: init, passthrough conditions,
    gap re-init, innovation-gate re-init, and the drop_key/reset hooks."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_first_solve_is_raw_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        result = make_result(35.0, -82.0, 1_000)
        out = track_filter.smooth_solve(result, "basics-1", None)
        assert out is result
        assert "smoother" not in out

    def test_duplicate_and_out_of_order_are_passthrough(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-2"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)

        dup = make_result(35.001, -82.0, 1_000)  # dt == 0
        out_dup = track_filter.smooth_solve(dup, key, None)
        assert out_dup is dup

        earlier = make_result(35.001, -82.0, 500)  # dt < 0
        out_earlier = track_filter.smooth_solve(earlier, key, None)
        assert out_earlier is earlier

    def test_large_gap_reinits_then_next_solve_behaves_like_second_ever(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-3"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)

        # 200 s gap > _KF_MAX_GAP_S (160 s): re-init, raw return.
        gap_result = make_result(35.001, -82.0, 1_000 + 200_000)
        out_gap = track_filter.smooth_solve(gap_result, key, None)
        assert out_gap is gap_result

        # The NEXT solve, 20 s later, now has exactly one prior — it should
        # behave like the second-ever solve for a fresh key: smoothed output.
        next_result = make_result(35.0011, -82.0, gap_result["timestamp_ms"] + 20_000)
        out_next = track_filter.smooth_solve(next_result, key, None)
        assert out_next["smoother"] == "kf"

    def test_innovation_gate_reanchors_on_large_jump(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-4"
        lat0, lon0 = 35.0, -82.0
        ts = 1_000

        track_filter.smooth_solve(make_result(lat0, lon0, ts), key, None)
        for i in range(2):
            ts += 20_000
            lat_i, lon_i = offset_latlon_m(lat0, lon0, east_m=5.0 * i, north_m=5.0 * i)
            track_filter.smooth_solve(make_result(lat_i, lon_i, ts), key, None)

        # A ~50 km jump east — far outside anything the filter's own
        # predicted uncertainty could explain as the same track.
        ts += 20_000
        jump_lat, jump_lon = offset_latlon_m(lat0, lon0, east_m=50_000.0, north_m=0.0)
        jump_result = make_result(jump_lat, jump_lon, ts)
        out_jump = track_filter.smooth_solve(jump_result, key, None)
        assert out_jump is jump_result  # gate breach: raw return

        # The FOLLOWING solve, near the jump, must smooth against the NEW
        # anchor — close to the jump position, nowhere near the old track.
        ts += 20_000
        near_lat, near_lon = offset_latlon_m(jump_lat, jump_lon, east_m=20.0, north_m=0.0)
        out_near = track_filter.smooth_solve(make_result(near_lat, near_lon, ts), key, None)
        assert out_near["smoother"] == "kf"
        assert abs(out_near["lon"] - jump_lon) < 0.01
        assert abs(out_near["lon"] - lon0) > 0.1

    def test_drop_key_and_reset_clear_state(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-5"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        assert key in track_filter._KF_TRACKS

        track_filter.drop_key(key)
        assert key not in track_filter._KF_TRACKS

        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        assert key in track_filter._KF_TRACKS

        track_filter.reset()
        assert track_filter._KF_TRACKS == {}

    def test_smoothed_output_shape(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "basics-6"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        out = track_filter.smooth_solve(make_result(35.001, -82.0, 21_000), key, None)

        assert out["smoother"] == "kf"
        assert math.isfinite(out["kf_pos_sigma_m"])
        assert out["kf_pos_sigma_m"] > 0
        assert out["lat"] == round(out["lat"], 6)
        assert out["lon"] == round(out["lon"], 6)


class TestKFWeighting:
    """The KF weights each solve by its per-solve covariance, treats ADS-B
    velocity as a real measurement, and treats solved vel_east/vel_north as
    an init-prior ONLY — never a recurring measurement (2026-08-09 staging:
    solved-velocity vector error measured a median of 127 m/s against the
    sigma=25 m/s it used to be trusted at as a measurement; re-applying it
    every solve dragged smoothed positions worse than raw).  These are three
    behaviours a plain mean (the old EWMA) cannot give you."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_tight_covariance_trusts_measurement_more_than_loose(self, monkeypatch):
        """cov_en_km2 now composes ADDITIVELY into R (see _measurement_R):
        R sigma = sqrt((_KF_R_INFLATE * formal_sigma)^2 + _KF_DEFAULT_POS_SIGMA_M^2),
        then clamped to [_KF_MIN_POS_SIGMA_M, _KF_MAX_POS_SIGMA_M] = [500, 8000] m
        (see TestRInflation for that formula in isolation):

          tight: formal sigma  100 m -> sqrt(400^2 + 1200^2)  ≈ 1265 m (no clamp)
          loose: formal sigma 2000 m -> sqrt(8000^2 + 1200^2) ≈ 8090 m -> capped at 8000 m

        Both now carry the same unconditional 1200 m base floor, so the gap
        between them (≈1265 vs 8000, a ~6.3x ratio) is smaller than it would
        be under inflation alone, but it is still wide enough that the
        qualitative claim (tighter reported cov -> posterior trusts the
        measurement more) holds clearly — verified against a hand Kalman
        replica for exactly this scenario's prediction/measurement geometry."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        lat0, lon0 = 35.0, -82.0
        loose_cov = [[4.0, 0.0], [0.0, 4.0]]  # formal ~2 km sigma -> R sigma capped at 8000 m
        tight_cov = [[0.01, 0.0], [0.0, 0.01]]  # formal ~100 m sigma -> R sigma ≈ 1265 m

        def run(key, cov_third):
            ts = 1_000
            # Two identical, stationary, loosely-covariant solves build up
            # identical prior state/covariance for both keys.
            track_filter.smooth_solve(make_result(lat0, lon0, ts, cov_en_km2=loose_cov), key, None)
            ts += 20_000
            track_filter.smooth_solve(make_result(lat0, lon0, ts, cov_en_km2=loose_cov), key, None)
            ts += 20_000
            # Third measurement, offset well away from the dead-reckoned
            # (stationary) prediction — only its covariance differs per key.
            meas_lat, meas_lon = offset_latlon_m(lat0, lon0, east_m=600.0, north_m=0.0)
            return track_filter.smooth_solve(make_result(meas_lat, meas_lon, ts, cov_en_km2=cov_third), key, None)

        out_tight = run("weight-tight", tight_cov)
        out_loose = run("weight-loose", loose_cov)

        meas_east = 600.0
        e_tight, _ = track_filter._enu_offset_m(lat0, lon0, out_tight["lat"], out_tight["lon"])
        e_loose, _ = track_filter._enu_offset_m(lat0, lon0, out_loose["lat"], out_loose["lon"])

        # Tight cov: posterior lands closer to the raw measurement.
        # Loose cov: posterior stays closer to the (stationary) prediction.
        assert abs(e_tight - meas_east) < abs(e_loose - meas_east)

    def test_adsb_velocity_preferred_over_solved(self, monkeypatch):
        """Mirrors TestDarkSolveSmoothing.test_adsb_velocity_is_preferred_over_solved
        in test_solver_worker.py, but exercised directly through
        track_filter.smooth_solve rather than the full solver pipeline."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        state.adsb_aircraft["abc123"] = {
            # 100 m/s = 194.384 kt, due north.
            "gs": 194.384,
            "track": 0.0,
            "lat": 35.0,
            "lon": -82.0,
            "last_seen_ms": int(time.time() * 1000),
        }
        lat0, lon0 = 35.0, -82.0
        d20s_deg = 2000.0 / 111_320.0  # ~100 m/s * 20 s north, in degrees lat
        lat2 = lat0 + d20s_deg
        key = "weight-adsb"

        # Solved velocity claims 200 m/s due EAST — junk that would drag the
        # estimate east if the filter used it instead of the ADS-B velocity.
        r1 = make_result(lat0, lon0, 1_000, vel_east=200.0)
        track_filter.smooth_solve(r1, key, "abc123")
        r2 = make_result(lat2, lon0, 21_000, vel_east=200.0)
        out2 = track_filter.smooth_solve(r2, key, "abc123")

        assert out2["smoother"] == "kf"
        assert out2["lat"] == pytest.approx(lat2, abs=2e-4)
        assert out2["lon"] == pytest.approx(lon0, abs=1e-3)

    def test_junk_solved_velocity_does_not_drag_position(self, monkeypatch):
        """Dark target, no ADS-B ever: solved vel_east/vel_north seeds only
        the init prior (_init_entry) and must NOT recur as a velocity
        measurement update on later solves — that recurring update is
        exactly what the 2026-08-09 staging finding condemned (median 127 m/s
        vector error fed in at a sigma=25 trust level, dragging smoothed
        positions worse than raw on moved records).

        Solve #1 seeds a junk vel_east=200 m/s claim.  Solve #2's TRUE
        position continues unchanged (a stationary target — the measurement
        the filter should trust) while its solved velocity AGAIN claims the
        same 200 m/s junk, exactly as a noisy dark-target CV fit might on a
        bad epoch.  This reconstructs the answer a position-only KF would
        give — using the SAME _f_q/_kf_correct building blocks _smooth_kf
        itself uses, starting from the SAME state _init_entry produces for
        solve #1 — rather than a hand-computed magic number, so the
        assertion tracks the production math instead of a snapshot of it.
        """
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        lat0, lon0 = 35.0, -82.0
        key = "junk-vel-key"

        r1 = make_result(lat0, lon0, 1_000, vel_east=200.0, vel_north=0.0)
        track_filter.smooth_solve(r1, key, None)  # init: ve=200 as a loose PRIOR only

        r2 = make_result(lat0, lon0, 21_000, vel_east=200.0, vel_north=0.0)
        out2 = track_filter.smooth_solve(r2, key, None)
        assert out2["smoother"] == "kf"

        # Independently reconstruct the position-only answer: same init,
        # same predict, same position update — but stop there (no vel update).
        r_pos = track_filter._measurement_R(r1)
        entry0 = track_filter._init_entry(lat0, lon0, 1.0, r1, False, 0.0, 0.0, r_pos)
        f, q = track_filter._f_q(20.0)
        x_pred = f @ entry0.x
        p_pred = f @ entry0.P @ f.T + q
        z_e, z_n = track_filter._enu_offset_m(lat0, lon0, lat0, lon0)
        x_expected, p_expected, _, _ = track_filter._kf_correct(
            x_pred, p_pred, np.array([z_e, z_n]), track_filter._H_POS, r_pos
        )

        # The actual smoothed output must match that position-only
        # reconstruction — proof no velocity update perturbed it (a real
        # perturbation is hundreds of metres, see below).  Tolerance 0.2 m,
        # not float precision: smooth_solve rounds lat/lon to 6 dp on output
        # (~0.11 m of latitude quantisation), which the direct reconstruction
        # here does not pass through.
        e_actual, n_actual = track_filter._enu_offset_m(lat0, lon0, out2["lat"], out2["lon"])
        assert e_actual == pytest.approx(float(x_expected[0]), abs=0.2)
        assert n_actual == pytest.approx(float(x_expected[2]), abs=0.2)

        # And confirm that position-only answer sits genuinely closer to the
        # true (stationary) measurement than the OLD behaviour would have
        # left it: apply the extra velocity update the old code used to do
        # (junk 200 m/s claim, sigma=25) on top of the same position-updated
        # state, and check it pulls east noticeably further from 0 — a hand
        # Kalman replica of this exact scenario put the old-style answer at
        # ~1842 m vs ~485 m for the position-only one, roughly 3.8x worse.
        old_measurement_sigma = 25.0
        zv = np.array([200.0, 0.0])
        r_vel_old = np.eye(2) * (old_measurement_sigma**2)
        x_old, _, _, _ = track_filter._kf_correct(x_expected, p_expected, zv, track_filter._H_VEL, r_vel_old)

        assert abs(e_actual) < abs(float(x_old[0]))


class TestJosephCovariance:
    """_kf_correct's covariance update must never return a negative variance.

    The Joseph form is a SUM of two congruence transforms of PSD matrices, so
    the property holds for any gain; the standard form it replaced is a
    DIFFERENCE of two nearly-equal matrices and loses it to roundoff (see
    _kf_correct).  The case below is one where that difference demonstrably
    goes negative: a rank-one prior — position and velocity perfectly
    correlated, the shape a CV filter fed nothing but position measurements
    is driven toward — with position variance 1e8 m^2, plus a 1 m^2 ridge so
    the prior is still strictly positive-definite, against a measurement
    covariance of 1e-4 m^2.  Verified while writing this: the standard form
    puts BOTH position diagonals negative (-0.44, -0.20 m^2) on the very
    first update and the ridge only papers over it afterwards.  The
    assertions stay on the current code — they pin the invariant, not the
    old bug.
    """

    def test_repeated_ill_conditioned_updates_stay_psd_and_symmetric(self):
        g = np.array([1e4, 100.0, 1e4, 100.0])  # sigma_pos 1e4 m, sigma_vel 100 m/s
        p = np.outer(g, g) + np.eye(4)
        x = np.zeros(4)
        z = np.array([1000.0, -500.0])
        r = np.eye(2) * 1e-4

        for step in range(10):
            x, p, _, _ = track_filter._kf_correct(x, p, z, track_filter._H_POS, r)
            assert np.all(np.diag(p) >= 0.0), f"step {step}: negative variance in {np.diag(p)}"
            assert np.allclose(p, p.T, rtol=0, atol=0), f"step {step}: asymmetric covariance"


class TestPoisonedCovariance:
    """kf_pos_sigma_m must be produced, never raised, from a filter entry whose
    covariance is already negative on the diagonal — the state the pre-fix
    standard-form update could leave behind, and the reason the sqrt in
    _smooth_kf is clamped.

    Poison magnitude matters here and is not arbitrary.  The predict step adds
    dt^2 * P_vel + Q to each position variance before the update ever sees it
    — ~1e6 m^2 at the 20 s cadence real solves arrive on — so a token -1e-9
    is washed out entirely and never reaches the sqrt.  Both magnitudes are
    exercised below: the small one for the end-to-end "does not throw", the
    large one because it is what actually drives the clamp.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    @staticmethod
    def _seeded_key(key):
        """Two solves, so the key has a live entry with a real covariance."""
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        track_filter.smooth_solve(make_result(35.001, -82.0, 21_000), key, None)

    @staticmethod
    def _poison(key, value):
        with track_filter._KF_LOCK:
            entry = track_filter._KF_TRACKS[key]
            entry.P[0, 0] = value
            entry.P[2, 2] = value

    def test_negative_covariance_diagonal_never_raises(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")

        # Accumulated-roundoff scale: swamped by the predict step, so this
        # asserts the path stays healthy, not that the clamp fired.
        self._seeded_key("poison-small")
        self._poison("poison-small", -1e-9)
        out = track_filter.smooth_solve(make_result(35.0011, -82.0, 22_000), "poison-small", None)
        assert out["smoother"] == "kf"
        assert out["kf_pos_sigma_m"] >= 0

        # Large enough to survive predict: without the clamp this call is the
        # ValueError the droplet was throwing.
        self._seeded_key("poison-large")
        self._poison("poison-large", -1e6)
        out = track_filter.smooth_solve(make_result(35.0011, -82.0, 22_000), "poison-large", None)
        assert out["smoother"] == "kf"
        assert out["kf_pos_sigma_m"] == 0.0  # clamped, not raised


class TestRInflation:
    """_measurement_R composes R ADDITIVELY, not by inflation alone:

        cov present and sane:  R = (_KF_R_INFLATE**2) * cov_m2 + base
        cov absent/degenerate: R = base                          (the cov=0 limit)

    where base = diag(_KF_DEFAULT_POS_SIGMA_M**2, _KF_DEFAULT_POS_SIGMA_M**2).
    The formal LM-fit covariance and the unmodeled-error floor are
    independent noise sources (frame-time skew, association contamination,
    altitude pinning don't shrink because a solve's Jacobian was tight), so
    their variances add rather than one replacing the other — see the module
    docstring and the _KF_R_INFLATE / _KF_DEFAULT_POS_SIGMA_M comments for
    the staging measurements (round 1: 1/19 multi-solve records smoothed;
    round 2, flat multiplicative inflation: formal pos_sigma_km measured at
    86 m median vs ~2.7 km true error, a ~31x ratio no single multiplier can
    bridge without saturating every solve's R to the same number) that made
    the additive form necessary.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_cov_derived_r_is_inflated_and_added_to_base_then_clamped(self):
        # A nominal formal sigma (1000 m) chosen so the composed result sits
        # inside [_KF_MIN_POS_SIGMA_M, _KF_MAX_POS_SIGMA_M] = [500, 8000]
        # unclamped: sqrt((4*1000)^2 + 1200^2) ≈ 4176.1 m — a clean,
        # directly-checkable number with the clamp not muddying what's being
        # verified (TestKFWeighting exercises the clamp itself).
        sigma_m = 1000.0
        cov_en_km2 = [[(sigma_m / 1000.0) ** 2, 0.0], [0.0, (sigma_m / 1000.0) ** 2]]
        result = make_result(35.0, -82.0, 1_000, cov_en_km2=cov_en_km2)

        r = track_filter._measurement_R(result)
        actual_sigma = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))

        expected_sigma = math.sqrt(
            (track_filter._KF_R_INFLATE * sigma_m) ** 2 + track_filter._KF_DEFAULT_POS_SIGMA_M**2
        )
        expected_sigma = min(max(expected_sigma, track_filter._KF_MIN_POS_SIGMA_M), track_filter._KF_MAX_POS_SIGMA_M)
        assert expected_sigma == pytest.approx(4176.1, abs=0.1)  # sanity: inside the clamp band
        assert actual_sigma == pytest.approx(expected_sigma)

    def test_fallback_r_is_exactly_the_base_floor(self):
        # No cov_en_km2 at all -> R is exactly the base floor, the cov=0
        # limit of the SAME additive formula, not a separately-inflated
        # branch (there is nothing left to inflate: (_KF_R_INFLATE**2)*0 = 0).
        result = make_result(35.0, -82.0, 1_000)

        r = track_filter._measurement_R(result)
        actual_sigma = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))

        # Assert the exact base value, not just "some value in the band" —
        # catches a regression that adds a spurious inflated term even with
        # no cov, or that fails to add the base term at all.
        assert actual_sigma == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)
        # Confirms the assumption the module's comments make: the base floor
        # already sits inside the clamp band on its own, so this test isn't
        # accidentally passing only because the clamp produced the same
        # number by coincidence.
        assert (
            track_filter._KF_MIN_POS_SIGMA_M < track_filter._KF_DEFAULT_POS_SIGMA_M < track_filter._KF_MAX_POS_SIGMA_M
        )


class TestKFReducesError:
    """A seeded synthetic constant-velocity track: the filtered RMSE against
    ground truth must beat the raw (unsmoothed) RMSE by at least 20%.

    Two choices here follow directly from _measurement_R's additive R model
    (R = (_KF_R_INFLATE**2)*cov_m2 + diag(_KF_DEFAULT_POS_SIGMA_M**2, ...)):

    1. No cov_en_km2.  The base term is now UNCONDITIONAL — every R has
       _KF_DEFAULT_POS_SIGMA_M (1200 m) in it regardless of cov, so R sigma
       can never fall below that floor no matter what covariance is fed.
       Dropping cov_en_km2 entirely (fallback path) gives exactly that floor
       — the most favourably-calibrated R this model can produce for a
       target whose true per-solve noise (800 m) is tighter than the floor.
       Feeding any cov_en_km2 could only make R *larger*, never smaller, so
       there is no cov choice that calibrates better than "none."
    2. Pooled repetitions, not a single 20-solve draw.  Even at that
       best-available R, a lone 20-sample RMSE has enough sampling variance
       that occasional seeds land under the 20% bar by chance alone — a
       property of the additive floor forcing R (1200 m) somewhat above the
       true noise (800 m), which trades some of the KF's convergence speed
       for the calibration honesty the whole feature exists for.  Verified
       with a hand Kalman replica (matching _f_q/_kf_correct's formulas) over
       200 seeds: a single 20-step run occasionally dips into single digits;
       pooling 16 independent repetitions of the IDENTICAL scenario (still
       120 m/s, still 20 solves 10 s apart, still gaussian sigma=800 m, still
       one fixed outer seed) kept the worst case observed above 25% margin.
       This changes how many times the scenario is repeated for statistical
       stability, not any parameter of the scenario itself.

    Re-checked after the round removing the solved-velocity MEASUREMENT
    update (see module docstring / _KF_VEL_SIGMA_SOLVE_MS): this scenario
    never fed vel_east/vel_north in the first place (dark target, no
    adsb_hex, make_result() called without those kwargs), so the removed
    code path never fired here either before or after — it is genuinely
    unaffected.  The init-prior sigma widening (60 -> 150, same change) DOES
    apply, since every solve here inits through the dark-target branch with
    a zero-mean velocity guess; re-run with the replica, a wider init prior
    converges to the true 120 m/s velocity slightly FASTER (a less-confident
    zero-velocity guess is overridden sooner by real position evidence),
    which raised the worst-case pooled margin observed from 26.5% to 28.4%
    over the same 200 outer seeds — a small improvement, not a regression.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_filtered_rmse_beats_raw_by_20_percent(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        rng = np.random.RandomState(20260809)

        lat0, lon0 = 35.0, -82.0
        v_ms = 120.0
        heading_rad = math.radians(35.0)
        ve_true = v_ms * math.sin(heading_rad)
        vn_true = v_ms * math.cos(heading_rad)
        dt_s = 10.0
        n_steps = 20
        sigma_m = 800.0  # actual injected measurement noise
        n_repeats = 16  # see class docstring: pooled for statistical robustness

        raw_err_sq: list[float] = []
        filt_err_sq: list[float] = []
        for rep in range(n_repeats):
            key = f"rmse-key-{rep}"
            for i in range(n_steps):
                true_e = ve_true * dt_s * i
                true_n = vn_true * dt_s * i
                noise_e, noise_n = rng.normal(scale=sigma_m, size=2)
                meas_e, meas_n = true_e + noise_e, true_n + noise_n

                lat, lon = offset_latlon_m(lat0, lon0, east_m=meas_e, north_m=meas_n)
                # No cov_en_km2 — see class docstring, point 1: this is the
                # most favourable R the additive model can produce here.
                result = make_result(lat, lon, 1_000 + int(i * dt_s * 1000))
                out = track_filter.smooth_solve(result, key, None)

                raw_err_sq.append((meas_e - true_e) ** 2 + (meas_n - true_n) ** 2)
                if "smoother" in out:
                    filt_e, filt_n = track_filter._enu_offset_m(lat0, lon0, out["lat"], out["lon"])
                    filt_err_sq.append((filt_e - true_e) ** 2 + (filt_n - true_n) ** 2)
                else:
                    filt_err_sq.append(raw_err_sq[-1])  # first solve: no smoothing happened yet

        raw_rmse = math.sqrt(sum(raw_err_sq) / len(raw_err_sq))
        filt_rmse = math.sqrt(sum(filt_err_sq) / len(filt_err_sq))

        assert filt_rmse < raw_rmse * 0.8, f"filtered RMSE {filt_rmse:.1f} vs raw {raw_rmse:.1f}"


class TestStoneSoupOracle:
    """track_filter's internal KF math against a Stone-Soup reference filter.

    Skipped entirely if stonesoup is not installed — nothing else in this
    file imports it or depends on it being present.

    Position-only updates: the measurement sequence carries no vel_east/
    vel_north and no ADS-B entry, so smooth_solve's velocity-update branch
    never fires and the two filters' predict+update chains stay directly
    comparable at every step.

    No cov_en_km2 either — deliberately.  cov_en_km2-derived R gets
    inflated by _KF_R_INFLATE inside track_filter (see _measurement_R), a
    step Stone-Soup's LinearGaussian model here knows nothing about, so
    feeding a covariance would desync the two filters' R immediately and
    break the exact-equality assertions below.  The fallback default R
    (_KF_DEFAULT_POS_SIGMA_M, un-inflated by design) is exactly what both
    sides use when cov_en_km2 is absent, so that is what this test compares.

    The importorskip lives in setup_method, not the class body: a class body
    runs at module-import time (collection), so a bare
    `pytest.importorskip("stonesoup")` statement there would risk skipping
    the WHOLE FILE's collection if stonesoup is absent, not just this class.
    Gating in setup_method means the skip only takes effect when pytest
    actually goes to run a test in this class, leaving every other class in
    the file untouched.
    """

    def setup_method(self):
        pytest.importorskip("stonesoup")
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_matches_stonesoup_kalman_at_every_step(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")

        from stonesoup.models.measurement.linear import LinearGaussian
        from stonesoup.models.transition.linear import (
            CombinedLinearGaussianTransitionModel,
            ConstantVelocity,
        )
        from stonesoup.predictor.kalman import KalmanPredictor
        from stonesoup.types.detection import Detection
        from stonesoup.types.hypothesis import SingleHypothesis
        from stonesoup.types.state import GaussianState
        from stonesoup.updater.kalman import KalmanUpdater

        ref_lat, ref_lon = 35.0, -82.0
        dt_s = 10.0
        n_steps = 15
        ve_true, vn_true = 80.0, -20.0
        # The fallback R — no cov_en_km2 in this sequence, so this is what
        # track_filter actually uses internally (see class docstring).
        r_sigma_m = track_filter._KF_DEFAULT_POS_SIGMA_M
        r_m2 = np.array([[r_sigma_m**2, 0.0], [0.0, r_sigma_m**2]])

        # A fixed, deterministic wiggle rather than random noise: this test
        # checks the KF arithmetic against an external oracle, not gate
        # robustness, so removing randomness removes any chance a seed trips
        # the innovation gate and desyncs the two filters being compared.
        # Both terms vanish at i=0 — the first measurement lands exactly on
        # the anchor (e=n=0), which is what makes the two filters' ENU
        # frames line up exactly (see the assertion just below the loop).
        def meas_en(i):
            return (
                ve_true * dt_s * i + 40.0 * math.sin(i * 0.7),
                vn_true * dt_s * i + 25.0 * math.sin(i * 0.9),
            )

        key = "oracle-key"
        base_ts_ms = 1_000_000
        entry_after = []
        for i in range(n_steps):
            e_i, n_i = meas_en(i)
            lat, lon = offset_latlon_m(ref_lat, ref_lon, east_m=e_i, north_m=n_i)
            result = make_result(lat, lon, base_ts_ms + int(i * dt_s * 1000))  # no cov_en_km2 -> fallback R
            track_filter.smooth_solve(result, key, None)
            entry = track_filter._KF_TRACKS[key]
            entry_after.append((np.array(entry.x, dtype=float), np.array(entry.P, dtype=float)))

        # First measurement is exactly at the anchor, so track_filter's ENU
        # frame is now EXACTLY (ref_lat, ref_lon) — no flat-earth round-trip
        # slop between the two coordinate systems compared below.
        assert track_filter._KF_TRACKS[key].ref_lat == ref_lat
        assert track_filter._KF_TRACKS[key].ref_lon == ref_lon

        q = track_filter._KF_SIGMA_A_MS2
        cv = ConstantVelocity(q)
        transition_model = CombinedLinearGaussianTransitionModel([cv, cv])
        measurement_model = LinearGaussian(ndim_state=4, mapping=(0, 2), noise_covar=r_m2)
        predictor = KalmanPredictor(transition_model)
        updater = KalmanUpdater(measurement_model)

        t0 = datetime.datetime(2026, 1, 1)
        # Prior at i=0 equals track_filter's own post-init state exactly —
        # same init rule (x=[0,0,0,0], P_pos=R, P_vel=_KF_VEL_SIGMA_SOLVE_MS^2
        # — the dark-target init-prior sigma; no vel_east/vel_north here
        # either, so the mean stays 0).  Captured directly from entry_after,
        # not hardcoded, so this stays correct regardless of that constant's
        # value.  No vel measurement ever applies in this sequence (no
        # ADS-B), so this prior is never touched again except by predict +
        # the position updates being compared below.
        x0, p0 = entry_after[0]
        prior = GaussianState(state_vector=x0, covar=p0, timestamp=t0)

        for i in range(1, n_steps):
            ts_i = t0 + datetime.timedelta(seconds=dt_s * i)
            e_i, n_i = meas_en(i)
            detection = Detection(state_vector=np.array([e_i, n_i]), timestamp=ts_i)
            prediction = predictor.predict(prior, timestamp=ts_i)
            hypothesis = SingleHypothesis(prediction, detection)
            post = updater.update(hypothesis)

            oracle_x = np.asarray(post.state_vector, dtype=float).flatten()
            oracle_p = np.asarray(post.covar, dtype=float)

            tf_x, tf_p = entry_after[i]
            assert np.allclose(oracle_x, tf_x, atol=1e-6), f"step {i} mean mismatch"
            assert np.allclose(oracle_p, tf_p, atol=1e-6), f"step {i} covar mismatch"

            prior = post


class TestEnuRoundTrip:
    """_enu_offset_m must be the exact algebraic inverse of
    services.geo.offset_latlon_m — the KF's measurement model depends on
    that being true to float precision, not just approximately true."""

    @pytest.mark.parametrize(
        "ref_lat,ref_lon,east_m,north_m",
        [
            (35.0, -82.0, 0.0, 0.0),
            (35.0, -82.0, 1234.5, -876.3),
            (0.0, 0.0, 50_000.0, -50_000.0),
            (65.0, 178.0, -12_000.0, 30_000.0),
            (-33.9, 151.2, 8_432.1, -2_001.7),
        ],
    )
    def test_round_trip_within_1e9_deg(self, ref_lat, ref_lon, east_m, north_m):
        lat, lon = offset_latlon_m(ref_lat, ref_lon, east_m=east_m, north_m=north_m)
        back_e, back_n = track_filter._enu_offset_m(ref_lat, ref_lon, lat, lon)
        lat2, lon2 = offset_latlon_m(ref_lat, ref_lon, east_m=back_e, north_m=back_n)

        assert abs(lat2 - lat) < 1e-9
        assert abs(lon2 - lon) < 1e-9


class TestLearnedVelocity:
    """learned_velocity() is the read-only accessor services/aircraft_feed.py
    uses for TRACK_DR_SOURCE=kf display dead-reckoning: it must expose the
    filter's own velocity STATE -- learned from the position sequence, the
    thing the KF is strictly better-informed about than the solved CV fit
    (see module docstring) -- not the solved-velocity init prior, and it must
    go stale exactly when the filter entry itself does (drop_key/reset)."""

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def test_unknown_key_returns_none(self):
        assert track_filter.learned_velocity("never-seen-key") is None

    def test_learns_velocity_from_position_sequence_not_init_prior(self, monkeypatch):
        """Three solves, 100 m/s due east, 10 s apart -- vel_east/vel_north
        are 0.0 on every fed result, so the init prior the first solve seeds
        is exactly zero.  If learned_velocity ever echoed that prior instead
        of the filter's own updated state, v_east would read ~0, not ~100:
        the position updates from solve #2 and #3 are what must move it.
        """
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "learned-vel-key"
        lat0, lon0 = 35.0, -82.0
        v_east_true = 100.0
        ts0_ms = 1_000

        last_ts_ms = ts0_ms
        for i in range(3):
            ts_ms = ts0_ms + i * 10_000
            lat_i, lon_i = offset_latlon_m(lat0, lon0, east_m=v_east_true * (i * 10.0), north_m=0.0)
            result = make_result(lat_i, lon_i, ts_ms, vel_east=0.0, vel_north=0.0)
            track_filter.smooth_solve(result, key, None)
            last_ts_ms = ts_ms

        lv = track_filter.learned_velocity(key)
        assert lv is not None
        v_east, v_north, vel_sigma, last_ts_s = lv
        assert abs(v_east - v_east_true) < 40.0  # learned, not the ~0 prior
        assert abs(v_north) < 40.0
        assert vel_sigma > 0
        assert last_ts_s == pytest.approx(last_ts_ms / 1000.0)

    def test_drop_key_and_reset_clear_learned_velocity(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "learned-vel-drop"

        def _feed():
            track_filter.smooth_solve(make_result(35.0, -82.0, 1_000, vel_east=0.0, vel_north=0.0), key, None)
            track_filter.smooth_solve(make_result(35.001, -82.0, 21_000, vel_east=0.0, vel_north=0.0), key, None)

        _feed()
        assert track_filter.learned_velocity(key) is not None

        track_filter.drop_key(key)
        assert track_filter.learned_velocity(key) is None

        _feed()
        assert track_filter.learned_velocity(key) is not None

        track_filter.reset()
        assert track_filter.learned_velocity(key) is None


class TestIndefiniteCovariance:
    """An indefinite cov_en_km2 must never reach the filter.

    This is the 2026-09-05 droplet failure (91 ValueError: math domain error
    in 40 minutes out of learned_velocity), and the mechanism is not roundoff
    — the Joseph form in _kf_correct was already deployed when it happened.
    cov_en_km2 is the top-left 2x2 of s2 * inv(JtJ) for the solver's 5-state
    fit, and the solver falls back to pinv only on an outright LinAlgError,
    so an ill-conditioned-but-not-singular JtJ (near-parallel baselines)
    inverts to garbage that is INDEFINITE while both diagonals stay positive
    — passing the solver's own check and, before the fix, _measurement_R's.

    Joseph preserves PSD for any gain but only GIVEN PSD P and R: its
    K R K^T term inherits R's negative eigenvalue, and _init_entry seeds P's
    position block straight from R.  Hence the determinant check.

    The covariances below are exactly that shape: equal diagonals with an
    off-diagonal larger than their geometric mean, i.e. a "correlation"
    above 1, which no real covariance has.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    @staticmethod
    def _indefinite_cov(var_km2=1.0, corr=1.2):
        return [[var_km2, corr * var_km2], [corr * var_km2, var_km2]]

    def test_indefinite_cov_falls_back_to_the_base_floor(self):
        r = track_filter._measurement_R(make_result(35.0, -82.0, 1_000, cov_en_km2=self._indefinite_cov()))

        # Rejected outright, so R is the cov=0 limit — exactly `base`, the
        # same answer a missing cov gets.
        assert r[0, 1] == 0.0
        assert math.sqrt(0.5 * (r[0, 0] + r[1, 1])) == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)
        # The property that actually matters downstream.
        assert np.all(np.linalg.eigvalsh(r) >= 0.0)

    def test_a_correlated_but_psd_cov_is_still_accepted(self):
        """The determinant check must reject only the impossible matrices.

        A genuine off-diagonal is real information about solve geometry (a
        two-node baseline has a long axis), and rejecting it would quietly
        throw away the relative weighting _KF_R_INFLATE exists to provide.
        corr=0.9 is strongly correlated but perfectly valid: det > 0.
        """
        cov = self._indefinite_cov(corr=0.9)
        r = track_filter._measurement_R(make_result(35.0, -82.0, 1_000, cov_en_km2=cov))

        assert r[0, 1] != 0.0  # the correlation survived
        assert np.all(np.linalg.eigvalsh(r) >= 0.0)

    @pytest.mark.parametrize("cadence_s", [1.4, 20.0])
    def test_indefinite_cov_keeps_the_filter_psd_and_learned_velocity_alive(self, monkeypatch, cadence_s):
        """The end-to-end regression: 200 solves carrying an indefinite cov.

        On origin/main this raised the droplet's ValueError within 16 solves
        at the 20 s cadence real solves arrive on, and within 7 at 1.4 s.
        """
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        cov = self._indefinite_cov()
        key = f"indef-{cadence_s}"
        lat0, lon0 = 35.0, -82.0

        for i in range(200):
            lat_i, lon_i = offset_latlon_m(lat0, lon0, east_m=250.0 * cadence_s * i, north_m=0.0)
            result = make_result(lat_i, lon_i, 1_000 + int(i * cadence_s * 1000), cov_en_km2=cov)
            track_filter.smooth_solve(result, key, None)

            entry = track_filter._KF_TRACKS.get(key)
            assert entry is not None
            assert np.all(np.diag(entry.P) >= 0.0), f"solve {i}: negative variance in {np.diag(entry.P)}"
            assert np.allclose(entry.P, entry.P.T, rtol=0, atol=0), f"solve {i}: asymmetric covariance"
            # The call that was throwing.
            assert track_filter.learned_velocity(key) is not None

    def test_tiny_position_sigma_cannot_produce_a_tight_r(self):
        """A 1 m formal sigma is floored, not believed.

        R is what makes the Joseph update ill-conditioned when it is tiny
        relative to P, so the unmodeled-error floor is the other half of this
        fix holding: frame-time skew, association contamination and altitude
        pinning do not shrink because one solve's Jacobian was tight, and no
        fix on this network is good to a metre.  The floor is additive and
        unconditional, so a metre-scale cov contributes only
        (_KF_R_INFLATE * 1)^2 = 16 m^2 against a base of 1200^2, leaving the
        composed sigma at the base to within a hundredth of a percent.
        """
        for sigma_m in (1e-6, 1.0):
            cov_km2 = (sigma_m / 1000.0) ** 2
            r = track_filter._measurement_R(
                make_result(35.0, -82.0, 1_000, cov_en_km2=[[cov_km2, 0.0], [0.0, cov_km2]])
            )
            actual_sigma = math.sqrt(0.5 * (r[0, 0] + r[1, 1]))
            assert actual_sigma == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M, rel=1e-4)
            assert actual_sigma >= track_filter._KF_MIN_POS_SIGMA_M
            # Nowhere near the few-metre R that would make the update
            # ill-conditioned, and an order of magnitude above even a
            # bare-sensor floor: the unmodeled-error terms dominate.
            assert actual_sigma > 1000.0

    def test_repeated_updates_at_a_tiny_r_stay_psd(self, monkeypatch):
        """200 solves with an effectively-zero cov and alternating positions.

        The floor above means the filter never actually sees a metre-scale R,
        so this asserts the composite invariant end to end rather than the
        ill-conditioning in isolation (TestJosephCovariance drives
        _kf_correct at R = 1e-4 directly).
        """
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "tiny-r"
        tiny = (1e-6 / 1000.0) ** 2
        cov = [[tiny, 0.0], [0.0, tiny]]

        for i in range(200):
            # Alternating, so the innovation never settles to zero.
            lat_i, lon_i = offset_latlon_m(35.0, -82.0, east_m=100.0 * (i % 2), north_m=0.0)
            track_filter.smooth_solve(make_result(lat_i, lon_i, 1_000 + i * 1_400, cov_en_km2=cov), key, None)

            entry = track_filter._KF_TRACKS.get(key)
            assert np.all(np.diag(entry.P) >= 0.0), f"solve {i}: negative variance in {np.diag(entry.P)}"
            assert np.allclose(entry.P, entry.P.T, rtol=0, atol=0), f"solve {i}: asymmetric covariance"
            assert track_filter.learned_velocity(key) is not None

    def test_negative_velocity_variance_degrades_to_zero_sigma(self, monkeypatch):
        """The last line of defence, on a hand-poisoned entry.

        learned_velocity has two callers that each lose real work when it
        raises — solver.py's multinode_key_decision drops the solve,
        aircraft_feed's multinode_to_aircraft drops the whole broadcast — so
        even a state no code path should now be able to reach must return a
        number rather than throw.
        """
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "neg-vel-var"
        track_filter.smooth_solve(make_result(35.0, -82.0, 1_000), key, None)
        track_filter.smooth_solve(make_result(35.001, -82.0, 21_000), key, None)

        with track_filter._KF_LOCK:
            entry = track_filter._KF_TRACKS[key]
            entry.P[1, 1] = -1e6
            entry.P[3, 3] = -1e6

        lv = track_filter.learned_velocity(key)
        assert lv is not None
        assert lv[2] == 0.0  # clamped, not raised


class TestRSource:
    """TRACK_KF_R_SOURCE picks where _measurement_R's additive BASE term comes
    from — the term that says how wrong a solve of this shape usually is,
    independent of its own Jacobian.

    "flat" (the default) is 1200 m for every solve, which prices a two-node
    solve and a five-node one identically even though the 2026-09-05 dark-lane
    tracing measured them 18x apart on the thing that matters: 36-62% of n=2
    joins land more than 3 km off against 2% at n>=5.  "uncertainty" takes the
    base from services/solve_uncertainty.solve_sigma_m instead — the
    node-count-aware, ground-truth-calibrated number the map already draws its
    disc from, so the filter trusts a solve exactly as much as the display
    claims to.

    Everything else about the composition is deliberately untouched: the
    inflated formal term still adds on top, a non-PSD cov is still rejected
    down to the base alone, and the final clamp still applies.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    @staticmethod
    def _result(n_nodes=3, **kwargs):
        """A solve-shaped result carrying a node count — solve_sigma_m needs
        one to pick a floor, and returns None without it."""
        result = make_result(35.0, -82.0, 1_000, **kwargs)
        result["n_nodes"] = n_nodes
        return result

    @staticmethod
    def _sigma_of(r):
        return math.sqrt(0.5 * (r[0, 0] + r[1, 1]))

    @staticmethod
    def _clamped(sigma_m):
        return min(max(sigma_m, track_filter._KF_MIN_POS_SIGMA_M), track_filter._KF_MAX_POS_SIGMA_M)

    def test_flat_is_todays_r_and_ignores_the_lane(self, monkeypatch):
        """The default: base is _KF_DEFAULT_POS_SIGMA_M whatever the solve
        looks like, so dark/known and n=2/n=5 all get the same R — bit
        identical to the answer before this flag existed (which is also what
        keeps TestRInflation's one-argument calls green)."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "flat")
        r_dark_n2 = track_filter._measurement_R(self._result(n_nodes=2), dark=True)
        r_known_n5 = track_filter._measurement_R(self._result(n_nodes=5), dark=False)
        r_positional = track_filter._measurement_R(self._result(n_nodes=2))

        assert np.array_equal(r_dark_n2, r_known_n5)
        assert np.array_equal(r_dark_n2, r_positional)  # dark defaults to False
        assert self._sigma_of(r_dark_n2) == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)

    def test_unset_and_unrecognised_values_are_flat(self, monkeypatch):
        """Same contract as TRACK_SMOOTHER: a missing or misspelt value must
        never silently change how much the filter trusts a solve."""
        monkeypatch.delenv("TRACK_KF_R_SOURCE", raising=False)
        r_unset = track_filter._measurement_R(self._result(n_nodes=2), dark=True)
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "some-nonsense-value")
        r_junk = track_filter._measurement_R(self._result(n_nodes=2), dark=True)

        assert self._sigma_of(r_unset) == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)
        assert np.array_equal(r_unset, r_junk)

    def test_uncertainty_base_is_the_calibrated_solve_sigma(self, monkeypatch):
        """base == solve_sigma_m(result, dark)^2, per node count, and an n=2
        solve is trusted strictly less than an n=3 one — the whole point of
        taking the base from the calibration instead of a flat constant."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")

        sigmas = {}
        for n in (2, 3):
            result = self._result(n_nodes=n)
            expected = solve_uncertainty.solve_sigma_m(result, dark=True)
            r = track_filter._measurement_R(result, dark=True)
            assert r[0, 1] == 0.0  # no cov -> the base alone, still diagonal
            assert self._sigma_of(r) == pytest.approx(self._clamped(expected))
            sigmas[n] = self._sigma_of(r)

        assert sigmas[2] > sigmas[3]

    def test_uncertainty_inflates_a_dark_solve_over_a_known_one(self, monkeypatch):
        """The dark gain in solve_sigma_m is a prior about the lane (no ADS-B
        fix seeded the guess, no pinned altitude), and it has to reach the
        filter — that is the reason _measurement_R now takes the lane at all."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        result = self._result(n_nodes=2)
        assert self._sigma_of(track_filter._measurement_R(result, dark=True)) > self._sigma_of(
            track_filter._measurement_R(result, dark=False)
        )

    def test_uncertainty_still_adds_the_inflated_formal_term(self, monkeypatch):
        """Only the base changes.  The formal cov term is still inflated by
        _KF_R_INFLATE and still ADDED — it is what keeps a well-conditioned
        solve distinguishable from an ill-conditioned one of the same node
        count."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        formal_sigma_m = 1000.0
        cov = [[(formal_sigma_m / 1000.0) ** 2, 0.0], [0.0, (formal_sigma_m / 1000.0) ** 2]]
        result = self._result(n_nodes=2, cov_en_km2=cov)

        base_sigma = solve_uncertainty.solve_sigma_m(result, dark=True)
        expected = self._clamped(math.sqrt((track_filter._KF_R_INFLATE * formal_sigma_m) ** 2 + base_sigma**2))

        r = track_filter._measurement_R(result, dark=True)
        assert self._sigma_of(r) == pytest.approx(expected)
        # And it is genuinely bigger than the same solve with no cov at all.
        assert self._sigma_of(r) > self._sigma_of(track_filter._measurement_R(self._result(n_nodes=2), dark=True))

    def test_uncertainty_rejects_a_non_psd_cov_down_to_its_own_base(self, monkeypatch):
        """The PSD rejection is unchanged and still lands on "base alone" —
        the base is just a different number now.  A sick cov must not be able
        to reach P through this path either (see TestIndefiniteCovariance)."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        indefinite = [[1.0, 1.2], [1.2, 1.0]]  # "correlation" > 1, no real covariance
        result = self._result(n_nodes=3, cov_en_km2=indefinite)

        r = track_filter._measurement_R(result, dark=True)
        assert r[0, 1] == 0.0
        assert self._sigma_of(r) == pytest.approx(self._clamped(solve_uncertainty.solve_sigma_m(result, dark=True)))
        assert np.all(np.linalg.eigvalsh(r) >= 0.0)

    def test_uncertainty_without_a_node_count_falls_back_to_the_flat_default(self, monkeypatch):
        """solve_sigma_m returns None with no n_nodes — there is no calibrated
        floor to apply — so the flat default is the honest answer rather than
        a sigma derived from the formal term alone, which would be dishonestly
        tight on exactly the solves that carry the least information."""
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        result = make_result(35.0, -82.0, 1_000)  # no n_nodes
        assert solve_uncertainty.solve_sigma_m(result, dark=True) is None
        r = track_filter._measurement_R(result, dark=True)
        assert self._sigma_of(r) == pytest.approx(track_filter._KF_DEFAULT_POS_SIGMA_M)

    def test_the_lane_reaches_r_from_the_track_key(self, monkeypatch):
        """End-to-end through smooth_solve: nothing passes `dark` explicitly
        in production, it is derived from the key prefix, so a wired-up
        mn-dark-* key must produce the wider dark R and an mn-adsb-* key the
        narrower one.  Compared through the filter's seeded position
        covariance, which _init_entry copies straight out of R."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        result = self._result(n_nodes=2)

        track_filter.smooth_solve(dict(result), "mn-dark-lane-r", None)
        track_filter.smooth_solve(dict(result), "mn-adsb-abc123", None)

        assert track_filter._KF_TRACKS["mn-dark-lane-r"].P[0, 0] > track_filter._KF_TRACKS["mn-adsb-abc123"].P[0, 0]


# Established-track fixtures for TestHoldOutliers below.  A dark key flying a
# straight line at 200 m/s (mid-envelope for the simulated commercial traffic
# the dark lane carries) with a solve every 5 s, which is roughly the cadence
# a multi-node dark target actually solves at.
_HOLD_LAT0, _HOLD_LON0 = 35.0, -82.0
_HOLD_V_MS = 200.0
_HOLD_STEP_S = 5.0
_HOLD_TS0_MS = 1_000


def _hold_result(east_m, north_m, step, n_nodes=3):
    """A dark solve ``step`` intervals into the line, offset by (east_m,
    north_m) from where the aircraft really is at that moment."""
    lat, lon = offset_latlon_m(
        _HOLD_LAT0, _HOLD_LON0, east_m=_HOLD_V_MS * _HOLD_STEP_S * step + east_m, north_m=north_m
    )
    result = make_result(lat, lon, int(_HOLD_TS0_MS + step * _HOLD_STEP_S * 1000), vel_east=_HOLD_V_MS, vel_north=0.0)
    result["n_nodes"] = n_nodes
    return result


def _establish(key, steps=3):
    """Feed ``steps`` true-position solves, leaving the entry established
    (n_updates == steps - 1: the first solve anchors, the rest update).
    Returns the next step index."""
    for i in range(steps):
        track_filter.smooth_solve(_hold_result(0.0, 0.0, i), key, None)
    return steps


class TestHoldOutliers:
    """TRACK_KF_OUTLIER_MODE=hold: an established dark track outvotes a single
    implausible solve instead of being dragged to it.

    The measurement behind this (test droplet, 2026-09-05, 20-minute captures
    15 and 17): dark feed entries more than 5 km from any aircraft are 10-14%
    of dark entries, and 58% of those ghost frames sit on keys that already
    had four-or-more-node solves behind them — what moved them was a two- or
    three-node solve joining by proximity.  Those joins are wrong far more
    often than their node count suggests (25% of n=3 joins land >3 km off
    against 5% at n=4 and 2% at n>=5) and the solve itself cannot tell: a
    three-node free-altitude fit is exactly determined, so its rms is ~0
    whether it is right or wrong.  The track can tell — 250 m/s x 5 s is
    1.25 km, so a 4 km innovation against a key that knows its position to a
    few hundred metres is not flight.

    These tests run with TRACK_KF_R_SOURCE=uncertainty, which is the pairing
    the flag is meant to ship in, and not an incidental choice: under the flat
    1200 m base an innovation has to reach ~5 km before it breaches chi² at
    all, which is already outside the solver's 6 km proximity gate — so with a
    flat R there is almost no in-gate bad join for hold to act on.  Tightening
    the base to the calibrated per-solve sigma is what makes the gate able to
    see the 4 km population in the first place.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    @pytest.fixture(autouse=True)
    def _hold_env(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        monkeypatch.setenv("TRACK_KF_OUTLIER_MODE", "hold")

    def test_an_established_track_holds_a_four_km_outlier(self):
        """The headline case: three good solves, then one 4 km off.

        The published position is the COAST — where the filter's own state
        says the aircraft is — not the outlier and not a compromise between
        them, so the key is refreshed without moving.  The filter's clock
        still advances (the entry must not look stale to the TTL sweep or to
        the next solve's dt), and the velocity state survives untouched, which
        is what keeps display-side dead reckoning working through a hold.
        """
        key = "mn-dark-hold-1"
        step = _establish(key)
        before = track_filter.learned_velocity(key)

        outlier = _hold_result(0.0, 4_000.0, step)
        out = track_filter.smooth_solve(outlier, key, None)

        assert out["kf_action"] == "held"
        assert out["smoother"] == "kf"
        assert out["kf_innov_m"] == pytest.approx(4_000.0, abs=1.0)
        assert out["kf_d2"] > track_filter._KF_GATE_CHI2
        assert out["kf_pos_sigma_m"] > 0

        # The returned position is the coast prediction: where the aircraft
        # would be on the established line, NOT the 4 km-off measurement.
        coast_lat, coast_lon = offset_latlon_m(
            _HOLD_LAT0, _HOLD_LON0, east_m=_HOLD_V_MS * _HOLD_STEP_S * step, north_m=0.0
        )
        de, dn = track_filter._enu_offset_m(coast_lat, coast_lon, out["lat"], out["lon"])
        assert math.hypot(de, dn) < 50.0
        assert abs(out["lat"] - outlier["lat"]) > 0.01  # emphatically not the raw solve

        entry = track_filter._KF_TRACKS[key]
        assert entry.last_ts_s == pytest.approx(outlier["timestamp_ms"] / 1000.0)
        assert entry.breach_streak == 1
        assert entry.n_updates == 2  # a hold is not an accepted update

        after = track_filter.learned_velocity(key)
        assert after[0] == pytest.approx(_HOLD_V_MS, abs=5.0)
        assert after[1] == pytest.approx(0.0, abs=5.0)
        assert after[3] == pytest.approx(outlier["timestamp_ms"] / 1000.0)
        assert after[0] == pytest.approx(before[0], abs=1.0)  # the coast changed nothing

    def test_a_confirmed_new_position_re_anchors_when_the_streak_runs_out(self):
        """Holding is a bet that the outlier was a one-off.  When the new
        position keeps being confirmed the identity really did change, and the
        filter concedes at _KF_HOLD_MAX_STREAK.

        The offset here is 6 km rather than the 4 km above for a reason worth
        recording: each hold coasts, so p_pred grows and the gate widens, and
        a repeated 4 km offset is simply ACCEPTED as an ordinary update on the
        very next solve (measured: d2 21.5 then 12.8) — the filter converges
        on the new position without ever needing the streak.  The streak is
        the backstop for a disagreement too large for that to happen quickly.
        """
        key = "mn-dark-hold-2"
        step = _establish(key)

        actions = []
        for i in range(track_filter._KF_HOLD_MAX_STREAK):
            out = track_filter.smooth_solve(_hold_result(0.0, 6_000.0, step + i), key, None)
            actions.append(out["kf_action"])

        assert actions == ["held"] * (track_filter._KF_HOLD_MAX_STREAK - 1) + ["reanchored"]

        # Re-anchored AT the outlier, with a fresh entry: no streak, no
        # accumulated evidence, exactly as any other re-anchor leaves it.
        entry = track_filter._KF_TRACKS[key]
        assert entry.breach_streak == 0
        assert entry.n_updates == 0
        assert entry.ref_lat == pytest.approx(_hold_result(0.0, 6_000.0, step + 2)["lat"])

    def test_a_good_solve_between_breaches_clears_the_streak(self):
        """The streak counts CONSECUTIVE breaches.  A solve the filter accepts
        says the track and the solves agree again, so the two isolated bad
        joins either side of it must not add up to a re-anchor."""
        key = "mn-dark-hold-3"
        step = _establish(key)

        assert track_filter.smooth_solve(_hold_result(0.0, 6_000.0, step), key, None)["kf_action"] == "held"
        assert track_filter._KF_TRACKS[key].breach_streak == 1

        good = track_filter.smooth_solve(_hold_result(0.0, 0.0, step + 1), key, None)
        assert good["kf_action"] == "smoothed"
        assert track_filter._KF_TRACKS[key].breach_streak == 0

        # Two more breaches now hold again rather than re-anchoring on the
        # second, which is what a non-reset streak would have done.
        for i in (2, 3):
            assert track_filter.smooth_solve(_hold_result(0.0, 6_000.0, step + i), key, None)["kf_action"] == "held"

    def test_a_young_track_re_anchors_instead_of_holding(self):
        """One accepted update is not standing to overrule a solve: the
        position estimate is still essentially the first solve's, so
        "the track disagrees" carries no information.  Today's behaviour."""
        key = "mn-dark-hold-4"
        step = _establish(key, steps=2)  # anchor + one accepted update
        assert track_filter._KF_TRACKS[key].n_updates == 1

        out = track_filter.smooth_solve(_hold_result(0.0, 6_000.0, step), key, None)
        assert out["kf_action"] == "reanchored"
        assert out["kf_innov_m"] > track_filter._KF_HOLD_MIN_INNOV_M

    def test_a_breach_below_the_minimum_innovation_is_never_held(self, monkeypatch):
        """A gate breach on a small innovation is the FILTER being wrong, not
        the solve: its velocity sigma is learned from a position sequence that
        shares the solver's biases, so a well-converged entry can call an
        ordinary sub-kilometre correction implausible.  A track must never be
        allowed to freeze itself against its own genuine drift on that basis,
        so below _KF_HOLD_MIN_INNOV_M the solve wins, exactly as today.

        The R constants are tightened here because at the shipped ones a
        1.2 km innovation cannot breach chi² at all (the 500 m sigma floor
        alone puts the gate above it) — which is precisely why this guard is
        cheap insurance rather than a load-bearing threshold.
        """
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "flat")
        monkeypatch.setattr(track_filter, "_KF_DEFAULT_POS_SIGMA_M", 150.0)
        monkeypatch.setattr(track_filter, "_KF_MIN_POS_SIGMA_M", 50.0)
        key = "mn-dark-hold-5"
        step = _establish(key)

        solve = _hold_result(0.0, 1_200.0, step)
        out = track_filter.smooth_solve(solve, key, None)

        assert out["kf_d2"] > track_filter._KF_GATE_CHI2  # it really is a breach
        assert out["kf_innov_m"] < track_filter._KF_HOLD_MIN_INNOV_M
        assert out["kf_action"] == "reanchored"  # today's behaviour, not held
        assert out is solve  # and the raw solve is what gets published
        assert track_filter._KF_TRACKS[key].ref_lat == pytest.approx(solve["lat"])

    def test_reanchor_mode_is_todays_behaviour(self, monkeypatch):
        """The default mode, on the exact scenario hold acts on: raw
        passthrough at the outlier, filter re-anchored there."""
        monkeypatch.setenv("TRACK_KF_OUTLIER_MODE", "reanchor")
        key = "mn-dark-hold-6"
        step = _establish(key)

        outlier = _hold_result(0.0, 4_000.0, step)
        out = track_filter.smooth_solve(outlier, key, None)

        assert out is outlier  # same object, only the diagnostic stamp added
        assert out["kf_action"] == "reanchored"
        assert "smoother" not in out
        assert track_filter._KF_TRACKS[key].ref_lat == pytest.approx(outlier["lat"])

    def test_an_unrecognised_mode_falls_back_to_reanchor(self, monkeypatch):
        monkeypatch.setenv("TRACK_KF_OUTLIER_MODE", "some-nonsense-value")
        key = "mn-dark-hold-7"
        step = _establish(key)
        out = track_filter.smooth_solve(_hold_result(0.0, 4_000.0, step), key, None)
        assert out["kf_action"] == "reanchored"

    def test_an_adsb_key_re_anchors_even_in_hold_mode(self):
        """Hold is dark-only.  The ADS-B lane keys off the transponder hex, so
        it cannot suffer the wrong-key proximity join this defends against,
        and an mn-adsb-* entry that disagrees with its own solves is a
        different (real) problem that must not be masked by holding."""
        key = "mn-adsb-abc123"
        step = _establish(key)
        out = track_filter.smooth_solve(_hold_result(0.0, 4_000.0, step), key, None)
        assert out["kf_action"] == "reanchored"

    def test_kf_action_is_stamped_on_every_path(self):
        """kf_action/kf_d2/kf_innov_m are the measurement this change exists
        to produce — solver.py copies them onto the history record, so a
        capture under one policy can say what the other would have shown.
        Every return path stamps, and the two numbers are None exactly where
        no innovation was computed."""
        key = "mn-dark-hold-8"

        first = track_filter.smooth_solve(_hold_result(0.0, 0.0, 0), key, None)
        assert (first["kf_action"], first["kf_d2"], first["kf_innov_m"]) == ("init", None, None)

        dup = track_filter.smooth_solve(_hold_result(0.0, 0.0, 0), key, None)
        assert (dup["kf_action"], dup["kf_d2"], dup["kf_innov_m"]) == ("passthrough", None, None)

        second = track_filter.smooth_solve(_hold_result(0.0, 0.0, 1), key, None)
        assert second["kf_action"] == "smoothed"
        assert second["kf_d2"] is not None and second["kf_innov_m"] is not None

        gap = _hold_result(0.0, 0.0, 1)
        gap["timestamp_ms"] += int(track_filter._KF_MAX_GAP_S * 1000) + 1_000
        assert track_filter.smooth_solve(gap, key, None)["kf_action"] == "init"


class TestHoldThroughTheSolver:
    """The solver end of a hold: it publishes like any other solve.

    Nothing in services/tasks/solver.py branches on kf_action — a held result
    is a result, and the entry it writes into state.multinode_tracks carries
    the held position.  That is the whole point: the key stays refreshed (so
    the aircraft does not blink out while a bad join is being ignored) and it
    simply does not move.  What the solver does add is observability — the
    three kf_* fields on the history record, and the dark-lane counters
    /api/test/solver-stats reports.
    """

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    def teardown_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()

    @pytest.fixture(autouse=True)
    def _hold_env(self, monkeypatch):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        monkeypatch.setenv("TRACK_KF_R_SOURCE", "uncertainty")
        monkeypatch.setenv("TRACK_KF_OUTLIER_MODE", "hold")

    @staticmethod
    def _solve_fn(result):
        def fn(s_in, cfgs):
            return dict(result)

        return fn

    def _run(self, result):
        return solver_mod._process_solver_item(({"n_nodes": result["n_nodes"]}, {}, time.time()), self._solve_fn(result))

    def test_a_held_solve_publishes_at_the_held_position(self):
        # Base the line at "now" so the entry never looks stale to the
        # solver's own gates, which are wall-clock (unlike the filter's).
        ts0_ms = int(time.time() * 1000)
        lat0, lon0 = 35.0, -82.0

        def line(step, north_m=0.0):
            lat, lon = offset_latlon_m(lat0, lon0, east_m=200.0 * 5.0 * step, north_m=north_m)
            return {
                "success": True,
                "lat": lat,
                "lon": lon,
                "alt_m": 9000.0,
                "timestamp_ms": int(ts0_ms + step * 5_000),
                "vel_east": 200.0,
                "vel_north": 0.0,
                "rms_delay": 1.0,
                "rms_doppler": 5.0,
                "n_nodes": 3,
                "n_measurements": 3,
                "contributing_node_ids": ["n1", "n2", "n3"],
            }

        for step in range(3):
            assert self._run(line(step)) is not None
        (key,) = state.multinode_tracks
        assert key.startswith("mn-dark-")

        # A 4 km-off solve, still inside the solver's 6 km proximity gate, so
        # it keys onto the SAME entry — which is exactly the production
        # failure this mode exists for.
        outlier = line(3, north_m=4_000.0)
        published = self._run(outlier)

        assert published is not None
        assert list(state.multinode_tracks) == [key]  # refreshed, not re-keyed or dropped
        entry = state.multinode_tracks[key]
        assert entry["kf_action"] == "held"
        # The published position is the coast, not the raw solve.
        assert abs(entry["lat"] - outlier["lat"]) > 0.01
        assert entry["lat"] == pytest.approx(line(3)["lat"], abs=1e-3)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["kf_action"] == "held"
        assert rec["kf_innov_m"] == pytest.approx(4_000.0, abs=10.0)
        assert rec["kf_d2"] > track_filter._KF_GATE_CHI2
        # raw_lat is the solver's own answer, lat the one that reached the map.
        assert rec["raw_lat"] == pytest.approx(outlier["lat"])
        assert rec["lat"] == pytest.approx(entry["lat"])

        assert state.kf_held == 1
        assert state.kf_reanchored == 0

    def test_the_counters_reset_with_the_other_solver_counters(self):
        state.kf_held = 3
        state.kf_reanchored = 5
        state._reset_for_tests()
        assert (state.kf_held, state.kf_reanchored) == (0, 0)
