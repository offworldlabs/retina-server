"""Tests for services/state_snapshot.py — save/restore of in-memory state."""

import copy
import hashlib
import json
import logging
import time
from collections import deque
from unittest.mock import patch

import pytest
from retina_analytics.reputation import NodeReputation
from retina_analytics.trust import AdsReportEntry, TrustScoreState
from retina_custody.models import IQCapturePackage, NodeIdentity

from core import state
from services.custody import ingest_chain_entry
from services.state_snapshot import restore_snapshot, save_snapshot
from tests.custody_helpers import ChainNode

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_trust_state(node_id="node-1", n_samples=3):
    ts = TrustScoreState(node_id=node_id)
    for i in range(n_samples):
        ts.add_sample(
            AdsReportEntry(
                timestamp_ms=1000 * i,
                predicted_delay=10.0 + i,
                predicted_doppler=50.0,
                measured_delay=10.5 + i,
                measured_doppler=51.0,
                adsb_hex=f"hex{i:03d}",
                adsb_lat=33.9,
                adsb_lon=-84.6,
            )
        )
    return ts


def _make_reputation(node_id="node-1", reputation=0.8, blocked=False):
    rep = NodeReputation(node_id=node_id, reputation=reputation, blocked=blocked)
    if blocked:
        rep.block_reason = "test block"
    return rep


# ── Round-trip: save → restore ────────────────────────────────────────────────


