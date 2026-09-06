"""Backend-side ADS-B seeding (ADSB_SEED_MODE) — _view_adsb_hex,
confirmed_track_views' adsb_hex export, state._adsb_for_seeding, and
process_one_frame's predictive-tagging / solver-queue plumbing.

The mechanism itself (verification, exclusion, seeded-input emission) is
covered by the lib's test_adsb_seeding.py; these tests pin the backend
plumbing around it, following test_frame_processor.py's conventions.
"""

import math
import queue
import time
import types

import pytest
from fastapi.testclient import TestClient
from retina_analytics.association import AssociationRound, predict_observation
from retina_tracker.track import TrackState

from config.constants import FT_TO_M
from core import state
from main import app
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from services.frame_processor import (
    _view_adsb_hex,
    confirmed_track_views,
    process_one_frame,
)

_NODE_CFG = {
    "rx_lat": 34.85,
    "rx_lon": -82.40,
    "rx_alt_ft": 1000,
    "tx_lat": 34.9412,
    "tx_lon": -82.4103,
    "tx_alt_ft": 2000,
    "fc_hz": 183e6,
    "beam_width_deg": 90,
    "max_range_km": 60,
    "beam_azimuth_deg": 45.0,
}


def _make_frame(ts: int = None, n: int = 3) -> dict:
    if ts is None:
        ts = int(time.time() * 1000)
    return {
        "timestamp": ts,
        "delay": [50.0 + i * 2.0 for i in range(n)],
        "doppler": [10.0 + i * 5.0 for i in range(n)],
        "snr": [20.0 + i for i in range(n)],
    }


def _hist_entry(hexn=None):
    return {"timestamp": 0, "delay": 10.0, "doppler": 1.0, "snr": 10.0, "adsb": {"hex": hexn} if hexn else None}


def _no_adsb_key_entry():
    """A detection with no "adsb" key at all — pre-correlation shape."""
    return {"timestamp": 0, "delay": 10.0, "doppler": 1.0, "snr": 10.0}


