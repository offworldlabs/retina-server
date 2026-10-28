"""The owner dashboard's SNR trend, derived from the detection archive.

Every file here is written by services/parquet_writer, the archive's own
writer, and every flush is frame_processor's own; nothing builds a table by
hand.  The archive root is a tmp_path, never the real coverage_data.
"""

import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pyarrow.parquet as pq
import pytest

from services import frame_processor as fp
from services import parquet_writer as pw
from services import snr_series as ss
from services import storage


def _at(day: int, hour: int, minute: int, second: int = 0, ms: int = 0) -> int:
    """Epoch ms of a March 2026 instant, UTC."""
    return int(datetime(2026, 3, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000) + ms


# Not on a bucket boundary, so the window's first and last buckets are partial.
NOW_MS = _at(10, 12, 2, 30)
WRITE_TS = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
HOUR_MS = 3_600_000
# When the node became the caller's, before every frame here. What the series
# holds is pinned here; how the tenure bounds it, by tests/test_owner_tenure.py.
SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _frame(ts_ms, snr) -> dict:
    return {
        "timestamp": ts_ms,
        "delay": [10.0 + i for i in range(len(snr))],
        "doppler": [-50.0 + i for i in range(len(snr))],
        "snr": list(snr),
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }


def _write(archive: Path, node_id: str, frames: list[dict], write_ts: datetime = WRITE_TS) -> str:
    key = pw.write_detections_parquet(node_id=node_id, frames=frames, base_dir=archive, write_ts=write_ts)
    assert key is not None
    return key


def _buffer(node_id: str, frames: list[dict]) -> None:
    """Queue frames for the archive exactly as frame_processor's ingest does."""
    with fp._archive_buffer_lock:
        for frame in frames:
            fp._archive_buffer[node_id].append(frame)


def _counts(series: dict) -> dict[int, int]:
    return {p["t_ms"]: p["count"] for p in series["points"]}


def _total(series: dict) -> int:
    return sum(p["count"] for p in series["points"])


def _key(node_id: str, hours: int) -> tuple[str, int, int]:
    """The cache's key for an answer read with SINCE as the tenure."""
    return node_id, hours, ss._since_ms(SINCE)


class Clock:
    """Stands in for the module's `time`: wall clock, monotonic clock and sleep."""

    def __init__(self):
        self.wall_s = NOW_MS / 1000
        self.mono = 1000.0
        self.slept: list[float] = []
        self.on_sleep = None

    def time(self):
        return self.wall_s

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()


@pytest.fixture(autouse=True)
def _fresh_cache():
    ss._reset_for_tests()
    yield
    ss._reset_for_tests()


@pytest.fixture
def archive(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "archive"
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(root))
    return root


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(ss, "time", c)
    return c


@pytest.fixture
def flush_writes_to(archive, monkeypatch):
    """Route frame_processor's real flush into the tmp archive, at WRITE_TS.

    _flush_archive_node is untouched: only the archive_detections it calls is
    pointed at the writer with a fixed write time, so the partition day does
    not depend on when the suite runs.
    """

    def write(node_id, frames):
        return _write(archive, node_id, frames)

    monkeypatch.setattr(fp, "archive_detections", write)
    return archive


# ── Buckets and the window ───────────────────────────────────────────────────


def test_a_detection_on_a_bucket_boundary_starts_that_bucket(archive):
    _write(archive, "node-A", [_frame(_at(10, 11, 5), [10.0]), _frame(_at(10, 11, 4, 59, 999), [20.0])])

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert series["points"] == [
        {"t_ms": _at(10, 11, 0), "median_db": 20.0, "p90_db": 20.0, "count": 1},
        {"t_ms": _at(10, 11, 5), "median_db": 10.0, "p90_db": 10.0, "count": 1},
    ]
    assert all(p["t_ms"] % 300_000 == 0 for p in series["points"])


def test_the_window_includes_both_edges_and_nothing_beyond_them(archive):
    start = NOW_MS - HOUR_MS  # 11:02:30
    _write(
        archive,
        "node-A",
        [
            _frame(start - 1, [1.0]),
            _frame(start, [2.0]),
            _frame(NOW_MS, [3.0]),
            _frame(NOW_MS + 1, [4.0]),
        ],
    )

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    # The first bucket starts before the window (11:00 < 11:02:30) and holds
    # only what is inside it; the last is the one `now` falls in.
    assert series["points"] == [
        {"t_ms": _at(10, 11, 0), "median_db": 2.0, "p90_db": 2.0, "count": 1},
        {"t_ms": _at(10, 12, 0), "median_db": 3.0, "p90_db": 3.0, "count": 1},
    ]
    assert series["through_ms"] == NOW_MS


def test_median_and_p90_match_hand_computed_values(archive):
    _write(
        archive,
        "node-A",
        [
            # 11:10, split over two frames: sorted 10 20 30 40.
            _frame(_at(10, 11, 10, 5), [40.0, 10.0]),
            _frame(_at(10, 11, 12), [30.0, 20.0]),
            # 11:20, unsorted and odd: sorted 1 3 5.
            _frame(_at(10, 11, 21), [5.0, 1.0, 3.0]),
            # 11:30, a single value.
            _frame(_at(10, 11, 34, 59, 999), [7.5]),
            # 11:40, 1..10.
            _frame(_at(10, 11, 40), [float(v) for v in range(10, 0, -1)]),
        ],
    )

    points = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)["points"]

    # Linear between closest ranks: the q-th percentile of n sorted values
    # sits at index q * (n - 1).
    #   10 20 30 40: median at 1.5 -> 25;  p90 at 2.7 -> 30 + 0.7 * 10 = 37
    #   1 3 5:       median at 1   -> 3;   p90 at 1.8 -> 3 + 0.8 * 2  = 4.6
    #   7.5:         both 7.5
    #   1..10:       median at 4.5 -> 5.5; p90 at 8.1 -> 9 + 0.1 * 1  = 9.1
    assert [p["t_ms"] for p in points] == [_at(10, 11, 10), _at(10, 11, 20), _at(10, 11, 30), _at(10, 11, 40)]
    assert [p["count"] for p in points] == [4, 3, 1, 10]
    assert [p["median_db"] for p in points] == [25.0, 3.0, 7.5, 5.5]
    assert [p["p90_db"] for p in points] == [
        37.0,
        pytest.approx(4.6, abs=1e-12),
        7.5,
        pytest.approx(9.1, abs=1e-12),
    ]


