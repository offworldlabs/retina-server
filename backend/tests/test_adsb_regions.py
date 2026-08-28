"""Query-region derivation for external ADS-B truth (86cb5p1jy site 1).

The regression these pin is a fleet-wide reduction: any single point or extent
covering every node is correct for one metro and wrong for a spread fleet.
"""

import math

import pytest

from config.constants import (
    ADSB_CELL_SPACING_KM,
    ADSB_MAX_REGIONS_PER_CYCLE,
    ADSB_NODE_RANGE_MARGIN_KM,
)
from services.adsb_regions import (
    _DLAT_FOR_MARGIN,
    _KM_PER_DEG_LAT_NOMINAL,
    _LAT_STEP_DEG,
    _MAX_RADIUS_NM,
    Box,
    _dlon_for_margin,
    _lon_step_deg,
    _region_from_members,
    cell_of,
    is_position_absent,
    regions_for_nodes,
)
from services.geo import R_EARTH_KM, haversine_km

# Real metros, far enough apart that no two can share a 400 km cell.
ATLANTA = (33.939182, -84.388)
MASSACHUSETTS = (42.5, -71.5)
SACRAMENTO = (38.6, -121.5)


class TestCellOf:
    def test_nearby_positions_share_a_cell(self):
        # Two receivers 10 km apart cannot be split across cells at 400 km spacing.
        a = cell_of(*ATLANTA)
        b = cell_of(ATLANTA[0] + 0.09, ATLANTA[1])
        assert a == b

    def test_distant_positions_get_distinct_cells(self):
        cells = {cell_of(*p) for p in (ATLANTA, MASSACHUSETTS, SACRAMENTO)}
        assert len(cells) == 3

    def test_cell_is_stable_across_calls(self):
        assert cell_of(*ATLANTA) == cell_of(*ATLANTA)

    def test_column_width_in_km_is_stable_across_latitudes(self):
        # The point of the cosine scaling, and the regression it guards: a fixed
        # degree step would give 448 km at 25 N and 172 km at 60 N, over-sampling
        # the north and spending requests against the cap.
        widths = []
        for lat in (25.0, 40.0, 60.0):
            row, _ = cell_of(lat, 0.0)
            centre_lat = (row + 0.5) * _LAT_STEP_DEG - 90.0
            widths.append(_lon_step_deg(row) * _KM_PER_DEG_LAT_NOMINAL * math.cos(math.radians(centre_lat)))
        assert max(widths) - min(widths) < 1.0

    def test_pole_does_not_explode(self):
        # cos(centre latitude) tends to zero; without the clamp this divides by
        # zero or produces a column count in the millions.
        _, col = cell_of(89.9, 173.0)
        assert col == 0


def _region_for(members):
    row, col = cell_of(*members[0])
    return _region_from_members(row, col, members)


def _only_box(region):
    """The single box a region asks for where the antimeridian is not in play."""
    assert len(region.boxes) == 1
    return region.boxes[0]


def _assert_in_range(region):
    """Every box is a real OpenSky request: in range, and not inside out."""
    for b in region.boxes:
        assert -90.0 <= b.lamin <= b.lamax <= 90.0
        assert -180.0 <= b.lomin <= b.lomax <= 180.0


def _covers(region, lat, lon):
    """Whether the union of a region's boxes holds a point, longitude wrapped into range."""
    lon = ((lon + 180.0) % 360.0) - 180.0
    return any(b.lamin <= lat <= b.lamax and b.lomin <= lon <= b.lomax for b in region.boxes)


def _dlon_of(lat, km):
    """The longitude span holding exactly *km* of ground distance along a parallel.

    Without `_MARGIN_SURPLUS`, so a probe at this offset sits inside the
    padding rather than on its edge, where the +-180 wrap costs an ulp.
    """
    return math.degrees(2 * math.asin(math.sin(km / (2 * R_EARTH_KM)) / math.cos(math.radians(lat))))


