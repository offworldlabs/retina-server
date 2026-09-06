"""The calibrated solve-uncertainty model (services/solve_uncertainty.py).

Pins the shape of the 2026-09-05 fit rather than its numbers alone: the floor
is per node-count, the formal LM-fit sigma adds IN QUADRATURE (not linearly)
and only after being capped, a missing/degenerate formal sigma degrades to the
floor rather than to zero, and no n_nodes means no answer at all.

Env-derived constants are exercised by monkeypatching the module attributes,
not by reimporting with a different environment — they are read once at import
by design (see the module docstring), and a test that reimported would be
testing importlib.
"""

import math
import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from services import solve_uncertainty as su  # noqa: E402
from services import track_filter  # noqa: E402
from services.geo import offset_latlon_m  # noqa: E402

LAT, LON = 35.0, -82.0


def _result(n_nodes=None, pos_sigma_km=None) -> dict:
    """A solver-result-shaped dict.  Fields are OMITTED unless given, matching
    the solver's own conditional fields — pos_sigma_km is absent on a solve
    whose Jacobian could not produce one, not present-and-None.  (Tests that
    want it present-and-None build the dict inline.)"""
    r: dict = {}
    if n_nodes is not None:
        r["n_nodes"] = n_nodes
    if pos_sigma_km is not None:
        r["pos_sigma_km"] = pos_sigma_km
    return r


class TestFloors:
    """With no usable formal sigma, sigma_solve IS the node-count floor."""

    @pytest.mark.parametrize(
        ("n_nodes", "expected"),
        [(2, 650.0), (3, 210.0), (4, 180.0), (7, 180.0)],
    )
    def test_floor_per_node_count(self, n_nodes, expected):
        assert su.solve_sigma_m(_result(n_nodes=n_nodes), dark=False) == pytest.approx(expected)

    def test_n_nodes_missing_returns_none(self):
        assert su.solve_sigma_m(_result(pos_sigma_km=0.5), dark=False) is None
        assert su.solve_sigma_m({}, dark=True) is None

    def test_n_nodes_explicit_none_returns_none(self):
        assert su.solve_sigma_m({"n_nodes": None, "pos_sigma_km": 0.5}, dark=False) is None


class TestFormalTerm:
    def test_formal_adds_in_quadrature(self):
        # 1 km formal at n>=4: sqrt(1000^2 + 180^2), NOT 1000 + 180 and not
        # 1000 alone — the two error sources are independent.
        got = su.solve_sigma_m(_result(n_nodes=4, pos_sigma_km=1.0), dark=False)
        assert got == pytest.approx(math.sqrt(1000.0**2 + 180.0**2))

    def test_formal_is_capped_before_it_is_used(self):
        # The degenerate tail: 3.8e9 m of formal sigma (near-parallel
        # baselines) contributes the 3000 m cap, not 3.8e9.
        got = su.solve_sigma_m(_result(n_nodes=4, pos_sigma_km=3.8e6), dark=False)
        assert got == pytest.approx(math.sqrt(3000.0**2 + 180.0**2))
        assert got <= 5000.0

    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1.0, 0.0])
    def test_unusable_formal_degrades_to_the_floor(self, bad):
        assert su.solve_sigma_m({"n_nodes": 3, "pos_sigma_km": bad}, dark=False) == pytest.approx(210.0)

    def test_absent_formal_matches_zero_formal(self):
        assert su.solve_sigma_m(_result(n_nodes=3), dark=False) == su.solve_sigma_m(
            {"n_nodes": 3, "pos_sigma_km": 0.0}, dark=False
        )

    def test_formal_gain_scales_only_the_formal_term(self, monkeypatch):
        monkeypatch.setattr(su, "_FORMAL_GAIN", 2.0)
        got = su.solve_sigma_m(_result(n_nodes=4, pos_sigma_km=1.0), dark=False)
        assert got == pytest.approx(math.sqrt(2000.0**2 + 180.0**2))


class TestDarkGain:
    def test_dark_inflates_by_the_gain(self):
        known = su.solve_sigma_m(_result(n_nodes=3), dark=False)
        dark = su.solve_sigma_m(_result(n_nodes=3), dark=True)
        assert known == pytest.approx(210.0)
        assert dark == pytest.approx(210.0 * 1.5)

    def test_dark_gain_is_env_tunable(self, monkeypatch):
        monkeypatch.setattr(su, "_DARK_GAIN", 1.0)
        assert su.solve_sigma_m(_result(n_nodes=2), dark=True) == pytest.approx(650.0)


