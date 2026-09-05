"""Tests for services/node_sites.py and the shared offset it produces.

The property under test is the one in the module docstring: receivers
configured at one set of coordinates are published at one point, because two
independent offsets around one house are two samples of it.  Everything else
here exists to show that this costs nothing to the fleet that is not
co-located — a node alone at its coordinates must key exactly as it did before
this module existed, or adopting it would re-fuzz everybody.
"""

import math
import os

import pytest

os.environ.setdefault("RETINA_ENV", "test")

from core import state  # noqa: E402
from services import node_sites as ns  # noqa: E402
from services import public_location as pl  # noqa: E402
from services.geo import haversine_km  # noqa: E402

_SALT = "test-salt-for-node-sites"

# One roof, two receivers, two illuminators — the shape of radar3/radar3a.
_SITE_LAT, _SITE_LON = 33.939182, -84.65191


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", _SALT)
    monkeypatch.delenv("NODE_FUZZ_MIN_KM", raising=False)
    monkeypatch.delenv("NODE_FUZZ_MAX_KM", raising=False)
    # Neither the runtime node files nor the database take part unless a test
    # asks: both are deployment state, and a test that read the real ones would
    # pass or fail on what happens to be installed.
    monkeypatch.setattr(ns, "_NODE_FILES", ())
    monkeypatch.setattr(ns, "_positions_from_db", dict)
    state.connected_nodes.clear()
    ns._reset_for_tests()
    pl._reset_for_tests()
    yield
    state.connected_nodes.clear()
    ns._reset_for_tests()
    pl._reset_for_tests()


def _connect(node_id: str, lat: float, lon: float) -> None:
    state.connected_nodes[node_id] = {"config": {"node_id": node_id, "rx_lat": lat, "rx_lon": lon}}
    ns._reset_for_tests()
    pl._reset_for_tests()


class TestSiteIdentity:
    def test_a_lone_node_keys_on_itself(self):
        _connect("solo", 34.0, -82.0)
        assert ns.site_identity("solo") == "solo"

    def test_co_located_nodes_key_on_the_lowest_id(self):
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("radar3-retnode") == "radar3-retnode"
        assert ns.site_identity("radar3a-retnode") == "radar3-retnode"

    def test_a_third_node_joins_the_same_site(self):
        for node_id in ("radar3-retnode", "radar3a-retnode", "radar3b-retnode"):
            _connect(node_id, _SITE_LAT, _SITE_LON)
        identities = {ns.site_identity(n) for n in ("radar3-retnode", "radar3a-retnode", "radar3b-retnode")}
        assert identities == {"radar3-retnode"}

    def test_a_disconnected_site_mate_does_not_dissolve_the_site(self):
        """The map must not move a node because its neighbour dropped off.

        Positions are remembered rather than re-derived from who is connected:
        a site that existed only while both nodes held a socket open would
        re-fuzz the survivor on every disconnect, which is a second sample of
        the site handed out for free.
        """
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("radar3a-retnode") == "radar3-retnode"

        del state.connected_nodes["radar3-retnode"]
        ns._expires_at = 0.0  # force the refresh a disconnect would eventually cause
        assert ns.site_identity("radar3a-retnode") == "radar3-retnode"

    def test_a_node_with_no_known_position_keys_on_itself(self):
        assert ns.site_identity("never-seen") == "never-seen"

    def test_a_bad_position_is_not_a_site(self):
        """Two nodes with no usable coordinates must not become site-mates."""
        _connect("broken-a", None, None)
        _connect("broken-b", float("nan"), float("nan"))
        assert ns.site_identity("broken-a") == "broken-a"
        assert ns.site_identity("broken-b") == "broken-b"

    def test_positions_differing_below_the_rounding_are_one_site(self):
        _connect("aa", _SITE_LAT, _SITE_LON)
        _connect("bb", _SITE_LAT + 1e-9, _SITE_LON - 1e-9)
        assert ns.site_identity("bb") == "aa"


