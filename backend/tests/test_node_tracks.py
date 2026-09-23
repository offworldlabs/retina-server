"""The per-node store of the tracks a node's own tracker sends (contract 1.6.0)."""

import time

import pytest

from config.constants import N2_TRACK_HISTORY_MAX, STALE_TRACK_S
from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from routes.node_schemas import DetectionFrame
from services import node_tracks
from services.node_pipeline import pipeline_frame
from services.node_tracks import NodeTrackStore
from tests.node_helpers import register_test_node

RUN = "k3n8v2qp71ab9x0c"
T0 = 1753900000000


def _track(**overrides) -> dict:
    return {
        "id": "260923-00001A",
        "state": "active",
        "hit": 0,
        "n_associated": 5,
        "n_missed": 0,
        "adsb_hex": None,
        "is_anomalous": False,
        "anomaly_types": [],
        "max_velocity_ms": 210.0,
        "born_t": None,
        "avg_snr": None,
        "shadow_fraction": None,
        "interference_fraction": None,
    } | overrides


def _frame(ts: int = T0, tracks=(), run: str | None = RUN, **overrides) -> dict:
    """A queue frame as pipeline_frame builds it: two detections."""
    frame = {
        "timestamp": ts,
        "delay": [12.4, 30.1],
        "doppler": [-118.0, 44.5],
        "snr": [14.2, 9.8],
        "adsb_hex": [None, None],
    }
    if run is not None:
        frame["tracker"] = {"run": run}
        if tracks is not None:
            frame["tracks"] = list(tracks)
    return frame | overrides


def _only(store: NodeTrackStore, node_id: str = "n1"):
    (track,) = store._nodes[node_id].tracks.values()
    return track


