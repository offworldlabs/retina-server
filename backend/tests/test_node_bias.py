"""Tests for services/node_bias.py — backend-computed residual accounting.

Covers the interface contract's unknown-key defaults, bias convergence and
its maturity bar, the lying-radar signature (a misfitting node's own trust
sinks), snapshot backward compatibility, and the analytics surfaces.
"""

import hashlib
import json
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import state
from main import app
from services import node_bias
from services.state_snapshot import restore_snapshot, save_snapshot

# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def now_ms():
    return int(time.time() * 1000)


@pytest.fixture()
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _feed(node_id, hex_, d_res, f_res, n, start_ms, step_ms=1000):
    """Record n residuals for (node, hex) starting at start_ms."""
    for i in range(n):
        node_bias.record_claim_residual(node_id, hex_, d_res, f_res, start_ms + i * step_ms)


# ── Unknown-key defaults (the contract functions) ─────────────────────────────


class TestContractDefaults:
    def test_unknown_node_bias_is_zero(self):
        assert node_bias.get_node_bias("never-seen") == (0.0, 0.0)

    def test_unknown_node_trust_is_neutral_prior(self):
        assert node_bias.get_node_trust("never-seen") == 0.5

    def test_record_accepts_unknown_keys_without_raising(self, now_ms):
        node_bias.record_claim_residual("new-node", "abc123", 1.0, 2.0, now_ms)
        assert "new-node" in state.node_analytics.trust_scores

    def test_record_drops_non_finite_and_empty_keys(self, now_ms):
        node_bias.record_claim_residual("n1", "h1", float("nan"), 0.0, now_ms)
        node_bias.record_claim_residual("n1", "h1", 0.0, float("inf"), now_ms)
        node_bias.record_claim_residual("", "h1", 0.0, 0.0, now_ms)
        node_bias.record_claim_residual("n1", "", 0.0, 0.0, now_ms)
        assert node_bias.node_summary("n1") is None
        assert "n1" not in state.node_analytics.trust_scores

    def test_trust_below_sample_bar_stays_at_prior(self, now_ms):
        """Below the M-of-N bar a single unlucky residual must not zero a
        brand-new node's solver weight."""
        node_bias.record_claim_residual("n2", "h1", 50.0, 100.0, now_ms)  # one misfit
        assert node_bias.get_node_trust("n2") == 0.5


# ── Bias estimator ────────────────────────────────────────────────────────────


class TestBiasEstimator:
    def test_converges_on_synthetic_biased_residuals(self, now_ms):
        """A node with a +3 us clock offset and -7 Hz oscillator drift shows
        those as systematic residuals; the EWMA must find them."""
        for i in range(40):
            d = 3.0 + 0.2 * ((i % 5) - 2)  # small deterministic noise
            f = -7.0 + 0.5 * ((i % 3) - 1)
            node_bias.record_claim_residual("biased", f"hex{i % 4}", d, f, now_ms + i * 1000)
        d_bias, f_bias = node_bias.get_node_bias("biased")
        assert d_bias == pytest.approx(3.0, abs=0.3)
        assert f_bias == pytest.approx(-7.0, abs=0.5)

    def test_maturity_bar_sample_count(self, now_ms):
        """11 samples (below the 12-sample bar) → no bias reported."""
        for i in range(11):
            node_bias.record_claim_residual("young", f"hex{i % 4}", 3.0, -7.0, now_ms + i * 1000)
        assert node_bias.get_node_bias("young") == (0.0, 0.0)
        for i in range(11, 13):
            node_bias.record_claim_residual("young", f"hex{i % 4}", 3.0, -7.0, now_ms + i * 1000)
        assert node_bias.get_node_bias("young") != (0.0, 0.0)

    def test_maturity_bar_distinct_hexes(self, now_ms):
        """20 samples but a single hex → no bias: one aircraft's bias is
        indistinguishable from that aircraft's transponder error."""
        _feed("onehex", "solo01", 3.0, -7.0, 20, now_ms)
        assert node_bias.get_node_bias("onehex") == (0.0, 0.0)
        summary = node_bias.node_summary("onehex")
        assert summary["mature"] is False
        assert summary["n_hexes"] == 1

    def test_maturity_bar_spread(self, now_ms):
        """High-variance residuals → the standard error exceeds the
        quarter-gate cap and the estimate abstains."""
        for i in range(20):
            d = 100.0 if i % 2 else -100.0  # mean ~0, huge spread
            node_bias.record_claim_residual("noisy", f"hex{i % 4}", d, 0.0, now_ms + i * 1000)
        assert node_bias.get_node_bias("noisy") == (0.0, 0.0)
        assert node_bias.node_summary("noisy")["mature"] is False


# ── Lying radar ───────────────────────────────────────────────────────────────