def test_points_are_oldest_first_and_empty_buckets_are_left_out(archive):
    # Newer file first, and each file newest first.
    _write(archive, "node-A", [_frame(_at(10, 11, 55), [3.0]), _frame(_at(10, 11, 50), [2.0])])
    _write(archive, "node-A", [_frame(_at(10, 11, 15), [1.0]), _frame(_at(10, 11, 10), [1.0])])

    points = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)["points"]

    assert [p["t_ms"] for p in points] == [_at(10, 11, 10), _at(10, 11, 15), _at(10, 11, 50), _at(10, 11, 55)]
    assert all(p["count"] > 0 for p in points)


def test_through_ms_is_the_newest_detection_included(archive):
    _write(
        archive,
        "node-A",
        [
            _frame(_at(10, 11, 30, 0, 123), [5.0]),
            _frame(_at(10, 11, 45), [None]),  # newer, but no SNR: not included
            _frame(_at(10, 12, 10), [6.0]),  # newer, but after now
        ],
    )

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert series["through_ms"] == _at(10, 11, 30, 0, 123)
    assert _total(series) == 1


def test_rows_without_a_finite_snr_are_left_out(archive):
    key = _write(archive, "node-A", [_frame(_at(10, 11, 30), [None, math.nan, math.inf, -math.inf, "junk", 12.0])])
    # The writer kept all six rows (junk as null): the filtering is ours.
    snr_column = pq.read_table(archive / key, partitioning=None).column("snr_db").to_pylist()
    assert len(snr_column) == 6
    assert snr_column[0] is None and snr_column[4] is None

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert series["points"] == [{"t_ms": _at(10, 11, 30), "median_db": 12.0, "p90_db": 12.0, "count": 1}]


# ── Which files are read ─────────────────────────────────────────────────────


