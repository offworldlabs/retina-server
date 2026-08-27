"""Tests for scripts/backfill_archive_fuzz.py — the pre-fuzz archive rewrite.

Phase 1 moved the receiver in NEW Parquet writes; everything archived before it
still carries the true coordinate and is downloadable by anyone.  This exercises
the transform that fixes that, entirely on in-memory buffers: nothing here
touches R2, and the two ``run()`` tests assert the refusals that happen before
any network call is made.

The property under test is narrow on purpose.  Displacing a receiver twice is
not a smaller error than not displacing it at all — it puts the node somewhere
neither the map nor the archive agrees on, and there is no undo — so
idempotence is asserted as directly as the displacement itself.
"""

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

os.environ.setdefault("RETINA_ENV", "test")

from config.constants import (  # noqa: E402
    NODE_FUZZ_MAX_KM_DEFAULT,
    NODE_FUZZ_MIN_KM_DEFAULT,
)
from scripts.backfill_archive_fuzz import (  # noqa: E402
    fuzz_rx_columns,
    needs_fuzzing,
    run,
    transform_parquet_bytes,
)
from services import public_location as pl  # noqa: E402
from services.geo import haversine_km  # noqa: E402
from services.parquet_writer import PUBLISHED_SCHEMA, SCHEMA, is_rx_published  # noqa: E402

_SALT = "test-salt-for-archive-backfill"
_TRUE_LAT, _TRUE_LON = 34.851234, -82.401234
_TRUE_TX_LAT, _TRUE_TX_LON = 34.901234, -82.301234
# Rounding the published coordinate to 4 decimals moves it by up to ~8 m.
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


def _table(rows, *, schema=SCHEMA, ingest_ts_ms=1_000):
    """A per-detection table in whatever rx frame ``rows`` supplies.

    ``rows`` is [(node_id, rx_lat, rx_lon), …].  Every other column is filled
    with a recognisable value so the test can assert nothing else moved.
    """
    cols = {f.name: [] for f in SCHEMA}
    for i, (node_id, rx_lat, rx_lon) in enumerate(rows):
        for name in cols:
            cols[name].append(None)
        cols["frame_ts_ms"][-1] = 1_700_000_000_000
        cols["ingest_ts_ms"][-1] = ingest_ts_ms
        cols["node_id"][-1] = node_id
        cols["detection_index"][-1] = i
        cols["delay_us"][-1] = 80.0 + i
        cols["doppler_hz"][-1] = -12.5
        cols["snr_db"][-1] = 20.0
        cols["adsb_hex"][-1] = "abc123"
        cols["adsb_lat"][-1] = 34.7
        cols["adsb_lon"][-1] = -82.2
        cols["rx_lat"][-1] = rx_lat
        cols["rx_lon"][-1] = rx_lon
        cols["rx_alt_ft"][-1] = 950.0
        cols["tx_lat"][-1] = _TRUE_TX_LAT
        cols["tx_lon"][-1] = _TRUE_TX_LON
        cols["tx_alt_ft"][-1] = 1600.0
        cols["fc_hz"][-1] = 195_000_000.0
        cols["fs_hz"][-1] = 2_000_000.0
    return pa.table(cols, schema=schema)


def _to_bytes(table) -> bytes:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


