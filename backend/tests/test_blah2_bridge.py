"""Tests for the config-driven blah2 bridge."""

import json
import re
import time

import pytest

from core.runtime_config import default_source_path
from core.task_registry import TASK_EXPECTED_INTERVAL_S
from services.blah2_bridge import (
    CONFIG_FILENAME,
    Blah2ConfigError,
    _build_node,
    _convert_frame,
    config_file_path,
    load_nodes,
    task_key,
)
from services.tcp_handler import is_synthetic_node

MINIMAL = {
    "node_id": "n1",
    "detection_url": "https://example.test/api/detection",
    "rx_lat": 33.9,
    "rx_lon": -84.6,
    "tx_lat": 33.8,
    "tx_lon": -84.1,
    "fc_hz": 177_000_000,
}


async def _noop_register(_node):
    """Bypass registration: it touches shared node state the ordering tests do not exercise."""


def _write(tmp_path, payload):
    p = tmp_path / "blah2_nodes.json"
    p.write_text(json.dumps(payload))
    return p


# ── Shipped default ───────────────────────────────────────────────────────────


class TestShippedConfig:
    """The tracked config is a template: no real node, no real geometry.

    So these assert the shape a deployment's own file must keep, not values.
    The live geometry lives in the runtime copy or under BLAH2_NODES_FILE and
    is verified against the node's own ADS-B truth, not here.

    Read the tracked file directly, never config_file_path(): that prefers a
    runtime overlay, so on a developer machine these would test whatever is
    installed there.
    """

    @staticmethod
    def _shipped():
        return load_nodes(default_source_path(CONFIG_FILENAME))

    def test_default_config_loads(self):
        assert len(self._shipped()) == 2

    def test_nodes_are_real_not_synthetic(self):
        """Registered with is_synthetic=False, so they must not trip the prefix
        classifier that strips synthetic nodes from the public feed."""
        for node in self._shipped():
            assert is_synthetic_node(node.node_id) is False

    def test_the_template_carries_no_real_identity(self):
        """A leak here is a leak in the public repo and in every image built
        from it."""
        for node in self._shipped():
            assert not re.search(r"radar3a?[-.]retnode|ret[0-9a-f]{8}", node.node_id + node.detection_url)

    def test_co_located_nodes_do_not_share_an_illuminator(self):
        """Two receivers on one roof still need their own TX and fc: reusing
        the other's misplaces every target from that node."""
        a, b = (n.config for n in self._shipped())
        assert (a["rx_lat"], a["rx_lon"]) == (b["rx_lat"], b["rx_lon"])
        assert (a["tx_lat"], a["tx_lon"]) != (b["tx_lat"], b["tx_lon"])
        assert a["fc_hz"] != b["fc_hz"]

    def test_every_node_tx_differs_from_its_rx(self):
        """A bistatic pair with TX on top of RX has no baseline to solve against."""
        for node in self._shipped():
            c = node.config
            assert (c["tx_lat"], c["tx_lon"]) != (c["rx_lat"], c["rx_lon"]), node.node_id

    def test_registers_a_staleness_key_per_node(self):
        for node in self._shipped():
            assert task_key(node.node_id) in TASK_EXPECTED_INTERVAL_S


# ── Loading arbitrary configs ─────────────────────────────────────────────────


