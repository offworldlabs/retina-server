"""A node's own tracks standing where its pipeline's in-process tracker stood."""

import time

import pytest
from retina_tracker.track import TrackState

from config.constants import DISPLAY_STALE_TRACK_S
from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, InMemoryEventWriter, PassiveRadarPipeline
from services import node_tracks
from services.frame_processor import confirmed_track_views, process_one_frame
from services.node_tracks import NodeTracker, NodeTrackStore
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
    } | overrides


def _frame(ts: int = T0, tracks=(), run: str | None = RUN, delay: float = 12.4) -> dict:
    frame = {
        "timestamp": ts,
        "delay": [delay, 30.1],
        "doppler": [-118.0, 44.5],
        "snr": [14.2, 9.8],
        "adsb_hex": [None, None],
    }
    if run is not None:
        frame["tracker"] = {"run": run}
        if tracks is not None:
            frame["tracks"] = list(tracks)
    return frame


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    monkeypatch.setattr(node_tracks, "store", NodeTrackStore())


def _feed(node_id: str, *frames: dict) -> None:
    for frame in frames:
        node_tracks.store.ingest(node_id, frame)


class TestViews:
    def test_an_open_track_is_viewed_with_a_namespaced_id(self):
        """Every node's tracker mints the same ids, and a restart reuses them."""
        _feed("n1", _frame(tracks=[_track()]))
        (view,) = node_tracks.store.views("n1", T0)
        assert view.id == f"n1:{RUN}:260923-00001A"
        assert view.state_status is TrackState.ACTIVE

    def test_a_coasting_track_is_coasting(self):
        _feed(
            "n1",
            _frame(tracks=[_track()]),
            _frame(ts=T0 + 1000, tracks=[_track(state="coasting", hit=None, n_missed=2)]),
        )
        (view,) = node_tracks.store.views("n1", T0 + 1000)
        assert view.state_status is TrackState.COASTING
        assert view.n_missed == 2

    def test_a_closed_track_is_not_viewed(self):
        _feed("n1", _frame(tracks=[_track(state="deleted", hit=None)]))
        assert node_tracks.store.views("n1", T0) == []

    def test_a_track_no_frame_has_named_for_the_display_window_is_not_viewed(self):
        """It is still held, and seen again if a frame names it; the forget
        clock is longer."""
        _feed("n1", _frame(tracks=[_track()]))
        shown_ms = int(DISPLAY_STALE_TRACK_S * 1000)
        assert len(node_tracks.store.views("n1", T0 + shown_ms)) == 1
        assert node_tracks.store.views("n1", T0 + shown_ms + 1) == []

    def test_a_node_the_store_has_never_seen_has_no_views(self):
        assert node_tracks.store.views("unknown", T0) == []

    def test_recent_detections_are_oldest_first_and_bounded(self):
        _feed("n1", *(_frame(ts=T0 + i * 1000, tracks=[_track()], delay=float(i)) for i in range(5)))
        (view,) = node_tracks.store.views("n1", T0 + 4000)
        assert [p["delay"] for p in view.get_recent_detections(3)] == [2.0, 3.0, 4.0]
        assert [p["delay"] for p in view.get_recent_detections(n=20)] == [0.0, 1.0, 2.0, 3.0, 4.0]
        assert view.get_recent_detections(0) == []

    def test_a_view_does_not_move_when_the_store_does(self):
        """Readers on other threads hold a view while the frame worker files."""
        _feed("n1", _frame(tracks=[_track()]))
        (view,) = node_tracks.store.views("n1", T0)
        _feed("n1", _frame(ts=T0 + 1000, tracks=[_track(n_missed=0)]))
        assert len(view.get_recent_detections()) == 1
        assert len(view.history["measurements"]) == 1

    def test_the_history_holds_the_measurements_the_feed_reads(self):
        _feed("n1", _frame(tracks=[_track(hit=1)]))
        (view,) = node_tracks.store.views("n1", T0)
        assert view.history["measurements"][-1]["delay"] == 30.1


