"""Day count tables, from archive files written by the repo's own writers.

Ids and positions are invented: one receiver and one transmitter 22.1 km apart.
The delay, range and bearing noted beside each position, and the K keys below,
were worked out apart from the module. The roots are tmp_paths.
"""

import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from services import adsb_writer, compaction, parquet_writer
from services import performance_data as pdata

NODE = "radar-alpha"
DAY = date(2026, 3, 10)
FLUSHED = datetime(2026, 3, 10, 18, 0, 0, tzinfo=UTC)
CONFIG = {
    "created_at": "2026-01-01 00:00:00.000000",
    "rx_lat": 10.0,
    "rx_lon": 20.0,
    "rx_alt_ft": 300.0,
    "tx_lat": 10.2,
    "tx_lon": 20.0,
    "tx_alt_ft": 1000.0,
    "fc_hz": 200e6,
    "fs_hz": 2e6,
    "cpi_s": 0.5,
    "max_range_km": 59.96,
    "beam_azimuth_deg": None,
    "beam_width_deg": None,
}

# At 12,000 ft.
NORTH_EAST = (10.1, 20.1)  # Rr 15.98 km, delay 32.65 us, bearing 44.73, K -137.58
SOUTH_WEST = (9.9, 19.9)  # Rr 15.98 km, delay 96.66 us, bearing 224.76
NORTH = (10.2, 20.01)  # Rr 22.44 km, delay 12.82 us, bearing 2.84, K -127.43
FAR_NORTH = (10.36, 20.0)  # Rr 39.99 km, delay 119.70 us
BEYOND = (10.5, 20.3)  # Rr 64.45 km, delay 297.40 us