class TestLoadNodes:
    def test_loads_arbitrary_node_count(self, tmp_path):
        entries = []
        for i in range(5):
            e = dict(MINIMAL)
            e["node_id"] = f"n{i}"
            e["detection_url"] = f"https://host{i}.test/api/detection"
            entries.append(e)
        nodes = load_nodes(_write(tmp_path, {"nodes": entries}))
        assert [n.node_id for n in nodes] == [f"n{i}" for i in range(5)]

    def test_bare_list_is_accepted(self, tmp_path):
        assert len(load_nodes(_write(tmp_path, [MINIMAL]))) == 1

    def test_missing_file_yields_no_nodes(self, tmp_path):
        assert load_nodes(tmp_path / "nope.json") == []

    def test_malformed_json_yields_no_nodes(self, tmp_path):
        p = tmp_path / "blah2_nodes.json"
        p.write_text("{not json")
        assert load_nodes(p) == []

    def test_bad_entry_is_skipped_others_survive(self, tmp_path):
        """One broken node must not take the rest of the network off the air."""
        good = dict(MINIMAL, node_id="good", detection_url="https://g.test/api/detection")
        bad = dict(MINIMAL, node_id="bad", fc_hz="not-a-number")
        nodes = load_nodes(_write(tmp_path, {"nodes": [bad, good]}))
        assert [n.node_id for n in nodes] == ["good"]

    def test_duplicate_node_id_is_skipped(self, tmp_path):
        a = dict(MINIMAL, detection_url="https://a.test/api/detection")
        b = dict(MINIMAL, detection_url="https://b.test/api/detection")
        nodes = load_nodes(_write(tmp_path, {"nodes": [a, b]}))
        assert len(nodes) == 1

    def test_duplicate_url_is_skipped(self, tmp_path):
        a = dict(MINIMAL, node_id="a")
        b = dict(MINIMAL, node_id="b")
        nodes = load_nodes(_write(tmp_path, {"nodes": [a, b]}))
        assert [n.node_id for n in nodes] == ["a"]

    def test_env_override_wins(self, tmp_path, monkeypatch):
        p = _write(tmp_path, {"nodes": [MINIMAL]})
        monkeypatch.setenv("BLAH2_NODES_FILE", str(p))
        assert config_file_path() == p


# ── Entry validation ──────────────────────────────────────────────────────────


class TestBuildNode:
    def test_optional_fields_get_defaults(self):
        cfg = _build_node(MINIMAL).config
        assert cfg["fs_hz"] == 2_000_000
        assert cfg["beam_width_deg"] == 42.0
        assert cfg["max_range_km"] == 140

    def test_fc_and_fs_aliases_track_each_other(self):
        """The pipeline factory reads FC/Fs, analytics reads fc_hz/fs_hz."""
        cfg = _build_node(MINIMAL).config
        assert cfg["FC"] == cfg["fc_hz"]
        assert cfg["Fs"] == cfg["fs_hz"]

    def test_peer_is_the_hostname(self):
        assert _build_node(MINIMAL).peer == "example.test"

    @pytest.mark.parametrize(
        "mutation,field",
        [
            ({"node_id": None}, "node_id"),
            ({"detection_url": None}, "detection_url"),
            ({"fc_hz": None}, "fc_hz"),
            ({"rx_lat": None}, "rx_lat"),
        ],
    )
    def test_missing_required_field_rejected(self, mutation, field):
        with pytest.raises(Blah2ConfigError, match=field):
            _build_node({**MINIMAL, **mutation})

    @pytest.mark.parametrize("url", ["ftp://h/a", "h/a", "", "file:///etc/passwd"])
    def test_non_http_url_rejected(self, url):
        with pytest.raises(Blah2ConfigError, match="http"):
            _build_node({**MINIMAL, "detection_url": url})

    @pytest.mark.parametrize(
        "field,value",
        [
            ("rx_lat", 91),
            ("tx_lat", -91),
            ("rx_lon", 181),
            ("tx_lon", -181),
        ],
    )
    def test_out_of_range_coordinates_rejected(self, field, value):
        with pytest.raises(Blah2ConfigError, match="out of range"):
            _build_node({**MINIMAL, field: value})

    @pytest.mark.parametrize("field", ["fc_hz", "fs_hz"])
    def test_non_positive_frequency_rejected(self, field):
        with pytest.raises(Blah2ConfigError, match="positive"):
            _build_node({**MINIMAL, field: 0})

    def test_non_numeric_rejected(self):
        with pytest.raises(Blah2ConfigError, match="not a number"):
            _build_node({**MINIMAL, "rx_lon": "west"})

    def test_non_dict_entry_rejected(self):
        with pytest.raises(Blah2ConfigError, match="must be an object"):
            _build_node("radar3")


# ── Frame conversion ──────────────────────────────────────────────────────────


