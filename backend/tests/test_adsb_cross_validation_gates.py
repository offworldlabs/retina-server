"""Cross-validating a node's ADS-B reports against external truth, safely.

_cross_validate_adsb_reports has been effectively dead: with the external cache
near-empty the lookup always missed.  Filling that cache wakes it, and it is
not safe to wake as written — the penalty it applies is persisted, and ten
mismatches in one cycle take a node from 1.0 to blocked, which survives
restarts and which apply_reward cannot undo while blocked.

Three ways a truthful node used to read as a liar: a position claim it never
made (null island), a comparison against truth measured minutes apart, and the
same sample being re-judged every cycle.  ClickUp 86cb9br6k.
"""

import time

import pytest
from retina_analytics.reputation import NodeReputation
from retina_analytics.trust import AdsReportEntry

from config.constants import XVAL_MAX_AGE_S
from core import state
from services.tasks.periodic import _cross_validate_adsb_reports

_NODE = "xval-node"
_HEX = "cafe01"
# Truth and claim ~30 km apart: unambiguously a mismatch, well past the 10 km bar.
_TRUTH = (51.5, -0.1)
_CLAIM_FAR = (51.5, 0.33)


@pytest.fixture(autouse=True)
def _clean():
    state.external_adsb_cache.clear()
    state.node_analytics.trust_scores.pop(_NODE, None)
    state.node_analytics.reputations.pop(_NODE, None)
    yield
    state.external_adsb_cache.clear()
    state.node_analytics.trust_scores.pop(_NODE, None)
    state.node_analytics.reputations.pop(_NODE, None)


def _reputation():
    rep = NodeReputation(node_id=_NODE)
    state.node_analytics.reputations[_NODE] = rep
    return rep


def _report(lat, lon, age_s=0.0):
    """One self-reported ADS-B correlation, captured `age_s` ago."""
    state.node_analytics.record_adsb_correlation(
        _NODE,
        AdsReportEntry(
            timestamp_ms=int((time.time() - age_s) * 1000),
            predicted_delay=100.0,
            predicted_doppler=0.0,
            measured_delay=100.5,
            measured_doppler=0.0,
            adsb_hex=_HEX,
            adsb_lat=lat,
            adsb_lon=lon,
        ),
    )


def _truth(age_s=0.0):
    state.external_adsb_cache[_HEX] = {
        "lat": _TRUTH[0],
        "lon": _TRUTH[1],
        "alt_m": 10000.0,
        "velocity": 200.0,
        "heading": 90.0,
        "last_seen_ms": int((time.time() - age_s) * 1000),
    }


class TestTheCheckStillWorks:
    def test_a_co_timed_divergent_report_is_penalised(self):
        """The capability being protected — without this the rest is vacuous."""
        rep = _reputation()
        _truth(age_s=1.0)
        _report(*_CLAIM_FAR, age_s=1.0)

        _cross_validate_adsb_reports()

        assert rep.reputation < 1.0
        assert rep.penalties

    def test_a_co_timed_agreeing_report_is_left_alone(self):
        rep = _reputation()
        _truth(age_s=1.0)
        _report(*_TRUTH, age_s=1.0)

        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0


class TestTemporalGates:
    def test_truth_older_than_the_budget_penalises_nobody(self):
        """An aircraft at 200 m/s moves 24 km in a 120 s poll interval, so a
        truthful node reporting a moving target would read as a mismatch."""
        rep = _reputation()
        _truth(age_s=XVAL_MAX_AGE_S + 30)
        _report(*_CLAIM_FAR, age_s=1.0)

        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0

    def test_a_report_older_than_the_budget_penalises_nobody(self):
        rep = _reputation()
        _truth(age_s=1.0)
        _report(*_CLAIM_FAR, age_s=XVAL_MAX_AGE_S + 30)

        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0

    def test_unstamped_truth_penalises_nobody(self):
        rep = _reputation()
        state.external_adsb_cache[_HEX] = {"lat": _TRUTH[0], "lon": _TRUTH[1], "alt_m": 10000.0}
        _report(*_CLAIM_FAR, age_s=1.0)

        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0


class TestNullIsland:
    def test_a_report_carrying_no_position_penalises_nobody(self):
        """routes/analytics.py requires only the delays, so adsb_lat/lon
        default to 0 — a node echoing a hex without a position lands at
        (0, 0), ~8000 km from any real node, every cycle."""
        rep = _reputation()
        _truth(age_s=1.0)
        _report(0.0, 0.0, age_s=1.0)

        _cross_validate_adsb_reports()

        assert rep.reputation == 1.0


class TestOneVotePerSample:
    def test_a_sample_is_not_re_penalised_on_the_next_cycle(self):
        """The function rescans samples[-10:] every cycle.  Re-judging them
        is what turns one bad fix into a block: the threshold is 0.2 from a
        start of 1.0, and each mismatch costs 0.1."""
        rep = _reputation()
        _truth(age_s=1.0)
        _report(*_CLAIM_FAR, age_s=1.0)

        _cross_validate_adsb_reports()
        after_first = rep.reputation
        _cross_validate_adsb_reports()

        assert rep.reputation == after_first

    def test_a_new_divergent_sample_still_counts(self):
        """The de-duplication must not also mute genuinely new evidence."""
        rep = _reputation()
        _truth(age_s=1.0)
        _report(*_CLAIM_FAR, age_s=2.0)
        _cross_validate_adsb_reports()
        after_first = rep.reputation

        _report(*_CLAIM_FAR, age_s=0.0)
        _cross_validate_adsb_reports()

        assert rep.reputation < after_first
