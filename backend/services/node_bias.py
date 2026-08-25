"""Backend-computed node bias and trust feeding.

The claiming stage (identity-first lane) binds detections to known ADS-B
aircraft and records, per claim, the measured AND predicted bistatic delay /
Doppler.  It calls ``record_claim_residual`` inline with the residual
(measured minus predicted) for every claim.  Everything in this module flows
from those residuals — which makes it the first trust input the *backend*
computes for itself.  The historical input, node self-reports POSTed to
routes/analytics.py, let a lying or miscalibrated node control its own trust
score; claim residuals are computed from the node's raw detections against an
ADS-B fix the backend fetched, so the node has no pen to grade itself with.
ADS-B itself is taken as ground truth: every residual is read as a statement
about the receiving node, never about the transponder.

Two outputs, one residual stream:

1.  **Per-node bias** (``get_node_bias``): an EWMA estimate of each node's
    systematic delay/Doppler offset.  A receiver clock offset shows up as a
    constant delay bias; oscillator drift as a Doppler bias.  Exposed only
    past a maturity bar (see ``_bias_mature``).

2.  **Trust** (``get_node_trust``): residuals are converted into
    ``AdsReportEntry`` samples (provenance ``"claim_residual"``) and pushed
    into the EXISTING ``state.node_analytics.trust_scores`` — the same
    ``TrustScoreState`` the self-report route feeds, so node_retirement,
    state_snapshot and the analytics surfaces keep working unchanged, and the
    score blends both provenances (``summary()`` breaks them down).

One NODE misfitting across many distinct hexes while its peers fit those same
hexes is a lying/miscalibrated RADAR — its own trust score sinks, because
every one of its residual samples lands in its TrustScoreState.

Application hook (dark lane; integration happens post-merge, deliberately not
wired here): the dark-association/solver path should (a) subtract
``get_node_bias(node_id)`` from each dark-target measurement's delay/Doppler
before solving, so a node with a stable clock offset still triangulates
cleanly, and (b) weight each node's measurements by ``get_node_trust``
when building solver inputs, so a lying radar's contribution decays instead
of poisoning fits.  services/tasks/solver.py and frame_processor.py are owned
by other slices and are not touched here.

All state is module-level and guarded by ``_lock`` (repo lock discipline:
plain ``threading.Lock``, taken for every read-modify-write, never held
across a call into another store — ``_feed_trust`` runs strictly *after*
``_lock`` is released, so lock order is acyclic).  Nothing is added to
core/state.py — slice A owns that file.
"""

import logging
import math
import threading
import time
from collections import deque

from retina_analytics.trust import AdsReportEntry, TrustScoreState

from core import state

log = logging.getLogger(__name__)

# ── Fit thresholds ────────────────────────────────────────────────────────────
# One definition of "this residual fits": TrustScoreState's own per-sample
# gates.  The trust score counts a sample good below exactly these, so deriving
# the bias maturity caps from them keeps "certain enough to correct" anchored
# to the same physics as "good enough to trust".
_FIT_DELAY_US: float = TrustScoreState.delay_threshold_us
_FIT_DOPPLER_HZ: float = TrustScoreState.doppler_threshold_hz

# ── Bias estimator ────────────────────────────────────────────────────────────
# EWMA with alpha=0.1: at ~1 claim/s/node the estimate re-centres over ~10
# samples, fast enough to track oscillator drift over minutes but slow enough
# that one aircraft pass cannot own it.  The window deque exists for two
# things the recursion can't do: windowed variance for the maturity bar and
# distinct-hex counting.
_BIAS_ALPHA = 0.1
_BIAS_WINDOW = 128

