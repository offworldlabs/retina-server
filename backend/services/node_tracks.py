"""Each node's own tracks, as its tracker reports them on the detection frame.

A node running a tracker sends every confirmed track on every frame the track
is alive, and each track names the detection it took by index into the frame's
arrays (contract 1.6.0).  This store keeps, per node, what the server's readers
take from a tracker: a window of each track's recent detections, built from
those hits, and the track's counters and flags.

A track is keyed on (run, id) within its node.  The tracker's ids repeat after
a restart, so a frame naming a run the store has not seen closes the previous
run's tracks.  A `deleted` track closes its key, and anything later sent for a
closed key is ignored.  Open or closed, a track is forgotten once no frame from
its node has named it for STALE_TRACK_S of frame time.

NodeTracker is how the readers get at it: once a node's pipeline is cut over
(NODE_TRACKS_MODE), it stands where the pipeline's in-process tracker stood and
presents the node's open tracks in the shape that tracker's readers take.

Those readers are the dark lane, and in binding mode a detection the known lane
or dark following claims never reaches the in-process tracker.  A node's tracker
keeps it, so each track is presented only from the hits after the newest one a
binding lane claimed, and not at all until there is one: to the dark lane, a
claim is a break in the track.
"""

import threading
from collections import deque
from dataclasses import dataclass, field

from retina_tracker.track import TrackState

from config.constants import DISPLAY_STALE_TRACK_S, N2_TRACK_HISTORY_MAX, STALE_TRACK_S
from services.id_utils import normalize_hex_key

_FORGET_MS = STALE_TRACK_S * 1000.0
# How long a track no frame has named stays among its node's tracks, as a
# coasting track stays in the in-process tracker's until it is deleted.
_SHOWN_MS = DISPLAY_STALE_TRACK_S * 1000.0
_STATES = ("active", "coasting", "deleted")
_TRACKER_STATE = {"active": TrackState.ACTIVE, "coasting": TrackState.COASTING}


@dataclass
class NodeTrack:
    """One track as the node's tracker last described it."""

    run: str
    id: str
    state: str = "active"
    # None while open; "deleted" or "tracker_restart" once closed.
    closed: str | None = None
    n_associated: int = 0
    n_missed: int = 0
    adsb_hex: str | None = None
    is_anomalous: bool = False
    anomaly_types: list[str] = field(default_factory=list)
    max_velocity_ms: float = 0.0
    born_t: float | None = None
    avg_snr: float | None = None
    shadow_fraction: float | None = None
    interference_fraction: float | None = None
    # Oldest first, as the in-process tracker's get_recent_detections returns
    # them: {timestamp, delay, doppler, snr, adsb}.
    window: deque = field(default_factory=lambda: deque(maxlen=N2_TRACK_HISTORY_MAX))
    # Hits filed since the newest one a binding lane claimed: the newest this
    # many points of the window are the dark lane's.
    unclaimed: int = 0
    # Hexes those hits were tagged with, which outlive the window.
    unclaimed_tags: set[str] = field(default_factory=set)
    # Frame time, ms, of the last frame that named the track.
    last_named_ms: int = 0


@dataclass
class _Node:
    run: str | None = None
    tracks: dict[tuple[str, str], NodeTrack] = field(default_factory=dict)


