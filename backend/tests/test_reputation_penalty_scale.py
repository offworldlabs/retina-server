"""REPUTATION_PENALTY_SCALE: no node can be blocked while the scale is 0.

The switch exists because one out-of-threshold trust sample was enough to
block a real mirrored node on the test droplet permanently: the evaluator acts
on any node with a sample, a single bad one scores 0.0, and 0.15 a pass at
60 s crosses the 0.2 block threshold in six minutes — after which every frame
that node sends is dropped and the block is persisted across restarts.

Three things are pinned here, and the third is the one that is easy to get
wrong: the switch stops NEW penalties, it does not unblock what a snapshot
already carries.  That is backend/scripts/unblock_nodes.py's job, and a switch
that quietly did it instead would hide from the operator which nodes had been
blocked and why.
"""

import time

import pytest
from retina_analytics.reputation import NodeReputation, set_penalty_scale
from retina_analytics.trust import AdsReportEntry

from core import state
from services.tasks.periodic import _cross_validate_adsb_reports

_NODE = "penalty-scale-node"
_HEX = "beef01"
# ~100 km apart: far past the 10 km mismatch bar, so nothing but the scale
# can be what keeps the penalty from landing.
_TRUTH = (51.5, -0.1)
_CLAIM = (51.5, 1.34)


@pytest.fixture(autouse=True)
def _clean():
    state.external_adsb_cache.clear()
    state.node_analytics.trust_scores.pop(_NODE, None)
    state.node_analytics.reputations.pop(_NODE, None)
    yield
    state.external_adsb_cache.clear()
    state.node_analytics.trust_scores.pop(_NODE, None)
    state.node_analytics.reputations.pop(_NODE, None)


def _seed_mismatch():
    """One fresh self-reported fix 100 km from an equally fresh external truth."""
    now = time.time()
    state.node_analytics.record_adsb_correlation(
        _NODE,
        AdsReportEntry(
            timestamp_ms=int(now * 1000),
            predicted_delay=100.0,
            predicted_doppler=0.0,
            measured_delay=100.5,
            measured_doppler=0.0,
            adsb_hex=_HEX,
            adsb_lat=_CLAIM[0],
            adsb_lon=_CLAIM[1],
        ),
    )
    state.external_adsb_cache[_HEX] = {
        "lat": _TRUTH[0],
        "lon": _TRUTH[1],
        "alt_m": 10000.0,
        "last_seen_ms": int(now * 1000),
    }
    rep = NodeReputation(node_id=_NODE)
    state.node_analytics.reputations[_NODE] = rep
    return rep


# ── (a) The deployed default records nothing ─────────────────────────────────


class TestDefaultScaleRecordsNoPenalty:
    def test_a_100km_mismatch_costs_nothing(self):
        """core/state.py sets the scale from the environment at import, and
        the suite runs with it unset — so this is the deployed configuration,
        not a fixture's idea of one."""
        rep = _seed_mismatch()
        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0
        assert rep.penalties == []
        assert rep.blocked is False

    def test_the_same_mismatch_does_land_with_penalties_on(self, penalties_on):
        """The control: without this the test above would pass just as well if
        the gates had silently stopped finding the mismatch at all."""
        rep = _seed_mismatch()
        _cross_validate_adsb_reports()

        assert rep.reputation < 1.0
        assert len(rep.penalties) == 1

    def test_rewards_still_work_at_zero_scale(self):
        """Only downrating is switched off — a node must still be able to
        climb back, or the switch would freeze every reputation where it sits."""
        rep = NodeReputation(node_id="reward-node", reputation=0.5)
        rep.evaluate_trust(0.9)
        assert rep.reputation > 0.5


# ── (b) Parsing the env var ──────────────────────────────────────────────────


