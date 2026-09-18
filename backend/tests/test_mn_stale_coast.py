"""Mint-time retirement of coasting dark keys (_stale_coast_candidate).

The hard-turn re-key, which the supersession block in _process_solver_item
cannot reach.  When a dark aircraft turns hard the KF's manoeuvre boost pushes
its velocity sigma past DARK_FOLLOW_MAX_VEL_SIGMA_MS, dark_follow drops the
key, and the next bottom-up solve of the same aircraft MINTS a second one.
Supersession is blind to that pair twice over: its prefilter wants a shared
source track id (node tracks renumber through a turn, so there is none) and
its spatial branch measures the old entry's DEAD-RECKONED position, which the
turn is precisely what invalidated — the old key is coasting off on the frozen
pre-turn velocity.  Measured over four 20-minute ground-truth captures on the
test droplet, 9 of 13 re-keyed turns left the old key drawn for a median 52 s.

So a minted dark key asks _stale_coast_candidate whether it is replacing
someone, judging RAW solve position against RAW solve position and requiring
evidence of the turn itself — a dark_follow drop inside its cooldown, or a
live KF manoeuvre level.  The winner is retired: its trail is transplanted
onto the new hex, its solve_count / max_n_nodes / recent_track_ids / anomaly
latch carry forward, result["predecessor_key"] names it, and _forget_mn_key
erases it (its KF state included — the pre-turn velocity is the bad one).

The geometry every test here shares is the geometry of the real event: the old
entry's last SOLVE is 4 km from the new one, but it has been coasting for 20 s
at 250 m/s, so where the key decision looks for it — 9 km out along the
pre-turn heading — is outside even the age-grown 8.6 km proximity gate.  That
is what makes the new solve a mint rather than a re-key, and it is the whole
reason raw-against-raw is the only distance that can see the pair.
"""

import time
from collections import deque

import pytest

from core import state
from services import dark_follow, track_filter
from services.geo import offset_latlon_m
from services.id_utils import multinode_hex_from_key
from services.tasks import multinode_identity as identity_mod
from services.tasks import solver as solver_mod

LAT, LON = 35.0, -82.0
OLD_KEY = "mn-dark-coasting"

# The coasting entry's age and speed.  20 s is inside MN_STALE_COAST_MIN_S..
# MAX_S; 250 m/s due north for 20 s is 5 km of dead reckoning, which added to
# the 4 km raw separation puts the entry's DRAWN position 9 km from this solve
# — past the 6.0 + 0.13*20 = 8.6 km key-decision gate, so the solve mints.
COAST_AGE_S = 20.0
COAST_VEL_NORTH_MS = 250.0
COAST_SEP_M = 4000.0


def _solve_fn(lat=LAT, lon=LON, **overrides):
    """A solve_fn returning a successful n=3 result at (lat, lon), now."""
    result = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": 7000.0,
        "timestamp_ms": int(time.time() * 1000),
        "vel_east": 0.0,
        "vel_north": 0.0,
        "rms_delay": 1.0,
        "rms_doppler": 5.0,
        "n_nodes": 3,
        "n_measurements": 3,
        "contributing_node_ids": ["n1", "n2", "n3"],
    }
    result.update(overrides)

    def fn(s_in, cfgs):
        return dict(result)

    return fn


def _seed_coasting_entry(
    key: str = OLD_KEY,
    age_s: float = COAST_AGE_S,
    alt_m: float = 7000.0,
    sep_m: float = COAST_SEP_M,
    solve_count: int = 9,
) -> dict:
    """The entry a hard turn leaves behind: last solved ``age_s`` ago, ``sep_m``
    north of where the aircraft is now, still holding the pre-turn velocity.

    Its source_track_ids are disjoint from every solve below on purpose —
    renumbered node tracks are why supersession never sees this entry, and a
    test that shared an id would be exercising that path instead of this one.
    """
    lat, lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=sep_m)
    entry = {
        "success": True,
        "lat": lat,
        "lon": lon,
        "alt_m": alt_m,
        "vel_east": 0.0,
        "vel_north": COAST_VEL_NORTH_MS,
        "rms_delay": 0.1,
        "rms_doppler": 1.0,
        "n_nodes": 4,
        "n_measurements": 4,
        "contributing_node_ids": ["n1", "n2", "n3", "n4"],
        "timestamp_ms": int((time.time() - age_s) * 1000),
        "solve_count": solve_count,
        "max_n_nodes": 4,
        "source_track_ids": ["t90", "t91"],
    }
    state.multinode_tracks[key] = entry
    return entry