class TestFiling:
    def test_a_frame_without_a_tracker_leaves_the_store_empty(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(run=None))
        assert store.summary() == {"nodes_sending": 0, "open": 0, "closed": 0, "points": 0}

    def test_an_active_track_gains_the_detection_it_hit(self):
        store = NodeTrackStore()
        adsb = [None, {"hex": "4ca1f2", "lat": 1.0, "lon": 2.0}]
        store.ingest("n1", _frame(tracks=[_track(hit=1, adsb_hex="4ca1f2")], adsb=adsb))

        track = _only(store)
        assert list(track.window) == [
            {"timestamp": T0, "delay": 30.1, "doppler": 44.5, "snr": 9.8, "adsb": adsb[1]},
        ]
        assert track.adsb_hex == "4ca1f2"
        assert track.closed is None

    def test_a_detection_without_a_tag_files_a_null_adsb(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        assert _only(store).window[0]["adsb"] is None

    def test_the_counters_and_flags_follow_the_latest_frame(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest(
            "n1",
            _frame(
                ts=T0 + 1000,
                tracks=[
                    _track(
                        state="coasting",
                        hit=None,
                        n_missed=1,
                        is_anomalous=True,
                        anomaly_types=["supersonic"],
                        max_velocity_ms=400.0,
                        shadow_fraction=0.2,
                    )
                ],
            ),
        )

        track = _only(store)
        assert (track.state, track.n_missed, track.is_anomalous) == ("coasting", 1, True)
        assert track.anomaly_types == ["supersonic"]
        assert (track.max_velocity_ms, track.shadow_fraction) == (400.0, 0.2)
        assert track.last_named_ms == T0 + 1000

    def test_a_coasting_track_adds_no_point(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, tracks=[_track(state="coasting", hit=None, n_missed=1)]))
        assert len(_only(store).window) == 1

    def test_the_window_holds_the_newest_points(self):
        store = NodeTrackStore()
        for i in range(N2_TRACK_HISTORY_MAX + 5):
            store.ingest("n1", _frame(ts=T0 + i * 1000, tracks=[_track()]))

        window = _only(store).window
        assert len(window) == N2_TRACK_HISTORY_MAX
        assert window[0]["timestamp"] == T0 + 5 * 1000
        assert window[-1]["timestamp"] == T0 + (N2_TRACK_HISTORY_MAX + 4) * 1000

    def test_nodes_are_kept_apart(self):
        """Every node's tracker mints the same ids."""
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n2", _frame(tracks=[_track(hit=1)]))
        assert _only(store, "n1").window[0]["delay"] == 12.4
        assert _only(store, "n2").window[0]["delay"] == 30.1


class TestRefusal:
    """Only an untyped frame, such as a mirrored one, can carry a malformed
    track; the route refuses one whole."""

    def test_a_malformed_track_is_refused_alone(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track(id="old")]))
        later = T0 + int(STALE_TRACK_S * 1000) + 1
        tracks = [_track(id="first"), {"id": "broken"}, _track(id="third", hit=1)]

        refused = store.ingest("n1", _frame(ts=later, tracks=tracks))

        assert refused == 1
        assert {key[1] for key in store._nodes["n1"].tracks} == {"first", "third"}, "old should be forgotten"

    def test_a_malformed_update_leaves_the_entry_as_it_was(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        broken = {k: v for k, v in _track(state="coasting", hit=None).items() if k != "n_missed"}

        assert store.ingest("n1", _frame(ts=T0 + 1000, tracks=[broken])) == 1

        track = _only(store)
        assert (track.state, track.last_named_ms) == ("active", T0)

    @pytest.mark.parametrize("hit", [-1, 2, True, "0"])
    def test_a_hit_that_names_no_detection_is_refused(self, hit):
        """-1 would otherwise read the last detection instead of failing."""
        store = NodeTrackStore()
        assert store.ingest("n1", _frame(tracks=[_track(hit=hit)])) == 1
        assert store._nodes["n1"].tracks == {}

    def test_a_well_formed_frame_refuses_nothing(self):
        assert NodeTrackStore().ingest("n1", _frame(tracks=[_track()])) == 0


class TestClosing:
    def test_deleted_closes_the_key(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, tracks=[_track(state="deleted", hit=None, n_missed=10)]))
        assert _only(store).closed == "deleted"

    def test_a_closed_key_ignores_what_follows(self):
        """`deleted` is accepted once; a repeat, or anything else, changes nothing."""
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track(state="deleted", hit=None)]))
        store.ingest("n1", _frame(ts=T0 + 1000, tracks=[_track(n_associated=9)]))

        track = _only(store)
        assert (track.state, track.n_associated, len(track.window)) == ("deleted", 5, 0)
        assert track.last_named_ms == T0

    def test_a_new_run_closes_the_previous_runs_tracks(self):
        """The tracker's ids repeat after a restart, so the same id under a new
        run is a new track."""
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, run="restartedrun00001", tracks=[_track(hit=1)]))

        old = store._nodes["n1"].tracks[(RUN, "260923-00001A")]
        new = store._nodes["n1"].tracks[("restartedrun00001", "260923-00001A")]
        assert old.closed == "tracker_restart"
        assert new.closed is None and new.window[0]["delay"] == 30.1

    def test_a_new_run_with_no_output_still_closes_the_old_one(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, run="restartedrun00001", tracks=None))
        assert _only(store).closed == "tracker_restart"

    def test_a_track_missing_from_a_frame_is_not_closed(self):
        """Only `deleted`, a new run or the forget clock end a track."""
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, tracks=[]))
        assert _only(store).closed is None


class TestAging:
    def test_a_node_that_stops_sending_a_tracker_is_forgotten_on_the_stale_clock(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + int(STALE_TRACK_S * 1000), run=None))
        assert store.summary()["open"] == 1, "not yet stale"

        store.ingest("n1", _frame(ts=T0 + int(STALE_TRACK_S * 1000) + 1, run=None))

        assert store.summary() == {"nodes_sending": 0, "open": 0, "closed": 0, "points": 0}

    def test_null_tracks_leave_the_store_untouched(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + 1000, tracks=None))

        track = _only(store)
        assert track.last_named_ms == T0 and len(track.window) == 1

    def test_a_track_no_frame_names_is_forgotten_after_the_stale_window(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track()]))
        store.ingest("n1", _frame(ts=T0 + int(STALE_TRACK_S * 1000), tracks=[]))
        assert len(store._nodes["n1"].tracks) == 1
        store.ingest("n1", _frame(ts=T0 + int(STALE_TRACK_S * 1000) + 1, tracks=[]))
        assert store._nodes["n1"].tracks == {}

    def test_a_closed_track_is_forgotten_on_the_same_clock(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track(state="deleted", hit=None)]))
        store.ingest("n1", _frame(ts=T0 + int(STALE_TRACK_S * 1000) + 1, tracks=[]))
        assert store._nodes["n1"].tracks == {}

    def test_the_summary_counts_what_is_held(self):
        store = NodeTrackStore()
        store.ingest("n1", _frame(tracks=[_track(), _track(id="260923-00001B", state="deleted", hit=None)]))
        store.ingest("n2", _frame(tracks=None))
        assert store.summary() == {"nodes_sending": 2, "open": 1, "closed": 1, "points": 1}


