"""Tests for services/public_location.py and the payloads it rewrites.

Two halves.  The first pins the offset itself — determinism, magnitude,
independence between nodes, the salt, rigid polygon translation, and the
off switch.  The second checks the property that actually matters, on the
payloads a stranger can fetch: no true receiver coordinate anywhere in them.
"""

import math
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RETINA_ENV", "test")

from config.constants import (  # noqa: E402
    NODE_FUZZ_MAX_KM_DEFAULT,
    NODE_FUZZ_MIN_KM_DEFAULT,
)
from core import state  # noqa: E402
from main import app  # noqa: E402
from services import public_location as pl  # noqa: E402
from services.geo import haversine_km  # noqa: E402
from services.tasks.analytics_refresh import _public_location_block  # noqa: E402

# A salt fixed here rather than left to the runtime file, so a failure is
# reproducible and never depends on what a previous run wrote to disk.
_SALT = "test-salt-for-public-location"

# Rounding the published coordinate to 4 decimals moves it by up to ~8 m, so a
# displacement drawn at exactly NODE_FUZZ_MIN_KM can land marginally inside the
# floor.  Assertions on the SERVED coordinate allow for that; assertions on
# public_offset_km(), which is unrounded, do not.
_ROUNDING_SLACK_KM = 0.02


@pytest.fixture(autouse=True)
def _fuzz_env(monkeypatch):
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", _SALT)
    monkeypatch.delenv("NODE_FUZZ_MIN_KM", raising=False)
    monkeypatch.delenv("NODE_FUZZ_MAX_KM", raising=False)
    pl._reset_for_tests()
    yield
    pl._reset_for_tests()


def _offset_km(node_id: str) -> float:
    east_km, north_km = pl.public_offset_km(node_id)
    return math.hypot(east_km, north_km)


# ── The offset ───────────────────────────────────────────────────────────────


class TestOffset:
    def test_same_node_same_offset_across_calls(self):
        assert pl.public_offset_km("node-a") == pl.public_offset_km("node-a")

    def test_same_node_same_offset_across_process_restarts(self):
        """The memo is a speed-up, not the source of the answer.

        Clearing it stands in for a restart: if the offset came from anything
        process-local (a random seed, a dict ordering, a clock) it would move
        here, and a node that moves per boot is averaged back to the truth by
        anyone logging the feed for a day.
        """
        first = pl.public_offset_km("node-a")
        pl._reset_for_tests()
        assert pl.public_offset_km("node-a") == first

    def test_displacement_within_configured_bounds(self):
        for node_id in (f"node-{i}" for i in range(200)):
            distance_km = _offset_km(node_id)
            assert NODE_FUZZ_MIN_KM_DEFAULT <= distance_km <= NODE_FUZZ_MAX_KM_DEFAULT

    def test_displacement_honours_custom_bounds(self, monkeypatch):
        monkeypatch.setenv("NODE_FUZZ_MIN_KM", "5.0")
        monkeypatch.setenv("NODE_FUZZ_MAX_KM", "7.0")
        for node_id in (f"node-{i}" for i in range(50)):
            assert 5.0 <= _offset_km(node_id) <= 7.0

    def test_inverted_bounds_do_not_escape_the_floor(self, monkeypatch):
        """A max below the min must clamp, not invert into a shorter hop."""
        monkeypatch.setenv("NODE_FUZZ_MIN_KM", "4.0")
        monkeypatch.setenv("NODE_FUZZ_MAX_KM", "1.0")
        assert _offset_km("node-a") == pytest.approx(4.0)

    def test_different_nodes_get_different_offsets(self):
        offsets = {pl.public_offset_km(f"node-{i}") for i in range(200)}
        assert len(offsets) == 200

    def test_bearings_cover_the_circle(self):
        """A donut, not an arc: every quadrant is reachable.

        Guards the digest split — slicing both draws out of the same bytes, or
        taking the bearing from too few bits, would collapse the offsets onto a
        line and make the true position recoverable from two nodes.
        """
        quadrants = set()
        for i in range(200):
            east_km, north_km = pl.public_offset_km(f"node-{i}")
            quadrants.add((east_km >= 0, north_km >= 0))
        assert len(quadrants) == 4

    def test_salt_changes_the_offset(self, monkeypatch):
        before = pl.public_offset_km("node-a")
        monkeypatch.setenv("NODE_FUZZ_SALT", "a-different-salt")
        pl._reset_for_tests()
        assert pl.public_offset_km("node-a") != before

    def test_missing_node_id_is_still_displaced(self):
        """A config fault must not fall through to publishing the truth."""
        lat, lon = pl.public_latlon(51.5, -0.12, None)
        assert haversine_km(51.5, -0.12, lat, lon) >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM


