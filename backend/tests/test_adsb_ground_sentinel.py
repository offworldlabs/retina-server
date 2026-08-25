"""tar1090 reports alt_baro as the string "ground"; the backend must survive it.

A bare multiply on that field raises TypeError, and nothing between
`_run_geolocation` and `frame_loop` catches it, so one grounded aircraft
costs the whole frame: track association, the state.adsb_aircraft refresh
and the archive append are all skipped.  The record keeps its freshness
stamp, so every subsequent frame dies the same way until it ages out.

Four sites sit on the frame path: the inline tag at the GeoDetection
boundary, the fresh fix injected into the initial guess, the between-solves
altitude refresh and the ADS-B bootstrap after a solver failure.  Two more
read the same raw record off it — the per-node verification refresh and
POST /api/test/validate — and are covered by the second class here; the
node-tag site in the known lane is covered in test_known_claiming.py.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import orjson
import pytest

from core import state
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from routes.test import validate_ground_truth
from services.geo import bistatic_delay_us
from services.tasks.analytics_refresh import _refresh_node_verification

_NODE_CONFIG = {**DEFAULT_NODE_CONFIG, "node_id": "ground-sentinel-node"}
_HEX = "gnd001"

_SOLUTION = {
    "success": True,
    "state": [1.0, 1.0, 5.0, 100.0, 0.0, 0.0],
    "rms_delay": 0.1,
    "rms_doppler": 0.1,
}


@pytest.fixture()
def pipe():
    return PassiveRadarPipeline(_NODE_CONFIG)


@pytest.fixture()
def grounded_aircraft():
    """A live ADS-B fix for an aircraft on the ground, stored as every writer stores it."""
    previous = state.adsb_aircraft.get(_HEX)
    state.adsb_aircraft[_HEX] = {
        "hex": _HEX,
        "lat": _NODE_CONFIG["rx_lat"] + 0.05,
        "lon": _NODE_CONFIG["rx_lon"] + 0.05,
        "alt_baro": "ground",
        "gs": 0,
        "track": 0,
        "last_seen_ms": int(time.time() * 1000),
    }
    yield
    if previous is None:
        state.adsb_aircraft.pop(_HEX, None)
    else:
        state.adsb_aircraft[_HEX] = previous
    # _run_geolocation publishes here and never retracts, so the fixture must.
    with state.geo_aircraft_lock:
        state.active_geo_aircraft.pop(_HEX, None)


def _det(ts_ms, delay_us):
    return {"timestamp": ts_ms, "delay": delay_us, "doppler": 10.0, "snr": 15.0, "adsb": None}


def _event(track_id, adsb_hex=_HEX):
    dets = [_det(1000, 50.0), _det(2000, 60.0), _det(3000, 70.0)]
    return {
        "track_id": track_id,
        "timestamp": dets[-1]["timestamp"],
        "length": len(dets),
        "detections": dets,
        "adsb_hex": adsb_hex,
        "adsb_initialized": False,
        "is_anomalous": False,
        "max_velocity_ms": 0.0,
        "anomaly_types": [],
    }


class TestGroundSentinelOnTheFramePath:
    def test_initial_guess_injection_does_not_kill_the_frame(self, pipe, grounded_aircraft):
        """The freshest fix is injected raw into the guess; the geolocator multiplies it
        outside the solver's try, so it must be coerced before it leaves this repo."""
        pipe._geolocate_track_event("trk-g1", _event("trk-g1"))  # must not raise

    def test_inline_tag_survives_when_the_fresh_injection_does_not_fire(self):
        """The injection only overwrites detection[0] when a live fix exists.

        With no entry in state.adsb_aircraft -- stale, absent, or a hex whose case
        does not match -- the tracker's own inline tag is what reaches the
        geolocator, still raw.
        """
        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.adsb_aircraft.pop(_HEX, None)

        event = _event("trk-g5")
        # The geolocator reads the tag only on an ADS-B-initialized track, and
        # returns early unless lat/lon and track are present, so all three are
        # needed to reach the alt_baro and gs multiplies.
        event["adsb_initialized"] = True
        event["detections"][0]["adsb"] = {
            "hex": _HEX,
            "lat": _NODE_CONFIG["rx_lat"] + 0.05,
            "lon": _NODE_CONFIG["rx_lon"] + 0.05,
            "alt_baro": "ground",
            "gs": None,
            "track": 0,
        }

        pipe._geolocate_track_event("trk-g5", event)  # must not raise

    @pytest.mark.parametrize("bad", ["ground", ["gnd001"]])
    def test_a_non_dict_tag_is_passed_through_not_unpacked(self, bad):
        """Nothing type-checks the tag: `frame["adsb"][i]` is copied onto the
        detection raw, and DetectionRequest.frames is `list[dict]` with extras
        allowed, so the entries inside a frame are unvalidated.  The geolocator
        tolerates a non-dict -- `"lat" not in adsb` is a valid membership test
        on a string or a list -- so coercion must not be the thing that raises.
        """
        assert PassiveRadarPipeline._coerced_adsb(bad) is bad

        pipe = PassiveRadarPipeline(_NODE_CONFIG)
        state.adsb_aircraft.pop(_HEX, None)
        event = _event("trk-g6")
        event["adsb_initialized"] = True
        event["detections"][0]["adsb"] = bad

        pipe._geolocate_track_event("trk-g6", event)  # must not raise

    def test_coercion_leaves_absent_keys_absent(self):
        """The geolocator branches on `"gs" in adsb`, so a key must not appear."""
        coerced = PassiveRadarPipeline._coerced_adsb({"hex": "abc123", "alt_baro": "ground"})

        assert coerced == {"hex": "abc123", "alt_baro": 0.0}

    def test_null_ground_speed_does_not_kill_the_frame(self, pipe, grounded_aircraft):
        """readsb omits velocity for some aircraft; the geolocator multiplies gs raw too."""
        state.adsb_aircraft[_HEX] = {**state.adsb_aircraft[_HEX], "gs": None, "track": None}

        pipe._geolocate_track_event("trk-g4", _event("trk-g4"))  # must not raise

    def test_refresh_between_solves_coerces_to_zero(self, pipe, grounded_aircraft):
        """Between solves the track keeps its position but refreshes altitude from ADS-B."""
        with (
            patch("pipeline.passive_radar.solve_track", return_value=_SOLUTION),
            patch(
                "pipeline.passive_radar.select_initial_guess",
                return_value=([1.0, 1.0, 5.0, 0.0, 0.0, 0.0], "beam"),
            ),
            patch(
                "pipeline.passive_radar.generate_initial_guess",
                return_value=[1.0, 1.0, 5.0, 0.0, 0.0, 0.0],
            ),
        ):
            seeded = pipe._geolocate_track_event("trk-g2", _event("trk-g2"))
        # Fixture guard: a None seed would fail the refresh assert below as an
        # AttributeError, blaming the coercion for a broken set-up.
        assert seeded is not None
        pipe.geolocated_tracks["trk-g2"] = seeded

        pipe._geo_last_solve["trk-g2"] = time.monotonic()  # rate limit active: refresh, do not re-solve
        pipe.event_writer.write_event("trk-g2", 5000, 1, [_det(5000, 71.5)], adsb_hex=_HEX)
        pipe._run_geolocation()

        assert pipe.geolocated_tracks["trk-g2"].alt_m == 0.0

    def test_adsb_bootstrap_after_solver_failure_coerces_to_zero(self, pipe, grounded_aircraft):
        """Solver fails on first encounter, so the track is built from ADS-B alone."""
        with patch("pipeline.passive_radar.solve_track", return_value={"success": False}):
            pipe.event_writer.write_event("trk-g3", 3000, 3, _event("trk-g3")["detections"], adsb_hex=_HEX)
            pipe._run_geolocation()

        track = pipe.geolocated_tracks.get("trk-g3")
        assert track is not None
        assert track.alt_m == 0.0