def test_a_window_across_midnight_reads_both_days_and_no_others(archive, monkeypatch):
    now = _at(11, 0, 30)
    # Day 10's partition holds day 10's frames; day 11's holds the frames its
    # first flush carried over midnight, as the hourly flush does.
    _write(
        archive,
        "node-A",
        [_frame(_at(10, 22, 40), [1.0]), _frame(_at(10, 23, 5), [2.0])],
        write_ts=datetime(2026, 3, 10, 23, 10, tzinfo=timezone.utc),
    )
    _write(
        archive,
        "node-A",
        [_frame(_at(10, 23, 55), [3.0]), _frame(_at(11, 0, 15), [4.0])],
        write_ts=datetime(2026, 3, 11, 0, 20, tzinfo=timezone.utc),
    )
    # Probes: partitions outside the window holding frames inside it.  No real
    # flush writes these; any of their rows in the answer means the partition
    # was opened.
    _write(
        archive,
        "node-A",
        [_frame(_at(10, 23, 0), [100.0])],
        write_ts=datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc),
    )
    _write(
        archive,
        "node-A",
        [_frame(_at(11, 0, 10), [100.0])],
        write_ts=datetime(2026, 3, 12, 1, 0, tzinfo=timezone.utc),
    )
    opened: list[Path] = []
    real_read = ss._read_file

    def spy(path):
        opened.append(path)
        return real_read(path)

    monkeypatch.setattr(ss, "_read_file", spy)

    series = ss.snr_series("node-A", 2, now_ms=now, since=SINCE)

    assert series["points"] == [
        {"t_ms": _at(10, 22, 40), "median_db": 1.0, "p90_db": 1.0, "count": 1},
        {"t_ms": _at(10, 23, 5), "median_db": 2.0, "p90_db": 2.0, "count": 1},
        {"t_ms": _at(10, 23, 55), "median_db": 3.0, "p90_db": 3.0, "count": 1},
        {"t_ms": _at(11, 0, 15), "median_db": 4.0, "p90_db": 4.0, "count": 1},
    ]
    assert series["through_ms"] == _at(11, 0, 15)
    assert sorted(p.relative_to(archive).parts[2] for p in opened) == ["day=10", "day=11"]


def test_a_file_whose_footer_puts_it_outside_the_window_is_not_read(archive, monkeypatch):
    before = _write(archive, "node-A", [_frame(_at(10, 10, 0), [1.0]), _frame(_at(10, 11, 2, 29, 999), [1.0])])
    straddling = _write(archive, "node-A", [_frame(_at(10, 10, 30), [2.0]), _frame(_at(10, 11, 30), [3.0])])
    read: list[Path] = []
    real_read = ss._read_file

    def spy(path):
        read.append(path)
        return real_read(path)

    monkeypatch.setattr(ss, "_read_file", spy)

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert read == [archive / straddling]
    assert archive / before not in read
    assert _counts(series) == {_at(10, 11, 30): 1}


def test_a_file_without_footer_statistics_is_still_counted(archive):
    bare = archive / _write(archive, "node-A", [_frame(_at(10, 11, 30), [4.0]), _frame(_at(10, 11, 5), [1.0])])
    # The writer's own table, written back without statistics: the footer can
    # no longer say what the file spans.
    pq.write_table(pq.read_table(bare, partitioning=None), bare, write_statistics=False)
    assert ss._time_range(bare) is None
    _write(archive, "node-A", [_frame(_at(10, 11, 10), [2.0]), _frame(_at(10, 11, 31), [6.0])])

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert _counts(series) == {_at(10, 11, 5): 1, _at(10, 11, 10): 1, _at(10, 11, 30): 2}
    assert series["points"][-1]["median_db"] == 5.0


def test_files_whose_spans_overlap_share_their_buckets(archive):
    # Written newest-spanning first, so neither path order nor write order
    # matches the order the spans start in.
    _write(
        archive,
        "node-A",
        [_frame(_at(10, 11, 20), [2.0]), _frame(_at(10, 11, 42), [4.0]), _frame(_at(10, 11, 52), [7.0])],
    )
    _write(archive, "node-A", [_frame(_at(10, 11, 10), [1.0]), _frame(_at(10, 11, 21), [3.0])])
    # Starts inside a bucket the file above already reached past.
    _write(archive, "node-A", [_frame(_at(10, 11, 41), [5.0]), _frame(_at(10, 11, 50), [6.0])])

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert [(p["t_ms"], p["count"], p["median_db"]) for p in series["points"]] == [
        (_at(10, 11, 10), 1, 1.0),
        (_at(10, 11, 20), 2, 2.5),
        (_at(10, 11, 40), 2, 4.5),
        (_at(10, 11, 50), 2, 6.5),
    ]