class TestPublicLatLon:
    TRUE_LAT, TRUE_LON = 51.5074123, -0.1278456

    def test_published_point_is_at_least_the_floor_away(self):
        lat, lon = pl.public_latlon(self.TRUE_LAT, self.TRUE_LON, "node-a")
        distance_km = haversine_km(self.TRUE_LAT, self.TRUE_LON, lat, lon)
        assert distance_km >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM
        assert distance_km <= NODE_FUZZ_MAX_KM_DEFAULT + _ROUNDING_SLACK_KM

    def test_rounded_to_four_decimals(self):
        lat, lon = pl.public_latlon(self.TRUE_LAT, self.TRUE_LON, "node-a")
        assert lat == round(lat, 4)
        assert lon == round(lon, 4)

    def test_longitude_scales_with_latitude(self):
        """Near the pole a degree of longitude is short; the shift must grow.

        A fuzz that added a fixed degree offset would displace a high-latitude
        node by a few hundred metres instead of kilometres.
        """
        equator = pl.public_latlon(0.0, 0.0, "node-a")
        arctic = pl.public_latlon(70.0, 0.0, "node-a")
        assert abs(arctic[1]) > abs(equator[1]) * 2

    def test_non_numeric_passes_through(self):
        assert pl.public_latlon(None, None, "node-a") == (None, None)
        assert pl.public_latlon(float("nan"), 1.0, "node-a")[1] == 1.0


class TestTranslatePolygon:
    _POLY = [[51.5, -0.12], [51.6, -0.10], [51.55, 0.02], [51.5, -0.12]]

    def test_translation_is_rigid(self):
        moved = pl.translate_polygon(self._POLY, "node-a")
        deltas = [(m[0] - v[0], m[1] - v[1]) for v, m in zip(self._POLY, moved)]
        for delta in deltas[1:]:
            assert delta[0] == pytest.approx(deltas[0][0], abs=1e-9)
            assert delta[1] == pytest.approx(deltas[0][1], abs=1e-9)

    def test_shape_is_unchanged(self):
        """Rigid means the edges keep their lengths — only the anchor moved."""
        moved = pl.translate_polygon(self._POLY, "node-a")
        for (a, b), (ma, mb) in zip(zip(self._POLY, self._POLY[1:]), zip(moved, moved[1:])):
            assert haversine_km(*a, *b) == pytest.approx(haversine_km(*ma, *mb), rel=1e-3)

    def test_apex_lands_on_the_published_receiver(self):
        """The apex IS the receiver, so it has to arrive where rx does.

        A polygon whose tip sat on the true position beside a displaced marker
        would make the whole exercise decorative.
        """
        rx_lat, rx_lon = self._POLY[0]
        moved = pl.translate_polygon(self._POLY, "node-a", anchor_lat=rx_lat)
        published = pl.public_latlon(rx_lat, rx_lon, "node-a")
        assert haversine_km(moved[0][0], moved[0][1], *published) < 0.02

    def test_empty_and_none(self):
        assert pl.translate_polygon(None, "node-a") is None
        assert pl.translate_polygon([], "node-a") == []


class TestFuzzDisabled:
    @pytest.fixture(autouse=True)
    def _off(self, monkeypatch):
        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()

    def test_offset_is_zero(self):
        assert pl.public_offset_km("node-a") == (0.0, 0.0)

    def test_latlon_is_identity_not_even_rounded(self):
        assert pl.public_latlon(51.5074123, -0.1278456, "node-a") == (51.5074123, -0.1278456)

    def test_polygon_is_identity(self):
        poly = [[51.5, -0.12], [51.6, -0.10]]
        assert pl.translate_polygon(poly, "node-a") == poly

    def test_node_cfg_is_the_same_object(self):
        cfg = {"node_id": "node-a", "rx_lat": 51.5, "rx_lon": -0.12}
        assert pl.fuzz_node_cfg(cfg) is cfg