def _seed_trail(key: str, n: int = 7) -> list:
    """A rendered trail on ``key``'s hex, in both stores, as the feed writes it."""
    hex_code = multinode_hex_from_key(key)
    points = [[LAT + 0.01 * i, LON, 23000.0, time.time() - (n - i)] for i in range(n)]
    for store in (state.track_histories, state.track_histories_public):
        store[hex_code] = deque(points, maxlen=state.TRACK_HISTORY_MAX)
    return points


class TestRetirementAtMint:
    """The publish path: a mint that finds a coasting predecessor, and the
    four ways it refuses to."""

    def setup_method(self):
        state._reset_for_tests()
        solver_mod._reset_for_tests()
        dark_follow._reset_for_tests()

    def teardown_method(self):
        solver_mod._reset_for_tests()
        dark_follow._reset_for_tests()

    def _run(self, s_in=None, solve_fn=None):
        return solver_mod._process_solver_item(
            (dict(s_in or {"n_nodes": 3, "track_ids": ["t1", "t2"]}), {}, time.time()),
            solve_fn or _solve_fn(),
        )

    def _new_key(self) -> str:
        (key,) = [k for k in state.multinode_tracks if k != OLD_KEY]
        return key

    def test_turn_dropped_key_is_retired_and_its_trail_transplanted(self):
        """The whole feature, end to end.  The follow lane dropped the key a
        moment ago (its cooldown is still running), the raw positions are 4 km
        apart at the same altitude, and the mint retires it: the entry is gone
        from every store, its trail is now the new hex's, and the new entry
        names it.
        """
        _seed_coasting_entry()
        old_points = _seed_trail(OLD_KEY)
        old_hex = multinode_hex_from_key(OLD_KEY)
        with state.anomaly_lock:
            state.anomaly_hexes.add(old_hex)
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        result = self._run()

        assert OLD_KEY not in state.multinode_tracks
        new_key = self._new_key()
        assert result["predecessor_key"] == OLD_KEY
        assert state.multinode_tracks[new_key]["predecessor_key"] == OLD_KEY
        # The trail moved rather than being abandoned on the dead hex.
        new_hex = multinode_hex_from_key(new_key)
        assert [p[:2] for p in state.track_histories[new_hex]] == [p[:2] for p in old_points]
        assert list(state.track_histories_public[new_hex]) == list(state.track_histories[new_hex])
        assert not state.track_histories[old_hex]
        # ...and _forget_mn_key took the rest of the old key with it.
        assert old_hex not in state.anomaly_hexes
        # Carried forward the way supersession carries it: the retired key's
        # solve_count (9) plus this solve, and its 4-node high-water mark, so
        # the re-keyed aircraft is not hidden again by the n=2 display gate or
        # dropped by DARK_FOLLOW_MIN_NODES on the next rebuild.
        assert state.multinode_tracks[new_key]["solve_count"] == 10
        assert state.multinode_tracks[new_key]["max_n_nodes"] == 4
        assert state.mn_stale_coast_retired == 1
        assert state.mn_stale_coast_none == 0

    def test_kf_manoeuvre_level_is_evidence_on_its_own(self, monkeypatch):
        """The other half of the evidence test.  dark_follow never dropped
        this key — the lane may be off entirely — but the display filter still
        reports it manoeuvring, which is the same turn seen from the other
        side.
        """
        _seed_coasting_entry()
        monkeypatch.setattr(
            solver_mod.track_filter,
            "manoeuvre_level",
            lambda k: 0.9 if k == OLD_KEY else None,
        )

        self._run()

        assert OLD_KEY not in state.multinode_tracks
        assert state.mn_stale_coast_retired == 1

    def test_no_turn_evidence_keeps_the_key(self):
        """The refusal that matters most.  Same geometry, same age, same
        altitude — and nothing says this key was turning.  Proximity alone is
        what popped 63 of 129 neighbours before the 2026-09-05 supersession
        guard, and this gate is looser in space than that one, so it does not
        get to decide anything by itself.
        """
        _seed_coasting_entry()

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert state.multinode_tracks[OLD_KEY]["solve_count"] == 9
        assert "predecessor_key" not in state.multinode_tracks[self._new_key()]
        assert state.mn_stale_coast_retired == 0
        assert state.mn_stale_coast_blocked_evidence == 1

    def test_altitude_mismatch_keeps_the_key(self):
        """A turn-dropped key 2.5 km below the solve is a different aircraft
        in the same sector — the neighbour-pop signature
        mn_superseded_blocked_alt was added for, counted separately here for
        the same reason.
        """
        _seed_coasting_entry(alt_m=4500.0)
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert state.mn_stale_coast_retired == 0
        assert state.mn_stale_coast_blocked_alt == 1
        # The evidence test is never reached, so it must not be the one blamed.
        assert state.mn_stale_coast_blocked_evidence == 0

    def test_a_key_too_far_away_is_no_candidate(self):
        """9 km of raw separation at dt=20 s, against a gate of
        min(10 km, 350*20/1000 + 2 km) = 9 km — just outside.  A dropped key
        on the far side of the sector is not this aircraft.
        """
        _seed_coasting_entry(sep_m=9100.0)
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert state.mn_stale_coast_retired == 0
        assert state.mn_stale_coast_none == 1

    def test_a_key_solved_a_moment_ago_is_no_candidate(self):
        """MN_STALE_COAST_MIN_S is most of the selectivity.  Dark solves land
        every 1-3 s, so a key last solved 1 s ago is being TRACKED, not
        coasted, and an aircraft 4 km from it is a neighbour — dropped key or
        not.  (No dead reckoning to speak of at dt=1 s either, so the mint
        here is forced by disjoint ids and the 4 km being inside no gate that
        matters; what is asserted is that the age floor refuses it.)
        """
        _seed_coasting_entry(age_s=1.0)
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert state.mn_stale_coast_retired == 0

    def test_a_key_older_than_the_ceiling_is_no_candidate(self):
        """Past MN_STALE_COAST_MAX_S the old entry is beyond MN_DARK_EXPIRY_S
        twice over — the feed has already withdrawn it, so there is no ghost
        left to retire and nothing to gain by risking the pop.
        """
        _seed_coasting_entry(age_s=90.0)
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert state.mn_stale_coast_retired == 0
        assert state.mn_stale_coast_none == 1

    def test_disabled_by_env_changes_nothing(self, monkeypatch):
        """MN_STALE_COAST_ENABLED=0 (read into the constant at import) leaves
        the old behaviour exactly as it was: both keys live, no counters move.
        """
        monkeypatch.setattr(solver_mod, "MN_STALE_COAST_ENABLED", False)
        _seed_coasting_entry()
        dark_follow.drop_target(OLD_KEY, "velocity sigma 400 m/s")

        self._run()

        assert OLD_KEY in state.multinode_tracks
        assert len(state.multinode_tracks) == 2
        assert state.mn_stale_coast_retired == 0
        assert state.mn_stale_coast_none == 0
        assert state.mn_stale_coast_blocked_evidence == 0

    def test_an_adsb_assisted_key_is_never_retired(self):
        """mn-adsb-* entries are anchored to a transponder fix, so they are
        not coasting on anything and are out of scope by key prefix.  Seeded
        with the same geometry and the same drop to prove the prefix is what
        excludes it, not the gates.
        """
        _seed_coasting_entry(key="mn-adsb-abc123")
        dark_follow.drop_target("mn-adsb-abc123", "velocity sigma 400 m/s")

        self._run()

        assert "mn-adsb-abc123" in state.multinode_tracks
        assert state.mn_stale_coast_retired == 0