class TestClaimedHits:
    """A detection a binding lane claimed never reaches the in-process tracker, and
    must not reach the dark lane through a node's track either."""

    def test_a_track_whose_newest_hit_was_claimed_is_not_viewed(self):
        node_tracks.store.ingest("n1", _frame(tracks=[_track()]), claimed={0})
        assert node_tracks.store.views("n1", T0) == []

    def test_a_hit_the_lanes_left_is_viewed(self):
        node_tracks.store.ingest("n1", _frame(tracks=[_track(hit=1)]), claimed={0})
        (view,) = node_tracks.store.views("n1", T0)
        assert [p["delay"] for p in view.get_recent_detections()] == [30.1]

    def test_the_view_starts_after_the_newest_claimed_hit(self):
        for i, claimed in enumerate(({0}, set(), {0}, set(), set())):
            node_tracks.store.ingest("n1", _frame(ts=T0 + i * 1000, tracks=[_track()], delay=float(i)), claimed=claimed)
        (view,) = node_tracks.store.views("n1", T0 + 4000)
        assert [p["delay"] for p in view.get_recent_detections()] == [3.0, 4.0]
        assert [p["delay"] for p in view.history["measurements"]] == [3.0, 4.0]

    def test_a_track_with_no_hit_yet_is_not_viewed(self):
        _feed("n1", _frame(tracks=[_track(state="coasting", hit=None, n_missed=2)]))
        assert node_tracks.store.views("n1", T0) == []

    def test_coasting_after_a_claimed_hit_leaves_the_track_unviewed(self):
        node_tracks.store.ingest("n1", _frame(tracks=[_track()]), claimed={0})
        _feed("n1", _frame(ts=T0 + 1000, tracks=[_track(state="coasting", hit=None, n_missed=1)]))
        assert node_tracks.store.views("n1", T0 + 1000) == []

    def test_a_track_hex_stands_where_a_viewed_hit_was_tagged_with_it(self):
        frame = _frame(tracks=[_track(adsb_hex="4ca1f2")])
        frame["adsb"] = [{"hex": "4CA1F2", "lat": 34.9, "lon": -82.4}, None]
        _feed("n1", frame)
        (view,) = node_tracks.store.views("n1", T0)
        assert view.adsb_hex == "4ca1f2"

    def test_a_tag_vouches_for_the_hex_after_the_window_moves_past_it(self):
        """The in-process tracker keeps a hex for the life of the track."""
        tagged = _frame(tracks=[_track(adsb_hex="4ca1f2")])
        tagged["adsb"] = [{"hex": "4ca1f2", "lat": 34.9, "lon": -82.4}, None]
        _feed("n1", tagged)
        _feed("n1", *(_frame(ts=T0 + i * 1000, tracks=[_track(adsb_hex="4ca1f2")]) for i in range(1, 25)))
        (view,) = node_tracks.store.views("n1", T0 + 24000)
        assert view.adsb_hex == "4ca1f2"

    def test_a_track_hex_no_viewed_hit_was_tagged_with_is_withheld(self):
        """The tracker keeps a hex after its track swaps onto an untagged
        target, so the hex alone does not name the aircraft."""
        _feed("n1", _frame(tracks=[_track(adsb_hex="4ca1f2")]))
        (view,) = node_tracks.store.views("n1", T0)
        assert view.adsb_hex is None

    def test_a_tag_on_a_claimed_hit_does_not_vouch_for_the_hex(self):
        tagged = _frame(tracks=[_track(adsb_hex="4ca1f2")])
        tagged["adsb"] = [{"hex": "4ca1f2", "lat": 34.9, "lon": -82.4}, None]
        node_tracks.store.ingest("n1", tagged, claimed={0})
        _feed("n1", _frame(ts=T0 + 1000, tracks=[_track(adsb_hex="4ca1f2")]))
        (view,) = node_tracks.store.views("n1", T0 + 1000)
        assert view.adsb_hex is None

    def test_a_blank_tag_gives_a_hexless_track_no_hex(self):
        """A mirrored frame reaches the store untyped."""
        frame = _frame(tracks=[_track()])
        frame["adsb"] = [{"hex": " ", "lat": 34.9, "lon": -82.4}, None]
        _feed("n1", frame)
        (view,) = node_tracks.store.views("n1", T0)
        assert view.adsb_hex is None

    def test_a_tag_for_another_hex_does_not_vouch_for_it(self):
        frame = _frame(tracks=[_track(adsb_hex="4ca1f2")])
        frame["adsb"] = [{"hex": "abcdef", "lat": 34.9, "lon": -82.4}, None]
        _feed("n1", frame)
        (view,) = node_tracks.store.views("n1", T0)
        assert view.adsb_hex is None


