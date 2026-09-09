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
from services import track_filter  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402


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


def fly(key, cadence_s, *, omega_dps, straight_s=60.0, turn_deg=180.0, tail_s=0.0, v_ms=250.0, lat0=35.0, lon0=-82.0):
    """Feed a noiseless coordinated-turn track through smooth_solve.

    Straight and level for ``straight_s`` (long enough for the filter to
    converge), then a constant-rate turn of ``turn_deg`` at ``omega_dps``,
    then ``tail_s`` more of straight flight.  Positions are integrated at
    50 ms and sampled every ``cadence_s``; no cov_en_km2, so R is the
    _KF_DEFAULT_POS_SIGMA_M floor exactly (the same choice, for the same
    reason, as TestKFReducesError).

    Returns a list of one dict per solve: elapsed time, degrees of turn
    completed, the filter's velocity vector error against the truth, the
    reported velocity sigma, and the sigma_a the entry would predict with
    next.
    """
    turn_end = straight_s + (turn_deg / omega_dps if omega_dps else 0.0)
    hdg = 0.0
    east = north = 0.0
    t = 0.0
    step = 0.05
    ts0 = 1_000_000
    next_solve = 0.0
    turned = 0.0
    out = []
    while t < turn_end + tail_s + 1e-9:
        if t >= next_solve - 1e-9:
            lat, lon = offset_latlon_m(lat0, lon0, east_m=east, north_m=north)
            track_filter.smooth_solve(make_result(lat, lon, ts0 + int(t * 1000)), key, None)
            lv = track_filter.learned_velocity(key)
            entry = track_filter._KF_TRACKS.get(key)
            out.append(
                {
                    "t": t,
                    "turn_deg": turned,
                    "vel_err_ms": math.hypot(lv[0] - v_ms * math.sin(hdg), lv[1] - v_ms * math.cos(hdg)),
                    "vel_sigma_ms": lv[2],
                    "sigma_a": track_filter._entry_sigma_a(entry) if entry else None,
                }
            )
            next_solve += cadence_s
        turning = straight_s <= t < turn_end
        if turning:
            turned += omega_dps * step
        east += v_ms * math.sin(hdg) * step
        north += v_ms * math.cos(hdg) * step
        hdg += math.radians(omega_dps) * step if turning else 0.0
        t += step
    return out


