"""Tests for detection archive storage module."""

import pytest

from services.node_config import canonical_config
from services.storage import archive_detections, list_archived_files, read_archived_file


class TestArchiveStorage:
    @pytest.fixture(autouse=True)
    def isolated_archive(self, monkeypatch, tmp_path):
        """Point the archive at a per-test directory.

        These tests used to write into the real backend/coverage_data/archive
        and delete every subdirectory of it afterwards. That is shared between
        concurrent suite runs, so one run's cleanup deletes another's parquet
        files mid-write, which surfaces as a FileNotFoundError from
        list_archived_files in whichever run loses. Two worktrees at once
        reproduces it; xdist workers would too. tmp_path is per test and pytest
        disposes of it, so nothing has to be torn down by hand. Matches
        test_storage_listing.py and test_parquet_writer.py, which already do
        this.
        """
        monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(tmp_path))

    def test_archive_returns_key(self):
        key = archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        assert isinstance(key, str) and "/" in key
        assert "test-storage-node" in key

    def test_a_legacy_spelled_node_archives_its_real_position(self):
        """The archive snapshots the canonical config, so the legacy flat
        lat/lon a node may still send is folded before it is written.

        Archive rows are permanent and not correctable once published, and a
        null here is indistinguishable from a node that genuinely declared no
        position. Reading an un-normalised config instead wrote nulls for a
        fully placed node, which is unrecoverable after the fact."""
        from core import state

        state.connected_nodes["test-legacy-node"] = {
            "config": canonical_config({"lat": 51.5, "lon": -0.12, "tx_lat": 51.6, "tx_lon": -0.2}),
            "status": "active",
        }
        try:
            archive_detections(
                "test-legacy-node",
                [{"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000}],
            )
            result = list_archived_files(node_id="test-legacy-node")
            data = read_archived_file(result["files"][0]["key"])
        finally:
            state.connected_nodes.pop("test-legacy-node", None)

        row = data["detections"][0]
        assert row["rx_lat"] is not None and row["rx_lon"] is not None
        # Fuzzed or not, the published receiver stays within a few km of truth.
        assert abs(row["rx_lat"] - 51.5) < 0.1

    def test_list_finds_archived(self):
        archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        result = list_archived_files(node_id="test-storage-node")
        files = result["files"]
        assert len(files) >= 1
        assert "key" in files[0]
        assert "size_bytes" in files[0]

    def test_read_archived_file(self):
        archive_detections(
            "test-storage-node",
            [
                {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
            ],
        )
        result = list_archived_files(node_id="test-storage-node")
        data = read_archived_file(result["files"][0]["key"])
        assert isinstance(data, dict)
        assert data.get("node_id") == "test-storage-node"
        assert isinstance(data.get("detections"), list)

    def test_path_traversal_blocked(self):
        assert read_archived_file("../../etc/passwd") is None