class TestSharedOffset:
    def test_co_located_nodes_publish_the_same_point(self):
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        first = pl.public_latlon(_SITE_LAT, _SITE_LON, "radar3-retnode")
        second = pl.public_latlon(_SITE_LAT, _SITE_LON, "radar3a-retnode")
        assert first == second

    def test_the_shared_point_is_still_displaced(self):
        """Sharing an offset must not mean sharing the true position."""
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        lat, lon = pl.public_latlon(_SITE_LAT, _SITE_LON, "radar3a-retnode")
        assert haversine_km(_SITE_LAT, _SITE_LON, lat, lon) >= 0.4

    def test_the_centroid_attack_gains_nothing(self):
        """Three receivers at one site, one published point.

        With independent offsets the three points sit on three annuli that
        intersect in about an eighth of the donut, and their centroid alone
        lands within a few hundred metres of the truth.  Sharing the offset
        leaves the attacker one sample: the centroid of the published points is
        the published point, which is a whole displacement away from home.
        """
        for node_id in ("radar3-retnode", "radar3a-retnode", "radar3b-retnode"):
            _connect(node_id, _SITE_LAT, _SITE_LON)
        points = [
            pl.public_latlon(_SITE_LAT, _SITE_LON, n) for n in ("radar3-retnode", "radar3a-retnode", "radar3b-retnode")
        ]
        centroid_lat = sum(p[0] for p in points) / len(points)
        centroid_lon = sum(p[1] for p in points) / len(points)
        assert haversine_km(_SITE_LAT, _SITE_LON, centroid_lat, centroid_lon) >= 0.4

    def test_a_lone_node_is_not_re_fuzzed_by_this_module(self, monkeypatch):
        """The regression that would make this change expensive to deploy.

        A node alone at its coordinates must hash exactly what it hashed
        before sites existed — its own id — or shipping this file would
        displace the whole fleet a second time and hand out a second sample of
        every operator's address.
        """
        _connect("solo", 34.0, -82.0)
        with_sites = pl.public_offset_km("solo")

        monkeypatch.setattr(ns, "site_identity", lambda node_id: node_id)
        pl._reset_for_tests()
        assert pl.public_offset_km("solo") == with_sites

    def test_arc_and_trail_deltas_agree_with_the_shared_anchor(self):
        """Every published artefact of a site-mate moves by the site's offset.

        public_point_delta and translate_polygon take the node id, not the
        anchor, so a site-mate whose delta was computed from its own id would
        put its arc and its trail around a point its marker is not on.
        """
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        assert pl.public_point_delta(_SITE_LAT, "radar3a-retnode") == pl.public_point_delta(_SITE_LAT, "radar3-retnode")
        vertices = [[_SITE_LAT, _SITE_LON], [_SITE_LAT + 0.1, _SITE_LON + 0.1]]
        assert pl.translate_polygon(vertices, "radar3a-retnode") == pl.translate_polygon(vertices, "radar3-retnode")


class TestAudit:
    def test_a_shared_site_is_reported(self):
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        report = ns.colocation_report()
        assert report["shared_sites"] == {"radar3-retnode": ["radar3-retnode", "radar3a-retnode"]}
        assert report["near_misses"] == []

    def test_a_near_miss_is_reported_not_merged(self):
        """The case the exact-equality rule cannot catch, made visible.

        30 m apart is one roof described twice, and these two are publishing
        two samples of it.  The module will not group them — a node's offset
        must not depend on its neighbours — so the audit has to name them.
        """
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)  # ~30 m north
        report = ns.colocation_report()
        assert report["shared_sites"] == {}
        assert [entry["nodes"] for entry in report["near_misses"]] == [["roof-a", "roof-b"]]
        assert report["near_misses"][0]["km"] == pytest.approx(0.030, abs=0.005)
        assert ns.site_identity("roof-b") == "roof-b"

    def test_ordinary_neighbours_are_not_near_misses(self):
        _connect("house-a", _SITE_LAT, _SITE_LON)
        _connect("house-b", _SITE_LAT + 0.02, _SITE_LON)  # ~2.2 km north
        assert ns.colocation_report()["near_misses"] == []

    def test_the_audit_threshold_is_configurable(self, monkeypatch):
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)
        monkeypatch.setenv("NODE_FUZZ_SITE_AUDIT_KM", "0.01")
        assert ns.colocation_report()["near_misses"] == []

    def test_the_audit_logs_once_per_change(self, caplog):
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)
        with caplog.at_level("WARNING"):
            ns.log_colocation_audit()
            first = len([r for r in caplog.records if "different coordinates" in r.message])
            ns.log_colocation_audit()
            second = len([r for r in caplog.records if "different coordinates" in r.message])
        assert first == 1
        assert second == 1


class TestSources:
    def test_a_file_defined_node_is_a_site(self, monkeypatch, tmp_path):
        """radar3/radar3a live in a runtime file, not in the database."""
        doc = tmp_path / "blah2_nodes.json"
        doc.write_text(
            '{"nodes": ['
            f'{{"node_id": "radar3-retnode", "rx_lat": {_SITE_LAT}, "rx_lon": {_SITE_LON}}},'
            f'{{"node_id": "radar3a-retnode", "rx_lat": {_SITE_LAT}, "rx_lon": {_SITE_LON}}}'
            "]}",
            encoding="utf-8",
        )
        monkeypatch.setattr(ns, "_NODE_FILES", ("blah2_nodes.json",))
        monkeypatch.setattr(ns, "runtime_path", lambda name: tmp_path / name)
        ns._reset_for_tests()
        assert ns.site_identity("radar3a-retnode") == "radar3-retnode"

    def test_a_failing_source_does_not_break_the_others(self, monkeypatch):
        def boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(ns, "_positions_from_db", boom)
        _connect("radar3-retnode", _SITE_LAT, _SITE_LON)
        _connect("radar3a-retnode", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("radar3a-retnode") == "radar3-retnode"

    def test_a_site_is_not_invented_from_a_missing_source(self, monkeypatch):
        def boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(ns, "_positions_from_db", boom)
        assert ns.site_identity("solo") == "solo"


def test_km_between_is_the_real_distance():
    """The audit's distance is geographic, not a degree difference."""
    gap = ns._km_between((_SITE_LAT, _SITE_LON), (_SITE_LAT + 0.01, _SITE_LON))
    assert gap == pytest.approx(1.11, abs=0.02)
    assert not math.isnan(gap)