class TestPenaltyScaleParsing:
    """Straight at the helper: importing config.constants fresh per case would
    mean reloading a module half the backend holds references into."""

    def test_unset_is_zero(self):
        from config.constants import _parse_penalty_scale

        assert _parse_penalty_scale(None) == 0.0
        assert _parse_penalty_scale("") == 0.0
        assert _parse_penalty_scale("   ") == 0.0

    def test_one_restores_the_historical_behaviour(self):
        from config.constants import _parse_penalty_scale

        assert _parse_penalty_scale("1") == 1.0
        assert _parse_penalty_scale("0.5") == 0.5
        assert _parse_penalty_scale("0") == 0.0

    @pytest.mark.parametrize("raw", ["-1", "nan", "-inf", "banana", "1,0"])
    def test_a_value_that_is_not_a_scale_falls_back_to_zero(self, raw, caplog):
        """Never the other way round: an unparseable gate must not read as
        "penalties on", which is the setting that blocks nodes."""
        from config.constants import _parse_penalty_scale

        with caplog.at_level("WARNING"):
            assert _parse_penalty_scale(raw) == 0.0
        assert any("REPUTATION_PENALTY_SCALE" in r.getMessage() for r in caplog.records)

    def test_infinity_is_refused(self):
        from config.constants import _parse_penalty_scale

        assert _parse_penalty_scale("inf") == 0.0

    def test_the_deployed_default_is_zero(self):
        from config.constants import REPUTATION_PENALTY_SCALE

        assert REPUTATION_PENALTY_SCALE == 0.0
        assert NodeReputation.penalty_scale == 0.0, "core/state.py must push the constant into the library at import"


# ── (c) The switch does not unblock anything ─────────────────────────────────


class TestRestoredBlocksSurvive:
    """A block a snapshot carries is state, not a live verdict — only
    backend/scripts/unblock_nodes.py clears it."""

    _NID = "restored-blocked-node"

    @pytest.fixture(autouse=True)
    def _clean_restored(self):
        yield
        state.node_analytics.reputations.pop(self._NID, None)
        state.node_analytics.trust_scores.pop(self._NID, None)
        state.node_analytics.metrics.pop(self._NID, None)

    def test_a_restored_block_is_unchanged_by_an_evaluator_pass(self):
        # Exactly the shape services/state_snapshot.restore_snapshot builds.
        entry = {
            "node_id": self._NID,
            "reputation": 0.1,
            "blocked": True,
            "block_reason": "Reputation 0.10 below threshold",
            "penalties": [{"time": 1.0, "amount": 0.15, "reason": "Trust score critically low: 0.000"}],
            "_condition_active": {"heartbeat_stale": False},
        }
        rep = NodeReputation(**entry)
        state.node_analytics.reputations[self._NID] = rep

        # A trust score that would charge 0.15 a pass at scale 1.
        from retina_analytics.trust import TrustScoreState

        bad = TrustScoreState(node_id=self._NID)
        for _ in range(5):
            bad.add_sample(
                AdsReportEntry(
                    timestamp_ms=int(time.time() * 1000),
                    predicted_delay=100.0,
                    predicted_doppler=0.0,
                    measured_delay=900.0,  # way past the 5 us threshold
                    measured_doppler=0.0,
                    adsb_hex=_HEX,
                    adsb_lat=0.0,
                    adsb_lon=0.0,
                )
            )
        assert bad.score == 0.0
        state.node_analytics.trust_scores[self._NID] = bad

        state.node_analytics.evaluate_reputations()

        assert rep.blocked is True, "the switch must not silently unblock — the script does"
        assert rep.block_reason == entry["block_reason"]
        assert rep.reputation == 0.1, "no new penalty, and no reward while blocked"
        assert len(rep.penalties) == 1, "nothing new recorded"

    def test_the_same_pass_would_have_charged_it_with_penalties_on(self, penalties_on):
        from retina_analytics.trust import TrustScoreState

        rep = NodeReputation(node_id=self._NID, reputation=0.5)
        state.node_analytics.reputations[self._NID] = rep
        bad = TrustScoreState(node_id=self._NID)
        for _ in range(5):
            bad.add_sample(
                AdsReportEntry(
                    timestamp_ms=int(time.time() * 1000),
                    predicted_delay=100.0,
                    predicted_doppler=0.0,
                    measured_delay=900.0,
                    measured_doppler=0.0,
                    adsb_hex=_HEX,
                    adsb_lat=0.0,
                    adsb_lon=0.0,
                )
            )
        state.node_analytics.trust_scores[self._NID] = bad

        state.node_analytics.evaluate_reputations()

        assert rep.reputation < 0.5
        assert rep.penalties


def test_set_penalty_scale_refuses_a_nonsense_value():
    """The library validates too, so a caller that is not config.constants
    cannot push the estate into an undefined state."""
    previous = NodeReputation.penalty_scale
    try:
        for bad in (-1.0, float("nan"), float("inf"), "x"):
            with pytest.raises(ValueError):
                set_penalty_scale(bad)
        assert NodeReputation.penalty_scale == previous
    finally:
        set_penalty_scale(previous)