def test_buckets_are_reduced_as_the_files_are_read(archive, monkeypatch):
    """Only about one file's detections are held at a time, however long the window."""
    for hour in range(6, 12):
        frames = [_frame(_at(10, hour, minute), [10.0 + hour, 20.0 + hour]) for minute in (10, 20, 30, 40, 50)]
        _write(archive, "node-A", frames, write_ts=datetime(2026, 3, 10, hour, 59, tzinfo=timezone.utc))
    held: list[int] = []

    class Recording(ss._Series):
        def add(self, ts, snr):
            super().add(ts, snr)
            held.append(sum(v.size for chunks in self.open.values() for v in chunks))

    monkeypatch.setattr(ss, "_Series", Recording)

    series = ss.snr_series("node-A", 6, now_ms=NOW_MS, since=SINCE)

    assert _total(series) == 60 and len(series["points"]) == 30
    assert max(held) == 10  # one file's rows, never all sixty


def test_other_nodes_files_and_buffers_are_ignored(archive):
    _write(archive, "node-A", [_frame(_at(10, 11, 30), [10.0])])
    _write(archive, "node-B", [_frame(_at(10, 11, 30), [50.0, 60.0])])
    _write(archive, "node-A2", [_frame(_at(10, 11, 30), [70.0])])  # shares node-A's prefix
    _buffer("node-B", [_frame(_at(10, 11, 40), [80.0])])

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert series["points"] == [{"t_ms": _at(10, 11, 30), "median_db": 10.0, "p90_db": 10.0, "count": 1}]
    assert series["through_ms"] == _at(10, 11, 30)


def test_no_data_is_an_empty_series(archive):
    empty = {"hours": 24, "bucket_s": 300, "through_ms": None, "points": []}
    # No archive directory at all.
    assert ss.snr_series("node-A", 24, now_ms=NOW_MS, since=SINCE) == empty
    # Files, but all of them outside the window.
    _write(archive, "node-A", [_frame(NOW_MS - 25 * HOUR_MS, [10.0]), _frame(NOW_MS + 1, [10.0])])
    assert ss.snr_series("node-A", 24, now_ms=NOW_MS, since=SINCE) == empty


def test_an_unreadable_file_is_skipped_and_logged(archive, caplog):
    key = _write(archive, "node-A", [_frame(_at(10, 11, 30), [10.0])])
    bad = (archive / key).parent / "part-000000-damaged.parquet"
    bad.write_bytes(b"not parquet")

    with caplog.at_level(logging.WARNING, logger="services.snr_series"):
        series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert _total(series) == 1
    assert str(bad) in caplog.text


def test_a_node_id_must_be_one_path_component(archive):
    _write(archive, "node-A", [_frame(_at(10, 11, 30), [10.0])])
    # Joined unchecked, this id walks from node_id=x to node-A's directory.
    (archive / "year=2026" / "month=03" / "day=10" / "node_id=x").mkdir()
    assert _total(ss.snr_series("x/../node_id=node-A", 1, now_ms=NOW_MS, since=SINCE)) == 0
    for node_id in ("", ".", "..", "a\\b"):
        assert ss.snr_series(node_id, 1, now_ms=NOW_MS, since=SINCE)["points"] == []


@pytest.mark.parametrize("hours", [0, 73, -1, True, 1.5, "24"])
def test_hours_outside_1_to_72_are_refused(hours):
    with pytest.raises(ValueError):
        ss.snr_series("node-A", hours, now_ms=NOW_MS, since=SINCE)


# ── The answer itself ────────────────────────────────────────────────────────