class NodeTrackStore:
    """Every node's tracks, fed one frame at a time.

    Frames for one node arrive on one frame worker, in order, but the store is
    read from other threads, so every access holds the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, _Node] = {}

    def ingest(self, node_id: str, frame: dict, claimed: set[int] = frozenset()) -> int:
        """File one queue frame's tracks, and return how many it refused.

        `frame` must be the frame as the node sent it: a track's `hit` indexes
        its arrays, and the known lane's strip renumbers them.  `claimed` holds
        the indices of the detections a binding lane took from it.  A frame
        naming no tracker run files nothing, but it still ages the node's
        tracks, and a node left holding none is forgotten: a node whose tracker
        stops does not keep its last tracks.  A frame whose `tracks` is absent
        is a tracker with no output for that frame.  A malformed track, possible
        only on a frame that arrived untyped, is refused alone: it changes
        nothing, and the frame's other tracks are filed regardless.
        """
        run = tracker_run(frame)
        refused = 0
        with self._lock:
            node = self._nodes.get(node_id)
            if run is None:
                if node is not None:
                    self._forget(node, int(frame["timestamp"]))
                    if not node.tracks:
                        del self._nodes[node_id]
                return 0
            now_ms = int(frame["timestamp"])
            if node is None:
                node = self._nodes[node_id] = _Node()
            if node.run != run:
                for track in node.tracks.values():
                    if track.closed is None:
                        track.closed = "tracker_restart"
                node.run = run
            for wire in frame.get("tracks") or ():
                try:
                    update, point = _read(wire, frame, now_ms)
                except (KeyError, IndexError, TypeError, ValueError):
                    refused += 1
                    continue
                self._file(node, run, update, point, wire.get("hit") in claimed, now_ms)
            self._forget(node, now_ms)
        return refused

    def views(self, node_id: str, now_ms: int) -> list["NodeTrackView"]:
        """The node's open tracks that a frame has named within
        DISPLAY_STALE_TRACK_S of `now_ms` and that hold a hit no binding lane
        claimed after it, snapshotted under the lock."""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return []
            return [
                NodeTrackView(node_id, track)
                for track in node.tracks.values()
                if track.closed is None and track.unclaimed and now_ms - track.last_named_ms <= _SHOWN_MS
            ]

    def summary(self) -> dict:
        """What the store holds, for the test dashboard."""
        with self._lock:
            tracks = [t for node in self._nodes.values() for t in node.tracks.values()]
            return {
                "nodes_sending": sum(1 for node in self._nodes.values() if node.run is not None),
                "open": sum(1 for t in tracks if t.closed is None),
                "closed": sum(1 for t in tracks if t.closed is not None),
                "claimed": sum(1 for t in tracks if t.closed is None and t.window and not t.unclaimed),
                "points": sum(len(t.window) for t in tracks),
            }

    @staticmethod
    def _file(node: _Node, run: str, update: dict, point: dict | None, claimed: bool, now_ms: int) -> None:
        key = (run, update["id"])
        track = node.tracks.get(key)
        if track is not None and track.closed is not None:
            return
        if track is None:
            track = node.tracks[key] = NodeTrack(run=run, id=key[1])
        track.state = update["state"]
        track.n_associated = update["n_associated"]
        track.n_missed = update["n_missed"]
        track.adsb_hex = update["adsb_hex"]
        track.is_anomalous = update["is_anomalous"]
        track.anomaly_types = update["anomaly_types"]
        track.max_velocity_ms = update["max_velocity_ms"]
        track.born_t = update["born_t"]
        track.avg_snr = update["avg_snr"]
        track.shadow_fraction = update["shadow_fraction"]
        track.interference_fraction = update["interference_fraction"]
        track.last_named_ms = now_ms
        if point is not None:
            track.window.append(point)
            if claimed:
                track.unclaimed = 0
                track.unclaimed_tags.clear()
            else:
                track.unclaimed += 1
                tag_hex = normalize_hex_key(point["adsb"].get("hex")) if isinstance(point["adsb"], dict) else ""
                if tag_hex:
                    track.unclaimed_tags.add(tag_hex)
        if track.state == "deleted":
            track.closed = "deleted"

    @staticmethod
    def _forget(node: _Node, now_ms: int) -> None:
        stale = [key for key, t in node.tracks.items() if now_ms - t.last_named_ms > _FORGET_MS]
        for key in stale:
            del node.tracks[key]


def tracker_run(frame: dict) -> str | None:
    """The tracker run a frame names, or None for a frame the store files nothing from."""
    tracker = frame.get("tracker")
    if not isinstance(tracker, dict) or not tracker.get("run"):
        return None
    return str(tracker["run"])


def _read(wire: dict, frame: dict, now_ms: int) -> tuple[dict, dict | None]:
    """One wire track's values and the detection it hit, read in full before the
    store changes anything, so a malformed track leaves its entry as it was."""
    if wire["state"] not in _STATES:
        raise ValueError(f"state {wire['state']!r} is not a track state")
    update = {
        "id": str(wire["id"]),
        "state": wire["state"],
        "n_associated": wire["n_associated"],
        "n_missed": wire["n_missed"],
        "adsb_hex": wire.get("adsb_hex"),
        "is_anomalous": wire["is_anomalous"],
        "anomaly_types": list(wire.get("anomaly_types") or ()),
        "max_velocity_ms": wire["max_velocity_ms"],
        "born_t": wire.get("born_t"),
        "avg_snr": wire.get("avg_snr"),
        "shadow_fraction": wire.get("shadow_fraction"),
        "interference_fraction": wire.get("interference_fraction"),
    }
    hit = wire.get("hit")
    if hit is None:
        return update, None
    # A negative index would read from the end of the arrays rather than fail.
    if not isinstance(hit, int) or isinstance(hit, bool) or hit < 0:
        raise ValueError(f"hit {hit!r} is not an index")
    adsb = frame.get("adsb")
    point = {
        "timestamp": now_ms,
        "delay": frame["delay"][hit],
        "doppler": frame["doppler"][hit],
        "snr": frame["snr"][hit],
        "adsb": adsb[hit] if adsb else None,
    }
    return update, point


class NodeTrackView:
    """One open node track in the shape readers of the in-process tracker take:
    the attributes PassiveRadarPipeline, confirmed_track_views, the feed's arcs
    and node verification read from a tracker's track.

    Built from a snapshot, so a reader on another thread never sees the window
    move under it.
    """

    def __init__(self, node_id: str, track: NodeTrack) -> None:
        # Unique across nodes and tracker restarts, since every store downstream
        # keys on it: the solver's claims, identity links, the map's `pr` hex.
        self.id = f"{node_id}:{track.run}:{track.id}"
        self.state_status = _TRACKER_STATE[track.state]
        self.n_associated = track.n_associated
        self.n_missed = track.n_missed
        self.is_anomalous = track.is_anomalous
        self.anomaly_types = list(track.anomaly_types)
        self.max_velocity_ms = track.max_velocity_ms
        points = list(track.window)
        self._points = points[max(0, len(points) - track.unclaimed) :]
        # The tracker keeps a hex after its track swaps onto an untagged
        # target, so the hex stands only where a hit since the newest claim
        # was tagged with it.
        hexn = normalize_hex_key(track.adsb_hex)
        self.adsb_hex = hexn if hexn in track.unclaimed_tags else None
        # The feed's arcs take the newest measurement from here.
        self.history = {"measurements": self._points}

    def get_recent_detections(self, n: int = N2_TRACK_HISTORY_MAX) -> list[dict]:
        """The newest `n` associated detections, oldest first, as the in-process
        tracker's tracks return them."""
        return self._points[-n:] if n > 0 else []