class TestFuzzRxColumns:
    def test_the_receiver_moves_into_the_donut(self):
        out = fuzz_rx_columns(_table([("node-a", _TRUE_LAT, _TRUE_LON)]))
        moved_km = haversine_km(_TRUE_LAT, _TRUE_LON, out.column("rx_lat")[0].as_py(), out.column("rx_lon")[0].as_py())
        assert moved_km >= NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM
        assert moved_km <= NODE_FUZZ_MAX_KM_DEFAULT + _ROUNDING_SLACK_KM

    def test_it_lands_exactly_where_the_live_surfaces_publish(self):
        """The archive and the map must agree about where a node is."""
        out = fuzz_rx_columns(_table([("node-a", _TRUE_LAT, _TRUE_LON)]))
        assert (out.column("rx_lat")[0].as_py(), out.column("rx_lon")[0].as_py()) == pl.public_latlon(
            _TRUE_LAT, _TRUE_LON, "node-a"
        )

    def test_every_other_column_is_untouched(self):
        src = _table([("node-a", _TRUE_LAT, _TRUE_LON)])
        out = fuzz_rx_columns(src)
        for name in SCHEMA.names:
            if name in ("rx_lat", "rx_lon"):
                continue
            assert out.column(name).to_pylist() == src.column(name).to_pylist(), name

    def test_the_transmitter_stays_true(self):
        """A licensed broadcast tower on a public register; moving it is a lie."""
        out = fuzz_rx_columns(_table([("node-a", _TRUE_LAT, _TRUE_LON)]))
        assert out.column("tx_lat")[0].as_py() == _TRUE_TX_LAT
        assert out.column("tx_lon")[0].as_py() == _TRUE_TX_LON

    def test_each_row_uses_its_own_nodes_offset(self):
        """node_id is a column, so a mixed file must not take one node's shift."""
        out = fuzz_rx_columns(_table([("node-a", _TRUE_LAT, _TRUE_LON), ("node-b", _TRUE_LAT, _TRUE_LON)]))
        assert out.column("rx_lat")[0].as_py() != out.column("rx_lat")[1].as_py()

    def test_a_null_receiver_stays_null(self):
        out = fuzz_rx_columns(_table([("node-a", None, None)]))
        assert out.column("rx_lat")[0].as_py() is None
        assert out.column("rx_lon")[0].as_py() is None

    def test_the_result_is_stamped_published(self):
        assert is_rx_published(fuzz_rx_columns(_table([("node-a", _TRUE_LAT, _TRUE_LON)])).schema)

    def test_the_input_table_is_not_stamped(self):
        src = _table([("node-a", _TRUE_LAT, _TRUE_LON)])
        fuzz_rx_columns(src)
        assert not is_rx_published(src.schema)


class TestNeedsFuzzing:
    def test_an_unstamped_file_needs_it(self):
        assert needs_fuzzing(_table([("node-a", _TRUE_LAT, _TRUE_LON)]), None) is True

    def test_a_stamped_file_does_not(self):
        table = _table([("node-a", _TRUE_LAT, _TRUE_LON)], schema=PUBLISHED_SCHEMA)
        assert needs_fuzzing(table, None) is False

    def test_the_timestamp_guard_skips_post_fuzz_files(self):
        """The deploy gap: written after the fuzz landed, before the stamp did."""
        table = _table([("node-a", _TRUE_LAT, _TRUE_LON)], ingest_ts_ms=2_000)
        assert needs_fuzzing(table, 2_000) is False
        assert needs_fuzzing(table, 2_001) is True


class TestTransformParquetBytes:
    def test_a_pre_fuzz_file_is_rewritten(self):
        raw = _to_bytes(_table([("node-a", _TRUE_LAT, _TRUE_LON)]))
        out = transform_parquet_bytes(raw)
        assert out is not None
        table = pq.read_table(pa.BufferReader(out))
        assert (table.column("rx_lat")[0].as_py(), table.column("rx_lon")[0].as_py()) == pl.public_latlon(
            _TRUE_LAT, _TRUE_LON, "node-a"
        )

    def test_a_second_pass_is_a_no_op(self):
        """Idempotence, end to end: the stamp survives the round trip to bytes."""
        raw = _to_bytes(_table([("node-a", _TRUE_LAT, _TRUE_LON)]))
        once = transform_parquet_bytes(raw)
        assert transform_parquet_bytes(once) is None

    def test_a_file_written_by_the_writer_is_never_touched(self):
        """services/parquet_writer already writes the published coordinate."""
        from services.parquet_writer import _flatten

        cols = _flatten(
            "node-a",
            [{"timestamp": 1, "delay": [10.0], "doppler": [1.0], "snr": [20.0]}],
            ingest_ts_ms=1,
            node_cfg={"rx_lat": _TRUE_LAT, "rx_lon": _TRUE_LON},
        )
        raw = _to_bytes(pa.table(cols, schema=PUBLISHED_SCHEMA))
        assert transform_parquet_bytes(raw) is None

    def test_force_rewrites_a_stamped_file(self):
        raw = _to_bytes(_table([("node-a", _TRUE_LAT, _TRUE_LON)], schema=PUBLISHED_SCHEMA))
        assert transform_parquet_bytes(raw, force=True) is not None


class TestRunRefusals:
    """Both refusals happen before R2 is contacted, so neither test needs it."""

    def test_it_refuses_without_an_explicit_salt(self, monkeypatch):
        """The persisted fallback is per-machine: the wrong salt is not undoable."""
        monkeypatch.delenv("NODE_FUZZ_SALT", raising=False)
        assert run()["error"] == "no_salt"

    def test_it_refuses_when_fuzzing_is_off(self, monkeypatch):
        monkeypatch.setenv("NODE_FUZZ_MODE", "off")
        pl._reset_for_tests()
        assert run()["error"] == "fuzz_disabled"
