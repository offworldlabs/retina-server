"""Tests for top-down claiming's solver-side integration.

Two things are pinned here:

- multinode_key_decision, the pure keying rule extracted from the old
  multinode_key_decision (precedent: claim_decision) — adsb precedence,
  anchor honoring (and its distance/liveness/dark-only guards), the
  proximity scan and its age-scaled gate (TestAgeScaledProximityGate
  below), and minting.
- Its threading through _process_solver_item: an anchor_key on the solver
  input drives publication onto the anchor's own key, bumps the
  solver_anchor_* / solver_anchored_published counters, is carried into
  mlat_solve_history, and — the regression pin — an anchored n=2 input
  with no cv_epochs is withheld exactly like any other unfitted n=2
  pairing, never publishing under a phantom fit.

Style follows test_solver_consensus.py / test_solver_worker.py /
test_track_claim.py.
"""

import time

import pytest

from core import state
from services.geo import offset_latlon_m
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0

# An n=2 solver input whose track pairing has already passed the
# constant-velocity fit — same shape as test_solver_worker.py's
# _CONFIRMED_N2, so the n=2 confirmation gate is a non-issue for the tests
# that are not specifically about it.
_CONFIRMED_N2 = {"n_nodes": 2, "chi2_per_dof": 0.5, "n_epochs": 8}


def _reset():
    state._reset_for_tests()
    solver_mod._reset_for_tests()


def _anchor_track(**overrides) -> dict:
    """A live mn-dark-* entry in state.multinode_tracks, at (LAT, LON)."""
    rec = {
        "lat": LAT,
        "lon": LON,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": int(time.time() * 1000) - 5000,
        "n_nodes": 2,
        "solve_count": 2,
    }
    rec.update(overrides)
    return rec


def _dark_entry(ts_ms: int, **overrides) -> dict:
    """A dark entry at (LAT, LON) last solved at ``ts_ms``, standing still.

    Clock-free, unlike _anchor_track above: the age-scaled gate is a function
    of the entry's age, so these tests state both timestamps outright rather
    than working relative to time.time().
    """
    rec = {
        "lat": LAT,
        "lon": LON,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": ts_ms,
        "n_nodes": 3,
        "solve_count": 2,
    }
    rec.update(overrides)
    return rec


def _north_of(km: float) -> tuple[float, float]:
    """(lat, lon) ``km`` north of (LAT, LON), via the same geo helper the
    rule dead-reckons with."""
    return offset_latlon_m(LAT, LON, east_m=0.0, north_m=km * 1000.0)


def _solve_fn(lat=LAT, lon=LON, ts_ms=None, node_ids=("n1", "n2")):
    def fn(s_in, cfgs):
        return {
            "success": True,
            "lat": lat,
            "lon": lon,
            "timestamp_ms": ts_ms if ts_ms is not None else int(time.time() * 1000),
            "contributing_node_ids": list(node_ids),
        }

    return fn


