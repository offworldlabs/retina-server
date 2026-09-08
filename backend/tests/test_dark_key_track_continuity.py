"""Node-track continuity in the dark keying rule — solver.multinode_key_decision.

Every solver input carries the node-level tracker track ids it was built from,
and each multinode entry now remembers the ones its recent solves used
(``recent_track_ids``, pruned to TRACK_LINK_AGE_S).  Two dark solves sharing
those ids inside the association gate are 84-94% the same aircraft (measured
2026-09-07 on test), so the rule uses them to re-rank candidates the gate
already admits.  Pinned here:

- a follow-owned key is JOINED (how "tracks") on >= TRACK_LINK_MIN_SHARED_JOIN
  shared ids inside the gate, shadows on exactly one, and is left alone with
  none — the pre-existing ownership behaviour;
- among ordinary candidates a track-sharing entry beats a nearer stranger;
- shared ids never widen the gate: outside it the same evidence is 0.15-0.42
  precise, so an out-of-gate candidate is not joined however much it shares;
- the id memory merges across writes and prunes past TRACK_LINK_AGE_S.

Geometry and fixture style follow test_dark_follow.py's TestKeyOwnership.
"""

import pytest

from core import state
from services import dark_follow
from services.geo import offset_latlon_m
from services.tasks import solver as solver_mod

_KEY = "mn-dark-followed"
_OTHER_KEY = "mn-dark-stranger"
_LAT, _LON = 34.88, -82.35
_TS_MS = 3_000_000
_TS_S = _TS_MS / 1000.0


def _entry(north_km=0.0, dt_s=1.0, recent=None):
    """A live, motionless dark entry ``north_km`` north of the reference point,
    last solved ``dt_s`` ago — so the decision's distance is exactly the offset.
    """
    lat, lon = offset_latlon_m(_LAT, _LON, east_m=0.0, north_m=north_km * 1000.0)
    return {
        "lat": lat,
        "lon": lon,
        "vel_east": 0.0,
        "vel_north": 0.0,
        "timestamp_ms": _TS_MS - int(dt_s * 1000),
        "n_nodes": 3,
        "solve_count": 5,
        "recent_track_ids": dict(recent or {}),
    }


def _result(north_km):
    lat, lon = offset_latlon_m(_LAT, _LON, east_m=0.0, north_m=north_km * 1000.0)
    return {"lat": lat, "lon": lon, "timestamp_ms": _TS_MS}


def _decide(tracks, north_km, track_ids=None, anchor_key=None):
    return solver_mod.multinode_key_decision(
        tracks,
        _result(north_km),
        None,
        anchor_key,
        learned_vel_fn=lambda _k: None,
        track_ids=track_ids,
    )


class TestFollowOwnedKeys:
    """Ownership exists to stop a DIFFERENT aircraft stealing an established
    key.  Shared node tracks answer that question directly, so inside the gate
    they override the ownership refusal (>=2) or extend it past the 2 km shadow
    radius (exactly 1) rather than letting a duplicate key be minted."""

    def setup_method(self):
        dark_follow._reset_for_tests()

    def teardown_method(self):
        dark_follow._reset_for_tests()

    def _followed(self, monkeypatch, recent):
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        dark_follow.note_follow_publish(_KEY, _TS_S - 2.0)
        return {_KEY: _entry(recent=recent)}

    def test_two_shared_tracks_join_the_followed_key(self, monkeypatch):
        tracks = self._followed(monkeypatch, {"260907-0004A9": _TS_S - 3.0, "260907-0004B1": _TS_S - 5.0})
        key, how, dist, _dt = _decide(tracks, 4.0, track_ids=["260907-0004A9", "260907-0004B1", "260907-00FFFF"])
        assert (key, how) == (_KEY, "tracks")
        assert dist == pytest.approx(4.0, abs=0.05)

    def test_one_shared_track_shadows_beyond_the_shadow_radius(self, monkeypatch):
        """4 km is twice DARK_FOLLOW_SHADOW_KM, so distance alone would have
        minted a second key here.  One shared id is too weak to take the key
        (0.71-0.92 precise) but strong enough to call the solve a duplicate."""
        tracks = self._followed(monkeypatch, {"260907-0004A9": _TS_S - 3.0})
        key, how, dist, _dt = _decide(tracks, 4.0, track_ids=["260907-0004A9"])
        assert (key, how) == (_KEY, "shadowed")
        assert dist == pytest.approx(4.0, abs=0.05)

    def test_no_shared_tracks_still_mints(self, monkeypatch):
        """Unchanged behaviour: past the shadow radius with no id evidence the
        solve is neither joined to the followed key nor refused."""
        tracks = self._followed(monkeypatch, {"260907-000111": _TS_S - 3.0})
        key, how, _dist, _dt = _decide(tracks, 4.0, track_ids=["260907-0004A9"])
        assert how == "minted"
        assert key != _KEY

    def test_shared_tracks_do_not_widen_the_gate(self, monkeypatch):
        """Beyond the gate the same evidence is 0.15-0.42 precise — the point
        at which shared node tracks stop meaning identity and start meaning two
        aircraft in one association candidate."""
        tracks = self._followed(monkeypatch, {"260907-0004A9": _TS_S - 3.0, "260907-0004B1": _TS_S - 3.0})
        key, how, _dist, _dt = _decide(tracks, 9.0, track_ids=["260907-0004A9", "260907-0004B1"])
        assert how == "minted"
        assert key != _KEY

    def test_stale_shared_ids_do_not_count(self, monkeypatch):
        """Node track ids live a median 7 s in solve records; past
        TRACK_LINK_AGE_S a match is id reuse, not continuity."""
        stale = _TS_S - (solver_mod.TRACK_LINK_AGE_S + 10.0)
        tracks = self._followed(monkeypatch, {"260907-0004A9": stale, "260907-0004B1": stale})
        _key, how, _dist, _dt = _decide(tracks, 4.0, track_ids=["260907-0004A9", "260907-0004B1"])
        assert how == "minted"