class TestRegionGeometry:
    def test_radius_covers_every_member_plus_its_range(self):
        members = [MASSACHUSETTS, (MASSACHUSETTS[0] + 0.5, MASSACHUSETTS[1] + 0.9)]
        r = _region_for(members)
        for lat, lon in members:
            gap_km = r.radius_nm * 1.852 - haversine_km(r.lat, r.lon, lat, lon)
            assert gap_km >= ADSB_NODE_RANGE_MARGIN_KM

    def test_bbox_contains_every_member_plus_its_range(self):
        # Both axes are measured in ground distance with haversine_km, the same
        # basis the padding is derived from.  A degree bound against the lattice
        # constant would accept latitude padding that is short of the margin.
        members = [MASSACHUSETTS, (MASSACHUSETTS[0] + 0.5, MASSACHUSETTS[1] + 0.9)]
        b = _only_box(_region_for(members))
        for lat, lon in members:
            assert haversine_km(b.lamin, lon, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM
            assert haversine_km(b.lamax, lon, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM
            assert haversine_km(lat, b.lomin, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM
            assert haversine_km(lat, b.lomax, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM

    def test_bbox_covers_high_latitude_members_across_the_cell(self):
        # Members at opposite ends of one cell's latitude band, high enough
        # that cos(lat) at the poleward member is meaningfully smaller than
        # cos(centre_lat).  Sizing dlon from the bbox midpoint under-covers
        # the poleward member; sizing it from the poleward extreme does not.
        row, col = cell_of(65.0, 0.0)
        step = ADSB_CELL_SPACING_KM / _KM_PER_DEG_LAT_NOMINAL
        equatorward = ((row + 0.02) * step - 90.0, 0.0)
        poleward = ((row + 0.98) * step - 90.0, 0.5)
        b = _only_box(_region_from_members(row, col, [equatorward, poleward]))
        for lat, lon in (equatorward, poleward):
            assert haversine_km(lat, b.lomin, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM
            assert haversine_km(lat, b.lomax, lat, lon) >= ADSB_NODE_RANGE_MARGIN_KM

    def test_radius_never_breaches_the_schema_cap(self):
        # Worst case: members at opposite corners of one cell.  283 km from the
        # centre plus the 150 km margin is 234 nm, so the 250 nm cap never binds.
        row, col = cell_of(*ATLANTA)
        step = ADSB_CELL_SPACING_KM / _KM_PER_DEG_LAT_NOMINAL
        corner_a = ((row + 0.02) * step - 90.0, ATLANTA[1])
        corner_b = ((row + 0.98) * step - 90.0, ATLANTA[1] + 4.0)
        r = _region_from_members(row, col, [corner_a, corner_b])
        # Strictly under: radius_nm is already min()-clamped to the cap, so
        # `<=` would hold even where the clamp had silently breached coverage.
        assert r.radius_nm < _MAX_RADIUS_NM

    def test_tight_cluster_is_far_tighter_than_the_ceiling(self):
        # Two co-located receivers need the margin and nothing more.
        r = _region_for([ATLANTA, (ATLANTA[0] + 0.01, ATLANTA[1] + 0.01)])
        assert r.radius_nm <= 90

    def test_metro_cluster_costs_one_opensky_credit(self):
        # Credits are charged by bbox area: 1 at <=25 sq deg.  A real cluster
        # must not drift into the 2-credit band.
        r = _region_for([ATLANTA, (ATLANTA[0] + 0.01, ATLANTA[1] + 0.01)])
        assert r.sq_deg() <= 25.0
        assert r.opensky_credits() == 1

    def test_as_area_matches_the_client_contract(self):
        r = _region_for([ATLANTA])
        area = r.as_area()
        assert set(area) == {"name", "lat", "lon", "radius_nm"}
        assert area["name"] == r.name


class TestDlonNearThePole:
    """`is_usable` admits latitudes up to +/-90 inclusive, so `_dlon_for_margin`
    must survive them: the flat estimate's cosine floor and the haversine
    correction pass both degenerate at a pole, since every longitude there is
    the same point.
    """

    @pytest.mark.parametrize("lat", [85.0, 89.0, 90.0])
    def test_no_exception_and_a_finite_bounded_span(self, lat):
        dlon = _dlon_for_margin(lat, ADSB_NODE_RANGE_MARGIN_KM)
        assert math.isfinite(dlon)
        assert 0.0 < dlon <= 180.0

    @pytest.mark.parametrize("lat", [85.0, 89.0])
    def test_coverage_invariant_holds_wherever_a_span_can_reach_it(self, lat):
        # A same-latitude box edge can never sit further than a full
        # 180-degree sweep from a member -- longitude wraps, so anything
        # wider only comes back around and narrows the gap again. Below the
        # latitude where that widest possible sweep still clears the margin,
        # the invariant must hold exactly as it does near the equator.
        widest_possible_km = haversine_km(lat, 0.0, lat, 180.0)
        assert widest_possible_km >= ADSB_NODE_RANGE_MARGIN_KM  # sanity: reachable at this lat
        dlon = _dlon_for_margin(lat, ADSB_NODE_RANGE_MARGIN_KM)
        assert haversine_km(lat, -dlon, lat, 0.0) >= ADSB_NODE_RANGE_MARGIN_KM
        assert haversine_km(lat, dlon, lat, 0.0) >= ADSB_NODE_RANGE_MARGIN_KM

    def test_the_pole_gets_the_honest_full_sweep(self):
        # At lat=90 every longitude is the same physical point, so no span
        # puts true ground distance between a member and the box edge -- the
        # widest sweep is the honest answer, not an attempt at the margin.
        assert _dlon_for_margin(90.0, ADSB_NODE_RANGE_MARGIN_KM) == 180.0
        assert _dlon_for_margin(-90.0, ADSB_NODE_RANGE_MARGIN_KM) == 180.0

    def test_a_polar_region_is_a_finite_in_range_box_not_an_explosion(self):
        # A member exactly at a pole must still give a real box: every
        # longitude, and the latitude padding on one side only.
        r = _region_from_members(*cell_of(90.0, 0.0), [(90.0, 0.0)])
        b = _only_box(r)
        assert math.isfinite(b.lomin)
        assert math.isfinite(b.lomax)
        assert b.lomin == -180.0
        assert b.lomax == 180.0
        assert b.lamin == pytest.approx(90.0 - _DLAT_FOR_MARGIN)
        assert b.lamax == 90.0
        # Pinned to the exact swept area (~486 sq deg).  A loose upper bound
        # leaves room for the box to grow again without the test noticing.
        assert r.sq_deg() == pytest.approx(_DLAT_FOR_MARGIN * 360.0)


class TestBboxStaysInRange:
    """A box built from an unclamped lon +/- dlon or lat +/- dlat can leave the
    coordinate range a real OpenSky request must stay inside; every member
    must still sit ADSB_NODE_RANGE_MARGIN_KM of true ground distance inside
    the union of the boxes, except across a full-sweep box where every
    longitude qualifies by definition.
    """

    def test_near_pole_latitude_is_clamped_not_pushed_past_ninety(self):
        # 89.5 N: dlat alone (~1.35 deg) would push lamax to 90.85.
        r = _region_from_members(*cell_of(89.5, 45.0), [(89.5, 45.0)])
        _assert_in_range(r)
        assert _only_box(r).lamax == 90.0

    def test_a_clamped_latitude_alone_forces_the_sweep(self):
        # The band between the two polar conditions: above ~88.65 N the
        # latitude clamp bites, but dlon does not saturate until ~89.33 N, so
        # only the pole crossing itself can widen this box.  Ground within the
        # member's reach lies over the pole at the opposite longitude, which a
        # tight longitude band would exclude while the latitude band admits it.
        member = (88.7, 0.0)
        assert _dlon_for_margin(member[0], ADSB_NODE_RANGE_MARGIN_KM) < 180.0  # sanity: no saturation here
        r = _region_from_members(*cell_of(*member), [member])
        _assert_in_range(r)
        assert _only_box(r).lamax == 90.0

        # Due north of the member, stopping just short of the margin: the
        # great circle runs over the pole and down the far side at lon 180.
        reach_km = ADSB_NODE_RANGE_MARGIN_KM - 1.0
        colat_deg = math.degrees(reach_km / R_EARTH_KM) - (90.0 - member[0])
        beyond_pole = (90.0 - colat_deg, member[1] + 180.0)
        assert haversine_km(*member, *beyond_pole) == pytest.approx(reach_km)  # sanity: inside the reach
        assert _covers(r, *beyond_pole)

    def test_saturated_dlon_gives_the_full_sweep_not_an_out_of_range_edge(self):
        # 89.5 N, lon 45: dlon saturates at 180, so the naive lomax is 225.
        # This close to the pole a same-latitude edge can sit within the
        # margin in ground distance no matter its longitude, which is exactly
        # why the full sweep -- every longitude covered by definition -- is
        # the box, rather than a still-real distance check on one edge.
        r = _region_from_members(*cell_of(89.5, 45.0), [(89.5, 45.0)])
        _assert_in_range(r)
        b = _only_box(r)
        assert (b.lomin, b.lomax) == (-180.0, 180.0)

    def test_a_pole_crossing_band_is_swept_rather_than_split(self):
        # 89 N, lon 120: dlon is ~85 deg (not saturated) and the padded band
        # lands at lomax=205, which across the antimeridian at an ordinary
        # latitude would be two boxes.  Here the padding crosses the pole, so
        # every longitude is genuinely reachable and no split covers it.
        r = _region_from_members(*cell_of(89.0, 120.0), [(89.0, 120.0)])
        assert _dlon_for_margin(89.0, ADSB_NODE_RANGE_MARGIN_KM) < 180.0  # sanity: this case does not saturate
        _assert_in_range(r)
        b = _only_box(r)
        assert (b.lomin, b.lomax) == (-180.0, 180.0)

    def test_normal_mid_latitude_region_is_unchanged(self):
        # Nowhere near a pole or the antimeridian: the clamp must be a no-op,
        # and the box must equal the un-clamped min/max +/- margin exactly.
        members = [MASSACHUSETTS, (MASSACHUSETTS[0] + 0.5, MASSACHUSETTS[1] + 0.9)]
        r = _region_for(members)
        lats = [lat for lat, _ in members]
        lons = [lon for _, lon in members]
        dlat = _DLAT_FOR_MARGIN
        extreme_lat = max((min(lats), max(lats)), key=abs)
        dlon = _dlon_for_margin(extreme_lat, ADSB_NODE_RANGE_MARGIN_KM)
        b = _only_box(r)
        assert b.lamin == pytest.approx(min(lats) - dlat)
        assert b.lamax == pytest.approx(max(lats) + dlat)
        assert b.lomin == pytest.approx(min(lons) - dlon)
        assert b.lomax == pytest.approx(max(lons) + dlon)
        _assert_in_range(r)


class TestAntimeridianSplit:
    """A padded band straddling +/-180 is asked for as one box each side.

    The whole parallel covers the same members, but a 360-degree box is always
    over 400 sq deg, so it costs the top credit band -- four of the five
    1-credit regions the free tier affords at this cadence -- and drags in
    every aircraft on the parallel, which is the irrelevant-candidate cost the
    per-cluster geometry exists to avoid.  Reachable from Fiji or eastern NZ.
    """

    FIJI = (10.0, 179.9)

    def test_a_band_across_the_antimeridian_is_split_not_swept(self):
        r = _region_for([self.FIJI])
        _assert_in_range(r)
        lower, upper = r.boxes
        assert lower.lomin == -180.0
        assert upper.lomax == 180.0
        # The seam itself is covered from both sides, and the far side of the
        # world is not queried at all.
        assert lower.lomax < upper.lomin
        assert _covers(r, *self.FIJI)
        assert _covers(r, self.FIJI[0], 180.0)
        assert _covers(r, self.FIJI[0], -180.0)
        assert not _covers(r, self.FIJI[0], 0.0)
        assert not _covers(r, self.FIJI[0], 100.0)

    def test_the_union_covers_every_member_plus_its_range(self):
        # Two members one side of the seam -- the lattice puts either side of
        # +/-180 in its own column, so a cell never holds both.
        members = [(10.0, 179.5), (10.2, 179.9)]
        assert cell_of(*members[0]) == cell_of(*members[1])  # sanity: one region
        r = _region_for(members)
        assert len(r.boxes) == 2
        for lat, lon in members:
            reach = _dlon_of(lat, ADSB_NODE_RANGE_MARGIN_KM)
            for probe in (lon - reach, lon + reach):
                assert haversine_km(lat, lon, lat, probe) == pytest.approx(ADSB_NODE_RANGE_MARGIN_KM)
                assert _covers(r, lat, probe)

    def test_the_split_costs_less_than_the_parallel_it_replaces(self):
        r = _region_for([self.FIJI])
        assert [b.opensky_credits() for b in r.boxes] == [1, 1]
        assert r.opensky_credits() == 2
        swept = Box(r.boxes[0].lamin, r.boxes[0].lamax, -180.0, 180.0)
        assert swept.opensky_credits() == 4

    def test_the_negative_side_splits_the_same_way(self):
        member = (10.0, -179.9)
        r = _region_for([member])
        _assert_in_range(r)
        assert len(r.boxes) == 2
        assert _covers(r, *member)
        assert r.opensky_credits() < 4

    def test_a_split_costing_the_same_as_the_sweep_is_still_taken(self):
        # Credits come in four coarse bands, so a tie is ordinary: at 87 S the
        # padded band splits into two boxes of 2 credits each against the
        # sweep's 4.  Nothing is saved by sweeping, and it would ask for the
        # whole parallel -- more bytes, and a larger candidate pool for the
        # verification matcher to scan every cycle.
        member = (-87.0, 179.9)
        r = _region_for([member])
        _assert_in_range(r)
        assert len(r.boxes) == 2
        swept = Box(r.boxes[0].lamin, r.boxes[0].lamax, -180.0, 180.0)
        assert r.opensky_credits() == swept.opensky_credits()  # sanity: this position does tie
        assert r.sq_deg() < swept.sq_deg()
        assert _covers(r, *member)

    def test_a_split_dearer_than_the_sweep_is_not_taken(self):
        # A lattice column at 86 N is ~96 degrees of longitude wide, so two
        # members 495 km apart can share one.  The padded band then splits into
        # a 90- and a 20-degree box, 3 + 2 credits against the sweep's 4, and
        # the sweep covers everything the split would.
        members = [(86.1, 110.2), (86.1, 179.9)]
        assert cell_of(*members[0]) == cell_of(*members[1])  # sanity: one region
        r = _region_from_members(*cell_of(*members[0]), members)
        b = _only_box(r)
        assert (b.lomin, b.lomax) == (-180.0, 180.0)
        assert r.opensky_credits() == 4
        for lat, lon in members:
            assert _covers(r, lat, lon)


class TestRegionsForNodes:
    def test_three_metros_give_three_regions(self):
        regions = regions_for_nodes([ATLANTA, MASSACHUSETTS, SACRAMENTO])
        assert len(regions) == 3

    def test_a_distant_node_does_not_displace_the_others(self):
        # The regression that recurs.  Under a fleet-wide bounding box, adding
        # the Greenwich placeholder moved the query into the North Atlantic and
        # every existing node lost coverage.
        before = regions_for_nodes([ATLANTA, MASSACHUSETTS, SACRAMENTO])
        after = regions_for_nodes([ATLANTA, MASSACHUSETTS, SACRAMENTO, (51.42, 0.0)])
        assert set(before) <= set(after)
        assert len(after) == len(before) + 1

    def test_request_count_does_not_grow_with_node_count(self):
        # The point of the exercise.  Twenty receivers in one metro are one query.
        dense = [(ATLANTA[0] + i * 0.01, ATLANTA[1] + i * 0.01) for i in range(20)]
        assert len(regions_for_nodes(dense)) == 1

    def test_unpositioned_nodes_are_dropped(self):
        regions = regions_for_nodes([ATLANTA, (0.0, 0.0)])
        assert len(regions) == 1


    def test_only_the_exact_origin_pair_reads_as_absent(self):
        # (0, 0) is the sentinel for "no position configured", not a real
        # location; the equator and the prime meridian are each fine on
        # their own, and the prime meridian is where the live placeholder
        # node actually sits.  An `or` in place of the `and` here would
        # silently drop every node on either line.
        regions = regions_for_nodes([(0.0, 45.0), (45.0, 0.0), (0.0, 0.0)])
        assert len(regions) == 2

    def test_ordering_is_deterministic(self):
        positions = [ATLANTA, MASSACHUSETTS, SACRAMENTO, (MASSACHUSETTS[0] + 0.02, MASSACHUSETTS[1])]
        first = [r.name for r in regions_for_nodes(positions)]
        assert first == [r.name for r in regions_for_nodes(list(reversed(positions)))]
        # Busiest cell first, so the cap sheds the least-populated regions.
        assert regions_for_nodes(positions)[0].n_nodes == 2

    def test_cap_is_enforced_and_logged(self, caplog):
        # Spread far enough that every node claims its own cell.
        spread = [(20.0 + i * 5.0, -100.0) for i in range(ADSB_MAX_REGIONS_PER_CYCLE + 3)]
        with caplog.at_level("WARNING"):
            regions = regions_for_nodes(spread)
        assert len(regions) == ADSB_MAX_REGIONS_PER_CYCLE
        assert "region cap" in caplog.text

        # The spec requires naming what was dropped, not just that the cap fired.
        all_regions = sorted(
            (_region_from_members(*cell_of(*p), [p]) for p in spread),
            key=lambda r: (-r.n_nodes, r.row, r.col),
        )
        for dropped in all_regions[ADSB_MAX_REGIONS_PER_CYCLE:]:
            assert f"{dropped.name} ({dropped.n_nodes} nodes)" in caplog.text

    def test_cap_boundary_is_not_truncated(self, caplog):
        # Exactly at the cap: every region survives and the cap does not fire.
        spread = [(20.0 + i * 5.0, -100.0) for i in range(ADSB_MAX_REGIONS_PER_CYCLE)]
        with caplog.at_level("WARNING"):
            regions = regions_for_nodes(spread)
        assert len(regions) == ADSB_MAX_REGIONS_PER_CYCLE
        assert "region cap" not in caplog.text

    def test_no_nodes_gives_no_regions(self):
        assert regions_for_nodes([]) == []


class TestMalformedPositions:
    """Positions arrive unvalidated: `/api/radar/detections/bulk` takes a
    free-form config dict, and json.loads accepts the `NaN` literal.  Raising
    here aborts the fetch for every region, so one node's rubbish would cost
    the whole fleet its external truth — the failure this work exists to end.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            (float("nan"), -84.4),
            (33.9, float("nan")),
            (float("inf"), -84.4),
            (33.9, float("-inf")),
            ("33.9", -84.4),
            (33.9, "-84.4"),
            (None, -84.4),
            (999.0, 999.0),
            (-90.5, 0.5),
            (0.5, -180.5),
        ],
        ids=[
            "nan-lat",
            "nan-lon",
            "inf-lat",
            "neg-inf-lon",
            "string-lat",
            "string-lon",
            "none-lat",
            "both-out-of-range",
            "lat-below-minus-90",
            "lon-below-minus-180",
        ],
    )
    def test_an_unusable_position_is_dropped_without_disturbing_the_rest(self, bad):
        regions = regions_for_nodes([ATLANTA, bad])
        assert [r.name for r in regions] == [_region_for([ATLANTA]).name]

    def test_the_extremes_of_the_valid_range_are_kept(self):
        # The bounds are inclusive: a pole and the antimeridian are positions,
        # not rubbish, and _lon_step_deg already survives both.
        regions = regions_for_nodes([(90.0, 180.0), (-90.0, -180.0)])
        assert len(regions) == 2

    def test_dropped_positions_are_logged_with_a_count(self, caplog):
        with caplog.at_level("WARNING"):
            regions_for_nodes([ATLANTA, (float("nan"), 1.0), (999.0, 999.0)])
        assert "dropped 2 unusable node position(s)" in caplog.text

    def test_a_fleet_of_rubbish_yields_no_regions_rather_than_an_exception(self):
        assert regions_for_nodes([(float("nan"), float("nan")), ("x", "y")]) == []

    def test_a_boolean_is_not_a_latitude(self):
        # True is an int in Python, so an unguarded numeric check would place
        # this node at 1 degree north rather than dropping it.
        assert regions_for_nodes([(True, -84.4)]) == []

    def test_boolean_false_pair_is_reported_as_unusable_not_absent(self, caplog):
        # False == 0.0, so a naive absence check reads {rx_lat: false, rx_lon:
        # false} as "never configured a position" and drops it silently.
        # That is a different fault than what it actually is -- malformed
        # config -- and is_usable must be the one to see and log it.
        with caplog.at_level("WARNING"):
            regions = regions_for_nodes([ATLANTA, (False, False)])
        assert [r.name for r in regions] == [_region_for([ATLANTA]).name]
        assert "dropped 1 unusable node position(s)" in caplog.text

    def test_is_position_absent_does_not_treat_a_bool_as_the_sentinel(self):
        assert is_position_absent(False, False) is False
        assert is_position_absent(0.0, 0.0) is True