class TestViewAdsbHex:
    def test_all_tagged_history_returns_the_hex(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("ABC123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) == "abc123"

    def test_last_n_untagged_is_the_swap_signature(self):
        """Older entries tagged, the newest 3 (ADSB_VIEW_TAG_FRESH_N)
        untagged — the receiver stopped correlating this hex, which is
        exactly the identity-swap signature, so the tag must not export."""
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("abc123") for _ in range(7)] + [_hist_entry(None) for _ in range(3)]
        assert _view_adsb_hex(track, hist) is None

    def test_two_distinct_hexes_in_window_is_ambiguous(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_hist_entry("abc123")] * 3 + [_hist_entry("def456")] * 3
        assert _view_adsb_hex(track, hist) is None

    def test_track_adsb_hex_mismatch_forces_none(self):
        track = types.SimpleNamespace(adsb_hex="zzzzzz")
        hist = [_hist_entry("abc123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) is None

    def test_track_adsb_hex_matching_is_fine(self):
        track = types.SimpleNamespace(adsb_hex="ABC123")
        hist = [_hist_entry("abc123") for _ in range(5)]
        assert _view_adsb_hex(track, hist) == "abc123"

    def test_hist_without_adsb_keys_returns_none(self):
        track = types.SimpleNamespace(adsb_hex=None)
        hist = [_no_adsb_key_entry() for _ in range(5)]
        assert _view_adsb_hex(track, hist) is None


class TestConfirmedTrackViewsAdsbHex:
    def test_view_carries_the_adsb_hex_key(self):
        hist = [_hist_entry("abc123") for _ in range(3)]
        track = types.SimpleNamespace(
            id="trk-1",
            state_status=TrackState.ACTIVE,
            adsb_hex=None,
            get_recent_detections=lambda n: hist[-n:],
        )
        tracker = types.SimpleNamespace(tracks=[track])
        views = confirmed_track_views(tracker)
        assert len(views) == 1
        assert views[0]["adsb_hex"] == "abc123"

    def test_untagged_track_carries_none(self):
        hist = [_no_adsb_key_entry() for _ in range(3)]
        track = types.SimpleNamespace(
            id="trk-2",
            state_status=TrackState.ACTIVE,
            adsb_hex=None,
            get_recent_detections=lambda n: hist[-n:],
        )
        tracker = types.SimpleNamespace(tracks=[track])
        views = confirmed_track_views(tracker)
        assert views[0]["adsb_hex"] is None


class TestAdsbForSeeding:
    def test_entry_conversion(self):
        state.adsb_aircraft["abc123"] = {
            "hex": "abc123",
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": 10000,
            "gs": 100,
            "track": 90,
            "flight": "TST1",
            "last_seen_ms": 123456,
        }
        out = state._adsb_for_seeding()
        assert "abc123" in out
        e = out["abc123"]
        assert e["alt_m"] == pytest.approx(10000 * 0.3048)
        # gs=100 kn, track=090 deg (east) -> vel_east ~= 100*0.514444, vel_north ~= 0
        assert e["vel_east"] == pytest.approx(51.4444, abs=0.01)
        assert e["vel_north"] == pytest.approx(0.0, abs=1e-6)
        assert e["timestamp_ms"] == 123456

    def test_on_ground_sentinel_strings_coerce_to_zero(self):
        # Real ADS-B feeds report alt_baro as the literal string "ground"
        # for on-ground aircraft. One such record used to raise TypeError
        # here on every frame for as long as it stayed live (and, before
        # the frame_processor fail-open guard, took the whole frame down
        # with it).
        state.adsb_aircraft["gnd001"] = {
            "hex": "gnd001",
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": "ground",
            "gs": None,
            "track": "unknown",
            "flight": "GND1",
            "last_seen_ms": 42,
        }
        out = state._adsb_for_seeding()
        e = out["gnd001"]
        assert e["alt_m"] == 0.0
        assert e["vel_east"] == 0.0
        assert e["vel_north"] == 0.0
        # Raw fields pass through untouched for downstream provenance.
        assert e["alt_baro"] == "ground"

    def test_skips_invalid_latlon(self):
        state.adsb_aircraft["badll"] = {
            "hex": "badll",
            "lat": float("nan"),
            "lon": -84.6,
            "alt_baro": 0,
            "gs": 0,
            "track": 0,
            "last_seen_ms": 0,
        }
        out = state._adsb_for_seeding()
        assert "badll" not in out

    def test_keys_stay_lowercase(self):
        # Writers already normalize (normalize_hex_key); the provider
        # trusts the dict key as-is rather than re-deriving it.
        state.adsb_aircraft["lower1"] = {
            "hex": "lower1",
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": 0,
            "gs": 0,
            "track": 0,
            "last_seen_ms": 0,
        }
        out = state._adsb_for_seeding()
        assert list(out.keys()) == ["lower1"]


def _legacy_derived(rec: dict) -> dict:
    """The derivation _adsb_for_seeding used to run per read, transcribed.

    The reference every writer's stored record is checked against, so a
    change to state.adsb_derived_fields has to be a deliberate one rather
    than something that slips through because both sides moved together.
    """

    def as_num(v):
        return float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0

    gs_ms = as_num(rec.get("gs")) * 0.514444
    trk = math.radians(as_num(rec.get("track")))
    return {
        "alt_m": as_num(rec.get("alt_baro")) * FT_TO_M,
        "vel_east": gs_ms * math.sin(trk),
        "vel_north": gs_ms * math.cos(trk),
    }


def _assert_derived(hexn: str, raw: dict) -> None:
    """The stored record carries the derived fields, and both it and the
    seeding snapshot agree with the pre-move formula."""
    rec = state.adsb_aircraft[hexn]
    want = _legacy_derived(raw)
    for key, value in want.items():
        assert rec[key] == pytest.approx(value, rel=1e-12, abs=1e-12), key
    assert rec["timestamp_ms"] == rec["last_seen_ms"]

    seeded = state._adsb_for_seeding()[hexn]
    for key, value in want.items():
        assert seeded[key] == pytest.approx(value, rel=1e-12, abs=1e-12), key
    assert seeded["timestamp_ms"] == rec["last_seen_ms"]


# gs/track present, gs/track absent, and the alt_baro="ground" sentinel that
# once took every frame down for as long as the record stayed live — the three
# shapes a live feed actually sends.
_WRITE_CASES = [
    ("moving", {"alt_baro": 35000, "gs": 250, "track": 90}),
    ("nokin", {}),
    ("ground", {"alt_baro": "ground", "gs": None, "track": "unknown"}),
]

# The sim push endpoint rejects non-transponder hexes (id_utils.is_transponder_hex),
# so its writer fixtures must look like real 24-bit addresses, not readable labels.
_SIM_CASE_HEX = {name: f"a1b2c{i}" for i, (name, _kin) in enumerate(_WRITE_CASES)}


class TestDerivedFieldsAtWriteTime:
    """Every path that writes state.adsb_aircraft stores the SI-unit fields
    the seeding provider hands out, so the provider does not re-derive them
    for the whole live fleet on every frame.

    One test per writer: a writer that forgets falls back to the old cost on
    read (covered below), which is invisible in behaviour and would otherwise
    only ever show up as a profile regression.
    """

    @pytest.mark.parametrize("name,kin", _WRITE_CASES)
    def test_tcp_handler_writer(self, name, kin):
        from services.tcp_handler import _apply_synthetic_adsb

        hexn = f"tcp{name}"
        entry = {"hex": hexn, "lat": 33.9, "lon": -84.6, **kin}
        _apply_synthetic_adsb({"data": {"timestamp": 1000, "adsb": [entry]}}, "synth-derived")

        _assert_derived(hexn, entry)

    @pytest.mark.parametrize("name,kin", _WRITE_CASES)
    def test_frame_processor_writer(self, name, kin):
        hexn = f"fp{name}"
        entry = {"hex": hexn, "lat": 33.9, "lon": -84.6, **kin}
        frame = {
            "timestamp": int(time.time() * 1000),
            "delay": [50.0],
            "doppler": [10.0],
            "snr": [20.0],
            "adsb": [entry],
        }
        process_one_frame("node-derived", frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        _assert_derived(hexn, entry)

    @pytest.mark.parametrize("name,kin", _WRITE_CASES)
    async def test_sim_ingest_writer(self, name, kin):
        # Called past the router so the test does not need the synthetic-fleet
        # mount gate open (see test_sim_ingest_mount.py) — the write path is
        # what is under test, not the gate in front of it.
        from routes.sim_ingest import sim_push_adsb_positions

        hexn = _SIM_CASE_HEX[name]
        entry = {"hex": hexn, "lat": 33.9, "lon": -84.6, **kin}
        await sim_push_adsb_positions(body={"ts_ms": 4242, "aircraft": [entry]}, _key=None)

        _assert_derived(hexn, entry)
        assert state.adsb_aircraft[hexn]["timestamp_ms"] == 4242

    @pytest.mark.parametrize("name,kin", _WRITE_CASES)
    def test_record_without_derived_fields_falls_back_on_read(self, name, kin):
        """A record no writer derived — state carried across a restart, or a
        write path that has not been updated — still seeds correctly.

        The failure this guards is silent and expensive: a missing vel_east /
        vel_north reads as 0.0, which dead-reckons a moving aircraft to a
        standstill and loses its claims rather than raising anything.
        """
        hexn = f"stale{name}"
        raw = {
            "hex": hexn,
            "lat": 33.9,
            "lon": -84.6,
            "alt_baro": kin.get("alt_baro", 0),
            "gs": kin.get("gs", 0),
            "track": kin.get("track", 0),
            "flight": "STALE1",
            "last_seen_ms": 99,
        }
        state.adsb_aircraft[hexn] = dict(raw)

        seeded = state._adsb_for_seeding()[hexn]

        for key, value in _legacy_derived(raw).items():
            assert seeded[key] == pytest.approx(value, rel=1e-12, abs=1e-12), key
        assert seeded["timestamp_ms"] == 99
        # Raw fields still pass through for downstream provenance, and the
        # fallback must not write the derived fields back into live state —
        # adsb_aircraft is read unlocked by other threads.
        assert seeded["alt_baro"] == raw["alt_baro"]
        assert seeded["flight"] == "STALE1"
        assert "alt_m" not in state.adsb_aircraft[hexn]


class TestProcessOneFrameSolverQueue:
    # Each test swaps in a private queue: solver worker daemons leaked by
    # TestClient lifespans elsewhere in the suite poll state.solver_queue and
    # would otherwise race the enqueued item away (see test_solver_worker's
    # private-queue rationale).

    def test_adsb_inputs_reach_the_solver_queue(self, monkeypatch):
        """adsb_inputs are already solver-input shaped (see
        association._adsb_seed_round), so process_one_frame must hand them
        to the queue directly, alongside pairs/anchored_inputs."""
        seeded = {
            "initial_guess": {"lat": 35.0, "lon": -82.0, "alt_km": 7.0},
            "initial_velocity": {"vel_east_ms": 100.0, "vel_north_ms": 50.0},
            "measurements": [
                {"node_id": "site-a", "delay_us": 10.0, "doppler_hz": 5.0, "snr": 15.0},
                {"node_id": "site-b", "delay_us": 12.0, "doppler_hz": 4.0, "snr": 14.0},
            ],
            "n_nodes": 2,
            "timestamp_ms": int(time.time() * 1000),
            "adsb_hex": "abc123",
            "chi2_per_dof": None,
            "n_epochs": 2,
            "cv_epochs": [{"t_s": 0.0, "measurements": []}],
            "track_pair_ids": [("t1", "t2")],
            "track_ids": ["t1", "t2"],
            "track_ids_by_node": {"site-a": ["t1"], "site-b": ["t2"]},
        }
        monkeypatch.setattr(
            state.node_associator,
            "submit_tracks_round",
            lambda *a, **kw: AssociationRound(pairs=[], anchored_inputs=[], claims=[], adsb_inputs=[seeded]),
        )
        monkeypatch.setattr(state, "solver_queue", queue.Queue())

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame("test-adsb-seed-queue", _make_frame(), default)

        s_in, _node_cfgs, _enqueued_at = state.solver_queue.get_nowait()
        assert s_in["adsb_hex"] == "abc123"
        assert s_in["n_nodes"] == 2

    def test_claiming_failure_cannot_cost_the_frame(self, monkeypatch):
        """The known lane fails open: an exception in claim_known_targets is
        counted, and the frame continues down the dark lane instead of dying
        in the frame worker (the alt_baro="ground" incident)."""
        import services.frame_processor as fp

        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "shadow")

        def _boom(node_id, frame):
            raise TypeError("can't multiply sequence by non-int of type 'float'")

        monkeypatch.setattr(fp, "claim_known_targets", _boom)
        before = state.known_claims_errors

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame("test-claim-fail-open", _make_frame(), default)

        assert state.known_claims_errors == before + 1

    def test_empty_adsb_inputs_add_nothing(self, monkeypatch):
        monkeypatch.setattr(
            state.node_associator,
            "submit_tracks_round",
            lambda *a, **kw: AssociationRound(pairs=[], anchored_inputs=[], claims=[], adsb_inputs=[]),
        )
        monkeypatch.setattr(state, "solver_queue", queue.Queue())

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame("test-adsb-seed-empty", _make_frame(), default)

        with pytest.raises(queue.Empty):
            state.solver_queue.get_nowait()


class TestPredictiveAttach:
    def _register(self, node_id):
        state.node_associator.register_node(node_id, _NODE_CFG)
        return state.node_associator.node_geometries[node_id]

    def test_frame_without_adsb_gains_index_aligned_list_in_active_mode(self, monkeypatch):
        node_id = "test-predictive-attach"
        geo = self._register(node_id)

        lat, lon, alt_km, ve, vn = 34.88, -82.35, 7.0, 180.0, -90.0
        d0, f0 = predict_observation(geo, lat, lon, alt_km, ve, vn)
        frame = _make_frame()
        frame["delay"] = [d0, d0 + 500.0]  # detection 0 = the plane, 1 = clutter
        frame["doppler"] = [f0, f0 + 500.0]
        frame.pop("adsb", None)

        fixed_states = {
            "abc123": {
                "hex": "abc123",
                "lat": lat,
                "lon": lon,
                "alt_m": alt_km * 1000.0,
                "vel_east": ve,
                "vel_north": vn,
                "timestamp_ms": frame["timestamp"],
                "alt_baro": 23000,
                "gs": 250,
                "track": 90,
                "flight": "TST1",
            },
        }
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: fixed_states)
        monkeypatch.setattr(state, "ADSB_SEED_MODE", "active")

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame(node_id, frame, default)

        assert frame["adsb"] is not None
        assert frame["adsb"][0]["hex"] == "abc123"
        assert frame["adsb"][1] is None

    def test_cross_world_state_is_not_attached(self, monkeypatch):
        """The auto-tag pass is a cache-wide assignment for a node with no
        receiver, so an other-world entry is a decoy its detections can bind
        to on a delay/Doppler coincidence — filtered before the lib call,
        which is node-agnostic."""
        node_id = "test-predictive-world"  # test-* prefix → sim world
        geo = self._register(node_id)

        lat, lon, alt_km, ve, vn = 34.88, -82.35, 7.0, 180.0, -90.0
        d0, f0 = predict_observation(geo, lat, lon, alt_km, ve, vn)
        frame = _make_frame()
        frame["delay"] = [d0]
        frame["doppler"] = [f0]
        frame.pop("adsb", None)

        decoy = {
            "hex": "a97cf2",
            "lat": lat,
            "lon": lon,
            "alt_m": alt_km * 1000.0,
            "vel_east": ve,
            "vel_north": vn,
            "timestamp_ms": frame["timestamp"],
            "world": "real",
        }
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: {"a97cf2": decoy})
        monkeypatch.setattr(state, "ADSB_SEED_MODE", "active")

        process_one_frame(node_id, frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        assert "adsb" not in frame or frame["adsb"] is None

        # Same state tagged with the node's own world attaches — the filter
        # removes decoys, not the capability.
        own = dict(decoy, world="sim")
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: {"a97cf2": own})
        frame2 = _make_frame()
        frame2["delay"] = [d0]
        frame2["doppler"] = [f0]
        frame2.pop("adsb", None)
        process_one_frame(node_id, frame2, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        assert frame2["adsb"] is not None
        assert frame2["adsb"][0]["hex"] == "a97cf2"

    def test_existing_adsb_list_never_overwritten(self, monkeypatch):
        node_id = "test-predictive-existing"
        self._register(node_id)
        frame = _make_frame()
        frame["adsb"] = [{"hex": "already", "lat": 33.9, "lon": -84.6, "alt_baro": 0, "gs": 0, "track": 0}]
        monkeypatch.setattr(state, "_adsb_for_seeding", lambda: {})
        monkeypatch.setattr(state, "ADSB_SEED_MODE", "active")

        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        process_one_frame(node_id, frame, default)

        assert frame["adsb"] == [{"hex": "already", "lat": 33.9, "lon": -84.6, "alt_baro": 0, "gs": 0, "track": 0}]

    def test_off_and_shadow_mode_leave_the_frame_untouched(self, monkeypatch):
        node_id = "test-predictive-offshadow"
        self._register(node_id)
        monkeypatch.setattr(
            state,
            "_adsb_for_seeding",
            lambda: {
                "abc123": {
                    "hex": "abc123",
                    "lat": 34.88,
                    "lon": -82.35,
                    "alt_m": 7000.0,
                    "vel_east": 0.0,
                    "vel_north": 0.0,
                    "timestamp_ms": int(time.time() * 1000),
                },
            },
        )
        default = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        for mode in ("off", "shadow"):
            frame = _make_frame()
            frame.pop("adsb", None)
            monkeypatch.setattr(state, "ADSB_SEED_MODE", mode)
            process_one_frame(node_id, frame, default)
            assert "adsb" not in frame or frame["adsb"] is None


class TestAdsbSeedStatusPayload:
    def test_association_status_carries_the_adsb_seed_block(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.get("/api/radar/association/status")
            assert r.status_code == 200
            body = r.json()
            assert "adsb_seed" in body
            assert body["adsb_seed"].keys() == {
                "mode",
                "rounds",
                "tagged",
                "no_state",
                "gate_rejects",
                "world_rejects",
                "tracklets_excluded",
                "inputs_emitted",
            }
            assert body["adsb_seed"]["mode"] == state.node_associator.adsb_seed_mode


class TestWorldStamp:
    """Both frame-list writers stamp which world the positions belong to, by
    the reporting node's class — claiming keys on it (see known_claiming's
    world gate).  The sim-ingest route's stamp is covered with that route's
    tests in test_sim_ingest.py."""

    def test_tcp_writer_stamps_sim_for_a_synthetic_node(self):
        from services.tcp_handler import _apply_synthetic_adsb

        entry = {"hex": "wrld01", "lat": 33.9, "lon": -84.6}
        _apply_synthetic_adsb({"data": {"timestamp": 1000, "adsb": [entry]}}, "synth-world")
        assert state.adsb_aircraft["wrld01"]["world"] == "sim"

    def test_tcp_writer_stamps_real_for_a_hardware_node(self):
        from services.tcp_handler import _apply_synthetic_adsb

        entry = {"hex": "wrld02", "lat": 33.9, "lon": -84.6}
        _apply_synthetic_adsb({"data": {"timestamp": 1000, "adsb": [entry]}}, "radar3-retnode")
        assert state.adsb_aircraft["wrld02"]["world"] == "real"

    def test_tcp_writer_honours_the_handshake_verdict(self):
        """A registered node's CONFIG verdict beats the prefix rule — a node
        may declare is_synthetic itself under any id."""
        from services.tcp_handler import _apply_synthetic_adsb

        with state.connected_nodes_lock:
            state.connected_nodes["oddname"] = {"is_synthetic": True}
        try:
            entry = {"hex": "wrld03", "lat": 33.9, "lon": -84.6}
            _apply_synthetic_adsb({"data": {"timestamp": 1000, "adsb": [entry]}}, "oddname")
            assert state.adsb_aircraft["wrld03"]["world"] == "sim"
        finally:
            with state.connected_nodes_lock:
                state.connected_nodes.pop("oddname", None)

    def test_frame_processor_writer_stamps_by_node_class(self):
        entry = {"hex": "wrld04", "lat": 33.9, "lon": -84.6}
        frame = {
            "timestamp": int(time.time() * 1000),
            "delay": [50.0],
            "doppler": [10.0],
            "snr": [20.0],
            "adsb": [entry],
        }
        process_one_frame("blah2-hw-node", frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))
        assert state.adsb_aircraft["wrld04"]["world"] == "real"


class TestSeedWorldWiring:
    def test_associator_gets_the_state_world_resolver(self):
        """One authority for the world question: the associator's seed gate
        must consult the same resolver claiming and the auto-tag filter use,
        or one consumer accepts what another rejects."""
        assert state.node_associator.node_world_provider is state.node_world

    def test_a_sim_and_a_real_node_over_one_footprint_get_no_overlap_zone(self):
        """The same resolver, one level down: bottom-up pairing must not build
        a grid across worlds either.  Registering a synthetic node and a
        hardware node on overlapping coverage used to leave a zone whose only
        possible pairing was a simulated echo against a real one — which is how
        real node ids reached the synthetic fleet's dark solves."""
        _a = state.node_associator
        try:
            _a.register_node("synth-GVL-9001", dict(_NODE_CFG))
            _a.register_node("hw-9001", dict(_NODE_CFG, rx_lat=34.86, rx_lon=-82.36))

            assert _a.overlap_zones == {}
            assert _a._neighbors.get("synth-GVL-9001", set()) == set()
            assert _a.assoc_world_skipped_pairs == 1
        finally:
            state._reset_for_tests()

    def test_two_synthetic_nodes_over_one_footprint_still_pair(self):
        """The gate is the world difference, not the registration."""
        _a = state.node_associator
        try:
            _a.register_node("synth-GVL-9001", dict(_NODE_CFG))
            _a.register_node("synth-GVL-9002", dict(_NODE_CFG, rx_lat=34.86, rx_lon=-82.36))

            assert _a.overlap_zones
            assert _a.assoc_world_skipped_pairs == 0
        finally:
            state._reset_for_tests()
