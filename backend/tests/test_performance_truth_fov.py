"""The P85 geometries per aircraft tier and altitude band, on invented bucket rows.

Rows are built straight into Arrow tables as (hex, class, alt_ft, ground_km, bearing_deg,
validated, shift_s), opportunity and geometric true unless a test says otherwise, so the
tests check the binning, the pooling, the background gate and the file plumbing, not the
scorer that writes the buckets.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from services import performance_truth_fov as fov

Row = tuple[str, str, float, float, float, bool, int]
ROW_COLUMNS = ("hex", "class", "alt_ft", "ground_km", "bearing_deg", "validated", "shift_s")


def row(
    hex_: str, cls: str, alt_ft: float, ground_km: float, bearing_deg: float, validated: bool, shift_s: int = 0
) -> Row:
    return (hex_, cls, float(alt_ft), float(ground_km), float(bearing_deg), bool(validated), int(shift_s))


def sector(
    hex_: str, cls: str, alt_ft: float, bearing_deg: float, ranges, detect_to: float, shift_s: int = 0
) -> list[Row]:
    """One opportunity per range along a bearing, validated out to ``detect_to`` km."""
    return [row(hex_, cls, alt_ft, r, bearing_deg, r <= detect_to, shift_s) for r in ranges]


def table(rows: list[Row], *, opportunity=True, geometric=True, extra: dict | None = None) -> pa.Table:
    """A bucket table of the rows given; ``extra`` adds the columns a real table carries too."""
    columns = list(zip(*rows, strict=True)) if rows else [[] for _ in ROW_COLUMNS]
    data = {n: pa.array(list(c), fov._COLUMNS_TYPES[n]) for n, c in zip(ROW_COLUMNS, columns, strict=True)}
    for name, flags in (("opportunity", opportunity), ("geometric", geometric)):
        data[name] = pa.array([flags] * len(rows) if isinstance(flags, bool) else list(flags), pa.bool_())
    for name, values in (extra or {}).items():
        data[name] = pa.array(values)
    return pa.table(data)


def stats(ground, bearing, detected, hexes) -> list[dict]:
    return fov.bin_stats(
        np.asarray(ground, float), np.asarray(bearing, float), np.asarray(detected, bool), np.array(hexes)
    )


# ── Classification ───────────────────────────────────────────────────────────


def test_tier_of_maps_every_class_and_leaves_the_rest_blank():
    classes = [c for tier_classes in fov.TIERS.values() for c in tier_classes]
    expected = [tier for tier, tier_classes in fov.TIERS.items() for _ in tier_classes]
    assert fov.tier_of(classes).tolist() == expected
    assert fov.tier_of(["unknown", None, "zeppelin"]).tolist() == ["", "", ""]


def test_band_of_uses_the_grid_altitude_edges():
    assert fov.ALT_BANDS_KFT == (0, 5, 10, 20, 45)
    alts = np.array([0, 4999, 5000, 19999, 20000, 44999, 45000, -1, np.nan])
    assert fov.band_of(alts).tolist() == [0, 0, 1, 2, 3, 3, -1, -1, -1]
    assert fov.band_labels() == ["all", "0-5 kft", "5-10 kft", "10-20 kft", "20-45 kft"]


def test_bearing_bin_wraps():
    assert fov.bearing_bin(np.array([0, 9.99, 10, 359.9, 360, -10])).tolist() == [0, 0, 1, 35, 0, 35]


# ── Bin statistics ───────────────────────────────────────────────────────────

TWENTY = np.arange(10.0, 50.0, 2.0)  # 10, 12, ..., 48 km


def test_bin_with_enough_detections_from_two_aircraft_is_drawn_from_its_own_rows():
    rows = stats(TWENTY, [45.0] * 20, [True] * 20, ["a", "b"] * 10)
    assert len(rows) == fov.FOV_BINS and [r["bearing_deg"] for r in rows] == [i * 10.0 for i in range(36)]
    b = rows[4]
    assert (b["n_opp"], b["n_det"], b["n_aircraft"], b["pd"]) == (20, 20, 2, 1.0)
    assert b["p85_det_km"] == np.percentile(TWENTY, 85)
    assert b["p95_det_km"] >= b["p85_det_km"]
    assert b["pooled"] is False
    assert b["p85_opp_km"] == b["p85_det_km"] and b["traffic_limited"] is True
    # An empty neighbour stays closed: pooling needs a detection of its own.
    assert rows[3]["n_opp"] == 0 and rows[3]["pd"] is None
    assert rows[3]["pooled"] is False and rows[3]["p85_det_km"] is None
    # A neighbour with one detection of its own is drawn from the pooled set.
    helped = stats([*TWENTY, 30.0], [45.0] * 20 + [35.0], [True] * 21, ["a", "b"] * 10 + ["b"])
    assert helped[3]["n_det"] == 1 and helped[3]["pooled"] is True
    assert helped[3]["p85_det_km"] == np.percentile([*TWENTY, 30.0], 85)


def test_one_aircraft_does_not_open_a_bin_until_a_neighbour_adds_a_second():
    alone = stats(TWENTY, [45.0] * 20, [True] * 20, ["a"] * 20)[4]
    assert alone["n_det"] == 20 and alone["n_aircraft"] == 1
    assert alone["p85_det_km"] is None and alone["p95_det_km"] is None
    assert alone["pooled"] is False and alone["traffic_limited"] is False
    helped = stats([*TWENTY, 30.0], [45.0] * 20 + [55.0], [True] * 21, ["a"] * 20 + ["b"])[4]
    assert helped["pooled"] is True and helped["n_aircraft"] == 1  # own-bin count, pooled geometry
    assert helped["p85_det_km"] == np.percentile([*TWENTY, 30.0], 85)


def test_short_bin_pools_its_neighbours_and_reads_pd_from_its_own_rows():
    own = [(10, True), (20, True), (30, True), (40, True), (50, False), (60, False), (70, False), (80, False)]
    left = [(12, True), (22, True), (32, True), (42, True), (100, False), (110, False)]
    right = [(14, True), (24, True), (34, True), (44, True)]
    ground = [g for g, _ in own + left + right]
    detected = [d for _, d in own + left + right]
    bearing = [105.0] * len(own) + [95.0] * len(left) + [115.0] * len(right)
    hexes = ["a"] * len(own) + ["b"] * len(left) + ["c"] * len(right)
    b = stats(ground, bearing, detected, hexes)[10]
    assert (b["n_opp"], b["n_det"], b["n_aircraft"], b["pd"]) == (8, 4, 1, 0.5)
    assert b["pooled"] is True
    assert b["p85_det_km"] == np.percentile([g for g, d in own + left + right if d], 85)
    assert b["p85_opp_km"] == np.percentile(ground, 85)  # every pooled opportunity, detected or not
    # The neighbours each pool one empty bin and stay short; alone, four detections stay closed.
    rows = stats(ground, bearing, detected, hexes)
    assert rows[9]["p85_det_km"] is None and rows[11]["p85_det_km"] is None
    alone = stats(ground[:8], bearing[:8], detected[:8], hexes[:8])[10]
    assert alone["p85_det_km"] is None and alone["pooled"] is False and alone["pd"] == 0.5
    assert alone["p85_opp_km"] == np.percentile(ground[:8], 85)


def test_traffic_limited_only_when_detections_reach_the_traffic():
    ranges = np.arange(1.0, 61.0)  # traffic out to 60 km
    short = stats(ranges, [185.0] * 60, ranges <= 20.0, ["a", "b"] * 30)[18]
    assert short["n_det"] == 20 and short["p85_opp_km"] == np.percentile(ranges, 85)
    assert short["p85_det_km"] == np.percentile(ranges[ranges <= 20.0], 85)
    assert short["traffic_limited"] is False
    far = stats(ranges, [185.0] * 60, ranges <= 56.0, ["a", "b"] * 30)[18]
    assert far["p85_det_km"] >= fov.TRAFFIC_LIMITED_RATIO * far["p85_opp_km"]
    assert far["traffic_limited"] is True


def test_last_bin_pools_the_first():
    ground = [10, 20, 30, 40] * 3
    bearing = [355.0] * 4 + [345.0] * 4 + [5.0] * 4
    rows = stats(ground, bearing, [True] * 12, ["a"] * 4 + ["b"] * 4 + ["c"] * 4)
    assert rows[35]["pooled"] is True and rows[35]["p85_det_km"] == np.percentile(ground, 85)
    assert rows[0]["p85_det_km"] is None and rows[34]["p85_det_km"] is None


def test_beats_background_needs_the_factor_and_the_margin():
    assert fov.beats_background(None, 0.0) is False
    assert fov.beats_background(0.3, None) is True
    assert fov.beats_background(0.30, 0.05) is True
    assert fov.beats_background(0.009, 0.008) is False
    assert fov.beats_background(0.05, 0.02) is False  # factor 2.5, under BACKGROUND_FACTOR
    assert fov.beats_background(0.035, 0.01) is True  # factor 3.5, margin 0.025
    assert fov.beats_background(0.03, 0.012) is False  # factor 2.5
    assert fov.beats_background(0.015, 0.004) is False  # factor 3.75 but margin 0.011
    assert fov.beats_background(0.07, 0.02) is True


# ── Strata ───────────────────────────────────────────────────────────────────


def real_rows() -> list[Row]:
    """Sixty opportunities in bin 0 from three aircraft, every other one validated."""
    return [
        row(hex_, "narrowbody", 8000, 10 + 2 * j + 0.1 * k, 5.0, j % 2 == 0)
        for k, hex_ in enumerate(("a1", "b2", "c3"))
        for j in range(20)
    ]


def control_rows(validated_every: int) -> list[Row]:
    """A hundred shifted-trajectory opportunities, one in ``validated_every`` validated."""
    return [
        row("c3", "narrowbody", 8000, 15 + j, 5.0, j % validated_every == 0, shift)
        for shift in (600, 1800)
        for j in range(50)
    ]


def test_stratum_at_chance_level_keeps_its_counts_but_no_geometry():
    m = fov.stratum_map(table(real_rows() + control_rows(2)), "airliner", "5-10 kft")
    assert (m["tier"], m["alt_band"], m["drawn"]) == ("airliner", "5-10 kft", False)
    assert (m["open_bins"], m["pooled_bins"], m["traffic_limited_bins"]) == (0, 0, 0)
    assert m["limits_km"] == [None] * fov.FOV_BINS
    assert (m["n_opp"], m["n_det"], m["n_aircraft"], m["pd"]) == (60, 30, 3, 0.5)
    assert (m["background"], m["background_n"], m["net_pd"]) == (0.5, 100, 0.0)
    b = m["bins"][0]
    assert (b["n_opp"], b["n_det"], b["pd"], b["p85_det_km"], b["pooled"]) == (60, 30, 0.5, None, False)


def test_stratum_beating_its_background_is_drawn():
    m = fov.stratum_map(table(real_rows() + control_rows(50)), "airliner", "5-10 kft")
    assert m["drawn"] is True and (m["n_opp"], m["n_det"], m["pd"]) == (60, 30, 0.5)
    assert (m["background"], m["background_n"]) == (0.02, 100) and abs(m["net_pd"] - 0.48) < 1e-12
    assert (m["open_bins"], m["pooled_bins"]) == (1, 0)  # the bin itself; its empty neighbours stay closed
    p85 = np.percentile([r[3] for r in real_rows() if r[5]], 85)
    assert m["limits_km"][0] == p85 and m["limits_km"][1] is None and m["limits_km"][35] is None
    assert m["bins"][0]["pooled"] is False and m["bins"][1]["pooled"] is False


def test_stratum_maps_cover_every_tier_and_band_with_all_first():
    rows = (
        [row("a1", "narrowbody", 8000, 20 + j, 5.0, True) for j in range(6)]
        + [row("h1", "widebody", 30000, 40 + j, 95.0, j % 2 == 0) for j in range(4)]
        + [row("u1", "unknown", 3000, 10 + j, 185.0, False) for j in range(3)]
        + [row("g1", "light_GA", 50000, 30 + j, 275.0, True) for j in range(2)]
        + [row("x9", "narrowbody", 8000, 25, 5.0, True, 600)]
    )
    maps = fov.stratum_maps(table(rows))
    labels = fov.band_labels()
    assert len(maps) == len(fov.TIER_ORDER) * len(labels) == 25
    assert [(m["tier"], m["alt_band"]) for m in maps[:2]] == [("all", "all"), ("all", "0-5 kft")]
    by = {(m["tier"], m["alt_band"]): m for m in maps}
    assert (by["all", "all"]["n_opp"], by["all", "all"]["n_det"], by["all", "all"]["background_n"]) == (15, 10, 1)
    assert by["airliner", "5-10 kft"]["n_opp"] == 6 and by["airliner", "5-10 kft"]["background_n"] == 1
    assert by["heavy", "20-45 kft"]["n_opp"] == 4 and by["heavy", "all"]["n_det"] == 2
    assert by["all", "0-5 kft"]["n_opp"] == 3  # the unknown class counts in the "all" tier only
    assert sum(by[t, "0-5 kft"]["n_opp"] for t in fov.TIERS) == 0
    assert by["light", "all"]["n_opp"] == 2  # 50 000 ft is in the "all" band only
    assert sum(by["light", b]["n_opp"] for b in labels[1:]) == 0
    for band in labels:
        assert sum(by[t, band]["n_opp"] for t in fov.TIERS) <= by["all", band]["n_opp"]


# ── Files and the node document ──────────────────────────────────────────────


def test_load_opportunities_filters_gates_keeps_controls_and_skips_bad_files(tmp_path: Path, caplog):
    rows = [
        row("a1", "narrowbody", 8000, 10, 5, True),
        row("a1", "narrowbody", 8000, 11, 5, False),
        row("a1", "narrowbody", 8000, 12, 5, True),
        row("a1", "narrowbody", 8000, 13, 5, True, 600),
    ]
    extra = {"node_id": ["radar-x"] * 4, "bucket_ms": [1, 2, 3, 4], "rr_km": [1.5, 2.5, 3.5, 4.5]}
    a = table(rows, opportunity=[True, True, False, True], geometric=[True, False, True, True], extra=extra)
    pq.write_table(a, tmp_path / "a.parquet")
    pq.write_table(table([row("b2", "widebody", 30000, 40, 95, True)]), tmp_path / "b.parquet")
    (tmp_path / "c.parquet").write_text("not a parquet file\n")
    with caplog.at_level(logging.WARNING, logger=fov.logger.name):
        t = fov.load_opportunities([tmp_path / "a.parquet", tmp_path / "c.parquet", tmp_path / "b.parquet"])
    assert t.column_names == list(fov._COLUMNS)
    assert t["hex"].to_pylist() == ["a1", "a1", "b2"]
    assert t["ground_km"].to_pylist() == [10.0, 13.0, 40.0] and t["shift_s"].to_pylist() == [0, 600, 0]
    assert "c.parquet" in caplog.text and "unreadable" in caplog.text
    assert fov.load_opportunities([tmp_path / "c.parquet"]).num_rows == 0
    empty = fov.load_opportunities([])
    assert empty.num_rows == 0 and empty.column_names == list(fov._COLUMNS)
    assert empty.schema.types == [fov._COLUMNS_TYPES[c] for c in fov._COLUMNS]


AIRLINER_RANGES = np.linspace(5.0, 50.0, 10)  # 5, 10, ..., 50 km


def airliner_rows() -> list[Row]:
    """Three narrowbodies at 8 000 ft in bins 0-5, detected out to 40 km."""
    return [
        r
        for bin_ in range(6)
        for hex_ in ("a1", "a2", "a3")
        for r in sector(hex_, "narrowbody", 8000, bin_ * 10 + 5.0, AIRLINER_RANGES, detect_to=40.0)
    ]


def test_node_document_bins_table_overview_and_polygon(tmp_path: Path):
    airliner_controls = [
        row("a1", "narrowbody", 8000, 10 + j % 40, 5.0, j < 2, 600 if j % 2 else 1800) for j in range(200)
    ]
    pq.write_table(table(airliner_rows() + airliner_controls), tmp_path / "w1.parquet")
    light = [
        row(f"g{j % 2}", "light_GA", 2000, 10 + j, 185.0, j % 10 == 0, shift) for shift in (0, 600) for j in range(50)
    ]
    gated = [row("z1", "light_GA", 2000, 10, 185.0, True), row("z2", "light_GA", 2000, 10, 185.0, True)]
    second = table(light + gated, opportunity=[True] * 100 + [False, True], geometric=[True] * 101 + [False])
    pq.write_table(second, tmp_path / "w2.parquet")

    windows = ["2026-03-10T12", "2026-03-10T15"]
    doc = fov.node_document(
        "radar-x", [tmp_path / "w1.parquet", tmp_path / "w2.parquet"], windows, includes_provisional=True, now_ms=123
    )
    assert (doc["schema"], doc["node_id"], doc["generated_ms"], doc["windows"]) == (1, "radar-x", 123, windows)
    assert doc["includes_provisional"] is True and doc["alt_bands_kft"] == [0, 5, 10, 20, 45]
    assert doc["tiers"]["airliner"] == ["narrowbody", "regional_jet"]
    assert (doc["n_opp"], doc["n_det"]) == (230, 149)  # real rows only, the gated two excluded
    assert len(doc["maps"]) == 25

    by = {(m["tier"], m["alt_band"]): m for m in doc["maps"]}
    m = by["airliner", "5-10 kft"]
    assert m["drawn"] is True and (m["n_opp"], m["n_det"], m["n_aircraft"]) == (180, 144, 3)
    assert (m["pd"], m["background"], m["background_n"]) == (0.8, 0.01, 200)
    p85 = np.percentile(np.tile(AIRLINER_RANGES[AIRLINER_RANGES <= 40.0], 3), 85)
    assert [i for i, v in enumerate(m["limits_km"]) if v is not None] == [0, 1, 2, 3, 4, 5]
    assert all(abs(v - p85) < 1e-9 for v in m["limits_km"] if v is not None)
    assert (m["open_bins"], m["pooled_bins"], m["traffic_limited_bins"]) == (6, 0, 0)
    assert [m["bins"][i]["pooled"] for i in (0, 5, 6, 35)] == [False, False, False, False]
    assert by["light", "0-5 kft"]["drawn"] is False and by["light", "0-5 kft"]["n_det"] == 5

    bt = fov.bins_table("radar-x", doc["maps"])
    assert bt.num_rows == 36 * 25 and set(bt["node_id"].to_pylist()) == {"radar-x"}
    assert bt.column_names == [
        "node_id", "tier", "alt_band", "bearing_deg", "n_opp", "n_det", "n_aircraft", "pd",
        "p85_det_km", "p95_det_km", "p85_opp_km", "pooled", "traffic_limited",
    ]  # fmt: skip
    airliner = [r for r in bt.to_pylist() if (r["tier"], r["alt_band"]) == ("airliner", "5-10 kft")]
    assert len(airliner) == 36 and sum(r["p85_det_km"] is not None for r in airliner) == 6
    assert airliner[0]["bearing_deg"] == 0.0 and airliner[0]["n_det"] == 24 and airliner[0]["pd"] == 0.8

    poly = fov.polygon_km(m["limits_km"])
    assert [b for b, _ in poly] == [(i + 0.5) * fov.BIN_DEG for i in range(36)]
    assert abs(poly[0][1] - p85) < 1e-9 and poly[20] == (205.0, 0.0)
    assert sum(1 for _, r in poly if r > 0) == 6

    view = fov.overview(doc)
    assert (view["n_opp"], view["n_det"], view["windows"]) == (230, 149, 2)
    assert (view["maps_drawn"], view["maps_chance_level"]) == (4, 3)
    assert set(view["maps"]) == {"all|all", "all|5-10 kft", "airliner|all", "airliner|5-10 kft"}
    assert view["maps"]["airliner|5-10 kft"] == {"n_det": 144, "pd": 0.8, "open_bins": 6}

    bare = fov.node_document("radar-x", [], ["w"], includes_provisional=False, now_ms=1)
    assert (bare["maps"], bare["n_opp"], bare["n_det"]) == ([], 0, 0)
    assert fov.overview(bare)["maps_drawn"] == 0 and fov.overview(bare)["maps_chance_level"] == 0