class TestStaleCoastCandidate:
    """The predicate in isolation — clock-free, with both evidence accessors
    injected, the same way TestSupersessionMatch exercises _supersession_match.
    """

    def _tracks(self, **overrides) -> dict:
        lat, lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=COAST_SEP_M)
        entry = {
            "lat": lat,
            "lon": lon,
            "alt_m": 7000.0,
            "timestamp_ms": (time.time() - COAST_AGE_S) * 1000.0,
        }
        entry.update(overrides)
        return {OLD_KEY: entry}

    def _call(self, tracks, dropped=True, manoeuvre=None, alt_m=7000.0):
        return identity_mod._stale_coast_candidate(
            tracks,
            "mn-dark-new",
            LAT,
            LON,
            time.time() * 1000.0,
            alt_m,
            dropped_fn=lambda k: dropped,
            manoeuvre_fn=lambda k: manoeuvre,
        )

    def test_picks_the_nearest_of_several_candidates(self):
        """One retirement per mint — a mint replaces one key — so of two
        qualifying coasting keys the nearer one wins.  The far one survives to
        be judged by its own next solve rather than being swept up here.
        """
        tracks = self._tracks()
        far_lat, far_lon = offset_latlon_m(LAT, LON, east_m=0.0, north_m=8000.0)
        tracks["mn-dark-farther"] = dict(tracks[OLD_KEY], lat=far_lat, lon=far_lon)
        assert self._call(tracks) == (OLD_KEY, "")

    def test_unknown_altitude_fails_open(self):
        """The rest of the pipeline's rule for optional fields, and
        _supersession_match's: an entry that never carried an altitude is
        judged on distance alone, not made unretirable.
        """
        assert self._call(self._tracks(alt_m=None)) == (OLD_KEY, "")
        assert self._call(self._tracks(), alt_m=None) == (OLD_KEY, "")

    def test_manoeuvre_at_the_threshold_is_not_evidence(self):
        """MN_STALE_COAST_MANOEUVRE is a floor to exceed, not to reach — the
        KF re-arms towards 1.0 on a real breach, so a level sitting exactly at
        the decayed threshold is the ambiguous case and stays refused.
        """
        assert self._call(self._tracks(), dropped=False, manoeuvre=0.3)[0] is None
        assert self._call(self._tracks(), dropped=False, manoeuvre=0.31) == (OLD_KEY, "")

    def test_evidence_refusal_outranks_an_altitude_one(self):
        """Two candidates, refused for different reasons.  "evidence" is the
        reason reported, because a candidate at the right place and height
        with no turn behind it is the refusal worth watching — it is what this
        rule costs against bare proximity.
        """
        tracks = self._tracks()
        tracks["mn-dark-high"] = dict(tracks[OLD_KEY], alt_m=11000.0)
        assert self._call(tracks, dropped=False, manoeuvre=None) == (None, "evidence")

    def test_an_entry_without_a_position_is_skipped(self):
        assert self._call(self._tracks(lat=None, lon=None)) == (None, "none")

    @pytest.mark.parametrize("dt_s", [0.0, 3.9, 60.1, 300.0])
    def test_ages_outside_the_band_are_skipped(self, dt_s):
        tracks = self._tracks(timestamp_ms=(time.time() - dt_s) * 1000.0)
        assert self._call(tracks) == (None, "none")