class TestConvertFrame:
    def _raw(self, ts_ms):
        return {
            "timestamp": ts_ms,
            "delay": [19.86, 18.27],
            "doppler": [-160.62, -111.43],
            "snr": [10.06, 5.81],
        }

    def test_tags_frame_with_its_own_node_id(self):
        """Frames from different nodes must stay attributable in the shared queue."""
        now_ms = int(time.time() * 1000)
        for node_id in ("example-node-a", "example-node-b"):
            assert _convert_frame(self._raw(now_ms), node_id)["_node_id"] == node_id

    def test_delay_converted_km_to_us(self):
        from config.constants import C_KM_US

        raw = self._raw(int(time.time() * 1000))
        frame = _convert_frame(raw, "n1")
        assert frame["delay"] == [d / C_KM_US for d in raw["delay"]]

    def test_stale_frame_rejected(self):
        assert _convert_frame(self._raw(1_000_000), "n1") is None

    def test_empty_frame_rejected(self):
        assert _convert_frame({"timestamp": 0, "delay": []}, "n1") is None


# ── Frame ordering ────────────────────────────────────────────────────────────


class _StopPolling(BaseException):
    """Ends the bridge's infinite loop; BaseException so its `except Exception` misses it."""


class TestFrameOrdering:
    """Only forward timestamps reach the queue.

    A repeat is a cached response and a lower one is a clock step backwards;
    either would hand the tracker a non-positive dt.  Nothing between the queue
    and `Tracker.process_frame` re-orders, so this guard is the only defence
    against it on the bridge's path.
    """

    async def _enqueued_timestamps(self, monkeypatch, timestamps):
        """Run the bridge over a scripted timestamp sequence; return what it queued."""
        import asyncio

        from core import state
        from services import blah2_bridge

        base_ms = int(time.time() * 1000)
        raw_frames = [
            {
                "timestamp": base_ms + offset_ms,
                "delay": [19.86],
                "doppler": [-160.62],
                "snr": [10.06],
            }
            for offset_ms in timestamps
        ]

        class _Response:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *_, **__):
                self._remaining = list(raw_frames)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def get(self, _url):
                if not self._remaining:
                    raise _StopPolling
                return _Response(self._remaining.pop(0))

        monkeypatch.setattr(blah2_bridge.httpx, "AsyncClient", _Client)
        monkeypatch.setattr(blah2_bridge, "_register_node", _noop_register)
        monkeypatch.setattr(blah2_bridge, "POLL_INTERVAL_S", 0)
        monkeypatch.setattr(state, "frame_queue", asyncio.Queue())

        node = _build_node(MINIMAL)
        with pytest.raises(_StopPolling):
            await blah2_bridge.blah2_bridge_task(node)

        queued = []
        while not state.frame_queue.empty():
            _node_id, frame = state.frame_queue.get_nowait()
            queued.append(frame["timestamp"] - base_ms)
        return queued

    async def test_first_frame_passes(self, monkeypatch):
        """`last_ts` starts at 0, so no special case is needed for the first frame."""
        assert await self._enqueued_timestamps(monkeypatch, [0]) == [0]

    async def test_repeat_dropped(self, monkeypatch):
        assert await self._enqueued_timestamps(monkeypatch, [0, 0, 0]) == [0]

    async def test_older_frame_dropped(self, monkeypatch):
        assert await self._enqueued_timestamps(monkeypatch, [1000, 500]) == [1000]

    async def test_newer_frame_after_older_still_passes(self, monkeypatch):
        """An out-of-order frame must not wedge the node against later good ones."""
        assert await self._enqueued_timestamps(monkeypatch, [1000, 500, 2000]) == [1000, 2000]


def test_an_omitted_altitude_stays_null():
    """No invented altitude on the way in.

    resolve_altitudes supplies the terrain figure at the geometry boundary, so
    a node that declares none must reach publication and the Parquet archive
    with a null: those rows are permanent, and a fabricated figure there cannot
    afterwards be told apart from a survey.
    """
    cfg = _build_node(MINIMAL).config

    assert cfg["rx_alt_ft"] is None
    assert cfg["tx_alt_ft"] is None