class TestLyingRadar:
    def test_misfitting_node_trust_sinks_while_peers_keep_theirs(self, now_ms):
        """One node misfitting across many hexes that its peers fit is the
        lying-radar signature: its own score sinks through the samples we
        feed, the peers stay high."""
        for i in range(15):
            h = f"aa{i % 5}"
            node_bias.record_claim_residual("liar", h, 12.0, 40.0, now_ms + i * 1000)
            node_bias.record_claim_residual("peer1", h, 0.5, 1.0, now_ms + i * 1000)
            node_bias.record_claim_residual("peer2", h, 0.4, -1.0, now_ms + i * 1000)
        assert node_bias.get_node_trust("liar") < 0.3
        assert node_bias.get_node_trust("peer1") > 0.7
        assert node_bias.get_node_trust("peer2") > 0.7


# ── Coexistence with the self-report route ───────────────────────────────────


class TestSelfReportCoexistence:
    def test_route_and_backend_samples_share_one_score(self, client, now_ms):
        """Both provenances land in the same TrustScoreState; the summary
        breaks them down."""
        client.post(
            "/api/radar/analytics/adsb-report",
            json={"node_id": "co-1", "predicted_delay": 100.0, "measured_delay": 100.5, "adsb_hex": "abc123"},
            headers={"X-API-Key": "test-key-abc123"},
        )
        _feed("co-1", "def456", 0.3, 0.5, 2, now_ms)
        ts = state.node_analytics.trust_scores["co-1"]
        assert len(ts.samples) == 3
        assert ts.summary()["samples_by_provenance"] == {"self_report": 1, "claim_residual": 2}

    def test_cross_validation_skips_backend_fed_samples(self, now_ms):
        """Claim residuals carry no position claim (lat/lon 0.0), so the
        external-truth cross-check must not read them as a >10 km mismatch."""
        from retina_analytics.reputation import NodeReputation

        from services.tasks.periodic import _cross_validate_adsb_reports

        _feed("cv-1", "cafe01", 0.3, 0.5, 3, now_ms)
        rep = NodeReputation(node_id="cv-1")
        state.node_analytics.reputations["cv-1"] = rep
        state.external_adsb_cache["cafe01"] = {"lat": 51.5, "lon": -0.1, "alt_m": 10000}
        _cross_validate_adsb_reports()
        assert rep.reputation == 1.0
        assert rep.penalties == []


# ── Snapshot persistence ──────────────────────────────────────────────────────


class TestSnapshotCompat:
    def test_old_format_snapshot_still_loads(self, tmp_path):
        """A snapshot written before provenance existed: trust samples
        without the field.  It must restore unchanged, defaulting provenance
        to self_report."""
        payload = json.dumps(
            {
                "saved_at": time.time(),
                "trust_scores": {
                    "old-node": {
                        "node_id": "old-node",
                        "samples": [
                            {
                                "timestamp_ms": 1000,
                                "predicted_delay": 10.0,
                                "predicted_doppler": 50.0,
                                "measured_delay": 10.5,
                                "measured_doppler": 51.0,
                                "adsb_hex": "abc123",
                                "adsb_lat": 33.9,
                                "adsb_lon": -84.6,
                            }
                        ],
                        "max_samples": 500,
                        "delay_threshold_us": 5.0,
                        "doppler_threshold_hz": 20.0,
                    }
                },
            }
        )
        snap_path = str(tmp_path / "old.json")
        with open(snap_path, "w") as f:
            json.dump({"schema": 2, "sha256": hashlib.sha256(payload.encode()).hexdigest(), "payload": payload}, f)

        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            assert restore_snapshot() is True

        restored = state.node_analytics.trust_scores["old-node"]
        assert len(restored.samples) == 1
        assert restored.samples[0].provenance == "self_report"
        assert restored.score == 1.0

    def test_backend_fed_samples_round_trip_with_provenance(self, tmp_path, now_ms):
        _feed("rt-node", "feed01", 0.3, 0.5, 3, now_ms)
        snap_path = str(tmp_path / "prov.json")
        with patch("services.state_snapshot._SNAPSHOT_PATH", snap_path):
            save_snapshot()
            state.node_analytics.trust_scores.clear()
            restore_snapshot()
        restored = state.node_analytics.trust_scores["rt-node"]
        assert [s.provenance for s in restored.samples] == ["claim_residual"] * 3


# ── Analytics surfaces ────────────────────────────────────────────────────────


class TestSurfacing:
    def test_node_analytics_includes_bias_block(self, client, now_ms):
        for i in range(15):
            node_bias.record_claim_residual("surf-1", f"hex{i % 4}", 3.0, -7.0, now_ms + i * 1000)
        r = client.get("/api/radar/analytics/surf-1")
        assert r.status_code == 200
        bias = r.json()["node_bias"]
        assert bias["mature"] is True
        assert bias["bias_delay_us"] == pytest.approx(3.0, abs=0.3)
        assert bias["n_hexes"] == 4

    def test_node_analytics_omits_bias_block_when_unknown(self, client):
        state.node_analytics.register_node("surf-2", {"name": "Test"})
        r = client.get("/api/radar/analytics/surf-2")
        assert r.status_code == 200
        assert "node_bias" not in r.json()