# Maturity bar (M-of-N convention, cf. track_gates' n_detections>=3):
# a bias is only reported once
#   - n >= 12 live samples (the M-of-N bar with headroom), and
#   - >= 3 distinct hexes (a bias confirmed by one aircraft is
#     indistinguishable from that aircraft's transponder error), and
#   - the standard error of the windowed mean is within a quarter of the fit
#     gate on both axes (1.25 us / 5 Hz).  The bias is destined to be
#     SUBTRACTED from dark-lane measurements; a correction less certain than
#     a quarter-gate can push borderline measurements across the gate in
#     either direction, which is worse than no correction.  Until the spread
#     justifies it, get_node_bias reports (0.0, 0.0) — "apply nothing".
_BIAS_MIN_SAMPLES = 12
_BIAS_MIN_HEXES = 3
_BIAS_SEM_MAX_US = _FIT_DELAY_US / 4.0
_BIAS_SEM_MAX_HZ = _FIT_DOPPLER_HZ / 4.0

# ── Trust prior ───────────────────────────────────────────────────────────────
# Neutral prior for unknown nodes, and the M-of-N bar again before the real
# score replaces it: below 3 samples the score quantizes to {0, 1/2, 1} and a
# single unlucky residual would zero a brand-new node's solver weight.
_TRUST_PRIOR = 0.5
_TRUST_MIN_SAMPLES = 3

_PROVENANCE = "claim_residual"

_lock = threading.Lock()


class _NodeBiasState:
    __slots__ = ("window", "ewma_delay", "ewma_doppler")

    def __init__(self) -> None:
        # (ts_ms, hex, d_res_us, f_res_hz)
        self.window: deque = deque(maxlen=_BIAS_WINDOW)
        self.ewma_delay: float = 0.0
        self.ewma_doppler: float = 0.0

    def push(self, ts_ms: int, hex_: str, d_res_us: float, f_res_hz: float) -> None:
        if not self.window:
            self.ewma_delay = d_res_us
            self.ewma_doppler = f_res_hz
        else:
            self.ewma_delay += _BIAS_ALPHA * (d_res_us - self.ewma_delay)
            self.ewma_doppler += _BIAS_ALPHA * (f_res_hz - self.ewma_doppler)
        self.window.append((ts_ms, hex_, d_res_us, f_res_hz))


_node_bias: dict[str, _NodeBiasState] = {}


def _now_ms() -> int:
    """Wall clock in ms.  A function (not inline calls) so tests can advance it."""
    return int(time.time() * 1000)


# ── Interface contract (called by the claiming stage via guarded import) ─────


def record_claim_residual(node_id: str, hex: str, d_res_us: float, f_res_hz: float, ts_ms: int) -> None:  # noqa: A002 - `hex` is the contract's parameter name
    """Record one claim residual (measured minus predicted delay/Doppler).

    Called inline by the claiming stage for every claim.  Cheap by design:
    O(1) per record.
    """
    if not node_id or not hex:
        return
    if not (math.isfinite(d_res_us) and math.isfinite(f_res_hz)):
        # A NaN residual would poison the EWMA permanently (NaN absorbs every
        # later sample); drop it here rather than trusting every caller.
        log.debug("node_bias: dropping non-finite residual from %s for %s", node_id, hex)
        return
    hex_ = hex.strip().lower()
    with _lock:
        _node_bias.setdefault(node_id, _NodeBiasState()).push(ts_ms, hex_, d_res_us, f_res_hz)
    # _feed_trust touches a store with its own (or no) locking; done outside
    # _lock so the module lock never nests inside another.
    _feed_trust(node_id, hex_, d_res_us, f_res_hz, ts_ms)


def get_node_bias(node_id: str) -> tuple[float, float]:
    """Current systematic (delay_us, doppler_hz) bias estimate for a node.

    (0.0, 0.0) — "apply no correction" — when the node is unknown or the
    estimate has not cleared the maturity bar (see the constants block).
    """
    with _lock:
        st = _node_bias.get(node_id)
        if st is None or not _bias_mature_locked(st):
            return (0.0, 0.0)
        return (st.ewma_delay, st.ewma_doppler)


