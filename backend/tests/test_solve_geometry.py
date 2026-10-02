"""Tests for services/solve_geometry.py: the delay dilution of precision.

The figure is the horizontal position error per microsecond of delay error,
from the spread of the nodes' bistatic-range gradients at the target.  Pinned
here: its closed form at two nodes, that it reads the transmitters and not
only the receivers, that it extends to three and four nodes on one roof, and
that an unknown geometry answers None rather than a number.
"""

import math

import pytest

from config.constants import C_KM_US
from services import solve_geometry
from services.solve_geometry import delay_dop_km_per_us

_KM = 1.0 / 111.195  # degrees of latitude per km
LAT0, LON0 = 40.0, -75.0


def _at(east_km: float, north_km: float) -> tuple[float, float]:
    return (LAT0 + north_km * _KM, LON0 + east_km * _KM / math.cos(math.radians(LAT0)))


def _node(rx_km, tx_km) -> dict:
    rx_lat, rx_lon = _at(*rx_km)
    tx_lat, tx_lon = _at(*tx_km)
    return {"rx_lat": rx_lat, "rx_lon": rx_lon, "tx_lat": tx_lat, "tx_lon": tx_lon}


def _bearing(r_km: float, deg: float) -> tuple[float, float]:
    return (r_km * math.sin(math.radians(deg)), r_km * math.cos(math.radians(deg)))


# The target is at the origin on the ground, so every gradient is horizontal
# and its direction is the bisector of the bearings to its two foci.
def _dop(nodes) -> float | None:
    return delay_dop_km_per_us(LAT0, LON0, 0.0, nodes)


class TestTwoNodes:
    def test_matches_the_closed_form(self):
        """sqrt(g1^2 + g2^2) / (g1 g2 sin(theta)), with the gradients built
        by hand from the bearings."""
        rx = _bearing(30.0, 200.0)
        a, b = _node(rx, _bearing(40.0, 80.0)), _node(rx, _bearing(40.0, 140.0))

        def grad(tx_deg):
            u = [-math.sin(math.radians(d)) for d in (tx_deg, 200.0)]
            v = [-math.cos(math.radians(d)) for d in (tx_deg, 200.0)]
            return (sum(u), sum(v))

        g1, g2 = grad(80.0), grad(140.0)
        m1, m2 = math.hypot(*g1), math.hypot(*g2)
        sin_theta = abs(g1[0] * g2[1] - g1[1] * g2[0]) / (m1 * m2)
        expected = math.sqrt(m1 * m1 + m2 * m2) / (m1 * m2 * sin_theta) * C_KM_US

        assert _dop([a, b]) == pytest.approx(expected, rel=1e-3)

    def test_cosited_receivers_cross_at_half_the_angle_between_their_towers(self):
        """Towers 60 degrees apart from the target, one shared receiver: the
        normals are the two bisectors, 30 degrees apart."""
        rx = _bearing(30.0, 200.0)
        a, b = _node(rx, _bearing(40.0, 80.0)), _node(rx, _bearing(40.0, 140.0))
        ga = solve_geometry._gradient(LAT0, LON0, 0.0, a)
        gb = solve_geometry._gradient(LAT0, LON0, 0.0, b)
        cross = abs(ga[0] * gb[1] - ga[1] * gb[0]) / (math.hypot(*ga) * math.hypot(*gb))

        assert math.degrees(math.asin(cross)) == pytest.approx(30.0, abs=0.1)

    def test_a_shared_tower_is_the_mirror_case(self):
        """Swap the roles of transmitter and receiver and nothing changes."""
        shared = _bearing(30.0, 200.0)
        cosited_rx = [_node(shared, _bearing(40.0, 80.0)), _node(shared, _bearing(40.0, 140.0))]
        shared_tx = [_node(_bearing(40.0, 80.0), shared), _node(_bearing(40.0, 140.0), shared)]

        assert _dop(cosited_rx) == pytest.approx(_dop(shared_tx), rel=1e-9)

    def test_it_grows_as_the_towers_close_up(self):
        rx = _bearing(30.0, 200.0)
        dops = [
            _dop([_node(rx, _bearing(40.0, 110.0 - half)), _node(rx, _bearing(40.0, 110.0 + half))])
            for half in (30.0, 10.0, 3.0, 1.0)
        ]

        assert dops == sorted(dops)
        assert dops[0] < 1.0
        assert dops[-1] > 10.0

    def test_cosited_receivers_on_one_mast_are_parallel(self):
        tx = _bearing(40.0, 90.0)
        a, b = _node(_bearing(30.0, 200.0), tx), _node(_bearing(30.03, 200.0), tx)

        assert _dop([a, b]) > 1000.0
        assert _dop([a, a]) == math.inf