def test_the_answer_is_the_contract_shape_and_plain_json(archive):
    _write(archive, "node-A", [_frame(_at(10, 11, 30), [10.0, 11.0, 12.5])])

    series = ss.snr_series("node-A", 24, now_ms=NOW_MS, since=SINCE)

    assert set(series) == {"hours", "bucket_s", "through_ms", "points"}
    assert type(series["hours"]) is int and series["hours"] == 24
    assert type(series["bucket_s"]) is int and series["bucket_s"] == 300
    assert type(series["through_ms"]) is int
    (point,) = series["points"]
    assert set(point) == {"t_ms", "median_db", "p90_db", "count"}
    assert [type(point[k]) for k in ("t_ms", "median_db", "p90_db", "count")] == [int, float, float, int]
    # No OPT_SERIALIZE_NUMPY: a numpy scalar anywhere would raise here.
    assert orjson.loads(orjson.dumps(series)) == series


# ── The cache ────────────────────────────────────────────────────────────────


def test_the_cache_answers_within_its_ttl_and_never_for_another_node_or_window(archive, clock):
    _write(archive, "node-A", [_frame(NOW_MS - 30 * 60_000, [10.0])])
    answered_at = clock.mono
    first = ss.snr_series("node-A", 1, since=SINCE)
    assert _total(first) == 1

    # More data: one newer detection, one older than an hour.
    _write(archive, "node-A", [_frame(NOW_MS - 20 * 60_000, [20.0]), _frame(NOW_MS - 90 * 60_000, [30.0])])
    _write(archive, "node-B", [_frame(NOW_MS - 10 * 60_000, [40.0])])
    clock.mono = answered_at + ss._CACHE_TTL_S - 0.001
    assert ss.snr_series("node-A", 1, since=SINCE) == first  # cached
    assert _total(ss.snr_series("node-A", 2, since=SINCE)) == 3  # another window
    node_b = ss.snr_series("node-B", 1, since=SINCE)  # another node
    assert node_b["points"] == [{"t_ms": _at(10, 11, 50), "median_db": 40.0, "p90_db": 40.0, "count": 1}]

    clock.mono = answered_at + ss._CACHE_TTL_S
    assert _total(ss.snr_series("node-A", 1, since=SINCE)) == 2


def test_a_cached_answer_cannot_be_changed_through_a_returned_copy(archive, clock):
    _write(archive, "node-A", [_frame(NOW_MS - 30 * 60_000, [10.0])])
    first = ss.snr_series("node-A", 1, since=SINCE)
    first["points"][0]["count"] = 999
    first["points"].clear()

    assert _total(ss.snr_series("node-A", 1, since=SINCE)) == 1


def test_a_pinned_now_neither_reads_nor_fills_the_cache(archive, clock):
    _write(archive, "node-A", [_frame(NOW_MS - 30 * 60_000, [10.0])])
    assert _total(ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)) == 1
    assert not ss._cache

    assert _total(ss.snr_series("node-A", 1, since=SINCE)) == 1  # fills it
    _write(archive, "node-A", [_frame(NOW_MS - 20 * 60_000, [20.0])])
    assert _total(ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)) == 2  # not served from it


def test_the_cache_is_bounded_and_drops_the_least_recent(archive, clock):
    for i in range(ss._CACHE_MAX + 5):
        ss.snr_series(f"node-{i}", 1, since=SINCE)
    assert len(ss._cache) == ss._CACHE_MAX
    assert _key("node-0", 1) not in ss._cache
    assert _key(f"node-{ss._CACHE_MAX + 4}", 1) in ss._cache

    # Expired answers go at the next insert, not only when crowded out.
    clock.mono += ss._CACHE_TTL_S
    ss.snr_series("node-fresh", 1, since=SINCE)
    assert list(ss._cache) == [_key("node-fresh", 1)]