def test_the_wire_shape_files_through_pipeline_frame():
    """The store reads the dict pipeline_frame builds, so the two shapes meet."""
    store = NodeTrackStore()
    wire = DetectionFrame(
        t=T0 / 1000,
        seq=1,
        boot_id="k3n8v2qp71ab",
        config_version=1,
        delay=[12.4, 30.1],
        doppler=[-118.0, 44.5],
        snr=[14.2, 9.8],
        adsb=[None, {"hex": "4ca1f2", "lat": 1.0, "lon": 2.0, "alt": 15375}],
        tracker={"run": RUN},
        tracks=[_track(hit=1, adsb_hex="4ca1f2", avg_snr=11.0)],
    )

    store.ingest("n1", pipeline_frame(wire))

    track = _only(store)
    assert track.avg_snr == 11.0
    assert track.window[0]["adsb"] == {"hex": "4ca1f2", "lat": 1.0, "lon": 2.0, "alt_baro": 15375}


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


class TestFrameProcessor:
    @pytest.fixture(autouse=True)
    def _fresh_store(self, monkeypatch):
        monkeypatch.setattr(node_tracks, "store", NodeTrackStore())

    def test_the_tracks_are_filed_before_the_known_lane_renumbers_the_frame(self, monkeypatch):
        """In binding mode a claimed detection is stripped and the rest shift
        down, so a hit read after the strip would name the wrong detection."""
        import services.frame_processor as fp

        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")
        monkeypatch.setattr(fp, "claim_known_targets", lambda node_id, frame, follow_claimed=None: {0})
        register_test_node("test-node-tracks-order", _NODE_CFG)
        frame = _frame(ts=int(time.time() * 1000), tracks=[_track(hit=1)])
        bound_before = state.known_claims_bound

        fp.process_one_frame("test-node-tracks-order", frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        assert state.known_claims_bound == bound_before + 1, "the strip this test guards against never ran"
        assert _only(node_tracks.store, "test-node-tracks-order").window[0]["delay"] == 30.1

    def test_a_malformed_track_cannot_cost_the_frame(self):
        """A mirrored frame arrives untyped: its tracks are counted as an error
        and its detections continue down the pipeline."""
        import services.frame_processor as fp

        register_test_node("test-node-tracks-fail-open", _NODE_CFG)
        before = state.node_tracks_errors
        frame = _frame(ts=int(time.time() * 1000), tracks=[{"id": "260923-00001A"}])

        fp.process_one_frame("test-node-tracks-fail-open", frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))

        assert state.node_tracks_errors == before + 1
        assert "test-node-tracks-fail-open" in state.node_pipelines

    def test_an_untracked_frame_does_not_reach_the_store(self):
        import services.frame_processor as fp

        register_test_node("test-node-tracks-untracked", _NODE_CFG)
        fp.process_one_frame(
            "test-node-tracks-untracked",
            _frame(ts=int(time.time() * 1000), run=None),
            PassiveRadarPipeline(DEFAULT_NODE_CONFIG),
        )
        assert node_tracks.store.summary()["nodes_sending"] == 0

    def test_a_node_that_stops_sending_a_tracker_ages_out_of_the_store(self):
        import services.frame_processor as fp

        register_test_node("test-node-tracks-stops", _NODE_CFG)
        now = int(time.time() * 1000)
        fp.process_one_frame(
            "test-node-tracks-stops", _frame(ts=now, tracks=[_track()]), PassiveRadarPipeline(DEFAULT_NODE_CONFIG)
        )
        fp.process_one_frame(
            "test-node-tracks-stops",
            _frame(ts=now + int(STALE_TRACK_S * 1000) + 1, run=None),
            PassiveRadarPipeline(DEFAULT_NODE_CONFIG),
        )
        assert node_tracks.store.summary()["nodes_sending"] == 0