class TestManoeuvreAdaptiveQ:
    """The CV model against a coordinated turn — see the module docstring's
    manoeuvre paragraph and the _KF_SIGMA_A_MANOEUVRE_MS2 comment.

    With a fixed sigma_a = 1.5 the filter's velocity error grows to ~350 m/s
    inside a standard-rate turn and the chi-squared gate then re-anchors the
    track around 110 degrees of turn, at every solve cadence.  The adaptive
    process noise has to remove the re-anchor (which is what splits one
    aircraft into two keys downstream) without touching straight flight.

    Every test here drives the SAME synthetic turn through the real
    smooth_solve entry point, and the disabled-path test below re-runs it with
    the manoeuvre sigma pinned to the base — that is the control that proves
    these assertions can still fail.
    """

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    @pytest.mark.parametrize("cadence_s", [4.6, 12.0])
    def test_standard_rate_turn_does_not_reanchor(self, monkeypatch, cadence_s):
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        solves = fly("turn-adaptive", cadence_s, omega_dps=3.0)

        stats = track_filter.filter_stats()
        assert stats["reanchors"] == 0, f"turn re-anchored at {cadence_s}s cadence: {stats}"
        # Manoeuvre mode really did engage — the assertion above must not be
        # passing because the turn was somehow benign.
        assert any(s["sigma_a"] > track_filter._KF_SIGMA_A_MS2 for s in solves)
        assert stats["manoeuvre_active"] >= 1

        # Velocity error: bounded well under the aircraft's own speed, unlike
        # the ~350 m/s the fixed-sigma_a filter reaches (see the disabled-path
        # test).  Not bounded to a single turn interval's worth of heading
        # change: at an R sigma of 1200 m the measurements simply do not carry
        # a faster velocity estimate than that — the same 180-degree turn run
        # at a FIXED sigma_a of 800 (i.e. manoeuvre mode engaged throughout,
        # the best this Q can do) still peaks at 157 m/s of velocity error.
        in_turn = [s for s in solves if s["turn_deg"] > 0]
        peak = max(s["vel_err_ms"] for s in in_turn[2:])
        assert peak < 300.0, f"peak in-turn velocity error {peak:.0f} m/s"

    def test_the_same_turn_reanchors_with_the_adaptive_path_disabled(self, monkeypatch):
        """The control for the test above: pin the manoeuvre sigma to the base
        (the documented way to disable the whole path) and the pre-adaptive
        behaviour comes back — a re-anchor mid-turn, and a velocity error the
        size of the aircraft's own speed on the way there."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        monkeypatch.setattr(track_filter, "_KF_SIGMA_A_MANOEUVRE_MS2", track_filter._KF_SIGMA_A_MS2)

        solves = fly("turn-fixed", 4.6, omega_dps=3.0)

        assert track_filter.filter_stats()["reanchors"] >= 1
        assert max(s["sigma_a"] for s in solves) == track_filter._KF_SIGMA_A_MS2
        assert max(s["vel_err_ms"] for s in solves) > 300.0

    def test_a_ten_kilometre_jump_still_reanchors(self, monkeypatch):
        """The retry must rescue manoeuvres, not identity breaks: the gate
        value stays 13.8 for both attempts precisely so a jump no acceleration
        could produce is still refused."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "turn-jump"
        lat0, lon0 = 35.0, -82.0
        ts = 1_000_000
        for i in range(6):
            lat, lon = offset_latlon_m(lat0, lon0, east_m=250.0 * 4.6 * i, north_m=0.0)
            track_filter.smooth_solve(make_result(lat, lon, ts + int(i * 4600)), key, None)
        assert track_filter.filter_stats()["reanchors"] == 0

        jump_lat, jump_lon = offset_latlon_m(lat0, lon0, east_m=250.0 * 4.6 * 6 + 10_000.0, north_m=0.0)
        out = track_filter.smooth_solve(make_result(jump_lat, jump_lon, ts + int(6 * 4600)), key, None)
        assert "smoother" not in out  # raw passthrough: the gate re-anchored
        stats = track_filter.filter_stats()
        assert stats["reanchors"] == 1
        assert stats["manoeuvre_rescues"] == 0

    def test_a_breach_inside_the_manoeuvre_envelope_is_rescued_not_reanchored(self, monkeypatch):
        """The retry's own path, isolated from the turn: a displacement that
        breaches the gate at the base process noise but not at the manoeuvre
        one is accepted, counted as a rescue, and leaves the track's identity
        (its ENU anchor) intact."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        key = "turn-rescue"
        lat0, lon0 = 35.0, -82.0
        ts = 1_000_000
        cadence = 12.0
        # 25 solves of straight flight first: the retry can only ever rescue
        # what the manoeuvre Q's own position term (sigma_a * dt^3/3, ~680 m
        # of sigma at 12 s) widens the gate by, so the band exists only once P
        # has converged and stopped dominating S.  A young track's gate is
        # already wide enough that the two attempts agree.
        for i in range(25):
            lat, lon = offset_latlon_m(lat0, lon0, east_m=250.0 * cadence * i, north_m=0.0)
            track_filter.smooth_solve(make_result(lat, lon, ts + int(i * cadence * 1000)), key, None)
        anchor = (track_filter._KF_TRACKS[key].ref_lat, track_filter._KF_TRACKS[key].ref_lon)

        # 5.2 km off the predicted position at a 12 s cadence: outside the
        # base-Q gate, inside the manoeuvre-Q one (Q's position term at
        # sigma_a=800 over 12 s is ~680 m of sigma, comparable to R itself).
        lat, lon = offset_latlon_m(lat0, lon0, east_m=250.0 * cadence * 25, north_m=5_200.0)
        out = track_filter.smooth_solve(make_result(lat, lon, ts + int(25 * cadence * 1000)), key, None)

        stats = track_filter.filter_stats()
        assert stats["manoeuvre_rescues"] == 1, stats
        assert stats["reanchors"] == 0
        assert out["smoother"] == "kf"  # smoothed, not a raw re-anchor passthrough
        assert (track_filter._KF_TRACKS[key].ref_lat, track_filter._KF_TRACKS[key].ref_lon) == anchor
        assert track_filter._entry_sigma_a(track_filter._KF_TRACKS[key]) == track_filter._KF_SIGMA_A_MANOEUVRE_MS2

    def test_manoeuvre_engagement_decays_after_the_turn(self, monkeypatch):
        """_KF_MANOEUVRE_TAU_S is a decay constant, not a latch: 45 s after the
        last surprising update the engagement is under a tenth of full, and it
        keeps falling.

        The claim is about ENGAGEMENT, not about sigma_a landing back on 1.5
        exactly: the manoeuvre sigma is ~500x the base, so even 5% of residual
        engagement is a sigma_a near 40.  Measured on this track, engagement
        is 4.7% at 46 s past the last re-arm and 0.4% a minute after that."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        solves = fly("turn-decay", 4.6, omega_dps=3.0, turn_deg=90.0, tail_s=120.0)

        # Measured from the LAST re-arm, not from the last degree of turn:
        # the filter is still catching up for a solve or two after the roll-out
        # (its velocity state entered the turn's exit believing the turn
        # continued), and those updates are genuinely surprising, so they
        # legitimately re-engage.  What tau promises is the decay AFTER the
        # surprises stop, which is what this measures.
        assert max(s["sigma_a"] for s in solves) > track_filter._KF_SIGMA_A_MS2  # it engaged at all
        # A re-arm is any solve that did NOT decay — held at full counts,
        # which is what most of a turn looks like once engagement saturates.
        last_rearm = max(
            s["t"]
            for i, s in enumerate(solves)
            if i and s["sigma_a"] > track_filter._KF_SIGMA_A_MS2 and s["sigma_a"] >= solves[i - 1]["sigma_a"]
        )
        settled = [s for s in solves if s["t"] >= last_rearm + 45.0]
        assert settled, "test needs solves past the decay window"
        full = track_filter._KF_SIGMA_A_MS2 + 0.1 * (
            track_filter._KF_SIGMA_A_MANOEUVRE_MS2 - track_filter._KF_SIGMA_A_MS2
        )
        assert all(s["sigma_a"] < full for s in settled)
        # ...and still falling, monotonically, over the rest of the tail.
        assert settled[-1]["sigma_a"] < settled[0]["sigma_a"] * 0.2

    def test_the_reported_velocity_sigma_grows_while_manoeuvring(self, monkeypatch):
        """Point of the whole exercise downstream: dark_follow and the feed
        gate on learned_velocity's sigma, so an inflated Q has to show up
        THERE, not just inside P.  It does — the velocity block of P carries
        the manoeuvre Q term directly."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        solves = fly("turn-sigma", 4.6, omega_dps=3.0)

        straight = [s for s in solves if s["turn_deg"] == 0.0]
        engaged = [s for s in solves if s["sigma_a"] > track_filter._KF_SIGMA_A_MS2]
        assert engaged
        assert max(s["vel_sigma_ms"] for s in engaged) > min(s["vel_sigma_ms"] for s in straight[-3:])

    def test_straight_flight_never_engages_the_manoeuvre_path(self, monkeypatch):
        """The adaptive path must be inert on a clean track — this is the
        property TestStoneSoupOracle depends on to keep comparing the shipped
        CV filter against Stone-Soup step by step."""
        monkeypatch.setenv("TRACK_SMOOTHER", "kf")
        solves = fly("turn-none", 4.6, omega_dps=0.0, straight_s=200.0, turn_deg=0.0)

        assert all(s["sigma_a"] == track_filter._KF_SIGMA_A_MS2 for s in solves)
        stats = track_filter.filter_stats()
        assert stats == {"reanchors": 0, "manoeuvre_rescues": 0, "manoeuvre_active": 0, "tracks": 1}


class TestN2WeakUpdate:
    """An n=2 solve updates the filter WEAKLY (see _KF_N2_POS_SIGMA_M).

    With altitude pinned, an n=2 fit is under-determined (5 unknowns, 4
    residuals) and lands a median 1.4-2.3 km from truth against 0.3-0.5 km
    for n>=3.  Fed at the n>=3 base sigma it dragged established tracks: when
    anchored n=2 follow solves started publishing, n=3 publishes on the SAME
    keys went from 0.44 km to 1.40 km median error, because the next claim
    round dead-reckoned from the dragged state.  The n=2 solve should still
    move the state — it is evidence, and it keeps the key alive — just far
    less than a well-determined one carrying the identical innovation.
    """

    REF_LAT, REF_LON = 35.0, -82.0
    BASE_TS_MS = 1_000_000
    # Comfortably inside the innovation gate at either sigma, so this test
    # measures the gain and not a re-anchor.
    INNOVATION_M = 1500.0

    def setup_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def teardown_method(self):
        track_filter.reset()
        state.adsb_aircraft.clear()

    def _east_after_second_solve(self, n_nodes, adsb_hex=None):
        """Seed a key with an n=4 solve, then apply one displaced solve at
        ``n_nodes`` and return the filter's east position, in metres.

        The seed is identical in every variant (same n_nodes, so the same R
        seeds P), which is what makes the returned numbers comparable: the
        only thing that differs between calls is the SECOND solve's R.
        """
        track_filter.reset()
        key = f"n2-weak-{n_nodes}-{adsb_hex}"
        first = make_result(self.REF_LAT, self.REF_LON, self.BASE_TS_MS)
        first["n_nodes"] = 4
        track_filter.smooth_solve(first, key, adsb_hex)

        lat, lon = offset_latlon_m(self.REF_LAT, self.REF_LON, east_m=self.INNOVATION_M, north_m=0.0)
        second = make_result(lat, lon, self.BASE_TS_MS + 10_000)
        second["n_nodes"] = n_nodes
        track_filter.smooth_solve(second, key, adsb_hex)
        return float(track_filter._KF_TRACKS[key].x[0])

    def test_default_n2_sigma(self):
        assert track_filter._KF_N2_POS_SIGMA_M == 2500.0
        assert track_filter._KF_N2_POS_SIGMA_M > track_filter._KF_DEFAULT_POS_SIGMA_M

    def test_n2_moves_the_state_less_than_n4(self):
        moved_n2 = self._east_after_second_solve(2)
        moved_n4 = self._east_after_second_solve(4)
        # Still moves — a weak update, not a discarded one.
        assert moved_n2 > 0.0
        assert moved_n2 < moved_n4
        # And meaningfully so, not by a rounding margin: the gain scales
        # roughly as 1/sigma^2 once the base term dominates R.
        assert moved_n2 < 0.75 * moved_n4

    def test_n2_sigma_is_tunable(self, monkeypatch):
        """The constant is what _base_pos_sigma_m reads, so raising it
        weakens the n=2 update further without a code change."""
        baseline = self._east_after_second_solve(2)
        monkeypatch.setattr(track_filter, "_KF_N2_POS_SIGMA_M", 8000.0)
        assert self._east_after_second_solve(2) < baseline

    def test_adsb_identified_n2_keeps_the_default_sigma(self):
        """The ADS-B lane is untouched: its displayed position comes from the
        transponder and its guess was already a fix, so none of the dark n=2
        measurement describes it."""
        result = make_result(self.REF_LAT, self.REF_LON, self.BASE_TS_MS)
        result["n_nodes"] = 2
        assert track_filter._base_pos_sigma_m(result, "a1b2c3") == track_filter._KF_DEFAULT_POS_SIGMA_M
        assert self._east_after_second_solve(2, adsb_hex="a1b2c3") == pytest.approx(
            self._east_after_second_solve(4, adsb_hex="a1b2c3")
        )

    def test_simulator_object_id_is_judged_dark(self):
        """An ``obj-*`` simulator id rides in the same field but is not a
        transponder identity — it is a dark-lane solve and gets the wide
        sigma, the same predicate solver.py keys it into mn-dark-* with."""
        result = make_result(self.REF_LAT, self.REF_LON, self.BASE_TS_MS)
        result["n_nodes"] = 2
        assert track_filter._base_pos_sigma_m(result, "obj-01373") == track_filter._KF_N2_POS_SIGMA_M

    def test_n3_and_above_keep_the_default_sigma(self):
        for n in (0, 1, 3, 4, 5):
            result = make_result(self.REF_LAT, self.REF_LON, self.BASE_TS_MS)
            result["n_nodes"] = n
            assert track_filter._base_pos_sigma_m(result) == track_filter._KF_DEFAULT_POS_SIGMA_M
        # A result with no n_nodes at all (the field is conditional on the
        # producer) must not accidentally land on the n=2 branch either.
        assert track_filter._base_pos_sigma_m(make_result(35.0, -82.0, 1_000)) == (track_filter._KF_DEFAULT_POS_SIGMA_M)
