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
"""

import threading
from collections import deque
from dataclasses import dataclass, field

from config.constants import N2_TRACK_HISTORY_MAX, STALE_TRACK_S

_FORGET_MS = STALE_TRACK_S * 1000.0


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

    def ingest(self, node_id: str, frame: dict) -> int:
        """File one queue frame's tracks, and return how many it refused.

        `frame` must be the frame as the node sent it: a track's `hit` indexes
        its arrays, and the known lane's strip renumbers them.  A frame with no
        `tracker` is a node that sends no tracks, and leaves the store as it
        was; so does one whose `tracks` is absent, which is a tracker with no
        output for that frame.  A malformed track, possible only on a frame that
        arrived untyped, is refused alone: it changes nothing, and the frame's
        other tracks are filed regardless.
        """
        tracker = frame.get("tracker")
        if not isinstance(tracker, dict) or not tracker.get("run"):
            return 0
        run = str(tracker["run"])
        now_ms = int(frame["timestamp"])
        refused = 0
        with self._lock:
            node = self._nodes.setdefault(node_id, _Node())
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
                self._file(node, run, update, point, now_ms)
            self._forget(node, now_ms)
        return refused

    def summary(self) -> dict:
        """What the store holds, for the test dashboard."""
        with self._lock:
            tracks = [t for node in self._nodes.values() for t in node.tracks.values()]
            return {
                "nodes_sending": sum(1 for node in self._nodes.values() if node.run is not None),
                "open": sum(1 for t in tracks if t.closed is None),
                "closed": sum(1 for t in tracks if t.closed is not None),
                "points": sum(len(t.window) for t in tracks),
            }

    @staticmethod
    def _file(node: _Node, run: str, update: dict, point: dict | None, now_ms: int) -> None:
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
        if track.state == "deleted":
            track.closed = "deleted"

    @staticmethod
    def _forget(node: _Node, now_ms: int) -> None:
        stale = [key for key, t in node.tracks.items() if now_ms - t.last_named_ms > _FORGET_MS]
        for key in stale:
            del node.tracks[key]


def _read(wire: dict, frame: dict, now_ms: int) -> tuple[dict, dict | None]:
    """One wire track's values and the detection it hit, read in full before the
    store changes anything, so a malformed track leaves its entry as it was."""
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


store = NodeTrackStore()