class TestSaltPersistence:
    def test_generated_salt_survives_a_restart(self, monkeypatch, tmp_path):
        """No NODE_FUZZ_SALT configured is the default deployment.

        The offsets still have to be the same after a reboot, which means the
        generated salt has to reach disk on first use.
        """
        monkeypatch.delenv("NODE_FUZZ_SALT", raising=False)
        monkeypatch.setattr(pl, "RUNTIME_DIR", tmp_path)
        monkeypatch.setattr(pl, "runtime_path", lambda name: tmp_path / name)
        pl._reset_for_tests()

        first = pl.public_offset_km("node-a")
        salt_file = tmp_path / pl._SALT_FILE
        assert salt_file.exists()
        assert salt_file.read_text(encoding="utf-8").strip()

        pl._reset_for_tests()  # stands in for a process restart
        assert pl.public_offset_km("node-a") == first


# ── The payloads ─────────────────────────────────────────────────────────────

_NODE_ID = "fuzz-int-1"
_TRUE_RX_LAT, _TRUE_RX_LON = 34.851234, -82.401234
_TRUE_TX_LAT, _TRUE_TX_LON = 34.901234, -82.301234
_NODE_CFG = {
    "node_id": _NODE_ID,
    "name": "Fuzz Integration Node",
    "rx_lat": _TRUE_RX_LAT,
    "rx_lon": _TRUE_RX_LON,
    "rx_alt_ft": 950.0,
    "tx_lat": _TRUE_TX_LAT,
    "tx_lon": _TRUE_TX_LON,
    "tx_alt_ft": 1600.0,
    "max_range_km": 50,
    "max_bistatic_range_km": 60,
}


def _km_from_true_rx(lat, lon) -> float:
    return haversine_km(_TRUE_RX_LAT, _TRUE_RX_LON, lat, lon)


def _assert_displaced(lat, lon, what: str):
    distance_km = _km_from_true_rx(lat, lon)
    assert distance_km >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM, (
        f"{what} is only {distance_km:.3f} km from the true receiver"
    )
    assert distance_km <= NODE_FUZZ_MAX_KM_DEFAULT + _ROUNDING_SLACK_KM, (
        f"{what} is {distance_km:.3f} km away — further than the declared uncertainty"
    )


@pytest.fixture()
def registered_node():
    """A node with known true coordinates, a coverage polygon and detections."""
    state.node_analytics.register_node(_NODE_ID, dict(_NODE_CFG))
    area = state.node_analytics.detection_areas[_NODE_ID]
    coverage = state.node_analytics.empirical_coverages[_NODE_ID]
    # MIN_POINTS (20) calibration points before to_polygon() emits anything.
    for i in range(30):
        coverage.add_point(_TRUE_RX_LAT + 0.05 + i * 0.002, _TRUE_RX_LON + 0.06 + i * 0.002)
    for i in range(5):
        area.record_verified_detection(_TRUE_RX_LAT + 0.1 + i * 0.01, _TRUE_RX_LON + 0.1, f"abc{i:03d}")
    yield _NODE_ID
    state.node_analytics.retire_node(_NODE_ID)


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestNodesPayload:
    """The `location` block of GET /api/radar/nodes."""

    def test_receiver_is_displaced_and_transmitter_is_not(self):
        block = _public_location_block(_NODE_ID, _NODE_CFG)
        _assert_displaced(block["rx_lat"], block["rx_lon"], "nodes payload rx")
        # The transmitter is a licensed broadcast tower on a public register.
        assert block["tx_lat"] == _TRUE_TX_LAT
        assert block["tx_lon"] == _TRUE_TX_LON

    def test_altitude_is_untouched(self):
        assert _public_location_block(_NODE_ID, _NODE_CFG)["rx_alt_ft"] == 950.0

    def test_uncertainty_is_declared(self):
        block = _public_location_block(_NODE_ID, _NODE_CFG)
        assert block["location_uncertainty_km"] == NODE_FUZZ_MAX_KM_DEFAULT

    def test_uncertainty_is_zero_when_disabled(self, monkeypatch):
        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()
        block = _public_location_block(_NODE_ID, _NODE_CFG)
        assert block["location_uncertainty_km"] == 0.0
        assert block["rx_lat"] == _TRUE_RX_LAT