class TestMultinodeKeyDecision:
    """The pure rule, units — same style as claim_decision's TestCompetition
    etc. in test_track_claim.py."""

    def test_adsb_takes_precedence_over_an_anchor(self):
        tracks = {"mn-dark-1": _anchor_track()}
        key, how, _dist = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            "abc123",
            "mn-dark-1",
        )
        assert (key, how) == ("mn-adsb-abc123", "adsb")

    def test_anchor_hit_when_close_and_still_live(self):
        tracks = {"mn-dark-anchor": _anchor_track()}
        # ~1.1 km away — comfortably inside the 6 km default.
        key, how, _dist = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT + 0.01, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-anchor",
        )
        assert (key, how) == ("mn-dark-anchor", "anchor")

    def test_missing_anchor_key_falls_back_to_mint(self):
        """anchor_key points at nothing live, and there is nothing else to
        fall back to by proximity either — mints, same as no anchor at all."""
        key, how, _dist = solver_mod.multinode_key_decision(
            {},
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-does-not-exist",
        )
        assert how == "minted"
        assert key.startswith("mn-dark-1000-")

    def test_anchor_beyond_max_dist_km_falls_back(self):
        tracks = {"mn-dark-anchor": _anchor_track()}
        # ~11 km away — past the 6 km default max_dist_km.  The consensus-
        # anchored-displacement edge case this distance check exists for.
        key, how, _dist = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT + 0.1, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-dark-anchor",
        )
        assert how != "anchor"
        assert key != "mn-dark-anchor"

    def test_non_dark_anchor_key_is_refused(self):
        """An anchor_key must start mn-dark-; a caller passing an ADS-B key
        (should never happen — only dark tracks are claim_eligible — but the
        rule itself must refuse it, not trust the caller)."""
        tracks = {"mn-adsb-abc": _anchor_track()}
        key, how, _dist = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            "mn-adsb-abc",
        )
        assert how != "anchor"

    def test_mints_a_new_key_with_no_anchor_and_no_claimant(self):
        key, how, _dist = solver_mod.multinode_key_decision(
            {},
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            None,
            None,
        )
        assert how == "minted"
        assert key.startswith("mn-dark-1000-")

    def test_non_transponder_adsb_hex_cannot_take_the_adsb_branch(self):
        """A simulator object id (or any non-transponder string) reaching
        adsb_hex must fall through to the dark branches — keying mn-adsb-obj-*
        put dark targets in the ADS-B lane and starved mn-dark-* entirely
        (observed live 2026-08-26)."""
        tracks = {"mn-dark-1": _anchor_track()}
        key, how, _dist = solver_mod.multinode_key_decision(
            tracks,
            {"lat": LAT, "lon": LON, "timestamp_ms": int(time.time() * 1000)},
            "obj-01373",
            None,
        )
        assert how == "proximity"
        assert key == "mn-dark-1"

    def test_tisb_tilde_adsb_hex_still_takes_the_adsb_branch(self):
        key, how, _dist = solver_mod.multinode_key_decision(
            {},
            {"lat": LAT, "lon": LON, "timestamp_ms": 1000},
            "~abc123",
            None,
        )
        assert (key, how) == ("mn-adsb-~abc123", "adsb")