class TestNodeTracker:
    def test_each_frame_replaces_the_track_list(self):
        """The feed and the solver iterate `tracks` off the frame worker, so a
        frame must not mutate the list they hold."""
        tracker = NodeTracker("n1", InMemoryEventWriter())
        _feed("n1", _frame(tracks=[_track()]))
        tracker.process_frame([], T0)
        held = tracker.tracks
        _feed("n1", _frame(ts=T0 + 1000, tracks=[]))
        tracker.process_frame([], T0 + 1000)
        assert tracker.tracks is not held
        assert len(held) == 1

    def test_a_track_that_took_a_detection_gets_an_event(self):
        writer = InMemoryEventWriter()
        tracker = NodeTracker("n1", writer)
        frame = _frame(tracks=[_track(adsb_hex="4ca1f2", is_anomalous=True, anomaly_types=["supersonic"])])
        frame["adsb"] = [{"hex": "4ca1f2", "lat": 34.9, "lon": -82.4}, None]
        _feed("n1", frame)

        tracker.process_frame([], T0)

        event = writer.events[f"n1:{RUN}:260923-00001A"]
        assert (event["timestamp"], event["length"], event["adsb_hex"]) == (T0, 1, "4ca1f2"), (
            "the one hit the dark lane is given"
        )
        assert event["is_anomalous"] is True and event["anomaly_types"] == ["supersonic"]
        assert event["adsb_initialized"] is False
        InMemoryEventWriter.resolve_event(event)
        assert [p["delay"] for p in event["detections"]] == [12.4]

    def test_a_coasting_track_gets_no_event(self):
        writer = InMemoryEventWriter()
        tracker = NodeTracker("n1", writer)
        _feed("n1", _frame(tracks=[_track()]), _frame(ts=T0 + 1000, tracks=[_track(state="coasting", hit=None)]))

        tracker.process_frame([], T0 + 1000)

        assert writer.get_new_events() == {}
        assert len(tracker.tracks) == 1

    def test_a_frame_with_no_tracker_output_emits_nothing_and_keeps_the_tracks(self):
        writer = InMemoryEventWriter()
        tracker = NodeTracker("n1", writer)
        _feed("n1", _frame(tracks=[_track()]))
        tracker.process_frame([], T0)
        writer.get_new_events()

        _feed("n1", _frame(ts=T0 + 1000, tracks=None))
        tracker.process_frame([], T0 + 1000)

        assert writer.get_new_events() == {}
        assert len(tracker.tracks) == 1

    def test_the_multinode_views_read_it_as_they_read_the_in_process_tracker(self):
        tracker = NodeTracker("n1", InMemoryEventWriter())
        _feed("n1", _frame(tracks=[_track()]), _frame(ts=T0 + 1000, tracks=[_track(hit=1)]))
        tracker.process_frame([], T0 + 1000)

        (view,) = confirmed_track_views(tracker, now_ts_ms=T0 + 1000)

        assert view["track_id"] == f"n1:{RUN}:260923-00001A"
        assert [h["delay_us"] for h in view["history"]] == [12.4, 30.1]
        assert [h["t_s"] for h in view["history"]] == [T0 / 1000, (T0 + 1000) / 1000]


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