class TestOrdinaryCandidates:
    """Among candidates the gate admits, shared ids discount the distance
    score, so the track-sharing entry wins over a nearer stranger."""

    def test_a_track_sharing_key_beats_a_nearer_stranger(self):
        tracks = {
            _OTHER_KEY: _entry(north_km=2.0),
            _KEY: _entry(north_km=4.0, recent={"260907-0004A9": _TS_S - 2.0}),
        }
        key, how, dist, _dt = _decide(tracks, 0.0, track_ids=["260907-0004A9"])
        assert (key, how) == (_KEY, "tracks")
        assert dist == pytest.approx(4.0, abs=0.05)

    def test_without_shared_ids_the_nearer_key_still_wins(self):
        tracks = {_OTHER_KEY: _entry(north_km=2.0), _KEY: _entry(north_km=4.0)}
        key, how, _dist, _dt = _decide(tracks, 0.0, track_ids=["260907-0004A9"])
        assert (key, how) == (_OTHER_KEY, "proximity")

    def test_an_out_of_gate_sharer_is_not_joined(self):
        tracks = {_KEY: _entry(north_km=9.0, recent={"260907-0004A9": _TS_S - 2.0})}
        _key, how, _dist, _dt = _decide(tracks, 0.0, track_ids=["260907-0004A9"])
        assert how == "minted"


class TestRecentTrackIdMemory:
    """merge_recent_track_ids is what the scan above reads."""

    def test_ids_merge_across_writes_and_prune_by_age(self):
        first = solver_mod.merge_recent_track_ids(None, ["a", "b"], 100.0)
        assert first == {"a": 100.0, "b": 100.0}
        second = solver_mod.merge_recent_track_ids(first, ["b", "c"], 120.0)
        assert second == {"a": 100.0, "b": 120.0, "c": 120.0}
        # 'a' is now older than TRACK_LINK_AGE_S and drops out; 'b' was
        # refreshed by the second write and survives.
        third = solver_mod.merge_recent_track_ids(second, ["d"], 100.0 + solver_mod.TRACK_LINK_AGE_S + 1.0)
        assert set(third) == {"b", "c", "d"}

    def test_the_memory_is_capped(self):
        prev = {f"t{i}": 100.0 + i for i in range(solver_mod.TRACK_LINK_MAX_IDS + 10)}
        merged = solver_mod.merge_recent_track_ids(prev, ["new"], 110.0)
        assert len(merged) <= solver_mod.TRACK_LINK_MAX_IDS
        assert "new" in merged

    def test_a_missing_or_malformed_memory_is_not_a_crash(self):
        entry = {"recent_track_ids": {"a": None}}
        assert solver_mod._shared_recent_tracks(entry, {"a"}, 100.0) == 0
        assert solver_mod._shared_recent_tracks({}, {"a"}, 100.0) == 0