def at(hour: int, minute: int = 0, second: int = 0, day: int = 10) -> int:
    return int(datetime(2026, 3, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000)


T = at(12, 0, 5)


def fix(ts_ms=T, hex_="a1b2c3", *, position=NORTH_EAST, alt=12000, source="aggregator", node=NODE, **fields) -> dict:
    return {
        "ts_ms": ts_ms,
        "hex": hex_,
        "lat": position[0],
        "lon": position[1],
        "alt_baro": alt,
        "gs": 450.0,
        "track": 90.0,
        "flight": None,
        "seen_ms": ts_ms,
        "world": "real",
        "source": source,
        "source_node_id": node,
        **fields,
    }


def frame(ts_ms=T, hexes=(None,), *, snr=None, delay=30.0, position=NORTH_EAST) -> dict:
    """One frame with a detection per entry of ``hexes``; None is an untagged one."""
    tag = {"lat": position[0], "lon": position[1], "alt_baro": 12000}
    return {
        "timestamp": ts_ms,
        "delay": [delay] * len(hexes),
        "doppler": [-50.0] * len(hexes),
        "snr": list(snr) if snr else [12.0] * len(hexes),
        "adsb": [{"hex": h, **tag} if h else None for h in hexes],
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


DETECTION_COLUMNS = (
    "claim_hex",
    "claim_path",
    "claim_contested",
    "claim_delay_res_us",
    "claim_doppler_res_hz",
    "claim_fix_delay_res_us",
    "aircraft_type",
    "aircraft_class",
    "tx_callsign",
    "cpi_s",
    "rx_site_id",
)


def set_columns(path: Path, **columns: tuple[pa.DataType, list]) -> None:
    """Rewrite a written file, replacing a column of that name or appending it when absent."""
    table = pq.ParquetFile(path).read()
    for name, (type_, values) in columns.items():
        field, array = pa.field(name, type_), pa.array(values, type_)
        if name in table.column_names:
            table = table.set_column(table.column_names.index(name), field, array)
        else:
            table = table.append_column(field, array)
    pq.write_table(table, path)


def drop_columns(path: Path, *names: str) -> None:
    """Rewrite a written file as one from before its writer had these columns; detections pass DETECTION_COLUMNS."""
    pq.write_table(pq.ParquetFile(path).read().drop_columns(list(names)), path)


class Archive:
    def __init__(self, root: Path) -> None:
        self.detections = root / "archive"
        self.adsb = root / "adsb"

    def frames(self, frames: list[dict], flushed=FLUSHED, node_id=NODE, node_cfg=None) -> Path:
        key = parquet_writer.write_detections_parquet(
            node_id=node_id, frames=frames, base_dir=self.detections, write_ts=flushed, node_cfg=node_cfg
        )
        assert key is not None
        return self.detections / key

    def fixes(self, rows: list[dict], flushed=FLUSHED) -> Path:
        key = adsb_writer.write_adsb_parquet(rows=rows, base_dir=self.adsb, write_ts=flushed)
        assert key is not None
        return self.adsb / key

    def table(self, node_id=NODE, day=DAY, *, configs=(CONFIG,), types_for=None, class_for=None, **options) -> dict:
        table = pdata.node_day_table(
            node_id,
            day,
            configs=configs,
            archive_root=self.detections,
            adsb_root=self.adsb,
            types_for=types_for or (lambda hexes: [None] * len(hexes)),
            class_for=class_for or (lambda icao_type: "unknown"),
            **options,
        )
        assert table is not None
        return table


@pytest.fixture
def archive(tmp_path) -> Archive:
    return Archive(tmp_path)


def test_one_detected_opportunity_fills_every_section(archive):
    archive.frames([frame(hexes=["a1b2c3"], snr=[14.0], delay=33.0)])
    archive.fixes([fix()])

    table = archive.table(types_for=lambda hexes: ["B738"] * len(hexes), class_for=lambda icao_type: "narrowbody")

    cell = [1, 1, 0]
    assert (table["opportunities"], table["detections_tags"], table["detections_claims"]) == (1, 1, 0)
    assert (table["denominator"], table["numerator"], table["gate_threshold_db"]) == ("aggregator", "tags", None)
    assert table["class_range_alt"] == {"narrowbody": {"0-20km": {"10-20kft": cell}}}
    assert table["class_geometry"] == {"narrowbody": {"-138": [1, 1, 0, 14.0, 196.0]}}
    assert table["class_ring"] == {"narrowbody": {"3": cell}}
    assert len(table["bearing"]) == 72
    assert [i for i, c in enumerate(table["bearing"]) if c[0]] == [8]
    assert table["bearing"][8] == cell
    assert table["by_range"] == {"10-20": cell}
    assert table["by_delay"] == {"25-50": cell}
    assert table["by_alt"] == {"10-20": cell}
    assert table["by_type"] == {"B738": cell}
    assert table["snr_hist"] == {"56": 1}
    # 33.0 less the 32.65 predicted.
    assert table["tag_residual_hist"] == {"6": 1}
    assert json.loads(json.dumps(table, allow_nan=False)) == table


def test_a_sim_row_is_no_opportunity(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", world="sim"), fix(hex_="d4e5f6")])

    assert archive.table()["opportunities"] == 1


def test_a_synthetic_node_gets_no_table_and_no_histogram(archive):
    archive.frames([frame(hexes=["a1b2c3"])], node_id="synth-radar")
    archive.fixes([fix(node="synth-radar")])

    assert (
        pdata.node_day_table(
            "synth-radar",
            DAY,
            configs=[CONFIG],
            archive_root=archive.detections,
            adsb_root=archive.adsb,
            types_for=lambda hexes: [None] * len(hexes),
            class_for=lambda icao_type: "unknown",
        )
        is None
    )
    assert pdata.snr_histogram("synth-radar", DAY, archive_root=archive.detections) == {}


def test_opportunities_are_the_rows_of_the_chosen_source_attributed_to_the_node(archive):
    archive.frames([frame()])
    archive.fixes(
        [
            fix(hex_="a00001", node=NODE),
            fix(hex_="a00002", node=f"radar-aaa,{NODE},radar-zzz"),
            fix(hex_="a00003", node="radar-alphabet"),
            fix(hex_="a00004", node="radar-alphabet,radar-zzz"),
            fix(hex_="a00005", source="node", node=NODE),
            fix(hex_="a00006", source="node", node="radar-alphabet"),
        ]
    )

    assert archive.table()["opportunities"] == 2
    assert archive.table(opportunities="node")["opportunities"] == 1


def test_a_row_must_be_airborne(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", alt=0), fix(hex_="c0ffee", alt=None), fix(hex_="d4e5f6")])
    # Below sea level, so a fix at 0 ft is above this receiver's horizon and only its altitude rules it out.
    sunken = {**CONFIG, "rx_alt_ft": -1000.0}

    assert archive.table(configs=[sunken])["opportunities"] == 1


def test_an_altitude_on_a_bin_edge_falls_in_the_lower_bin(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", alt=5000), fix(hex_="d4e5f6", alt=20000)])

    table = archive.table()

    assert table["by_alt"] == {"2-5": [1, 0, 0], "10-20": [1, 0, 0]}
    assert table["class_range_alt"] == {"unknown": {"0-20km": {"0-5kft": [1, 0, 0], "10-20kft": [1, 0, 0]}}}
    assert table["class_ring"] == {"unknown": {"3": [1, 0, 0]}}


def test_the_delay_window_comes_from_max_range_and_defaults_to_200_us(archive):
    archive.frames([frame()])
    archive.fixes(
        [
            fix(hex_="a1b2c3", position=NORTH_EAST),
            fix(hex_="c0ffee", position=FAR_NORTH),
            fix(hex_="d4e5f6", position=BEYOND),
        ]
    )

    assert archive.table()["opportunities"] == 2
    assert archive.table(configs=[{**CONFIG, "max_range_km": 14.99}])["opportunities"] == 1
    assert archive.table(configs=[{**CONFIG, "max_range_km": 119.92}])["opportunities"] == 3
    assert archive.table(configs=[{**CONFIG, "max_range_km": None}])["opportunities"] == 2


def test_a_fix_below_the_receivers_horizon_is_no_opportunity(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", position=FAR_NORTH, alt=100), fix(hex_="d4e5f6", position=FAR_NORTH)])

    assert archive.table()["opportunities"] == 1


def test_the_declared_beam_applies_only_when_azimuth_and_width_are_both_declared(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", position=NORTH), fix(hex_="d4e5f6", position=SOUTH_WEST)])
    # Wraps through north: 2.84 degrees is 12.84 off this axis.
    beam = {**CONFIG, "beam_azimuth_deg": 350.0, "beam_width_deg": 40.0}

    assert archive.table(configs=[beam])["opportunities"] == 1
    assert archive.table(configs=[{**beam, "beam_width_deg": None}])["opportunities"] == 2


def test_a_learned_field_of_view_replaces_the_declared_beam(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", position=NORTH), fix(hex_="d4e5f6", position=SOUTH_WEST)])
    beam = {**CONFIG, "beam_azimuth_deg": 0.0, "beam_width_deg": 20.0}
    ranges = []

    def south_only(bearing_deg, range_km):
        ranges.extend(range_km.tolist())
        return bearing_deg > 180.0

    table = archive.table(configs=[beam], in_fov=south_only)

    assert table["opportunities"] == 1
    assert table["bearing"][44] == [1, 0, 0]
    assert [round(km, 1) for km in ranges] == [22.4, 16.0]


def test_an_aggregator_bucket_with_no_row_of_the_node_is_no_opportunity(archive):
    archive.frames([frame(at(12, 0, 5))])
    archive.frames([frame(at(13))], node_id="radar-beta")
    archive.fixes([fix(at(12, 0, 5), "a1b2c3"), fix(at(12, 0, 15), "c0ffee"), fix(at(13), "d4e5f6")])

    assert archive.table()["opportunities"] == 1


def test_a_held_fix_counts_once_across_snapshots_and_files(archive):
    archive.frames([frame(T), frame(T + 10_000)])
    held = {"seen_ms": T}
    archive.fixes([fix(T + 10_000, **held), fix(T + 20_000, **held)])
    archive.fixes([fix(T + 30_000, **held), fix(T + 30_000, seen_ms=T + 1_000)], flushed=FLUSHED + timedelta(minutes=1))
    assert archive.table()["opportunities"] == 1

    archive.fixes([fix(T + 40_000, seen_ms=T + 10_000)], flushed=FLUSHED + timedelta(minutes=2))
    assert archive.table()["opportunities"] == 2


def test_node_mode_counts_every_row_by_its_snapshot_time(archive):
    archive.frames([frame(T, ["a1b2c3"])])
    held = {"source": "node", "seen_ms": T - 40_000}
    archive.fixes([fix(T, **held), fix(T + 1_000, **held), fix(T + 300_000, **held)])

    table = archive.table(opportunities="node")

    assert (table["denominator"], table["opportunities"], table["detections_tags"]) == ("node", 3, 2)


def test_a_tag_in_a_neighbouring_bucket_detects_and_one_further_out_does_not(archive):
    archive.frames(
        [
            frame(T),
            frame(T - 10_000, ["A1B2C3"], snr=[15.0]),
            frame(T + 10_000, ["A1B2C3"], snr=[14.0]),
            frame(T + 20_000, ["D4E5F6"]),
        ]
    )
    archive.fixes([fix(T, "a1b2c3"), fix(T, "d4e5f6")])

    table = archive.table()

    assert (table["opportunities"], table["detections_tags"]) == (2, 1)
    assert table["class_geometry"] == {"unknown": {"-138": [2, 1, 0, 15.0, 225.0]}}


def test_a_claim_counts_only_through_the_gate(archive):
    hexes = ["a00001", "a00002", "a00003", "a00004", "a00005", "a00006"]
    path = archive.frames([frame(snr=[12.0, 12.0, 12.0, 12.0, 10.5, 11.0], hexes=[None] * 6)])
    set_columns(
        path,
        claim_hex=(pa.string(), hexes),
        claim_path=(pa.string(), ["tag", "dr", "dr", "dr", "dr", "hold"]),
        claim_contested=(pa.bool_(), [False, True, False, False, False, None]),
        claim_delay_res_us=(pa.float64(), [2.9, 0.1, 3.5, 0.1, 0.1, -3.0]),
        claim_doppler_res_hz=(pa.float64(), [7.0, 1.0, 1.0, None, 1.0, -8.0]),
    )
    archive.fixes([fix(hex_=h) for h in hexes])

    gated = archive.table(threshold_db=10.0)

    assert (gated["numerator"], gated["gate_threshold_db"]) == ("claims", 10.0)
    assert (gated["opportunities"], gated["detections_tags"], gated["detections_claims"]) == (6, 0, 2)
    assert archive.table()["detections_claims"] == 3


def test_claim_residuals_hold_every_uncontested_refreshed_hold_past_both_gates(archive):
    path = archive.frames([frame(snr=[12.0, 2.0, 12.0, 12.0], hexes=[None] * 4)])
    set_columns(
        path,
        claim_hex=(pa.string(), ["a00001", None, "a00003", None]),
        claim_path=(pa.string(), ["hold", "hold", "hold", "dr"]),
        claim_contested=(pa.bool_(), [False, None, True, None]),
        claim_delay_res_us=(pa.float64(), [0.1, 0.1, 0.1, 7.0]),
        claim_doppler_res_hz=(pa.float64(), [99.0, 1.0, 1.0, None]),
        claim_fix_delay_res_us=(pa.float64(), [5.02, -0.02, 0.1, None]),
    )
    archive.fixes([fix(hex_="a00001")])

    # 5.02 us is past the claim gate, and the row at 2 dB is under the SNR's. The contested hold and the dr row drop out.
    table = archive.table(threshold_db=10.0)

    assert table["claim_residual_hist"] == {"100": 1, "-1": 1}
    merged = pdata.merge_tables([table, table | {"day": "2026-03-11", "claim_residual_hist": {"100": 2}}])
    assert merged["claim_residual_hist"] == {"100": 3, "-1": 1}

    drop_columns(path, "claim_fix_delay_res_us")
    assert archive.table()["claim_residual_hist"] == {}
    assert sum(archive.table()["snr_hist"].values()) == 4


def test_a_dark_tracks_claim_has_no_hex_and_detects_nothing(archive):
    dark = {
        "claim_hex": None,
        "claim_path": "follow",
        "claim_contested": False,
        "claim_delay_res_us": 0.1,
        "claim_doppler_res_hz": 1.0,
    }
    archive.frames([{**frame(), "_claims": {0: dark}}])
    archive.fixes([fix()])

    table = archive.table(threshold_db=10.0)

    assert (table["numerator"], table["opportunities"]) == ("claims", 1)
    assert (table["detections_tags"], table["detections_claims"]) == (0, 0)


def test_an_old_file_falls_back_on_tags(archive):
    drop_columns(archive.frames([frame(hexes=["a1b2c3"])]), *DETECTION_COLUMNS)
    archive.fixes([fix()])

    table = archive.table(threshold_db=10.0)

    assert (table["numerator"], table["detections_tags"], table["detections_claims"]) == ("tags", 1, 0)


def test_a_file_without_the_claim_columns_holds_its_day_on_tags(archive):
    old = archive.frames([frame(at(6), ["a1b2c3"])], flushed=FLUSHED - timedelta(hours=11))
    drop_columns(old, *DETECTION_COLUMNS)
    claimed = archive.frames([frame(at(18), ["d4e5f6"])])
    set_columns(
        claimed,
        claim_hex=(pa.string(), ["d4e5f6"]),
        claim_path=(pa.string(), ["dr"]),
        claim_delay_res_us=(pa.float64(), [0.1]),
        claim_doppler_res_hz=(pa.float64(), [1.0]),
    )
    archive.fixes([fix(at(6), "a1b2c3"), fix(at(18), "d4e5f6")])

    table = archive.table()

    assert (table["numerator"], table["detections_tags"], table["detections_claims"]) == ("tags", 2, 1)

    # Null columns are a file with nothing to claim, and only rows of the day count.
    set_columns(old, claim_hex=(pa.string(), [None]), claim_path=(pa.string(), [None]))
    outside = archive.frames([frame(at(23, 59, 55, day=9), ["a1b2c3"])], flushed=FLUSHED + timedelta(minutes=1))
    drop_columns(outside, *DETECTION_COLUMNS)
    assert archive.table()["numerator"] == "claims"


def test_a_compacted_day_and_a_part_after_it_read_each_row_once(archive):
    archive.fixes([fix(at(10), "a1b2c3", source="node")])
    archive.fixes([fix(at(11), "c0ffee", source="node")])
    assert compaction.compact_day(compaction.ADSB, str(archive.adsb), DAY, now_ms=at(1, day=11)) == "compacted"
    archive.fixes([fix(at(12), "d4e5f6", source="node")], flushed=FLUSHED + timedelta(hours=1))

    assert archive.table(opportunities="node")["opportunities"] == 3


def test_rows_in_the_next_days_folders_belong_to_the_day_of_their_own_time(archive):
    flushed = datetime(2026, 3, 11, 0, 5, 0, tzinfo=UTC)
    last = at(23, 59, 55)
    archive.frames(
        [frame(last, ["a1b2c3"]), frame(at(0, 0, 5, day=11), ["d4e5f6"]), frame(at(0, 0, 30, day=11))],
        flushed=flushed,
    )
    archive.fixes([fix(at(0, 0, 20, day=11), "a1b2c3", seen_ms=last), fix(last, "d4e5f6")], flushed=flushed)

    table = archive.table()
    following = archive.table(day=date(2026, 3, 11))

    assert (table["opportunities"], table["detections_tags"]) == (2, 2)
    assert sum(table["snr_hist"].values()) == 1
    assert following["opportunities"] == 0
    assert sum(following["snr_hist"].values()) == 2


def test_geometry_takes_the_true_position_from_the_config_not_the_rows_published_one(archive):
    published = {**CONFIG, "rx_lat": 11.0, "rx_lon": 21.0, "tx_lat": 12.0, "tx_lon": 22.0}
    archive.frames([frame(hexes=["a1b2c3"], delay=33.0)], node_cfg=published)

    assert archive.table()["tag_residual_hist"] == {"6": 1}


def test_each_row_takes_the_config_version_active_at_its_time(archive):
    configs = [
        {**CONFIG, "created_at": "2026-03-10 18:00:00.000000", "rx_lat": 30.0, "tx_lat": 30.2},
        {**CONFIG, "created_at": datetime(2026, 3, 10, 12, 0, 0), "rx_lat": None},
        {**CONFIG, "created_at": "2026-03-10 06:00:00.000000"},
    ]
    times = [at(3), at(9), at(15), at(21)]
    archive.frames([frame(t, ["a1b2c3"], delay=33.0) for t in times])
    archive.fixes([fix(t) for t in times])

    table = archive.table(configs=configs)

    # Before the oldest and under it; a null position gives neither, and a site out of reach only a clipped residual.
    assert table["opportunities"] == 2
    assert table["tag_residual_hist"] == {"6": 2, "-20000": 1}


def test_a_merged_window_reads_one_numerator_unless_overridden(archive):
    first = frame(T, ["a1b2c3", "d4e5f6"], snr=[14.0, 16.0])
    claimed = archive.frames([first])
    set_columns(
        claimed,
        claim_hex=(pa.string(), ["a1b2c3", None]),
        claim_path=(pa.string(), ["tag", None]),
        claim_delay_res_us=(pa.float64(), [0.1, None]),
        claim_doppler_res_hz=(pa.float64(), [0.1, None]),
    )
    archive.fixes([fix(T, "a1b2c3"), fix(T, "d4e5f6")])
    next_day = at(12, 0, 5, day=11)
    next_flush = FLUSHED + timedelta(days=1)
    drop_columns(
        archive.frames([frame(next_day, ["a1b2c3"], snr=[10.0]), frame(next_day)], flushed=next_flush),
        *DETECTION_COLUMNS,
    )
    archive.fixes([fix(next_day, "a1b2c3"), fix(next_day, "d4e5f6")], flushed=next_flush)
    days = [json.loads(json.dumps(archive.table(day=d))) for d in (date(2026, 3, 11), DAY)]

    merged = pdata.merge_tables(days)

    assert (merged["window"], merged["days"]) == (["2026-03-10", "2026-03-11"], ["2026-03-10", "2026-03-11"])
    assert merged["numerator"] == "tags"
    assert (merged["opportunities"], merged["detections"]) == (4, 3)
    assert (merged["detections_claims"], merged["detections_tags"]) == (1, 3)
    assert merged["by_range"] == {"10-20": [4, 3]}
    assert merged["bearing"][8] == [4, 3]
    assert merged["class_range_alt"] == {"unknown": {"0-20km": {"10-20kft": [4, 3]}}}
    assert merged["class_ring"] == {"unknown": {"3": [4, 3]}}
    assert merged["class_geometry"] == {"unknown": {"-138": [4, 3, 3, 40.0, 552.0]}}
    assert merged["class_geometry_by_day"] == {
        "2026-03-10": {"unknown": {"-138": [2, 2]}},
        "2026-03-11": {"unknown": {"-138": [2, 1]}},
    }
    assert merged["snr_hist"] == {"56": 1, "64": 1, "40": 1, "48": 1}

    as_claims = pdata.merge_tables(days, numerator="claims")
    assert (as_claims["numerator"], as_claims["detections"], as_claims["by_range"]) == ("claims", 1, {"10-20": [4, 1]})

    empty = {**days[0], "day": "2026-03-12", "numerator": "tags", "opportunities": 0}
    assert pdata.merge_tables([days[1], {**empty, "class_geometry": {}}])["numerator"] == "claims"

    # The columns and no claim: a quiet day counts its tag as a miss and leaves the window on claims.
    quiet_time, quiet_flush = at(12, 0, 5, day=12), FLUSHED + timedelta(days=2)
    archive.frames([frame(quiet_time, ["a1b2c3"])], flushed=quiet_flush)
    archive.fixes([fix(quiet_time, "a1b2c3"), fix(quiet_time, "d4e5f6")], flushed=quiet_flush)
    quiet = json.loads(json.dumps(archive.table(day=date(2026, 3, 12))))
    window = pdata.merge_tables([days[1], quiet])
    assert (window["numerator"], window["detections"], window["by_range"]) == ("claims", 1, {"10-20": [4, 1]})
    assert (quiet["numerator"], quiet["claim_columns"], days[0]["claim_columns"]) == ("tags", True, False)
    assert pdata.merge_tables([days[1], quiet, days[0]])["numerator"] == "tags"

    with pytest.raises(ValueError):
        pdata.merge_tables([])
    with pytest.raises(ValueError):
        pdata.merge_tables([days[0], {**days[1], "denominator": "node"}])
    with pytest.raises(ValueError):
        pdata.merge_tables([days[0], {**days[1], "node_id": "radar-beta"}])


def test_a_ring_holds_its_hits_and_merges_them_by_the_windows_numerator(archive):
    path = archive.frames([frame(T, ["a1b2c3", "d4e5f6"])])
    set_columns(
        path,
        claim_hex=(pa.string(), ["a1b2c3", None]),
        claim_path=(pa.string(), ["tag", None]),
        claim_delay_res_us=(pa.float64(), [0.1, None]),
        claim_doppler_res_hz=(pa.float64(), [0.1, None]),
    )
    archive.fixes([fix(T, "a1b2c3"), fix(T, "d4e5f6"), fix(T, "c0ffee")])

    day = archive.table()

    assert day["class_ring"] == {"unknown": {"3": [3, 2, 1]}}
    assert pdata.merge_tables([day])["class_ring"] == {"unknown": {"3": [3, 1]}}
    assert pdata.merge_tables([day], numerator="tags")["class_ring"] == {"unknown": {"3": [3, 2]}}


def test_a_rows_own_type_comes_first_and_the_resolver_fills_the_rest(archive):
    archive.frames([frame(T), frame(T + 10_000)])
    path = archive.fixes([fix(T, "a1b2c3"), fix(T, "c0ffee"), fix(T, "d4e5f6")])
    set_columns(path, aircraft_type=(pa.string(), ["A320", None, None]))
    drop_columns(archive.fixes([fix(T + 10_000, "d4e5f6")], flushed=FLUSHED + timedelta(minutes=1)), "aircraft_type")

    def class_for(icao_type):
        return {"A320": "narrowbody", "C172": "light_GA"}.get(icao_type, "unknown")

    table = archive.table(types_for=lambda hexes: [{"d4e5f6": "C172"}.get(h) for h in hexes], class_for=class_for)

    assert table["by_type"] == {"A320": [1, 0, 0], "C172": [2, 0, 0]}
    one = [1, 0, 0]
    assert table["class_ring"] == {"narrowbody": {"3": one}, "light_GA": {"3": [2, 0, 0]}, "unknown": {"3": one}}


def test_a_real_node_with_no_adsb_day_gets_a_table_of_zeros(archive):
    archive.frames([frame(hexes=["a1b2c3"])])

    table = archive.table()

    assert (table["opportunities"], table["detections_tags"], table["detections_claims"]) == (0, 0, 0)
    assert table["bearing"] == [[0, 0, 0]] * 72
    assert table["class_geometry"] == {}
    assert table["snr_hist"] == {"48": 1}


def test_the_last_stretch_of_the_window_stays_out_of_the_geometry_section_alone(archive):
    archive.frames([frame()])
    archive.fixes([fix(hex_="a1b2c3", position=NORTH), fix(hex_="d4e5f6", position=NORTH_EAST)])

    table = archive.table(configs=[{**CONFIG, "max_range_km": 14.99}])

    assert table["by_delay"] == {"0-25": [1, 0, 0], "25-50": [1, 0, 0]}
    assert table["class_geometry"] == {"unknown": {"-128": [1, 0, 0, 0.0, 0.0]}}


def test_a_nan_snr_reaches_neither_a_histogram_nor_a_sum(archive):
    archive.frames([frame(T, ["a1b2c3", "a1b2c3", "d4e5f6"], snr=[math.nan, 13.0, math.nan])])
    archive.fixes([fix(T, "a1b2c3"), fix(T, "d4e5f6")])

    table = archive.table()

    assert table["snr_hist"] == {"52": 1}
    assert table["class_geometry"] == {"unknown": {"-138": [2, 2, 0, 13.0, 169.0]}}
    assert json.loads(json.dumps(table, allow_nan=False)) == table


def test_the_snr_histogram_holds_the_days_rows_only(archive):
    flushed = datetime(2026, 3, 11, 0, 5, 0, tzinfo=UTC)
    archive.frames(
        [frame(T, [None, None]), frame(T + 1_000, snr=[12.3]), frame(at(0, 0, 1, day=11), snr=[30.0])], flushed=flushed
    )

    assert pdata.snr_histogram(NODE, DAY, archive_root=archive.detections) == {"48": 2, "49": 1}


def test_ring_grid_gives_k_and_delay_per_ring_and_bearing_bin():
    grid = pdata.ring_grid(CONFIG)

    assert grid is not None
    assert grid["ring_km"].tolist()[:3] == [2.5, 7.5, 12.5]
    assert grid["known_db"].shape == grid["delay_us"].shape == (30, 72)
    assert grid["window_us"] == pytest.approx(200.005, abs=0.001)
    # 17.5 km of slant range, on a bearing of 42.5 degrees, at 12,500 ft.
    assert grid["known_db"][3, 8] == pytest.approx(-138.06, abs=0.01)
    assert grid["delay_us"][3, 8] == pytest.approx(35.88, abs=0.01)
    # 147.5 km out on a bearing of 92.5 degrees, where one earth radius for every direction drifts.
    assert grid["delay_us"][29, 18] == pytest.approx(919.16, abs=0.02)
    assert pdata.ring_grid({**CONFIG, "tx_lat": None}) is None
    assert pdata.ring_grid({**CONFIG, "fc_hz": None}) is None