class TestCutOver:
    def _process(self, node_id: str, frame: dict):
        process_one_frame(node_id, frame, PassiveRadarPipeline(DEFAULT_NODE_CONFIG))
        return state.node_pipelines[node_id]

    def test_live_cuts_a_node_over_on_its_first_tracked_frame(self, monkeypatch):
        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        register_test_node("test-cutover-live", _NODE_CFG)
        now = int(time.time() * 1000)

        pipeline = self._process("test-cutover-live", _frame(ts=now, tracks=[_track()]))

        assert isinstance(pipeline.tracker, NodeTracker)
        assert f"test-cutover-live:{RUN}:260923-00001A" in pipeline.event_writer.events

    def test_off_keeps_the_in_process_tracker(self, monkeypatch):
        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "off")
        register_test_node("test-cutover-off", _NODE_CFG)

        pipeline = self._process("test-cutover-off", _frame(ts=int(time.time() * 1000), tracks=[_track()]))

        assert not isinstance(pipeline.tracker, NodeTracker)
        assert node_tracks.store.summary()["open"] == 1, "the tracks are still filed"

    def test_an_untracked_frame_does_not_cut_a_node_over(self, monkeypatch):
        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        register_test_node("test-cutover-untracked", _NODE_CFG)

        pipeline = self._process("test-cutover-untracked", _frame(ts=int(time.time() * 1000), run=None))

        assert not isinstance(pipeline.tracker, NodeTracker)

    def test_node_verification_reads_a_cut_over_nodes_detections(self, monkeypatch):
        """Node verification counts an aircraft as detected when a track took a
        detection this frame, from the detection's own tag."""
        from services.tasks.analytics_refresh import _detected_hexes_for

        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        # A fresh claim also counts as a detection, so with the known lane on
        # this would pass without the track being read at all.
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "off")
        register_test_node("test-cutover-verify", _NODE_CFG)
        frame = _frame(
            ts=int(time.time() * 1000),
            tracks=[_track(hit=1), _track(id="coast", state="coasting", hit=None, n_missed=1, adsb_hex="abcdef")],
        )
        frame["adsb"] = [None, {"hex": "4ca1f2", "lat": 34.9, "lon": -82.4}]
        self._process("test-cutover-verify", frame)

        assert "4ca1f2" in _detected_hexes_for("test-cutover-verify")
        assert "abcdef" not in _detected_hexes_for("test-cutover-verify"), "a coasting track detected nothing"

    def _two_tracks_one_tagged(self, node_id: str, monkeypatch, known_lane_mode: str):
        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", known_lane_mode)
        register_test_node(node_id, _NODE_CFG)
        frame = _frame(
            ts=int(time.time() * 1000), tracks=[_track(id="lit", adsb_hex="4ca1f2"), _track(id="dark", hit=1)]
        )
        frame["adsb"] = [{"hex": "4ca1f2", "lat": 34.88, "lon": -82.35, "alt_baro": 23000}, None]
        return {t.id.rsplit(":", 1)[1] for t in self._process(node_id, frame).tracker.tracks}

    def test_a_binding_claim_keeps_its_track_from_the_dark_lane(self, monkeypatch):
        assert self._two_tracks_one_tagged("test-cutover-binding", monkeypatch, "binding") == {"dark"}

    def test_a_shadow_claim_leaves_its_track_to_the_dark_lane(self, monkeypatch):
        assert self._two_tracks_one_tagged("test-cutover-shadow", monkeypatch, "shadow") == {"lit", "dark"}

    def test_a_failed_strip_leaves_every_hit_to_the_dark_lane(self, monkeypatch):
        """The frame then continues whole on the dark lane, so its tracks must too."""
        import services.frame_processor as fp

        def fail(frame, claimed_idx):
            raise ValueError("unstrippable")

        monkeypatch.setattr(fp, "strip_claimed_detections", fail)
        assert self._two_tracks_one_tagged("test-cutover-strip-fails", monkeypatch, "binding") == {"lit", "dark"}

    def test_a_node_on_probation_keeps_every_hit_on_its_own_tracks(self, monkeypatch):
        from services import probation

        monkeypatch.setattr(probation, "in_probation", lambda node_id: True)
        assert self._two_tracks_one_tagged("test-cutover-probation", monkeypatch, "binding") == {"lit", "dark"}

    def test_a_binding_dark_follow_keeps_its_track_from_the_dark_lane(self, monkeypatch):
        import services.frame_processor as fp

        def follow_the_second(node_id, frame, follow_claimed=None):
            follow_claimed.add(1)
            return set()

        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        monkeypatch.setattr(state, "KNOWN_LANE_MODE", "binding")
        monkeypatch.setattr(state, "DARK_FOLLOW_MODE", "binding")
        monkeypatch.setattr(fp, "claim_known_targets", follow_the_second)
        register_test_node("test-cutover-follow", _NODE_CFG)
        frame = _frame(ts=int(time.time() * 1000), tracks=[_track(id="free"), _track(id="followed", hit=1)])

        tracks = self._process("test-cutover-follow", frame).tracker.tracks

        assert {t.id.rsplit(":", 1)[1] for t in tracks} == {"free"}

    def test_a_cut_over_node_stays_over_when_a_frame_comes_without_tracks(self, monkeypatch):
        monkeypatch.setattr(state, "NODE_TRACKS_MODE", "live")
        register_test_node("test-cutover-stays", _NODE_CFG)
        now = int(time.time() * 1000)
        self._process("test-cutover-stays", _frame(ts=now, tracks=[_track()]))

        pipeline = self._process("test-cutover-stays", _frame(ts=now + 1000, run=None))

        assert isinstance(pipeline.tracker, NodeTracker)