class TestSnapshotRoundTrip:
    """Verify that save_snapshot → restore_snapshot preserves all data types."""

    def test_trust_scores_survive_round_trip(self, tmp_path):
        from core import state

        ts = _make_trust_state("node-42", n_samples=5)
        state.node_analytics.trust_scores["node-42"] = ts

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

            # Wipe state
            state.node_analytics.trust_scores.clear()
            assert "node-42" not in state.node_analytics.trust_scores

            ok = restore_snapshot()

        assert ok is True
        restored = state.node_analytics.trust_scores["node-42"]
        assert restored.node_id == "node-42"
        assert len(restored.samples) == 5
        assert restored.samples[0].adsb_hex == "hex000"
        assert restored.score == ts.score

        # Cleanup
        state.node_analytics.trust_scores.pop("node-42", None)

    @pytest.mark.usefixtures("penalties_on")
    def test_reputations_survive_round_trip(self, tmp_path):
        from core import state

        rep = _make_reputation("node-7", reputation=0.65, blocked=True)
        rep.apply_penalty(0.1, "test penalty")
        state.node_analytics.reputations["node-7"] = rep

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.node_analytics.reputations.clear()
            restore_snapshot()

        restored = state.node_analytics.reputations["node-7"]
        assert restored.node_id == "node-7"
        assert restored.blocked is True
        assert len(restored.penalties) >= 1

        state.node_analytics.reputations.pop("node-7", None)

    def test_node_metrics_and_the_server_clock_survive_round_trip(self, tmp_path):
        from retina_analytics.availability import MinuteRing
        from retina_analytics.metrics import NodeMetrics

        from core import state

        now = time.time()
        metrics = NodeMetrics(node_id="node-9", first_seen=now - 7200)
        up = MinuteRing()
        for ago in range(1, 61):
            up.mark(int((now - ago * 60) // 60))
            if ago <= 45:
                metrics.record_frame({"delay": [1.0, 2.0], "snr": [9.0, 11.0]}, now=now - ago * 60)
        expected = metrics.summary(up, now=now)
        assert (expected["availability_7d"], expected["total_detections"]) == (0.75, 90)

        saved_up = state.node_analytics.server_minutes
        state.node_analytics.metrics["node-9"] = metrics
        state.node_analytics.server_minutes = up
        snap_path = str(tmp_path / "snap.json")
        try:
            with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
                save_snapshot()
                state.node_analytics.metrics.pop("node-9")
                state.node_analytics.server_minutes = MinuteRing()
                restore_snapshot()
            restored = state.node_analytics.metrics["node-9"]
            assert restored.summary(state.node_analytics.server_minutes, now=now) == expected
        finally:
            state.node_analytics.metrics.pop("node-9", None)
            state.node_analytics.server_minutes = saved_up

    def test_accuracy_samples_survive_round_trip(self, tmp_path):
        from core import state

        orig_samples = deque(state.accuracy_samples)
        state.accuracy_samples = deque(
            [
                {"error_km": 1.5, "position_source": "solver"},
                {"error_km": 0.3, "position_source": "adsb"},
            ],
            maxlen=state.ACCURACY_MAX_SAMPLES,
        )

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.accuracy_samples.clear()
            restore_snapshot()

        assert len(state.accuracy_samples) == 2
        assert state.accuracy_samples[0]["error_km"] == 1.5

        state.accuracy_samples = orig_samples

    def test_chain_entries_survive_round_trip(self, tmp_path):
        from core import state

        orig = dict(state.chain_entries)
        # Windowed: restore drops rows of the retired hourly chain.
        state.chain_entries["node-99"] = [{"ts": 100, "hash": "abc", "window_end": "2026-10-19T14:15:00Z"}]

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.chain_entries.clear()
            restore_snapshot()

        assert "node-99" in state.chain_entries
        assert state.chain_entries["node-99"][0]["hash"] == "abc"

        state.chain_entries.clear()
        state.chain_entries.update(orig)

    def test_anomaly_log_survives_round_trip(self, tmp_path):
        from core import state

        orig = list(state.anomaly_log)
        state.anomaly_log = [{"ts": 1234, "type": "spoof", "hex": "aaa"}]

        snap_path = str(tmp_path / "snap.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.anomaly_log = []
            restore_snapshot()

        assert len(state.anomaly_log) == 1
        assert state.anomaly_log[0]["type"] == "spoof"

        state.anomaly_log = orig


# ── Edge cases ────────────────────────────────────────────────────────────────


class TestSnapshotEdgeCases:
    def test_restore_returns_false_when_no_file(self, tmp_path):
        snap_path = str(tmp_path / "nonexistent.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is False

    def test_restore_handles_corrupted_json(self, tmp_path):
        snap_path = str(tmp_path / "corrupt.json")
        with open(snap_path, "w") as f:
            f.write("{this is not valid json!!!")

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is False

    def test_restore_handles_empty_snapshot(self, tmp_path):
        snap_path = str(tmp_path / "empty.json")
        with open(snap_path, "w") as f:
            json.dump({"saved_at": time.time()}, f)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is True  # succeeds, just nothing to restore

    def test_save_uses_atomic_write(self, tmp_path):
        """Save writes to .tmp then replaces — if we crash mid-write, old file survives."""

        snap_path = str(tmp_path / "snap.json")
        # Write a known-good snapshot first
        with open(snap_path, "w") as f:
            json.dump({"saved_at": 1.0, "trust_scores": {}}, f)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

        # Verify the snapshot is a valid schema-2 envelope (checksum travels
        # inside the file so payload+checksum replace atomically).
        with open(snap_path) as f:
            envelope = json.load(f)
        assert envelope["schema"] == 2
        data = json.loads(envelope["payload"])
        assert "saved_at" in data
        assert data["saved_at"] > 0

    def test_full_round_trip_with_all_fields(self, tmp_path):
        """Integration: populate every field, save, wipe, restore, verify."""
        from core import state

        # Save originals
        orig_trust = dict(state.node_analytics.trust_scores)
        orig_reps = dict(state.node_analytics.reputations)
        orig_acc = deque(state.accuracy_samples)
        orig_chain = dict(state.chain_entries)
        orig_iq = dict(state.iq_commitments)
        orig_anom = list(state.anomaly_log)

        # Populate state
        state.node_analytics.trust_scores["rt-1"] = _make_trust_state("rt-1", 2)
        state.node_analytics.reputations["rt-1"] = _make_reputation("rt-1", 0.9)
        state.accuracy_samples = deque([{"error_km": 0.5, "position_source": "mn"}], maxlen=state.ACCURACY_MAX_SAMPLES)
        state.chain_entries["rt-1"] = [{"ts": 1, "hash": "h1", "window_end": "2026-10-19T14:15:00Z"}]
        state.iq_commitments["rt-1"] = [{"ts": 2, "digest": "d1"}]
        state.anomaly_log = [{"ts": 3, "msg": "test"}]

        snap_path = str(tmp_path / "full.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()

            # Wipe everything
            state.node_analytics.trust_scores.clear()
            state.node_analytics.reputations.clear()
            state.accuracy_samples.clear()
            state.chain_entries.clear()
            state.iq_commitments.clear()
            state.anomaly_log = []

            restore_snapshot()

        assert "rt-1" in state.node_analytics.trust_scores
        assert "rt-1" in state.node_analytics.reputations
        assert len(state.accuracy_samples) == 1
        assert "rt-1" in state.chain_entries
        assert "rt-1" in state.iq_commitments
        assert len(state.anomaly_log) == 1

        # Restore originals
        state.node_analytics.trust_scores.clear()
        state.node_analytics.trust_scores.update(orig_trust)
        state.node_analytics.reputations.clear()
        state.node_analytics.reputations.update(orig_reps)
        state.accuracy_samples = orig_acc
        state.chain_entries.clear()
        state.chain_entries.update(orig_chain)
        state.iq_commitments.clear()
        state.iq_commitments.update(orig_iq)
        state.anomaly_log = orig_anom


# ── Custody data the removed seeder fabricated ────────────────────────────────

_SEEDED = ("fleet-node-0001", "fleet-node-0002")
_REAL = "field-node-a1"
_MIXED = "field-node-b2"  # seeded first, then registered a key and sent windows
_MIXED_HOURS = 5
_DROP_LOG = "Dropped seeded custody data from the snapshot"


def _register(tmp_path, node_id: str) -> ChainNode:
    """A node with its own key, registered as the custody routes register one."""
    node = ChainNode(tmp_path, node_id)
    body = node.register_body
    state.node_identities[node_id] = NodeIdentity(
        node_id=node_id,
        public_key_pem=body["public_key_pem"],
        public_key_fingerprint=body["fingerprint"],
        serial_number=body["serial_number"],
        signing_mode=body["signing_mode"],
        registered_at="2026-10-19T12:00:00+00:00",
    )
    return node


def _send_windows(node: ChainNode, n: int) -> None:
    for _ in range(n):
        entry, batch = node.next_window()
        assert ingest_chain_entry(node.node_id, entry, batch)["verified"] is True


def _real_iq(node_id: str, n: int, **extra) -> dict:
    """A capture commitment as a node sends one, numbered like the seeder's."""
    capture = IQCapturePackage(
        node_id=node_id,
        capture_id=f"iq-{node_id[-8:]}-{n:03d}",
        window_start_ms=1_800_000_000_000 + n,
        window_end_ms=1_800_000_001_000 + n,
        iq_hash=hashlib.sha256(f"raw iq {node_id} {n}".encode()).hexdigest(),
        signature="30" * 36,
        signing_mode="software",
    )
    return capture.to_dict() | extra | {"_received_at": "2026-10-19T12:05:00+00:00"}


def _seed_as_ensure_custody_data(node_id: str, hours: int) -> None:
    """What `_ensure_custody_data` left for a connected node after `hours` refreshes."""
    now_iso = "2026-10-12T00:00:04.123456+00:00"
    if node_id not in state.node_identities:
        state.node_identities[node_id] = NodeIdentity(
            node_id=node_id,
            public_key_pem=f"-----SIM-KEY-{node_id[-8:]}-----",
            public_key_fingerprint=hashlib.sha256(node_id.encode()).hexdigest()[:16],
            serial_number=f"SIM-{node_id[-6:]}",
            signing_mode="software",
            registered_at=now_iso,
        )
    entries = state.chain_entries.setdefault(node_id, [])
    for h in range(hours):
        hour_utc = f"2026-10-{12 + h // 24:02d}T{h % 24:02d}:00:00Z"
        prev_hash = entries[-1].get("entry_hash", "0" * 64) if entries else "0" * 64
        content_hash = hashlib.sha256(f"{node_id}:{hour_utc}".encode()).hexdigest()
        entries.append(
            {
                "node_id": node_id,
                "hour_utc": hour_utc,
                "prev_hash": prev_hash,
                "content_hash": content_hash,
                "entry_hash": hashlib.sha256(f"{prev_hash}:{content_hash}".encode()).hexdigest(),
                "_verified": True,
                "_received_at": now_iso,
            }
        )
    if not state.iq_commitments.get(node_id):
        state.iq_commitments[node_id] = [
            {
                "node_id": node_id,
                "capture_id": f"iq-{node_id[-8:]}-001",
                "sha256": hashlib.sha256(f"iq:{node_id}".encode()).hexdigest(),
                "_received_at": now_iso,
            }
        ]


def _add_real_node(tmp_path) -> None:
    """A registered node with verified windows and two captures.

    Its first capture is numbered like the seeder's and carries a sha256 of
    its own, so only the seeder's exact pair of markers can match it.
    """
    _send_windows(_register(tmp_path, _REAL), 3)
    state.iq_commitments[_REAL] = [
        _real_iq(_REAL, 1, sha256=hashlib.sha256(b"capture bytes").hexdigest()),
        _real_iq(_REAL, 2),
    ]


def _add_seeded_nodes(tmp_path) -> None:
    """Two nodes only the seeder ever touched, and one it seeded before the node
    registered a real key and sent windows and a capture of its own."""
    for nid in _SEEDED:
        _seed_as_ensure_custody_data(nid, 168)
    _seed_as_ensure_custody_data(_MIXED, _MIXED_HOURS)
    _send_windows(_register(tmp_path, _MIXED), 2)
    state.iq_commitments[_MIXED].append(_real_iq(_MIXED, 2))


def _save_wipe_restore(tmp_path) -> dict:
    """Save, wipe custody state, restore. Returns the custody state as saved."""
    saved = {
        "node_identities": {nid: ident.to_dict() for nid, ident in state.node_identities.items()},
        "chain_entries": copy.deepcopy(state.chain_entries),
        "iq_commitments": copy.deepcopy(state.iq_commitments),
    }
    with patch("services.state_snapshot._SNAPSHOT_PATH", str(tmp_path / "custody.json")):
        save_snapshot()
        state.node_identities.clear()
        state.chain_entries.clear()
        state.iq_commitments.clear()
        assert restore_snapshot() is True
    return saved


def _restored() -> dict:
    return {
        "node_identities": {nid: ident.to_dict() for nid, ident in state.node_identities.items()},
        "chain_entries": state.chain_entries,
        "iq_commitments": state.iq_commitments,
    }


class TestSeededCustodyDropped:
    """`_ensure_custody_data` fabricated an identity, an hourly chain and an IQ
    commitment for every connected node until it was removed.  Snapshots
    written before then carry them, so deployed servers kept showing fake
    custody chains across every restart."""

    def test_only_the_real_custody_data_is_restored(self, tmp_path):
        _add_real_node(tmp_path)
        _add_seeded_nodes(tmp_path)

        saved = _save_wipe_restore(tmp_path)

        assert len(saved["chain_entries"][_SEEDED[0]]) == 168
        assert _restored() == {
            "node_identities": {nid: saved["node_identities"][nid] for nid in (_REAL, _MIXED)},
            "chain_entries": {
                _REAL: saved["chain_entries"][_REAL],
                _MIXED: saved["chain_entries"][_MIXED][_MIXED_HOURS:],
            },
            "iq_commitments": {
                _REAL: saved["iq_commitments"][_REAL],
                _MIXED: saved["iq_commitments"][_MIXED][1:],
            },
        }
        assert all("window_end" in row for rows in state.chain_entries.values() for row in rows)

    def test_the_dropped_counts_are_logged(self, tmp_path, caplog):
        _add_real_node(tmp_path)
        _add_seeded_nodes(tmp_path)

        with caplog.at_level(logging.INFO):
            _save_wipe_restore(tmp_path)

        drops = [r for r in caplog.records if r.getMessage().startswith(_DROP_LOG)]
        assert [(r.levelno, r.getMessage()) for r in drops] == [
            (
                logging.INFO,
                f"{_DROP_LOG}: 2 identities, {2 * 168 + _MIXED_HOURS} hourly chain rows, 3 IQ commitments",
            )
        ]

    def test_a_snapshot_without_seeded_data_restores_unchanged(self, tmp_path, caplog):
        _add_real_node(tmp_path)
        # Registered, nothing sent yet: an empty list is the node's, not the seeder's.
        _register(tmp_path, "field-node-c3")
        state.chain_entries["field-node-c3"] = []
        state.iq_commitments["field-node-c3"] = []

        with caplog.at_level(logging.INFO):
            saved = _save_wipe_restore(tmp_path)

        assert _restored() == saved
        assert not [r for r in caplog.records if r.getMessage().startswith(_DROP_LOG)]


# ── Simulation physics config ─────────────────────────────────────────────────


class TestSimulationConfigPersistence:
    """The Physics tab's config must survive a rebuild.

    Before it was snapshotted, `docker compose up -d --build` reverted the dict
    to boot state and — because `_updated_at` is stamped at import — the
    fleet's 5 s poll then pushed those defaults into the running world.
    """

    def _save_then_wipe(self, tmp_path, snap_name="sim.json"):
        """Save current state, then reset the config to boot defaults."""
        from core import state

        snap_path = str(tmp_path / snap_name)
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
        state.simulation_config.clear()
        state.simulation_config.update(state._SIMULATION_CONFIG_DEFAULTS)
        return snap_path

    def test_fractions_and_counts_survive_round_trip(self, tmp_path):
        from core import state

        state.simulation_config.update(
            {
                "frac_anomalous": 0.10,
                "frac_drone": 0.25,
                "frac_dark": 0.30,
                "min_aircraft": 64,
                "max_aircraft": 80,
                "_updated_at": 1234.5,
            }
        )
        snap_path = self._save_then_wipe(tmp_path)

        assert state.simulation_config.get("min_aircraft") is None  # wiped

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is True

        assert state.simulation_config["frac_anomalous"] == 0.10
        assert state.simulation_config["frac_drone"] == 0.25
        assert state.simulation_config["frac_dark"] == 0.30
        assert state.simulation_config["min_aircraft"] == 64
        assert state.simulation_config["max_aircraft"] == 80

    def test_updated_at_restored_verbatim_not_restamped(self, tmp_path):
        """The fleet applies config only when the stamp strictly exceeds its
        last-seen one.  Re-stamping to now() would force a pointless re-apply
        on a fleet that never restarted, and would read as an operator edit to
        the UI's drift detection."""
        from core import state

        state.simulation_config.update({"frac_drone": 0.2, "_updated_at": 999.0})
        snap_path = self._save_then_wipe(tmp_path)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            restore_snapshot()

        assert state.simulation_config["_updated_at"] == 999.0

    def test_scene_keys_survive_round_trip(self, tmp_path):
        from core import state

        state.simulation_config.update(
            {
                "n_nodes": 48,
                "dual_fraction": 0.25,
                "max_range_km": 120,
            }
        )
        snap_path = self._save_then_wipe(tmp_path)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            restore_snapshot()

        assert state.simulation_config["n_nodes"] == 48
        assert state.simulation_config["dual_fraction"] == 0.25
        assert state.simulation_config["max_range_km"] == 120

    def test_retired_keys_are_not_resurrected(self, tmp_path):
        """A key dropped from the schema must not come back via an old file."""
        from core import state

        state.simulation_config["frac_zeppelin"] = 0.99
        snap_path = self._save_then_wipe(tmp_path)
        state.simulation_config.pop("frac_zeppelin", None)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            restore_snapshot()

        assert "frac_zeppelin" not in state.simulation_config

    def test_env_change_since_snapshot_beats_saved_config(self, tmp_path):
        """Editing SIM_FRAC_* and redeploying is a deliberate change of intent
        — it must outrank a stale runtime tweak."""
        from core import state

        state.simulation_config.update({"frac_drone": 0.40})
        with patch.object(
            state, "_SIMULATION_ENV_BASELINE", {"frac_anomalous": 0.0, "frac_drone": 0.0, "frac_dark": 0.15}
        ):
            snap_path = self._save_then_wipe(tmp_path)

        # Deploy changes the intended mix.
        deployed = {"frac_anomalous": 0.0, "frac_drone": 0.0, "frac_dark": 0.50}
        with patch.object(state, "_SIMULATION_ENV_BASELINE", deployed):
            state.simulation_config.update(deployed)
            with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
                restore_snapshot()

        assert state.simulation_config["frac_drone"] == 0.0
        assert state.simulation_config["frac_dark"] == 0.50

    def test_unchanged_env_lets_runtime_put_win(self, tmp_path):
        from core import state

        baseline = {"frac_anomalous": 0.0, "frac_drone": 0.0, "frac_dark": 0.15}
        with patch.object(state, "_SIMULATION_ENV_BASELINE", baseline):
            state.simulation_config.update({"frac_drone": 0.40})
            snap_path = self._save_then_wipe(tmp_path)
            with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
                restore_snapshot()

        assert state.simulation_config["frac_drone"] == 0.40

    def test_pre_env_snapshot_restores_when_env_is_default(self, tmp_path):
        """A snapshot written before env seeding existed carries no baseline;
        its effective baseline was the hardcoded fallbacks."""
        from core import state

        snap_path = str(tmp_path / "legacy.json")
        payload = json.dumps(
            {
                "saved_at": time.time(),
                "simulation_config": {"frac_drone": 0.35, "_updated_at": 42.0},
            }
        )
        with open(snap_path, "w") as f:
            json.dump({"schema": 2, "sha256": hashlib.sha256(payload.encode()).hexdigest(), "payload": payload}, f)

        with patch.object(state, "_SIMULATION_ENV_BASELINE", dict(state._SIM_FRAC_FALLBACKS)):
            with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
                restore_snapshot()

        assert state.simulation_config["frac_drone"] == 0.35

    def test_pre_env_snapshot_discarded_when_env_overrides(self, tmp_path):
        from core import state

        snap_path = str(tmp_path / "legacy2.json")
        payload = json.dumps(
            {
                "saved_at": time.time(),
                "simulation_config": {"frac_drone": 0.35, "_updated_at": 42.0},
            }
        )
        with open(snap_path, "w") as f:
            json.dump({"schema": 2, "sha256": hashlib.sha256(payload.encode()).hexdigest(), "payload": payload}, f)

        deployed = dict(state._SIM_FRAC_FALLBACKS, frac_drone=0.05)
        with patch.object(state, "_SIMULATION_ENV_BASELINE", deployed):
            state.simulation_config.update(deployed)
            with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
                restore_snapshot()

        assert state.simulation_config["frac_drone"] == 0.05


class TestSimulationEnvSeeding:
    """SIM_FRAC_* lets compose own the intended scene across rebuilds."""

    def test_env_values_are_read(self):
        from core import state

        with patch.dict("os.environ", {"SIM_FRAC_DRONE": "0.3", "SIM_FRAC_DARK": "0.2"}):
            seeded = state._seed_sim_fracs_from_env()

        assert seeded["frac_drone"] == 0.3
        assert seeded["frac_dark"] == 0.2
        assert seeded["frac_anomalous"] == 0.0  # unset → fallback

    def test_unset_env_uses_fallbacks(self):
        from core import state

        with patch.dict("os.environ", {}, clear=True):
            assert state._seed_sim_fracs_from_env() == state._SIM_FRAC_FALLBACKS

    def test_garbage_value_falls_back_without_raising(self):
        from core import state

        with patch.dict("os.environ", {"SIM_FRAC_DARK": "not-a-number"}):
            seeded = state._seed_sim_fracs_from_env()

        assert seeded["frac_dark"] == state._SIM_FRAC_FALLBACKS["frac_dark"]

    def test_out_of_range_value_falls_back(self):
        from core import state

        with patch.dict("os.environ", {"SIM_FRAC_DARK": "1.5"}):
            seeded = state._seed_sim_fracs_from_env()

        assert seeded["frac_dark"] == state._SIM_FRAC_FALLBACKS["frac_dark"]

    def test_over_budget_mix_is_refused_whole(self):
        """Commercial traffic is the remainder, so a >100% mix would spawn
        against a negative share — refuse all three rather than half-apply."""
        from core import state

        with patch.dict(
            "os.environ",
            {
                "SIM_FRAC_ANOMALOUS": "0.5",
                "SIM_FRAC_DRONE": "0.4",
                "SIM_FRAC_DARK": "0.4",
            },
        ):
            assert state._seed_sim_fracs_from_env() == state._SIM_FRAC_FALLBACKS