class TestClamp:
    def test_floor_of_the_clamp_binds(self, monkeypatch):
        # Only reachable with the floors tuned absurdly low — the point is
        # that a mis-set env key cannot produce a disc claiming centimetre
        # accuracy.
        monkeypatch.setattr(su, "_FLOOR_N4_M", 1.0)
        assert su.solve_sigma_m(_result(n_nodes=4), dark=False) == pytest.approx(50.0)

    def test_ceiling_of_the_clamp_binds(self, monkeypatch):
        monkeypatch.setattr(su, "_DARK_GAIN", 100.0)
        assert su.solve_sigma_m(_result(n_nodes=2), dark=True) == pytest.approx(5000.0)

    def test_returns_a_float(self):
        assert isinstance(su.solve_sigma_m(_result(n_nodes=2), dark=False), float)


class TestGrownSigma:
    def test_growth_adds_in_quadrature(self):
        assert su.grown_sigma_m(180.0, 25.0, 10.0) == pytest.approx(math.sqrt(180.0**2 + 250.0**2))

    def test_zero_age_is_the_solve_sigma(self):
        assert su.grown_sigma_m(650.0, 25.0, 0.0) == pytest.approx(650.0)

    def test_growth_is_capped_at_the_growth_horizon(self):
        # The horizon mirrors the frontend's UNCERTAINTY_DR_CAP_S (30 s),
        # which in turn mirrors MN_DARK_EXPIRY_S.
        assert su._GROWTH_MAX_AGE_S == 30.0
        at_cap = su.grown_sigma_m(180.0, 25.0, su._GROWTH_MAX_AGE_S)
        assert su.grown_sigma_m(180.0, 25.0, 600.0) == pytest.approx(at_cap)
        assert at_cap == pytest.approx(math.sqrt(180.0**2 + 750.0**2))

    def test_negative_age_does_not_shrink_or_grow(self):
        assert su.grown_sigma_m(650.0, 25.0, -5.0) == pytest.approx(650.0)

    def test_growth_is_monotonic_up_to_the_cap(self):
        seq = [su.grown_sigma_m(210.0, 30.0, t) for t in (0.0, 5.0, 20.0, 29.0)]
        assert seq == sorted(seq)
        assert seq[0] < seq[-1]


class TestVelocitySigma:
    """The growth term's sigma_v: the KF's own velocity marginal when it has
    one, the env default when it does not."""

    def setup_method(self):
        track_filter.reset()

    def teardown_method(self):
        track_filter.reset()

    def _seed_kf_entry(self, key: str, v_north_true: float = 50.0) -> None:
        """Two solves 20 s apart moving purely north, vel_east/vel_north left
        at 0.0 on the fed results so the filter's velocity state comes from
        the POSITION sequence rather than an echoed prior — same discipline as
        test_track_filter.TestLearnedVelocity."""
        lat2, lon2 = offset_latlon_m(LAT, LON, east_m=0.0, north_m=v_north_true * 20.0)
        track_filter.smooth_solve(
            {"lat": LAT, "lon": LON, "timestamp_ms": 1_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )
        track_filter.smooth_solve(
            {"lat": lat2, "lon": lon2, "timestamp_ms": 21_000, "vel_east": 0.0, "vel_north": 0.0}, key, None
        )

    def test_unknown_key_falls_back_to_the_default(self):
        assert track_filter.learned_velocity("never-smoothed") is None
        assert su.velocity_sigma_ms("never-smoothed") == pytest.approx(25.0)

    def test_default_is_env_tunable(self, monkeypatch):
        monkeypatch.setattr(su, "_VEL_DEFAULT_MS", 40.0)
        assert su.velocity_sigma_ms("never-smoothed") == pytest.approx(40.0)

    def test_kf_state_supplies_the_sigma(self):
        key = "mn-dark-vsig"
        self._seed_kf_entry(key)
        lv = track_filter.learned_velocity(key)
        assert lv is not None
        assert su.velocity_sigma_ms(key) == pytest.approx(lv[2])
        # Genuinely the filter's number, not the fallback that would also be
        # returned if the lookup silently failed.
        assert su.velocity_sigma_ms(key) != pytest.approx(25.0)
        assert 5.0 <= su.velocity_sigma_ms(key) <= 150.0

    @pytest.mark.parametrize(("raw", "expected"), [(0.5, 5.0), (4.9, 5.0), (900.0, 150.0), (42.0, 42.0)])
    def test_kf_sigma_is_clamped(self, monkeypatch, raw, expected):
        monkeypatch.setattr(track_filter, "learned_velocity", lambda k: (0.0, 0.0, raw, 0.0))
        assert su.velocity_sigma_ms("any") == pytest.approx(expected)

    def test_non_finite_kf_sigma_falls_back(self, monkeypatch):
        monkeypatch.setattr(track_filter, "learned_velocity", lambda k: (0.0, 0.0, float("nan"), 0.0))
        assert su.velocity_sigma_ms("any") == pytest.approx(25.0)