class TestMoreThanTwoNodes:
    def test_three_on_one_roof_with_towers_around_the_compass(self):
        rx = _bearing(30.0, 200.0)
        nodes = [_node(rx, _bearing(40.0, d)) for d in (20.0, 140.0, 260.0)]

        assert _dop(nodes) < 0.5

    def test_three_on_one_roof_and_one_tower_farm(self):
        """Masts within a kilometre of each other at 40 km: the third node
        halves the variance and leaves the geometry where it was."""
        rx = _bearing(30.0, 200.0)
        farm = [_node(rx, _bearing(40.0, 90.0 + d)) for d in (0.0, 0.8, -0.7)]

        assert _dop(farm) > 10.0
        assert _dop(farm) < _dop(farm[:2])

    def test_four_on_one_roof_and_two_farms(self):
        """Two masts on each of two farms: as good as the farms are apart,
        and better than one node per farm by the averaging alone."""
        rx = _bearing(30.0, 200.0)
        one_each = [_node(rx, _bearing(40.0, 70.0)), _node(rx, _bearing(40.0, 130.0))]
        two_each = [_node(rx, _bearing(40.0, d)) for d in (70.0, 70.6, 130.0, 130.6)]

        assert _dop(two_each) == pytest.approx(_dop(one_each) / math.sqrt(2.0), rel=0.05)

    def test_a_node_never_makes_it_worse(self):
        rx = _bearing(30.0, 200.0)
        pair = [_node(rx, _bearing(40.0, 80.0)), _node(rx, _bearing(40.0, 81.0))]
        third = _node(_bearing(25.0, 300.0), _bearing(35.0, 10.0))

        assert _dop([*pair, third]) < _dop(pair)

    def test_order_does_not_matter(self):
        rx = _bearing(30.0, 200.0)
        nodes = [_node(rx, _bearing(40.0, d)) for d in (20.0, 95.0, 260.0)]

        assert _dop(nodes) == pytest.approx(_dop(nodes[::-1]), rel=1e-12)


class TestAltitude:
    def test_a_target_overhead_of_everything_has_no_horizontal_geometry(self):
        """Straight above a co-sited pair's shared receiver and far above its
        towers the curves flatten: the horizontal gradients shrink and the
        figure climbs."""
        rx = (0.0, 0.0)
        nodes = [_node(rx, _bearing(5.0, 80.0)), _node(rx, _bearing(5.0, 140.0))]

        assert delay_dop_km_per_us(LAT0, LON0, 12.0, nodes) > delay_dop_km_per_us(LAT0, LON0, 1.0, nodes)

    def test_reads_the_foci_altitudes_in_feet(self):
        rx = _bearing(3.0, 200.0)
        low = [_node(rx, _bearing(4.0, 80.0)), _node(rx, _bearing(4.0, 140.0))]
        high = [dict(n, tx_alt_ft=3280.84, rx_alt_ft=3280.84) for n in low]

        # Foci raised to the target's own 1 km: the same picture as all three
        # on the ground.
        assert delay_dop_km_per_us(LAT0, LON0, 1.0, high) == pytest.approx(_dop(low), rel=1e-6)


class TestUnknown:
    """None is "not known", and callers must not read it as bad geometry."""

    def test_fewer_than_two_nodes(self):
        assert _dop([]) is None
        assert _dop([_node((10.0, 0.0), (0.0, 10.0))]) is None

    def test_a_missing_config(self):
        assert _dop([_node((10.0, 0.0), (0.0, 10.0)), None]) is None

    @pytest.mark.parametrize("key", ["rx_lat", "rx_lon", "tx_lat", "tx_lon"])
    def test_a_config_that_does_not_place_a_focus(self, key):
        good = _node((10.0, 0.0), (0.0, 10.0))
        bad = dict(_node((-10.0, 5.0), (3.0, -12.0)))
        bad[key] = None

        assert _dop([good, bad]) is None

    def test_a_focus_at_the_target(self):
        good = _node((10.0, 0.0), (0.0, 10.0))
        on_top = _node((0.0, 0.0), (3.0, -12.0))

        assert _dop([good, on_top]) is None