class TestEvidenceAccessors:
    """The two read-only accessors the predicate leans on."""

    def setup_method(self):
        dark_follow._reset_for_tests()
        track_filter.reset()

    def teardown_method(self):
        dark_follow._reset_for_tests()
        track_filter.reset()

    def test_was_dropped_reports_a_live_cooldown_and_nothing_else(self):
        now = time.monotonic()
        assert dark_follow.was_dropped("mn-dark-x", now) is False
        dark_follow.drop_target("mn-dark-x", "test")
        assert dark_follow.was_dropped("mn-dark-x", now) is True
        # Past the cooldown the guard has genuinely forgotten the drop, and
        # the accessor says so rather than inventing a longer memory.
        assert dark_follow.was_dropped("mn-dark-x", now + dark_follow.DARK_FOLLOW_COOLDOWN_S + 1) is False

    def test_was_dropped_honours_a_shorter_window(self):
        now = time.monotonic()
        dark_follow.drop_target("mn-dark-x", "test")
        assert dark_follow.was_dropped("mn-dark-x", now + 10.0, within_s=5.0) is False
        assert dark_follow.was_dropped("mn-dark-x", now + 10.0, within_s=20.0) is True

    def test_manoeuvre_level_is_none_without_filter_state(self):
        assert track_filter.manoeuvre_level("mn-dark-never-smoothed") is None
