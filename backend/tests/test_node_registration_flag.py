"""Which nodes get their DECLARED cone published as their detection area.

A synthetic node's cone is what the simulator enforces before it emits a
detection, so the cone is its detection area by definition; a real receiver's
declared aim was never surveyed, so it keeps the evidence-only polygon it has
always had.  register_node_blocking is the one door both go through, and the
verdict is services.tcp_handler.is_synthetic_node's.

Node ids here are prefix-legal for tests/test_no_real_identities.py: synth-*
and test-* are synthetic, and ret1a2b3c4d is the allow-listed stand-in for a
real board.
"""

import math
import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")

from core import state  # noqa: E402
from services import public_location as pl  # noqa: E402
from services.geo import haversine_km  # noqa: E402
from services.node_registration import register_node_blocking  # noqa: E402

_SYNTH_ID = "synth-GVL-0001"
_REAL_ID = "ret1a2b3c4d"

_RX_LAT, _RX_LON = 34.851234, -82.401234
_TX_LAT, _TX_LON = 34.901234, -82.301234

_CFG = {
    "rx_lat": _RX_LAT,
    "rx_lon": _RX_LON,
    "rx_alt_ft": 950.0,
    "tx_lat": _TX_LAT,
    "tx_lon": _TX_LON,
    "tx_alt_ft": 1600.0,
    "max_range_km": 50,
    "max_bistatic_range_km": 60,
    "beam_azimuth_deg": 45.0,
    "beam_width_deg": 42.0,
}


def _coverage(node_id):
    return state.node_analytics.get_node_summary(node_id)["empirical_coverage"]


@pytest.fixture()
def registered():
    """Both kinds of node, registered the way an entry point registers them."""
    for node_id in (_SYNTH_ID, _REAL_ID):
        register_node_blocking(node_id, dict(_CFG, node_id=node_id))
    yield
    for node_id in (_SYNTH_ID, _REAL_ID):
        state.node_analytics.retire_node(node_id)


class TestWhoseGeometryIsTruth:
    def test_a_synthetic_node_publishes_its_declared_cone(self, registered):
        cov = _coverage(_SYNTH_ID)
        assert cov["polygon_source"] == "declared"
        assert cov["polygon"]

    def test_it_is_published_before_any_traffic_has_flown(self, registered):
        """The declared cone is known at registration; the evidence gate that
        holds a real node's polygon back does not apply to it."""
        cov = _coverage(_SYNTH_ID)
        assert cov["n_points"] == 0
        assert len(cov["polygon"]) > 3

    def test_the_cone_is_the_declared_one(self, registered):
        """Every vertex inside the declared half-width — the bug this fixes
        was a 42 deg beam published across 71 of 72 bearings."""
        polygon = _coverage(_SYNTH_ID)["polygon"]
        for lat, lon in polygon[1:-1]:
            bearing = math.degrees(
                math.atan2(
                    (lon - _RX_LON) * math.cos(math.radians(_RX_LAT)),
                    lat - _RX_LAT,
                )
            )
            assert abs((bearing - 45.0 + 180.0) % 360.0 - 180.0) <= 21.0 + 0.25

    def test_a_real_node_still_publishes_evidence_only(self, registered):
        cov = _coverage(_REAL_ID)
        assert cov["polygon_source"] == "evidence"
        # Nothing measured yet, so there is nothing to draw — deliberately,
        # rather than a theoretical sector nobody surveyed.
        assert cov["polygon"] is None

    def test_the_two_registries_still_agree_on_the_node(self, registered):
        """The flag is an extra argument to one of the two registrations; the
        associator must still have been told about the node."""
        assert _SYNTH_ID in state.node_associator.node_configs


# ── What a stranger fetches ──────────────────────────────────────────────────

_SALT = "test-salt-for-declared-wedge"
_ROUNDING_SLACK_KM = 0.02


@pytest.fixture()
def _fuzz_on(monkeypatch):
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", _SALT)
    monkeypatch.delenv("NODE_FUZZ_MIN_KM", raising=False)
    monkeypatch.delenv("NODE_FUZZ_MAX_KM", raising=False)
    pl._reset_for_tests()
    yield
    pl._reset_for_tests()


class TestPublishedPayload:
    def test_the_source_survives_the_public_rewrite(self, registered, _fuzz_on):
        public = pl.public_node_summaries({_SYNTH_ID: state.node_analytics.get_node_summary(_SYNTH_ID)})
        assert public[_SYNTH_ID]["empirical_coverage"]["polygon_source"] == "declared"

    def test_the_declared_polygon_is_translated_rigidly(self, registered, _fuzz_on):
        """public_location keys on empirical_coverage.polygon, so a declared
        wedge is fuzzed like any other polygon — same offset on every vertex,
        shape untouched, no vertex left on the true receiver."""
        truth = state.node_analytics.get_node_summary(_SYNTH_ID)["empirical_coverage"]["polygon"]
        published = pl.public_node_summary(_SYNTH_ID, state.node_analytics.get_node_summary(_SYNTH_ID))
        moved = published["empirical_coverage"]["polygon"]

        assert len(moved) == len(truth)
        offsets = {(round(m[0] - t[0], 6), round(m[1] - t[1], 6)) for m, t in zip(moved, truth)}
        assert len(offsets) == 1, f"vertices moved by different offsets: {offsets}"
        (dlat, dlon) = offsets.pop()
        assert (dlat, dlon) != (0.0, 0.0)
        assert min(haversine_km(_RX_LAT, _RX_LON, lat, lon) for lat, lon in moved) > _ROUNDING_SLACK_KM

    def test_the_manager_keeps_the_true_wedge(self, registered, _fuzz_on):
        """The rewrite is a copy: the solver still gates on the real cone."""
        pl.public_node_summary(_SYNTH_ID, state.node_analytics.get_node_summary(_SYNTH_ID))
        apex = state.node_analytics.get_node_summary(_SYNTH_ID)["empirical_coverage"]["polygon"][0]
        assert apex == [round(_RX_LAT, 5), round(_RX_LON, 5)]