_VERIFY_NODE_ID = "ground-sentinel-verify"
_RX = (34.85, -82.40)
_TX = (34.90, -82.20)
_TARGET = (34.88, -82.35)


class TestGroundSentinelOffTheFramePath:
    """Both consumers read alt_baro straight off a state.adsb_aircraft record."""

    def test_node_verification_survives_a_grounded_truth_candidate(self):
        """float("ground") raises ValueError, and the caller's blanket except
        turns that into a node with no verification payload at all."""
        now = time.time()
        state.adsb_aircraft[_HEX] = {
            "hex": _HEX,
            "lat": _TARGET[0],
            "lon": _TARGET[1],
            "alt_baro": "ground",
            "gs": 120,
            "track": 90,
            "last_seen_ms": int(now * 1000),
        }
        track = SimpleNamespace(
            latest_delay_us=bistatic_delay_us(_TX[0], _TX[1], _RX[0], _RX[1], _TARGET[0], _TARGET[1]),
            wall_clock_ts=now,
            lat=_TARGET[0],
            lon=_TARGET[1],
            vel_east=0.0,
            vel_north=0.0,
            alt_m=3000.0,
        )
        cfg = {"node_id": _VERIFY_NODE_ID, "rx_lat": _RX[0], "rx_lon": _RX[1], "tx_lat": _TX[0], "tx_lon": _TX[1]}
        with state.geo_aircraft_lock:
            state.active_geo_aircraft["gnd-trk"] = (track, cfg)

        _refresh_node_verification(_VERIFY_NODE_ID)

        data = orjson.loads(state.latest_node_verification_bytes[_VERIFY_NODE_ID])
        assert data["n_matched"] == 1
        (m,) = data["tracks"]
        assert m["truth_alt_m"] == 0.0
        assert m["altitude_error_m"] == 3000.0

    async def test_validate_ground_truth_survives_a_grounded_aircraft(self, monkeypatch):
        """A truthiness test is no guard here — "ground" is truthy."""
        monkeypatch.setattr(
            state,
            "latest_aircraft_json",
            {"aircraft": [{"hex": _HEX, "lat": _TARGET[0], "lon": _TARGET[1], "alt_baro": "ground"}]},
        )
        body = {"ground_truth": [{"id": "gt1", "lat": _TARGET[0], "lon": _TARGET[1], "alt_km": 1.0}]}

        result = await validate_ground_truth(body=body, _key=None)

        assert result["validation"]["matched"] == 1
        assert result["accuracy"]["avg_altitude_error_m"] == 1000