class TestAgeScaledProximityGate:
    """The proximity gate grows with the age of the entry being matched.

    The scan does not compare two simultaneous positions: it compares this
    solve against an existing entry dead-reckoned forward over dt.  That
    prediction is only as good as the velocity behind it, measured at a
    median 127 m/s vector error, so its uncertainty grows by
    _MN_ASSOC_DRIFT_KM_PER_S every second while a flat 6 km gate does not.

    Live (26 min, this deployment): of 57 dark key births, 11 landed 6-10 km
    from a dark entry that then disappeared within 10 s — one aircraft
    re-keyed because its predecessor's dead-reckoned position had drifted
    past the flat gate.  Those 11 are the population these tests describe.
    """

    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def _decide(self, tracks, solve_ts_ms, km_north, **kwargs):
        lat, lon = _north_of(km_north)
        return solver_mod.multinode_key_decision(
            tracks,
            {"lat": lat, "lon": lon, "timestamp_ms": solve_ts_ms},
            None,
            kwargs.pop("anchor_key", None),
            **kwargs,
        )

    # ── the formula itself ──────────────────────────────────────────────────

    def test_gate_starts_at_the_flat_radius(self):
        assert solver_mod._mn_assoc_gate_km(0.0) == solver_mod._MN_ASSOC_MAX_DIST_KM

    def test_gate_grows_by_the_measured_drift_rate(self):
        assert solver_mod._mn_assoc_gate_km(30.0) == pytest.approx(9.9)

    def test_gate_is_capped_and_stays_capped_to_the_age_limit(self):
        assert solver_mod._mn_assoc_gate_km(50.0) == solver_mod._MN_ASSOC_MAX_DIST_CAP_KM
        assert solver_mod._mn_assoc_gate_km(solver_mod._MN_ASSOC_MAX_AGE_S) == pytest.approx(12.0)

    def test_a_widened_base_is_never_narrowed_by_the_cap(self):
        """The cap is a ceiling on the DRIFT allowance, not a veto on the
        caller's own base — a bench sweeping max_dist_km past 12 km must get
        at least what it asked for."""
        assert solver_mod._mn_assoc_gate_km(60.0, base_km=20.0) == pytest.approx(20.0)

    # ── what that buys, at the rule ─────────────────────────────────────────

    def test_eight_km_from_a_30s_old_entry_rekeys_onto_it(self):
        """The 6-10 km re-key band: 8 km is past the flat 6 km and inside
        the 9.9 km gate a 30 s-old entry earns."""
        tracks = {"mn-dark-old": _dark_entry(ts_ms=100_000)}
        key, how, dist = self._decide(tracks, 130_000, 8.0)
        assert (key, how) == ("mn-dark-old", "proximity")
        assert dist == pytest.approx(8.0, abs=0.1)

    def test_the_same_eight_km_from_a_2s_old_entry_mints(self):
        """Nothing has been widened for a fresh entry: at dt=2 the gate is
        6.26 km, so 8 km is still a different aircraft."""
        tracks = {"mn-dark-fresh": _dark_entry(ts_ms=100_000)}
        key, how, dist = self._decide(tracks, 102_000, 8.0)
        assert how == "minted"
        assert key.startswith("mn-dark-102000-")
        assert dist is None

    def test_the_cap_holds_at_the_age_limit(self):
        tracks = {"mn-dark-ancient": _dark_entry(ts_ms=100_000)}
        _, how_in, _ = self._decide(tracks, 160_000, 11.5)
        assert how_in == "proximity"
        _, how_out, _ = self._decide(tracks, 160_000, 12.5)
        assert how_out == "minted"

    def test_the_age_window_itself_is_unchanged(self):
        """Past _MN_ASSOC_MAX_AGE_S the entry is one the map has already
        dropped — the wider gate must not reach across the expiry."""
        tracks = {"mn-dark-expired": _dark_entry(ts_ms=100_000)}
        _, how, _ = self._decide(tracks, 170_000, 1.0)
        assert how == "minted"

    # ── competition between candidates ──────────────────────────────────────

    def test_a_fresh_close_entry_beats_an_old_far_one(self):
        tracks = {
            "mn-dark-far-old": _dark_entry(ts_ms=100_000),
            "mn-dark-near-fresh": dict(_dark_entry(ts_ms=139_000), lat=_north_of(1.0)[0]),
        }
        key, how, _ = self._decide(tracks, 140_000, 1.0)
        assert (key, how) == ("mn-dark-near-fresh", "proximity")

    def test_normalising_by_the_gate_beats_ranking_on_raw_kilometres(self):
        """The candidate nearer in km is 7 km from an entry solved 1 s ago —
        outside its own 6.13 km gate.  The farther one is 8 km from an entry
        solved 40 s ago, comfortably inside its 11.2 km gate.  Ranking on raw
        distance would take the first and mis-key the solve; d / gate_km
        takes the second, which is the only one whose prediction actually
        supports the match."""
        tracks = {
            "mn-dark-fresh-7km": dict(_dark_entry(ts_ms=139_000), lat=_north_of(15.0)[0]),
            "mn-dark-old-8km": _dark_entry(ts_ms=100_000),
        }
        key, how, dist = self._decide(tracks, 140_000, 8.0)
        assert (key, how) == ("mn-dark-old-8km", "proximity")
        assert dist == pytest.approx(8.0, abs=0.1)

    # ── the velocity the prediction is made with ────────────────────────────

    def test_learned_velocity_is_preferred_when_the_filter_has_it(self):
        """The entry's own solved velocity says it went south; the KF says
        north.  Matching the feed matters because the feed DRAWS the entry
        with the learned vector — keying off the other one would associate a
        solve to a track that is not under it on the map."""
        tracks = {"mn-dark-kf": _dark_entry(ts_ms=100_000, vel_north=-300.0)}
        # 20 s later: raw DR puts the entry 6 km south, learned DR 6 km north.
        key, how, dist = self._decide(tracks, 120_000, 6.0, learned_vel_fn=lambda _k: (0.0, 300.0, 5.0, 0.0))
        assert (key, how) == ("mn-dark-kf", "proximity")
        assert dist == pytest.approx(0.0, abs=0.1)

    def test_no_learned_velocity_falls_back_to_the_entrys_own(self):
        """Same geometry, no filter state — the fallback dead-reckons the
        entry 6 km SOUTH, 12 km from the solve and past the 8.6 km gate.
        This is also the off-path: TRACK_SMOOTHER != kf, a first solve, a
        TTL-swept key, and the offline bench all arrive here."""
        tracks = {"mn-dark-kf": _dark_entry(ts_ms=100_000, vel_north=-300.0)}
        _, how, _ = self._decide(tracks, 120_000, 6.0, learned_vel_fn=lambda _k: None)
        assert how == "minted"

    def test_track_dr_source_solve_pins_the_entrys_own_velocity(self, monkeypatch):
        """The feed's rollback switch reaches the key decision too, so the
        two cannot end up dead-reckoning the same entry differently."""
        monkeypatch.setenv("TRACK_DR_SOURCE", "solve")
        tracks = {"mn-dark-kf": _dark_entry(ts_ms=100_000, vel_north=-300.0)}
        _, how, _ = self._decide(tracks, 120_000, 6.0, learned_vel_fn=lambda _k: (0.0, 300.0, 5.0, 0.0))
        assert how == "minted"

    # ── the anchor branch is deliberately not age-scaled ────────────────────

    def test_the_anchor_branch_keeps_the_flat_radius(self):
        """8 km from a 30 s-old anchor is inside the PROXIMITY gate but past
        the anchor branch's flat 6 km: the anchor check asks whether this
        solve converged onto the aircraft the claim named, which is not a
        dead-reckoning question and gets no drift allowance.  The key still
        comes out the same here — by proximity, on its own merits."""
        tracks = {"mn-dark-anchored": _dark_entry(ts_ms=100_000)}
        key, how, _ = self._decide(tracks, 130_000, 8.0, anchor_key="mn-dark-anchored")
        assert key == "mn-dark-anchored"
        assert how == "proximity"

    def test_an_anchor_inside_the_flat_radius_still_reports_its_distance(self):
        tracks = {"mn-dark-anchored": _dark_entry(ts_ms=100_000)}
        key, how, dist = self._decide(tracks, 130_000, 2.0, anchor_key="mn-dark-anchored")
        assert (key, how) == ("mn-dark-anchored", "anchor")
        # Flat, un-dead-reckoned distance — the anchor branch measures against
        # the entry as it stands, not against a prediction.
        assert dist == pytest.approx(2.0, abs=0.1)


