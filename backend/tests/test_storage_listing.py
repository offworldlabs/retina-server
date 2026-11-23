"""Tests for list_archived_files() — date_prefix and no-prefix branches."""

import logging
import os
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import services.storage as _storage
from services.parquet_writer import write_detections_parquet
from services.storage import list_archived_files


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    d = tmp_path / "archive"
    d.mkdir()
    monkeypatch.setattr(_storage, "_LOCAL_ARCHIVE_DIR", str(d))
    return d


class TestListArchivedFiles:
    def test_date_prefix_nonexistent_returns_empty(self, archive_dir):
        result = list_archived_files(date_prefix="2099/12/31")
        assert result == {"files": [], "count": 0, "total": 0}

    def test_date_prefix_returns_files_in_range(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_120000.json").write_text('{"test": 1}')
        (node_dir / "detections_130000.json").write_text('{"test": 2}')

        result = list_archived_files(date_prefix="2025/06/21")
        assert result["count"] == 2
        assert result["total"] == 2
        assert len(result["files"]) == 2

    def test_date_prefix_node_filter(self, archive_dir):
        for node in ("node-A", "node-B"):
            nd = archive_dir / "2025" / "06" / "21" / node
            nd.mkdir(parents=True)
            (nd / "detections_120000.json").write_text('{"node": "' + node + '"}')

        result = list_archived_files(date_prefix="2025/06/21", node_id="node-A")
        assert result["count"] == 1
        assert result["total"] == 1
        assert "node-A" in result["files"][0]["key"]

    def test_date_prefix_sort_desc(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        older = node_dir / "detections_100000.json"
        newer = node_dir / "detections_110000.json"
        older.write_text('{"order": "old"}')
        newer.write_text('{"order": "new"}')

        # Force distinct mtimes: older gets mtime in the past
        old_mtime = 1_000_000.0
        new_mtime = 2_000_000.0
        os.utime(str(older), (old_mtime, old_mtime))
        os.utime(str(newer), (new_mtime, new_mtime))

        result = list_archived_files(date_prefix="2025/06/21", sort_desc=True)
        files = result["files"]
        assert len(files) == 2
        # Newest file (detections_110000) must come first by identity, not string comparison.
        assert "detections_110000" in files[0]["key"]
        assert "detections_100000" in files[1]["key"]

    def test_date_prefix_pagination_offset(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        for i in range(3):
            (node_dir / f"detections_10000{i}.json").write_text(f'{{"i": {i}}}')

        result = list_archived_files(date_prefix="2025/06/21", limit=2, offset=1)
        assert result["total"] == 3
        assert result["count"] == 2
        assert len(result["files"]) == 2

    def test_no_date_prefix_returns_files(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-X"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_080000.json").write_text('{"x": 1}')

        result = list_archived_files()
        keys = [f["key"] for f in result["files"]]
        assert any("node-X" in k for k in keys)

    def test_no_date_prefix_missing_base_returns_empty(self, tmp_path, monkeypatch):
        # _ensure_local_dir() creates the dir if absent, then the traversal finds
        # no files and returns an empty result.
        missing = str(tmp_path / "does_not_exist")
        monkeypatch.setattr(_storage, "_LOCAL_ARCHIVE_DIR", missing)

        result = list_archived_files()
        assert result["files"] == []
        assert result["count"] == 0
        assert result["total"] == 0

    def test_file_entry_has_required_fields(self, archive_dir):
        node_dir = archive_dir / "2025" / "06" / "21" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_120000.json").write_text('{"check": true}')

        result = list_archived_files(date_prefix="2025/06/21")
        assert result["count"] == 1
        entry = result["files"][0]
        assert "key" in entry
        assert "size_bytes" in entry
        assert "modified" in entry


# ── The first frame time ─────────────────────────────────────────────────────

_DAY = "2025/10/18"
_WRITTEN = datetime(2025, 10, 18, 23, 0, 0, tzinfo=timezone.utc)


def _write(base, node_id: str, *frame_ts_ms: int) -> str:
    """A detection file holding one frame per timestamp, in the order given; its key."""
    frames = [{"timestamp": ts, "delay": [1.0], "doppler": [2.0], "snr": [9.0]} for ts in frame_ts_ms]
    return write_detections_parquet(node_id=node_id, frames=frames, base_dir=base, write_ts=_WRITTEN)


def _first_frames(listing: dict) -> dict:
    return {f["key"]: f["first_frame_ms"] for f in listing["files"]}


@pytest.fixture
def footer_reads(monkeypatch):
    """The paths whose Parquet footer the listing reads."""
    reads = []
    real = _storage._footer_first_frame_ms

    def spy(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(_storage, "_footer_first_frame_ms", spy)
    return reads


class TestFirstFrame:
    @pytest.mark.parametrize("query", [{"date_prefix": _DAY}, {}], ids=["dated", "undated"])
    def test_it_is_the_earliest_frame_in_the_file(self, query, archive_dir):
        """Rows are not sorted by frame time: the earliest is neither the first nor the last."""
        key = _write(archive_dir, "node-A", 1760825647000, 1760825640000, 1760825699000)
        assert _first_frames(list_archived_files(**query)) == {key: 1760825640000}

    def test_a_legacy_json_file_has_none(self, archive_dir):
        node_dir = archive_dir / "2025" / "10" / "18" / "node-A"
        node_dir.mkdir(parents=True)
        (node_dir / "detections_221400.json").write_text('{"detections": [{"timestamp": 1760825640000}]}')
        assert _first_frames(list_archived_files(date_prefix=_DAY)) == {
            "2025/10/18/node-A/detections_221400.json": None
        }

    @pytest.mark.parametrize("damage", ["garbage", "truncated", "no statistics"])
    def test_a_file_whose_footer_cannot_say_has_none_and_the_rest_still_list(self, damage, archive_dir, caplog):
        caplog.set_level(logging.DEBUG, logger="services.storage")
        good = _write(archive_dir, "node-A", 1760825640000)
        bad = "year=2025/month=10/day=18/node_id=node-B/part-230000-0.parquet"
        (archive_dir / bad).parent.mkdir(parents=True)
        if damage == "garbage":
            (archive_dir / bad).write_bytes(b"not a parquet file " * 50)
        elif damage == "truncated":
            whole = (archive_dir / good).read_bytes()
            (archive_dir / bad).write_bytes(whole[: len(whole) // 2])
        else:
            table = pa.table({"frame_ts_ms": pa.array([1760825640000], pa.int64())})
            pq.write_table(table, archive_dir / bad, write_statistics=False)

        listing = list_archived_files(date_prefix=_DAY)
        assert _first_frames(listing) == {good: 1760825640000, bad: None}
        assert listing["total"] == 2
        # An unreadable footer is noted at debug, never above it.
        levels = {r.levelno for r in caplog.records if r.name == "services.storage"}
        assert levels == (set() if damage == "no statistics" else {logging.DEBUG})

    @pytest.mark.parametrize("query", [{"date_prefix": _DAY}, {}], ids=["dated", "undated"])
    def test_only_the_returned_page_is_read(self, query, archive_dir, footer_reads):
        for i in range(5):
            _write(archive_dir, f"node-{i}", 1760825640000 + i)
        listing = list_archived_files(limit=2, offset=1, **query)
        assert (listing["count"], listing["total"]) == (2, 5)
        assert sorted(footer_reads) == sorted(str(archive_dir / f["key"]) for f in listing["files"])

    def test_an_unchanged_file_is_read_once_and_a_rewritten_one_again(self, archive_dir, footer_reads):
        keys = [_write(archive_dir, f"node-{i}", 1760825640000) for i in range(3)]
        list_archived_files(date_prefix=_DAY)
        assert len(footer_reads) == 3
        footer_reads.clear()
        list_archived_files(date_prefix=_DAY)
        assert footer_reads == []

        # Replaced in place, as the archive backfill does: same key, new mtime and size.
        rewritten = archive_dir / keys[0]
        os.replace(archive_dir / _write(archive_dir, "node-0", 1760825650000, 1760825600000), rewritten)
        os.utime(rewritten, (1760830000, 1760830000))
        listing = list_archived_files(date_prefix=_DAY)
        assert footer_reads == [str(rewritten)]
        assert _first_frames(listing) == {keys[0]: 1760825600000, keys[1]: 1760825640000, keys[2]: 1760825640000}
