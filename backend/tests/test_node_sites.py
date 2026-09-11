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

# One roof, two receivers, two illuminators. Invented coordinates: the real
# receive sites are private, and this repo is public.
_SITE_LAT, _SITE_LON = 34.0, -84.0


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
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("example-node-a") == "example-node-a"
        assert ns.site_identity("example-node-b") == "example-node-a"

    def test_a_third_node_joins_the_same_site(self):
        for node_id in ("example-node-a", "example-node-b", "example-node-c"):
            _connect(node_id, _SITE_LAT, _SITE_LON)
        identities = {ns.site_identity(n) for n in ("example-node-a", "example-node-b", "example-node-c")}
        assert identities == {"example-node-a"}

    def test_a_disconnected_site_mate_does_not_dissolve_the_site(self):
        """The map must not move a node because its neighbour dropped off.

        Positions are remembered rather than re-derived from who is connected:
        a site that existed only while both nodes held a socket open would
        re-fuzz the survivor on every disconnect, which is a second sample of
        the site handed out for free.
        """
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("example-node-b") == "example-node-a"

        del state.connected_nodes["example-node-a"]
        ns._expires_at = 0.0  # force the refresh a disconnect would eventually cause
        assert ns.site_identity("example-node-b") == "example-node-a"

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
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        first = pl.public_latlon(_SITE_LAT, _SITE_LON, "example-node-a")
        second = pl.public_latlon(_SITE_LAT, _SITE_LON, "example-node-b")
        assert first == second

    def test_the_shared_point_is_still_displaced(self):
        """Sharing an offset must not mean sharing the true position."""
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        lat, lon = pl.public_latlon(_SITE_LAT, _SITE_LON, "example-node-b")
        assert haversine_km(_SITE_LAT, _SITE_LON, lat, lon) >= 0.4

    def test_the_centroid_attack_gains_nothing(self):
        """Three receivers at one site, one published point.

        With independent offsets the three points sit on three annuli that
        intersect in about an eighth of the donut, and their centroid alone
        lands within a few hundred metres of the truth.  Sharing the offset
        leaves the attacker one sample: the centroid of the published points is
        the published point, which is a whole displacement away from home.
        """
        for node_id in ("example-node-a", "example-node-b", "example-node-c"):
            _connect(node_id, _SITE_LAT, _SITE_LON)
        points = [
            pl.public_latlon(_SITE_LAT, _SITE_LON, n) for n in ("example-node-a", "example-node-b", "example-node-c")
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
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        assert pl.public_point_delta(_SITE_LAT, "example-node-b") == pl.public_point_delta(_SITE_LAT, "example-node-a")
        vertices = [[_SITE_LAT, _SITE_LON], [_SITE_LAT + 0.1, _SITE_LON + 0.1]]
        assert pl.translate_polygon(vertices, "example-node-b") == pl.translate_polygon(vertices, "example-node-a")


class TestAudit:
    def test_a_shared_site_is_reported(self):
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        report = ns.colocation_report()
        assert report["shared_sites"] == {"example-node-a": ["example-node-a", "example-node-b"]}
        assert report["near_misses"] == []

    def test_a_near_miss_beyond_the_radius_is_reported_not_merged(self):
        """Two nodes just outside the merge radius, made visible.

        200 m apart could still be one site described twice, so the audit
        names them, but the module will not group them — at that distance it
        is at least as likely to be two houses.
        """
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.0018, _SITE_LON)  # ~200 m north
        report = ns.colocation_report()
        assert report["shared_sites"] == {}
        assert report["proximity_joins"] == []
        assert [entry["nodes"] for entry in report["near_misses"]] == [["roof-a", "roof-b"]]
        assert report["near_misses"][0]["km"] == pytest.approx(0.200, abs=0.005)
        assert ns.site_identity("roof-b") == "roof-b"

    def test_ordinary_neighbours_are_not_near_misses(self):
        _connect("house-a", _SITE_LAT, _SITE_LON)
        _connect("house-b", _SITE_LAT + 0.02, _SITE_LON)  # ~2.2 km north
        assert ns.colocation_report()["near_misses"] == []

    def test_the_radius_is_configurable_under_both_names(self, monkeypatch):
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)  # ~30 m north
        monkeypatch.setenv("NODE_FUZZ_SITE_KM", "0.02")  # merge to 20 m, audit to 40 m
        ns._reset_for_tests()
        report = ns.colocation_report()
        assert report["proximity_joins"] == []
        assert [entry["nodes"] for entry in report["near_misses"]] == [["roof-a", "roof-b"]]
        assert ns.site_identity("roof-b") == "roof-b"

        monkeypatch.delenv("NODE_FUZZ_SITE_KM")
        monkeypatch.setenv("NODE_FUZZ_SITE_AUDIT_KM", "0.02")  # the older name still counts
        ns._reset_for_tests()
        assert ns.colocation_report()["proximity_joins"] == []

    def test_a_zero_radius_is_the_exact_rule_alone(self, monkeypatch):
        monkeypatch.setenv("NODE_FUZZ_SITE_KM", "0")
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)
        _connect("roof-c", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("roof-b") == "roof-b"
        assert ns.site_identity("roof-c") == "roof-a"
        assert ns.colocation_report()["near_misses"] == []

    def test_the_audit_logs_once_per_change(self, caplog):
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        _connect("roof-b", _SITE_LAT + 0.00027, _SITE_LON)
        with caplog.at_level("INFO"):
            ns.log_colocation_audit()
            first = len([r for r in caplog.records if "is published from that site's position" in r.message])
            ns.log_colocation_audit()
            second = len([r for r in caplog.records if "is published from that site's position" in r.message])
        assert first == 1
        assert second == 1


class TestProximityJoin:
    """The second rule: a lone receiver within the radius of a site joins it.

    The live fleet motivated this — a fourth receiver 56 m from three at one
    address, a receiver 16 m from a pair at another — so the distances below
    are those.  Invented coordinates, as everywhere in this file.
    """

    # Degrees of latitude for a given number of metres north.
    _M = 1.0 / 111_195.0

    def test_a_receiver_56_m_from_a_site_joins_it(self):
        for node_id in ("example-node-a", "example-node-b", "example-node-c"):
            _connect(node_id, _SITE_LAT, _SITE_LON)
        _connect("example-node-d", _SITE_LAT + 56 * self._M, _SITE_LON)
        assert ns.site_identity("example-node-d") == "example-node-a"
        report = ns.colocation_report()
        assert report["shared_sites"] == {
            "example-node-a": ["example-node-a", "example-node-b", "example-node-c", "example-node-d"]
        }
        assert [(j["node"], j["anchor"]) for j in report["proximity_joins"]] == [("example-node-d", "example-node-a")]
        assert report["proximity_joins"][0]["km"] == pytest.approx(0.056, abs=0.002)
        assert report["near_misses"] == []

    def test_a_receiver_16_m_from_a_pair_joins_it_even_with_a_lower_id(self):
        """Exact-equality sites are anchored first, whatever the ids say.

        The pair is already published at one point.  A newcomer with a lower
        id must join *it*, not become its anchor — or shipping this rule would
        re-fuzz a site the exact rule had already protected.
        """
        _connect("radar-a", _SITE_LAT, _SITE_LON)
        _connect("radar-b", _SITE_LAT, _SITE_LON)
        pair_before = pl.public_latlon(_SITE_LAT, _SITE_LON, "radar-b")

        _connect("aaa-newcomer", _SITE_LAT + 16 * self._M, _SITE_LON)
        assert ns.site_identity("aaa-newcomer") == "radar-a"
        assert pl.public_latlon(_SITE_LAT, _SITE_LON, "radar-b") == pair_before

    def test_the_joined_node_is_published_at_the_site_point(self):
        """The whole point: one marker, and the receivers' true gap not on the wire.

        Sharing only the offset would publish the joiner 56 m from its
        site-mates — a second marker, and the exact baseline between the two
        receivers as the distance between them.
        """
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        own_lat = _SITE_LAT + 56 * self._M
        _connect("example-node-d", own_lat, _SITE_LON)
        assert pl.public_latlon(own_lat, _SITE_LON, "example-node-d") == pl.public_latlon(
            _SITE_LAT, _SITE_LON, "example-node-a"
        )

    def test_the_joined_node_lands_on_the_anchor_as_configured_not_as_rounded(self):
        """The anchor's published point comes from its raw configured value.

        Publishing the joiner from the 6-decimal equality key instead would,
        one time in a few hundred, straddle a 4-decimal rounding boundary the
        anchor does not and put the two markers 11 m apart.
        """
        anchor_lat, anchor_lon = _SITE_LAT + 0.00004999999, _SITE_LON + 0.00004999999
        _connect("example-node-a", anchor_lat, anchor_lon)
        _connect("example-node-b", anchor_lat, anchor_lon)
        own_lat = anchor_lat + 56 * self._M
        _connect("example-node-d", own_lat, anchor_lon)
        assert ns.site_position("example-node-d") == (anchor_lat, anchor_lon)
        assert pl.public_latlon(own_lat, anchor_lon, "example-node-d") == pl.public_latlon(
            anchor_lat, anchor_lon, "example-node-a"
        )

    def test_the_joined_nodes_artefacts_move_with_its_marker(self):
        """Arcs, trails and polygons of a joined node land around the site point.

        public_point_delta and translate_polygon carry the join's shift as
        well as the offset: a coverage polygon whose apex is the joiner's own
        receiver has to end up with its apex on the published site point, not
        56 m away from it.
        """
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        own_lat = _SITE_LAT + 56 * self._M
        _connect("example-node-d", own_lat, _SITE_LON)

        published = pl.public_latlon(own_lat, _SITE_LON, "example-node-d")
        dlat, dlon = pl.public_point_delta(own_lat, "example-node-d")
        assert (own_lat + dlat, _SITE_LON + dlon) == pytest.approx(published, abs=1e-4)

        apex = pl.translate_polygon([[own_lat, _SITE_LON], [own_lat + 0.1, _SITE_LON + 0.1]], "example-node-d")[0]
        assert tuple(apex) == pytest.approx(published, abs=1e-4)
        # The polygon is still rigid: both vertices moved by the same amount.
        moved = pl.translate_polygon([[own_lat, _SITE_LON], [own_lat + 0.1, _SITE_LON + 0.1]], "example-node-d")
        assert moved[1][0] - moved[0][0] == pytest.approx(0.1, abs=1e-9)
        assert moved[1][1] - moved[0][1] == pytest.approx(0.1, abs=1e-9)

    def test_the_declared_uncertainty_widens_for_the_whole_site(self):
        """One published point, one honest radius, for every member."""
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        assert pl.location_uncertainty_km("example-node-a") == pl.location_uncertainty_km()

        _connect("example-node-d", _SITE_LAT + 56 * self._M, _SITE_LON)
        widened = pl.location_uncertainty_km() + 0.15
        assert pl.location_uncertainty_km("example-node-d") == pytest.approx(widened)
        assert pl.location_uncertainty_km("example-node-a") == pytest.approx(widened)
        assert pl.location_uncertainty_km("example-node-b") == pytest.approx(widened)

    def test_two_lone_receivers_within_the_radius_become_one_site(self):
        _connect("roof-b", _SITE_LAT + 30 * self._M, _SITE_LON)
        _connect("roof-a", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("roof-a") == "roof-a"
        assert ns.site_identity("roof-b") == "roof-a"
        assert pl.public_latlon(_SITE_LAT + 30 * self._M, _SITE_LON, "roof-b") == pl.public_latlon(
            _SITE_LAT, _SITE_LON, "roof-a"
        )

    def test_a_chain_of_near_neighbours_does_not_become_one_site(self):
        """Greedy, not transitive: every member is within the radius of ITS anchor.

        Four receivers 100 m apart in a line span 300 m.  Single-linkage would
        make them one site and publish a house 300 m away at its neighbour's
        point; this rule makes two sites of two.
        """
        for i, node_id in enumerate(("house-a", "house-b", "house-c", "house-d")):
            _connect(node_id, _SITE_LAT + 100 * i * self._M, _SITE_LON)
        assert ns.site_identity("house-a") == "house-a"
        assert ns.site_identity("house-b") == "house-a"
        assert ns.site_identity("house-c") == "house-c"
        assert ns.site_identity("house-d") == "house-c"
        # And the pair across the seam is the audit's remaining business.
        assert ns.colocation_report()["near_misses"][0]["nodes"] == ["house-b", "house-c"]

    def test_the_join_does_not_depend_on_connection_order(self):
        def build(order):
            state.connected_nodes.clear()
            for node_id in order:
                _connect(node_id, *positions[node_id])
            return {n: ns.site_identity(n) for n in positions}

        positions = {
            "n1": (_SITE_LAT, _SITE_LON),
            "n2": (_SITE_LAT + 40 * self._M, _SITE_LON),
            "n3": (_SITE_LAT + 120 * self._M, _SITE_LON),
            "n4": (_SITE_LAT, _SITE_LON),
        }
        first = build(["n1", "n2", "n3", "n4"])
        second = build(["n3", "n4", "n2", "n1"])
        assert first == second == {"n1": "n1", "n2": "n1", "n3": "n1", "n4": "n1"}

    def test_a_lone_receiver_joins_the_nearest_site(self):
        _connect("east-a", _SITE_LAT, _SITE_LON + 0.0020)
        _connect("east-b", _SITE_LAT, _SITE_LON + 0.0020)
        _connect("west-a", _SITE_LAT, _SITE_LON - 0.0020)
        _connect("west-b", _SITE_LAT, _SITE_LON - 0.0020)
        _connect("between", _SITE_LAT, _SITE_LON + 0.0006)  # ~130 m from east, ~240 m from west
        assert ns.site_identity("between") == "east-a"

    def test_a_lone_receiver_beyond_the_radius_keys_on_itself(self):
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        _connect("far", _SITE_LAT + 160 * self._M, _SITE_LON)
        assert ns.site_identity("far") == "far"
        assert ns.site_position("far") is None
        assert ns.site_shift_deg("far") == (0.0, 0.0)
        assert pl.location_uncertainty_km("far") == pl.location_uncertainty_km()

    def test_a_disconnected_anchor_does_not_move_the_joined_node(self):
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        own_lat = _SITE_LAT + 56 * self._M
        _connect("example-node-d", own_lat, _SITE_LON)
        before = pl.public_latlon(own_lat, _SITE_LON, "example-node-d")

        del state.connected_nodes["example-node-a"]
        del state.connected_nodes["example-node-b"]
        ns._expires_at = 0.0
        assert pl.public_latlon(own_lat, _SITE_LON, "example-node-d") == before


class TestSources:
    def test_a_file_defined_node_is_a_site(self, monkeypatch, tmp_path):
        """The blah2 bridge nodes live in a runtime file, not in the database."""
        doc = tmp_path / "blah2_nodes.json"
        doc.write_text(
            '{"nodes": ['
            f'{{"node_id": "example-node-a", "rx_lat": {_SITE_LAT}, "rx_lon": {_SITE_LON}}},'
            f'{{"node_id": "example-node-b", "rx_lat": {_SITE_LAT}, "rx_lon": {_SITE_LON}}}'
            "]}",
            encoding="utf-8",
        )
        monkeypatch.setattr(ns, "_NODE_FILES", ("blah2_nodes.json",))
        monkeypatch.setattr(ns, "runtime_path", lambda name: tmp_path / name)
        ns._reset_for_tests()
        assert ns.site_identity("example-node-b") == "example-node-a"

    def test_a_failing_source_does_not_break_the_others(self, monkeypatch):
        def boom():
            raise RuntimeError("database is gone")

        monkeypatch.setattr(ns, "_positions_from_db", boom)
        _connect("example-node-a", _SITE_LAT, _SITE_LON)
        _connect("example-node-b", _SITE_LAT, _SITE_LON)
        assert ns.site_identity("example-node-b") == "example-node-a"

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
