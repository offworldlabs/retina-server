"""The feed's arc builder must not iterate a track history deque in place.

`Track.history["measurements"]` is a bounded `deque` (retina-tracker #25).  The
frame worker appends to it once per frame; `build_combined_aircraft_json` runs
on the aircraft-flush executor and reverse-scans it for the newest associated
measurement.  Iterating a deque with Python-level code while another thread
appends raises `RuntimeError: deque mutated during iteration` — which a plain
list tolerated — and killed one whole 1 s feed tick each time it fired
(`task_error_counts {'aircraft_flush': N}` live).  `list(dq)` is a single C
call, hence an atomic snapshot under the GIL, so the scan runs off a copy.
"""

import os
import threading
import time
import types
from collections import deque

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from core import state  # noqa: E402
from retina_tracker.track import TrackState  # noqa: E402
from services import aircraft_feed  # noqa: E402
from services.frame_processor import build_combined_aircraft_json  # noqa: E402

NODE_ID = "snapshot-node"


def _measurement(i):
    return {"delay": 40.0 + (i % 20) * 0.5, "doppler": 12.0, "snr": 18.0}


def _make_pipeline():
    track = types.SimpleNamespace(
        id=7,
        adsb_hex=None,
        state_status=TrackState.ACTIVE,
        history={"measurements": deque([_measurement(0)], maxlen=600)},
    )
    tracker = types.SimpleNamespace(tracks=[track])
    config = {
        "node_id": NODE_ID,
        "latitude": 35.0,
        "longitude": -82.0,
        "tx_latitude": 35.2,
        "tx_longitude": -82.3,
    }
    return types.SimpleNamespace(tracker=tracker, config=config, geolocated_tracks={}), track


def test_arc_builder_survives_a_concurrently_appended_history():
    pipeline, track = _make_pipeline()
    saved_pipelines = dict(state.node_pipelines)
    state.node_pipelines.clear()
    state.node_pipelines[NODE_ID] = pipeline

    meas = track.history["measurements"]
    stop = threading.Event()
    errors = []

    def appender():
        i = 0
        try:
            while not stop.is_set():
                i += 1
                # The frame worker appends None on the coast path, so the
                # reverse scan has to skip entries as it walks.
                meas.append(None if i % 4 == 0 else _measurement(i))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    writer = threading.Thread(target=appender, daemon=True)
    writer.start()
    builds = 0
    try:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            # The arc section is recomputed at most once per _ARC_REFRESH_S;
            # clear the timer so every call really walks the history.
            aircraft_feed._arcs_last_ts = 0.0
            build_combined_aircraft_json(types.SimpleNamespace(geolocated_tracks={}, config={}))
            builds += 1
    finally:
        stop.set()
        writer.join(timeout=2.0)
        state.node_pipelines.clear()
        state.node_pipelines.update(saved_pipelines)
        aircraft_feed._arcs_last_ts = 0.0

    assert not errors, errors
    assert builds > 10, f"stress loop did too little work to be meaningful ({builds} builds)"