class NodeTracker:
    """A node's own tracks, standing where PassiveRadarPipeline keeps its tracker.

    With NODE_TRACKS_MODE live, a node's first tracked frame swaps its
    pipeline's tracker for this, and every reader of `pipeline.tracker` then
    reads the node's tracks unchanged.  `tracks` is a new list each frame, as
    the in-process tracker's is, because the feed, node verification and the
    solver thread iterate it off the frame worker.
    """

    def __init__(self, node_id: str, event_writer) -> None:
        self.node_id = node_id
        self.event_writer = event_writer
        self.tracks: list[NodeTrackView] = []

    def process_frame(self, detections, timestamp) -> None:
        """The in-process tracker's turn in PassiveRadarPipeline.process_frame.

        `detections` is the pipeline's copy of the frame, which the known lane
        may have renumbered, so it is not read: the node's tracks index the
        frame as sent, and frame_processor filed them from it already.  A track
        that took a detection this frame gets an event, as a confirmed track's
        association does in the in-process tracker, and _run_geolocation reads
        its window through the reference.
        """
        now_ms = int(timestamp)
        self.tracks = store.views(self.node_id, now_ms)
        for view in self.tracks:
            newest = view.get_recent_detections(1)
            if not newest or newest[0]["timestamp"] != now_ms:
                continue
            self.event_writer.write_event_lazy(
                view.id,
                now_ms,
                len(view.get_recent_detections()),
                view,
                adsb_hex=view.adsb_hex,
                # The pipeline sets its own when it injects a fresh ADS-B fix.
                adsb_initialized=False,
                is_anomalous=view.is_anomalous,
                max_velocity_ms=view.max_velocity_ms,
                anomaly_types=set(view.anomaly_types),
            )


store = NodeTrackStore()