class TestPerNodeAnalyticsRoute:
    """GET /api/radar/analytics/{node_id} — unauthenticated, no cache."""

    def test_detection_area_receiver_is_displaced(self, client, registered_node):
        body = client.get(f"/api/radar/analytics/{registered_node}").json()
        rx = body["detection_area"]["rx"]
        _assert_displaced(rx["lat"], rx["lon"], "detection_area.rx")

    def test_transmitter_survives(self, client, registered_node):
        tx = client.get(f"/api/radar/analytics/{registered_node}").json()["detection_area"]["tx"]
        assert tx["lat"] == pytest.approx(_TRUE_TX_LAT)
        assert tx["lon"] == pytest.approx(_TRUE_TX_LON)

    def test_coverage_polygon_apex_is_displaced(self, client, registered_node):
        polygon = client.get(f"/api/radar/analytics/{registered_node}").json()["empirical_coverage"]["polygon"]
        assert polygon, "fixture failed to build a polygon"
        # First and last vertex are both the sector tip, i.e. the receiver.
        _assert_displaced(polygon[0][0], polygon[0][1], "coverage polygon apex")
        _assert_displaced(polygon[-1][0], polygon[-1][1], "coverage polygon closing vertex")

    def test_no_polygon_vertex_sits_on_the_true_receiver(self, client, registered_node):
        polygon = client.get(f"/api/radar/analytics/{registered_node}").json()["empirical_coverage"]["polygon"]
        assert min(_km_from_true_rx(v[0], v[1]) for v in polygon) >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM

    def test_furthest_detections_are_not_published(self, client, registered_node):
        body = client.get(f"/api/radar/analytics/{registered_node}").json()
        assert "furthest_detections" not in body["detection_area"]

    def test_the_manager_keeps_the_truth(self, client, registered_node):
        """The rewrite is a copy — the pipeline still solves against the truth."""
        client.get(f"/api/radar/analytics/{registered_node}")
        area = state.node_analytics.detection_areas[registered_node]
        assert area.rx_lat == _TRUE_RX_LAT
        assert area.rx_lon == _TRUE_RX_LON
        assert state.node_analytics.get_node_summary(registered_node)["detection_area"]["furthest_detections"]


class TestDetectionRangeRoute:
    """GET /api/test/node/{node_id}/detection-range."""

    def test_receiver_and_polygon_are_displaced(self, client, registered_node):
        body = client.get(f"/api/test/node/{registered_node}/detection-range").json()
        _assert_displaced(body["rx"]["lat"], body["rx"]["lon"], "detection-range rx")
        assert body["empirical_coverage_polygon"], "fixture failed to build a polygon"
        for vertex in body["empirical_coverage_polygon"]:
            assert _km_from_true_rx(vertex[0], vertex[1]) >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM

    def test_furthest_detections_are_gone(self, client, registered_node):
        """Aircraft fix + range-from-receiver is a ranging circle per entry."""
        body = client.get(f"/api/test/node/{registered_node}/detection-range").json()
        assert "furthest_detections" not in body


class TestArchiveRows:
    """services/parquet_writer.py — /api/data/archive serves these raw."""

    def test_rows_carry_the_published_receiver(self, tmp_path):
        from services.parquet_writer import _flatten

        cols = _flatten(
            _NODE_ID,
            [{"timestamp": 1, "delay": [10.0], "doppler": [1.0], "snr": [20.0]}],
            ingest_ts_ms=1,
            node_cfg=dict(_NODE_CFG),
        )
        _assert_displaced(cols["rx_lat"][0], cols["rx_lon"][0], "archive rx")
        assert cols["tx_lat"][0] == _TRUE_TX_LAT
        assert cols["tx_lon"][0] == _TRUE_TX_LON


class TestPublishedArc:
    """The ambiguity arc a websocket client receives."""

    def test_arc_is_solved_around_the_published_receiver(self):
        from services.track_gates import _build_single_node_arc

        true_arc = _build_single_node_arc(80.0, _NODE_CFG)
        public_arc = _build_single_node_arc(80.0, pl.fuzz_node_cfg(_NODE_CFG))
        assert true_arc and public_arc
        assert public_arc != true_arc

    def test_arc_geometry_is_unchanged_when_disabled(self, monkeypatch):
        from services.track_gates import _build_single_node_arc

        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()
        assert _build_single_node_arc(80.0, pl.fuzz_node_cfg(_NODE_CFG)) == _build_single_node_arc(80.0, _NODE_CFG)