class TestProcessSolverItemAnchorHonoring:
    def setup_method(self):
        _reset()

    def teardown_method(self):
        _reset()

    def test_anchored_publish_lands_on_the_anchor_key(self):
        state.multinode_tracks["mn-dark-anchor-1"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-1")
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn())

        assert result is not None
        assert "mn-dark-anchor-1" in state.multinode_tracks
        assert state.solver_anchored_published == 1
        assert state.solver_anchor_hits == 1
        assert state.solver_anchor_fallbacks == 0

    def test_fallback_still_publishes_and_counts_as_a_fallback(self):
        """Anchor proposed but too far from THIS solve's own result — falls
        through to mint, still publishes, and is attributed as a fallback
        rather than a hit."""
        state.multinode_tracks["mn-dark-anchor-2"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-2")
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn(lat=LAT + 0.5, lon=LON))  # ~55 km away

        assert result is not None
        assert state.solver_anchored_published == 1
        assert state.solver_anchor_hits == 0
        assert state.solver_anchor_fallbacks == 1

    def test_history_carries_the_anchor_key(self):
        state.multinode_tracks["mn-dark-anchor-3"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-3")
        item = (s_in, {}, time.time())

        solver_mod._process_solver_item(item, _solve_fn())

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "published"
        assert rec["anchor_key"] == "mn-dark-anchor-3"

    def test_anchor_key_is_recorded_on_rejects_too(self):
        """Windowed fragmentation needs the field on every attempt, not just
        successful publishes — solver.py stamps it unconditionally, before
        any gate has a chance to reject."""
        state.multinode_tracks["mn-dark-anchor-4"] = _anchor_track()
        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-4")
        item = (s_in, {}, time.time())

        def unconverged_solve_fn(s_in, cfgs):
            return {"success": False}  # LM ran, did not converge

        solver_mod._process_solver_item(item, unconverged_solve_fn)

        rec = state.mlat_solve_history[-1]
        assert rec["outcome"] == "unconverged"
        assert rec["anchor_key"] == "mn-dark-anchor-4"

    def test_supersession_merges_a_proximity_minted_fragment_into_the_anchor(self):
        """A fragment built from EXACTLY the anchor's source tracks, minted
        earlier under a different key (bottom-up path, missed the 6 km
        match), is absorbed into the anchor when the anchor's own solve
        arrives — never the reverse: the anchor's key is what survives.

        111 km away, so nothing spatial rescues it: this is
        _supersession_match's identical-inputs branch, and the reason that
        branch exists.  Same measurements in, same aircraft, however far
        apart the two solves converged."""
        state.multinode_tracks["mn-dark-anchor-5"] = _anchor_track(source_track_ids=["t1", "t2"])
        state.multinode_tracks["mn-dark-fragment"] = _anchor_track(
            lat=LAT + 1.0, solve_count=1, source_track_ids=["t1", "t2"]
        )

        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-5", track_ids=["t1", "t2"])
        item = (s_in, {}, time.time())
        solver_mod._process_solver_item(item, _solve_fn())

        assert "mn-dark-anchor-5" in state.multinode_tracks
        assert "mn-dark-fragment" not in state.multinode_tracks
        assert state.mn_superseded == 1
        assert state.mn_superseded_blocked == 0
        # solve_count carries forward through the merge.
        assert state.multinode_tracks["mn-dark-anchor-5"]["solve_count"] == 3

    def test_supersession_refuses_a_far_fragment_that_only_partially_shares_inputs(self):
        """The other half of the test above, and the whole point of the
        guard: an entry 111 km away that shares ONE tracker track with this
        solve rather than being built from a subset of its inputs is a
        different aircraft whose candidate happened to carry a common track.
        It keeps its key; the refusal is counted."""
        state.multinode_tracks["mn-dark-anchor-7"] = _anchor_track(source_track_ids=["t1", "t2"])
        state.multinode_tracks["mn-dark-neighbour"] = _anchor_track(
            lat=LAT + 1.0, solve_count=1, source_track_ids=["t1", "t9"]
        )

        s_in = dict(_CONFIRMED_N2, anchor_key="mn-dark-anchor-7", track_ids=["t1", "t2"])
        item = (s_in, {}, time.time())
        solver_mod._process_solver_item(item, _solve_fn())

        assert "mn-dark-anchor-7" in state.multinode_tracks
        assert "mn-dark-neighbour" in state.multinode_tracks
        assert state.multinode_tracks["mn-dark-neighbour"]["solve_count"] == 1
        assert state.mn_superseded == 0
        assert state.mn_superseded_blocked == 1

    def test_anchored_n2_without_cv_epochs_is_withheld(self):
        """Regression pin: an anchored n=2 input MUST carry cv_epochs (built
        by _merge_epochs_multi) or it dies at this gate forever — without
        them _resolve_cv_fit has nothing to fit, chi2_per_dof never
        resolves, and the n=2 confirmation gate withholds it exactly like
        any other unfitted pairing.  It must NOT publish just because it
        has an anchor_key."""
        state.multinode_tracks["mn-dark-anchor-6"] = _anchor_track()
        s_in = {"n_nodes": 2, "anchor_key": "mn-dark-anchor-6"}  # no cv_epochs
        item = (s_in, {}, time.time())

        result = solver_mod._process_solver_item(item, _solve_fn())

        assert result is not None  # the solve itself ran
        assert state.n2_unconfirmed == 1
        assert state.solver_anchored_published == 0
        assert state.solver_anchor_hits == 0
        # The pre-existing anchor entry is untouched — nothing was written.
        assert state.multinode_tracks["mn-dark-anchor-6"]["solve_count"] == 2