async def test_the_async_wrapper_reads_on_a_worker_thread_and_answers_hits_in_place(archive, clock, monkeypatch):
    _write(archive, "node-A", [_frame(NOW_MS - 30 * 60_000, [10.0])])
    expected = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)
    loop_thread = threading.get_ident()
    read_threads: list[int] = []
    real_read = ss._read_file

    def spy(path):
        read_threads.append(threading.get_ident())
        return real_read(path)

    monkeypatch.setattr(ss, "_read_file", spy)
    calls = []
    real_series = ss.snr_series

    def counted(*args, **kwargs):
        calls.append(threading.get_ident())
        return real_series(*args, **kwargs)

    monkeypatch.setattr(ss, "snr_series", counted)

    first = await ss.snr_series_async("node-A", 1, since=SINCE)
    second = await ss.snr_series_async("node-A", 1, since=SINCE)

    assert first == second == expected
    assert len(calls) == 1 and calls[0] != loop_thread  # the hit never left the loop
    assert read_threads and loop_thread not in read_threads
    with pytest.raises(ValueError):
        await ss.snr_series_async("node-A", 73, since=SINCE)


# ── The unflushed archive buffer ─────────────────────────────────────────────


def test_unflushed_frames_are_merged_with_the_files(archive, monkeypatch):
    _write(archive, "node-A", [_frame(_at(10, 11, 10), [1.0, 2.0])])
    _buffer("node-A", [_frame(_at(10, 11, 11), [3.0]), _frame(_at(10, 11, 55, 0, 7), [4.0, 5.0])])
    disk_io = []

    def outside_the_ingest_lock(real):
        def call(*args):
            assert not fp._archive_buffer_lock.locked()
            disk_io.append(real.__name__)
            return real(*args)

        return call

    for name in ("_partition_files", "_time_range", "_read_file"):
        monkeypatch.setattr(ss, name, outside_the_ingest_lock(getattr(ss, name)))

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert disk_io == ["_partition_files", "_time_range", "_read_file"]

    assert series["points"] == [
        {"t_ms": _at(10, 11, 10), "median_db": 2.0, "p90_db": pytest.approx(2.8), "count": 3},
        {"t_ms": _at(10, 11, 55), "median_db": 4.5, "p90_db": pytest.approx(4.9), "count": 2},
    ]
    assert series["through_ms"] == _at(10, 11, 55, 0, 7)
    # The read left the buffer as it found it.
    assert len(fp._archive_buffer["node-A"]) == 2


def test_a_long_buffer_is_reduced_run_by_run(archive, monkeypatch):
    """A full buffer is not converted in one go: each run of frames streams like a file."""
    monkeypatch.setattr(ss, "_BUFFER_CHUNK", 30)
    # Sixty frames ten seconds apart from 11:10: runs of thirty fill 11:10 and 11:15.
    _buffer("node-A", [_frame(_at(10, 11, 10) + k * 10_000, [1.0 + k, 2.0 + k]) for k in range(60)])
    held: list[int] = []

    class Recording(ss._Series):
        def add(self, ts, snr):
            super().add(ts, snr)
            held.append(sum(v.size for chunks in self.open.values() for v in chunks))

    monkeypatch.setattr(ss, "_Series", Recording)

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert _counts(series) == {_at(10, 11, 10): 60, _at(10, 11, 15): 60}
    assert held == [60, 60]
    assert series["through_ms"] == _at(10, 11, 10) + 59 * 10_000


def test_reading_a_node_with_nothing_buffered_creates_no_buffer_entry(archive):
    ss.snr_series("node-Z", 1, now_ms=NOW_MS, since=SINCE)
    assert "node-Z" not in fp._archive_buffer


def test_buffered_frames_count_exactly_as_their_archived_rows_will(archive):
    frames = [
        _frame(_at(10, 11, 10), [None, math.nan, math.inf, "junk", "12.5", True, 3]),
        dict(_frame(_at(10, 11, 20), [1.0]), delay=[1.0, 2.0, 3.0]),  # more detections than SNRs
        dict(_frame(_at(10, 11, 30), []), snr=None),
        {k: v for k, v in _frame(_at(10, 11, 40), [9.0]).items() if k != "snr"},
        _frame(_at(10, 11, 50) + 0.9, [4.0]),  # a float timestamp
        {k: v for k, v in _frame(0, [8.0]).items() if k != "timestamp"},
        _frame(_at(10, 11, 52), [6.0, -2.5, 6.0]),
    ]
    _write(archive, "node-file", frames)
    _buffer("node-buffer", frames)

    from_file = ss.snr_series("node-file", 1, now_ms=NOW_MS, since=SINCE)
    from_buffer = ss.snr_series("node-buffer", 1, now_ms=NOW_MS, since=SINCE)

    assert from_buffer == from_file
    assert _total(from_file) == 3 + 1 + 1 + 3