def get_node_trust(node_id: str) -> float:
    """Trust 0..1 for a node, from the existing TrustScoreState.

    Neutral prior (0.5) for unknown nodes and below the M-of-N sample bar —
    the score blends self-reports and backend-fed claim residuals, so a node
    the backend has judged across many hexes converges to its earned score.
    """
    ts = state.node_analytics.trust_scores.get(node_id)
    if ts is None or len(ts.samples) < _TRUST_MIN_SAMPLES:
        return _TRUST_PRIOR
    return ts.score


# ── Surfacing helpers (routes/analytics.py) ──────────────────────────────────


def node_summary(node_id: str) -> dict | None:
    """Bias block for the per-node analytics payload; None when unknown."""
    with _lock:
        st = _node_bias.get(node_id)
        if st is None:
            return None
        n = len(st.window)
        hexes = {s[1] for s in st.window}
        d_std, f_std = _window_std_locked(st)
        mature = _bias_mature_locked(st)
        return {
            "bias_delay_us": round(st.ewma_delay, 3),
            "bias_doppler_hz": round(st.ewma_doppler, 2),
            "mature": mature,
            "n_samples": n,
            "n_hexes": len(hexes),
            "delay_std_us": round(d_std, 3),
            "doppler_std_hz": round(f_std, 2),
        }


# ── Internals (call with _lock held unless noted) ─────────────────────────────


def _bias_mature_locked(st: _NodeBiasState) -> bool:
    n = len(st.window)
    if n < _BIAS_MIN_SAMPLES:
        return False
    if len({s[1] for s in st.window}) < _BIAS_MIN_HEXES:
        return False
    d_std, f_std = _window_std_locked(st)
    sqrt_n = math.sqrt(n)
    return d_std / sqrt_n <= _BIAS_SEM_MAX_US and f_std / sqrt_n <= _BIAS_SEM_MAX_HZ


def _window_std_locked(st: _NodeBiasState) -> tuple[float, float]:
    n = len(st.window)
    if n == 0:
        return (0.0, 0.0)
    d_mean = sum(s[2] for s in st.window) / n
    f_mean = sum(s[3] for s in st.window) / n
    d_var = sum((s[2] - d_mean) ** 2 for s in st.window) / n
    f_var = sum((s[3] - f_mean) ** 2 for s in st.window) / n
    return (math.sqrt(d_var), math.sqrt(f_var))


# ── Internals that take other stores' locks (call WITHOUT _lock held) ─────────


def _feed_trust(node_id: str, hex_: str, d_res_us: float, f_res_hz: float, ts_ms: int) -> None:
    """Push one residual into the existing trust score as an AdsReportEntry.

    predicted=0 / measured=residual keeps TrustScoreState's error arithmetic
    (|predicted - measured|) computing exactly |residual|, so score and RMS
    semantics match the self-report path sample-for-sample.

    Deliberately NOT via NodeAnalyticsManager.record_adsb_correlation, which
    the self-report route uses: that method also feeds the node's
    HistoricalCoverageMap with the sample's position.  A claim residual
    carries no position (the claims registry holds the fix; this contract
    passes residuals only), so routing through it would stamp null-island
    (0,0) coverage cells and, at one entry per claim, churn real coverage
    entries out of the 10k-capped list.  The trust-side logic below is the
    method's trust half verbatim: create-if-absent, then add_sample (the
    500-sample cap lives in add_sample).
    """
    scores = state.node_analytics.trust_scores
    ts = scores.get(node_id)
    if ts is None:
        ts = scores[node_id] = TrustScoreState(node_id=node_id)
    ts.add_sample(
        AdsReportEntry(
            timestamp_ms=ts_ms,
            predicted_delay=0.0,
            predicted_doppler=0.0,
            measured_delay=d_res_us,
            measured_doppler=f_res_hz,
            adsb_hex=hex_,
            adsb_lat=0.0,
            adsb_lon=0.0,
            provenance=_PROVENANCE,
        )
    )


def _reset_for_tests() -> None:
    """Restore module state to boot.  Tests only — see core.state's twin."""
    with _lock:
        _node_bias.clear()