def test_a_flush_completing_mid_read_is_not_counted_twice(flush_writes_to, clock, monkeypatch):
    archive = flush_writes_to
    _write(archive, "node-A", [_frame(_at(10, 11, 5), [1.0])])
    _buffer("node-A", [_frame(_at(10, 11, 30), [2.0, 3.0]), _frame(_at(10, 11, 40), [4.0])])
    real_list = ss._partition_files
    flushed = []

    def flush_between_buffer_and_files(*args):
        # The whole hourly flush lands after the buffer was read and before
        # the files are listed: the listing finds its file, the buffer copy
        # still holds the same frames.
        if not flushed:
            fp._flush_archive_node("node-A")
            flushed.append(True)
        return real_list(*args)

    monkeypatch.setattr(ss, "_partition_files", flush_between_buffer_and_files)

    series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)

    assert flushed and "node-A" not in fp._archive_buffer
    assert _counts(series) == {_at(10, 11, 5): 1, _at(10, 11, 30): 2, _at(10, 11, 40): 1}
    assert clock.slept == [ss._RETRY_WAIT_S]


def _start_paused_flush(archive, monkeypatch, node_id="node-A"):
    """Run frame_processor's flush on a thread that stops once its file is published.

    That is the moment a flush holds the node's frames on disk and in the
    buffer at once.  Returns (thread, release).
    """
    published = threading.Event()
    release = threading.Event()

    def write_then_wait(nid, frames):
        key = _write(archive, nid, frames)
        published.set()
        assert release.wait(10)
        return key

    monkeypatch.setattr(fp, "archive_detections", write_then_wait)
    thread = threading.Thread(target=fp._flush_archive_node, args=(node_id,))
    thread.start()
    assert published.wait(10)
    return thread, release


def test_a_flush_in_flight_is_waited_out(archive, clock, monkeypatch):
    _buffer("node-A", [_frame(_at(10, 11, 30), [2.0, 3.0]), _frame(_at(10, 11, 40), [4.0])])
    thread, release = _start_paused_flush(archive, monkeypatch)
    listings = []
    real_list = ss._partition_files

    def counted(*args):
        listings.append(args)
        return real_list(*args)

    monkeypatch.setattr(ss, "_partition_files", counted)

    def finish_the_flush():
        release.set()
        thread.join(10)

    clock.on_sleep = finish_the_flush
    try:
        series = ss.snr_series("node-A", 1, now_ms=NOW_MS, since=SINCE)
    finally:
        release.set()
        thread.join(10)

    assert _counts(series) == {_at(10, 11, 30): 2, _at(10, 11, 40): 1}
    assert clock.slept == [ss._RETRY_WAIT_S]
    # The files were not read while the flush was still writing: that read
    # could only have been thrown away.
    assert len(listings) == 1


def test_a_flush_that_stays_in_flight_gets_a_files_only_answer_that_is_not_cached(archive, clock, monkeypatch, caplog):
    _buffer("node-A", [_frame(_at(10, 11, 30), [2.0, 3.0])])
    thread, release = _start_paused_flush(archive, monkeypatch)
    try:
        # Arrives while the flush is writing: in the buffer only.
        _buffer("node-A", [_frame(_at(10, 11, 45), [5.0])])
        with caplog.at_level(logging.WARNING, logger="services.snr_series"):
            degraded = ss.snr_series("node-A", 1, since=SINCE)
    finally:
        release.set()
        thread.join(10)

    # The flushed frames once, from their file; the newer one is missed.
    assert _counts(degraded) == {_at(10, 11, 30): 2}
    assert clock.slept == [ss._RETRY_WAIT_S * 2**i for i in range(ss._READ_ATTEMPTS - 1)]
    assert "answering from flushed files only" in caplog.text
    assert not ss._cache

    complete = ss.snr_series("node-A", 1, since=SINCE)
    assert _counts(complete) == {_at(10, 11, 30): 2, _at(10, 11, 45): 1}
    assert _key("node-A", 1) in ss._cache
