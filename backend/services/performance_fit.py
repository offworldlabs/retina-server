"""A node's published performance figures, from its merged count tables.

One observation model.  An opportunity with known term K (dB: everything the
geometry and the waveform fix) is detected with probability

    Pd = c * Phi((K + sigma_k + a) / s),    a = EIRP + S_n - T_n

sigma_k being the class's offset against narrowbody, s the slope in dB, T_n the
node's SNR floor and c the node's ceiling, the share of opportunities it detects
however strong the geometry.  EIRP is not in the tables, so the fit always yields
``a``, ``s`` and ``c``, and the system constant S_n exists only where a matched
tower and an SNR floor complete the sum and the cells either hold the curve's
transition or stop above it.  S_n stays the 50 % point of Phi, which is half the
ceiling in Pd.

T_n serves either numerator.  A claim's gate sits 1 dB over the floor row by row, but
one row in the bucket detects the opportunity, so on the same detections ``a`` moves
a fraction of that dB, and a term for the gate would move S_n by the rest.

What each figure claims:

- ``threshold_db`` is T_n, the lower half-peak edge of the node's SNR histogram.
  On real nodes that edge is a steep shoulder, not a cliff: a tenth of the rows
  or so sit below it, so it is a floor to compare nodes by, not a hard cut.
- ``system_constant_bound`` is ``"at_least"`` where the cells stop above the 50 % point,
  and ``"measured"`` for a located fit that doesn't sit past the weakest of them.  A
  located fit past that cell keeps its own value, which on real cells moved stronger
  reads low.  With no located curve, a plateau of at least 0.5 and weakest cells still
  at half of it say the same thing.  S_n then reads ``-edge - EIRP + T_n``, the edge
  being the top of the weakest 3 dB band reading 0.5: with c at most 1 that band has
  passed the 50 % point under any ceiling, so the true figure is higher by as much as
  the node outruns its window.  A flat node under 0.5 and a curve that falls under half
  its plateau without locating have no figure: neither is stronger than the window.  A
  slow rise that stays over half its plateau and doesn't locate looks the same from the
  cells, so it reads "at least" too, and scatter can carry a band to 0.5 early: a few
  dB over, 7 dB at worst in 100 generated runs each at slopes of 20-29 dB.
- ``system_constant_sigma_db`` is a delete-one-day jackknife of ``a``.  Ten-second
  samples of one aircraft are not independent, so the Fisher figure would be far
  too small.  It covers day-to-day scatter only: an error in the tower's EIRP or
  in the class table moves S_n without moving sigma.  With no fit left once a day
  is removed there is no figure, and none on a value no fit gave.
- ``pd_ceiling`` is c, with the located fit: 1.0 where the cells can't tell a
  ceiling from none.  It is every class's plateau pooled, so it sits under the
  reference class's own.  With no located fit it is the plateau behind an "at least"
  figure, the share the strong half of the evidence detects, and otherwise no value.
- ``tag_delay_residual_mad_us`` reads the tags, and with fewer than the evidence floor
  of them the node's uncontested claims, ``delay_residual_source`` saying which; the
  same line judges both.  Of the claims it reads refreshed holds alone, each against
  the live fix's prediction: a hold's own residual is the node's own prediction, which
  can't see the site.
- ``rank_status`` ``"low_ceiling"`` is a located ceiling under 0.5.  S_n stands, but a
  node missing most of what it could see however strong the geometry doesn't rank.
  A located fit whose 50 % point sits past its strongest cell with evidence reads the
  same: it never reached half its aircraft, and its ceiling was never seen.
- ``range_50pct_km`` walks the class's Pd out over the rings that hold evidence.
  Each reads the lower of the fitted Pd and the share it measured, because K can't
  see what a node loses towards the edge of its delay window.  A ring under the
  evidence floor neither ends the walk nor carries it.  A ring measuring under 0.5
  ends it, unless pooled with the next ring that holds evidence the two reach 0.5:
  one ring reads a few aircraft, and scatters far past its binomial figure.
- The fitted Pd sits under the class's own plateau, the most it holds on rings with
  evidence at the curve's top, or the node's ceiling where it has none.  K bins can't
  give the plateau: their top holds the opportunities lost near the window's edge,
  and reads classes far under what their rings measure.
- A ``"measured"`` value is a crossing, or 0.0 when the innermost ring is already
  under 0.5, and only a located fit gives one.  ``"window"`` and ``"evidence"``
  values were not crossed, or crossed on a fit not located, and read as "at least":
  ``"window"`` when the last ring with evidence still reads 0.5 or over and is more
  than half cut off, ``"evidence"`` when the data ran out first.  A walk starting
  further out that is already under 0.5 measured nothing: no value, bound
  ``"evidence"``.  So does a plateau of 0.5 or under, which no ring can cross.
- A node reading "at least" has no curve its rings can stand on: they walk alone under
  the plateau, and never read ``"measured"``.
- ``snr_median_db`` and ``verified_range_p85_km`` read the detections the numerator
  counts: the median of the best SNR that detected each, and the 85th percentile of the
  fixes' ground distance from the receiver.  Each is the centre of the bin holding that
  nearest rank.  ``hours_observed`` is the window's 10 s buckets holding at least one
  archived detection, in hours.  A frame that detected nothing archives no row, so a
  node that hears nothing reads low: it is not the hours the node was sending.  Days
  tabled before these counts add nothing to them, and a window of only such days has
  none.

With c at 1 the likelihood is concave in (a / s, 1 / s), so that fit runs there and
one start finds the maximum.  A free c breaks that: its fit runs from two starts, in
c's logit, whose curvature stays near 1 where c's own runs off as c nears 1.  The
slope's bounds are not a prior: an optimum resting on one means the cells did not
identify the slope, and that is no fit, exactly as a non-converged optimiser is.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from itertools import zip_longest
from typing import Any, NamedTuple

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, log_expit, log_ndtr, logit, ndtr, xlogy

CLASSES = (
    "military_transport",
    "widebody",
    "regional_jet",
    "narrowbody",
    "turboprop",
    "bizjet",
    "light_GA",
    "helicopter",
)
UNKNOWN_CLASS = "unknown"
REFERENCE_CLASS = "narrowbody"

# Excess SNR per class, measured over a week of the fleet, re-based on narrowbody.
DEFAULT_CLASS_OFFSETS_DB = {
    "military_transport": 1.14,
    "widebody": 0.77,
    "regional_jet": 0.26,
    "narrowbody": 0.0,
    "turboprop": -0.11,
    "bizjet": -0.23,
    "light_GA": -0.43,
    "helicopter": -0.70,
}

RANGE_LABELS = ("0-20km", "20-40km", "40-60km", "60-100km")
ALT_LABELS = ("0-5kft", "5-10kft", "10-20kft", "20-45kft")

EVIDENCE_FLOOR = 30
MIN_THRESHOLD_ROWS = 1000
MIN_RANK_OPPORTUNITIES = 1000
MIN_FLEET_OPPORTUNITIES = 5000
MIN_JACKKNIFE_DAYS = 3
RESIDUAL_MAD_SUSPECT_US = 1.5

# A fitted offset's sd is 1.54 dB at 30 opportunities and 0.23 at 1,000, against
# 0.11-0.51 dB between neighbouring defaults.
MIN_CLASS_OPPORTUNITIES = 1000
MIN_CLASS_OUTCOMES = 30

# How far outside the cells the 50 % point may sit for a fit's own S_n to stand.  A strong node's
# cells stop inside the curve's upper tail, and how much of the fall they show goes by
# slopes, not dB: past one slope a free ceiling and a stronger node look alike and the
# error runs from under 1 dB to tens.  The dB cap is for a flat curve, whose slope
# would otherwise carry the 50 % point far past the evidence.
TRANSITION_MARGIN_SLOPES = 1.0
TRANSITION_MARGIN_DB = 5.0

# The weakest cells: one 1 dB bin over the weakest held one, the narrowest band real cells' noise didn't flip.
WEAKEST_CELLS_DB = 1.0

# Three 1 dB bins over a band's weakest cell: scatter read a slow rise's 0.5 up to 13 dB early at one bin, 7 at three.
EDGE_BAND_DB = 3.0

# Half of what the node could see: under this, no geometry reaches a 50 % point in Pd.
MIN_PLATEAU_SHARE = 0.5

# A flat Pd fits as a plateau with its fall just under the first bins, so a curve has
# to beat a flat line: 5 % on two degrees of freedom, in units of the cells' scatter.
SHAPE_LR_MIN = 6.0

# c leaves 1 only on 1 % of the half chi-square a parameter at its bound leaves, in
# units of the cells' scatter, and on evidence past the curve's top.  The test alone
# reads a knee in the last bins as a plateau: those ran under 1 dB, true ones 20 dB.
CEILING_LR_MIN = 5.41
PLATEAU_MIN_DB = 5.0
_PLATEAU_SLOPES = 2.0

# Below 0.5 dB the whole transition sits inside one 1 dB bin.  At 30 dB Pd takes
# 60 dB to go from 0.16 to 0.84: a curve that flat says nothing about the slope.
SLOPE_MIN_DB = 0.5
SLOPE_MAX_DB = 30.0
_BETA_BOUNDS = (1.0 / SLOPE_MAX_DB, 1.0 / SLOPE_MIN_DB)
_BETA_START = 0.2
# From a high ceiling alone or a low plateau alone the fit misses the optimum on about
# 1 % of curves; from both, on none of 3,666.  Each start is alpha, beta and c's logit.
_CAPPED_STARTS = ((0.0, _BETA_START, 3.0), (3.0, 0.5, -1.0))
_LOGIT_MAX = 12.0
_CONVERGED_GRADIENT = 1e-7
# The joint fit's units come from its start, where a class deep in Pd's tail has next
# to no information, so float noise at the optimum reads far over the node gate.  A
# fit stopped 0.01 dB short of the optimum reads over this line.
_JOINT_CONVERGED_GRADIENT = 1e-4

_K_BIN_HALF_WIDTH_DB = 0.5
_FIGURE_DIGITS = 4
_MS_PER_HOUR = 3_600_000
_PD_LIMIT = 1e-12
_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)


class _Cells(NamedTuple):
    """One row per table cell of a class with an offset."""

    known: np.ndarray  # K at the bin centre
    offset: np.ndarray  # sigma_k
    cls: np.ndarray  # index into the offsets' classes
    counts: np.ndarray  # the table's cell, one column per field


class _Fit(NamedTuple):
    a: float
    slope: float
    ceiling: float = 1.0


class _Plateau(NamedTuple):
    """What cells that stop above their 50 % point show."""

    share: float  # Pd of the strong half of the evidence
    edge: float  # K + sigma_k at the top of the weakest band reading 0.5


def threshold_db(hist: Mapping[str, int], bin_db: float = 0.25) -> float | None:
    """The node's SNR floor: where the histogram first reaches half its peak, walking up.

    Interpolated between bin centres, so T_n is not quantised to a bin.  None
    under ``MIN_THRESHOLD_ROWS`` rows.
    """
    counts = {int(key): int(count) for key, count in hist.items()}
    if sum(counts.values()) < MIN_THRESHOLD_ROWS:
        return None
    half = max(counts.values()) / 2.0
    edge = next(key for key in sorted(counts) if counts[key] >= half)
    below = counts.get(edge - 1, 0)
    return _figure((edge - 0.5 + (half - below) / (counts[edge] - below)) * bin_db)


def fit_node(
    merged: Mapping[str, Any],
    *,
    class_offsets: Mapping[str, float] = DEFAULT_CLASS_OFFSETS_DB,
    tower: Mapping[str, Any] | None = None,
    grid: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The node's published file, from its merged tables, in plain JSON types."""
    floor = threshold_db(merged["snr_hist"], merged["snr_bin_db"])
    cells = _cells(merged["class_geometry"], class_offsets)
    fit = _fit_cells(cells)
    located = fit if fit is not None and _transition_in_data(cells, fit) else None
    plateau = _plateau_above_the_fall(cells) if located is None else None
    at_least = plateau is not None or (located is not None and _past_the_weakest_cell(cells, located))
    short_of_half = located is not None and _past_the_strongest_cell(cells, located)
    published_tower = _published_tower(tower)
    eirp = published_tower["eirp_dbm"] if published_tower["status"] == "matched" else None

    system_constant = sigma = bound = None
    if floor is not None and eirp is not None:
        if located is not None:
            system_constant = _figure(located.a - eirp + floor)
            sigma = _figure(_jackknife_sigma_db(merged["class_geometry_by_day"], class_offsets, located))
        elif plateau is not None:
            system_constant = _figure(-plateau.edge - eirp + floor)
        if system_constant is not None:
            bound = "at_least" if at_least else "measured"

    top = located.ceiling if located is not None else plateau.share if plateau is not None else None
    ceiling = _figure(top)
    pd_table, evidence = _class_tables(merged["class_range_alt"], _classes_present(merged))
    residual_mad, residual_source = _delay_residual(merged)
    # "At least" stands on no curve, so the rings walk alone: Phi is 1 on every one.
    walked = _Fit(math.inf, 1.0, top) if at_least and top is not None else fit
    range_km, range_bound = _range_figures(merged, class_offsets, walked, located is not None and not at_least, grid)
    opportunities = int(merged["opportunities"])
    return {
        "schema": 1,
        "node_id": merged["node_id"],
        "window": list(merged["window"]),
        "opportunities": opportunities,
        "detections": int(merged["detections"]),
        "detections_claims": int(merged["detections_claims"]),
        "detections_tags": int(merged["detections_tags"]),
        "denominator": merged["denominator"],
        "numerator": merged["numerator"],
        "system_constant_db": system_constant,
        "system_constant_bound": bound,
        "system_constant_sigma_db": sigma,
        "pd_ceiling": ceiling,
        "threshold_db": floor,
        "pd_by_class_range_alt": pd_table,
        "evidence_by_class_range_alt": evidence,
        "pd_by_bearing": [_pd(cell) for cell in merged["bearing"]],
        "tag_delay_residual_mad_us": residual_mad,
        "delay_residual_source": residual_source,
        "range_50pct_km": range_km,
        "range_50pct_bound": range_bound,
        **_observed(merged),
        "classes_with_evidence": [
            name
            for name in CLASSES
            if any(n >= EVIDENCE_FLOOR for alts in evidence.get(name, {}).values() for n in alts.values())
        ],
        "tower": published_tower,
        "rank_status": _rank_status(opportunities, residual_mad, ceiling, short_of_half, floor, eirp is not None),
    }


def fit_fleet(
    nodes: Mapping[str, Mapping[str, Any]],
    *,
    min_opportunities: int = MIN_FLEET_OPPORTUNITIES,
) -> dict[str, Any] | None:
    """The fleet's class table: one joint fit, a and s per node, sigma_k shared.

    Only a node whose own curve locates a transition enters: a curve that never
    reaches it has its classes level at equal K whatever their offsets are, and
    would pull the table to zero.  A node the data hold at no ceiling stays there.
    Any other node has a ceiling per class, since classes differ by their plateau as
    well as by their shift, and one ceiling would read a low plateau as a far lower
    offset and stretch the slope to fit it.  So sigma_k comes from where each curve
    rises, never from how high it ends.

    Only nodes the reference class reaches enter, directly or through a class they
    share with a node it does reach, and a class links a node only where it has
    both outcomes there: anywhere else a class's offset and the node's ``a`` trade
    off freely, or the offset runs off along a one-sided tail, and the optimiser
    would report a converged arbitrary number.  A class stays at its
    default unless it is reached that way and the entering nodes hold
    ``MIN_CLASS_OPPORTUNITIES`` of it, with ``MIN_CLASS_OUTCOMES`` hits and as many
    misses; a class held there links nothing, or its few cells would set the offset
    of a busy class behind it.  None when nothing anchors the table.
    """
    classes = list(DEFAULT_CLASS_OFFSETS_DB)
    reference = classes.index(REFERENCE_CLASS)

    entries = []
    for key in sorted(nodes):
        if nodes[key]["opportunities"] >= min_opportunities:
            cells = _cells(nodes[key]["class_geometry"], DEFAULT_CLASS_OFFSETS_DB)
            fit = _fit_cells(cells)
            if fit is not None and _transition_in_data(cells, fit):
                entries.append((nodes[key], cells, fit))
    tallies = np.array([_class_tally(cells, len(classes)) for _, cells, _ in entries]).reshape(-1, len(classes), 2)
    joined, placed = _anchored(reference, tallies)
    if not joined.any():
        return None
    entries = [entry for entry, enters in zip(entries, joined, strict=True) if enters]

    count = len(entries)
    sizes = [cells.known.size for _, cells, _ in entries]
    node = np.repeat(np.arange(count), sizes)
    known = np.concatenate([cells.known for _, cells, _ in entries])
    cls = np.concatenate([cells.cls for _, cells, _ in entries])
    n, d = np.concatenate([cells.counts[:, :2] for _, cells, _ in entries]).T

    # Each node's K about its own centre, as in the node fit.
    centre = np.bincount(node, n * known, count) / np.bincount(node, n, count)
    centred = known - centre[node]
    default = np.array([DEFAULT_CLASS_OFFSETS_DB[name] for name in classes])
    free = np.flatnonzero(placed & (np.arange(len(classes)) != reference))
    pairs, pair = np.unique(node * len(classes) + cls, return_inverse=True)
    pair_nodes = pairs // len(classes)
    capped = np.array([fit.ceiling < 1.0 for _, _, fit in entries])[pair_nodes]
    total = n.sum()
    logits_from = 2 * count + free.size

    def at(theta: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        alpha, beta = theta[:count], theta[count : 2 * count]
        offsets = default.copy()
        offsets[free] = theta[2 * count : logits_from]
        logits = np.full(pairs.size, np.inf)
        logits[capped] = theta[logits_from:]
        shifted = centred + offsets[cls]
        return alpha[node] + beta[node] * shifted, shifted, beta[node], offsets, logits[pair]

    def per_parameter(
        for_alpha: np.ndarray, for_beta: np.ndarray, for_offset: np.ndarray, for_logit: np.ndarray
    ) -> np.ndarray:
        by_class = np.bincount(cls, for_offset, len(classes))[free]
        by_pair = np.bincount(pair, for_logit, pairs.size)[capped]
        return np.concatenate(
            [np.bincount(node, for_alpha, count), np.bincount(node, for_beta, count), by_class, by_pair]
        )

    start = np.concatenate(
        [
            [(centre[index] + fit.a) / fit.slope for index, (_, _, fit) in enumerate(entries)],
            [1.0 / fit.slope for _, _, fit in entries],
            default[free],
            logit([entries[index][2].ceiling for index in pair_nodes[capped]]),
        ]
    )
    # A rare class's curvature is orders of magnitude under a busy node's, which
    # stalls the optimiser short of the maximum and still reports success.  Each
    # variable runs in units of its own Fisher information, so all have curvature 1.
    z, shifted, beta, _, logits = at(start)
    weight = _fisher_weight(z, n, logits)
    plateau_weight = n * expit(logits) * expit(-logits)
    information = per_parameter(weight, weight * shifted * shifted, weight * beta * beta, plateau_weight)
    scale = np.sqrt(total / np.maximum(information, 1e-12 * total))

    def objective(step: np.ndarray) -> tuple[float, np.ndarray]:
        z, shifted, beta, _, logits = at(start + scale * step)
        loss, dz, dlogit = _capped_loss(z, logits, n, d)
        return loss / total, scale * per_parameter(dz, dz * shifted, dz * beta, dlogit) / total

    slope_bounds = [
        ((_BETA_BOUNDS[0] - value) / width, (_BETA_BOUNDS[1] - value) / width)
        for value, width in zip(start[count : 2 * count], scale[count : 2 * count], strict=True)
    ]
    bounds = [(None, None)] * count + slope_bounds + [(None, None)] * (free.size + int(capped.sum()))
    step = _minimise(objective, np.zeros(start.size), bounds, _JOINT_CONVERGED_GRADIENT)
    if step is None:
        return None

    offsets = at(start + scale * step)[3]
    return {
        "schema": 1,
        "class_offsets_db": {name: _figure(value) for name, value in zip(classes, offsets, strict=True)},
        "nodes": count,
        "opportunities": sum(int(merged["opportunities"]) for merged, _, _ in entries),
    }


def tobit_check(
    merged: Mapping[str, Any],
    *,
    class_offsets: Mapping[str, float] = DEFAULT_CLASS_OFFSETS_DB,
) -> dict[str, float | None]:
    """Censored regression of SNR on the same cells, as an offline cross-check of the fit.

    Mean SNR at a cell is ``K + sigma_k + b``.  Tagged opportunities are observed
    through their sums; the rest are censored below T_n.  Where the two models
    agree, ``b`` is ``probit_offset_db`` and ``sigma_db`` is ``probit_slope_db``.
    """
    floor = threshold_db(merged["snr_hist"], merged["snr_bin_db"])
    cells = _cells(merged["class_geometry"], class_offsets)
    probit = _fit_cells(cells)
    fit = None
    if floor is not None and _identifiable(cells.known, cells.counts[:, 0], cells.counts[:, 2]):
        n, tagged, snr_sum, snr_sq_sum = cells.counts[:, [0, 2, 3, 4]].T
        fit = _tobit_mle(cells.known + cells.offset, n, tagged, snr_sum, snr_sq_sum, floor)
    probit_offset = None
    if probit is not None and floor is not None:
        probit_offset = _figure(probit.a + floor)
    return {
        "offset_db": _figure(fit.a) if fit else None,
        "sigma_db": _figure(fit.slope) if fit else None,
        "threshold_db": floor,
        "probit_offset_db": probit_offset,
        "probit_slope_db": _figure(probit.slope) if probit else None,
    }


def _figure(value: Any) -> float | None:
    """A plain rounded float, or None for anything JSON can't carry."""
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), _FIGURE_DIGITS)


def _pd(cell: Sequence[int]) -> float | None:
    return _figure(cell[1] / cell[0]) if cell[0] >= EVIDENCE_FLOOR else None


def _published_tower(tower: Mapping[str, Any] | None) -> dict[str, Any]:
    tower = tower or {}
    return {
        "callsign": tower.get("callsign"),
        "eirp_dbm": _figure(tower.get("eirp_dbm")),
        "status": tower.get("status"),
    }


def _classes_present(merged: Mapping[str, Any]) -> list[str]:
    sections = (merged["class_range_alt"], merged["class_geometry"], merged["class_ring"])
    return [name for name in (*CLASSES, UNKNOWN_CLASS) if any(section.get(name) for section in sections)]


def _class_tables(class_range_alt: Mapping[str, Any], classes: Sequence[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pd and evidence, dense over the range and altitude labels for each class."""
    pd_table: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for name in classes:
        table = class_range_alt.get(name, {})
        cells = {r: {alt: table.get(r, {}).get(alt, (0, 0)) for alt in ALT_LABELS} for r in RANGE_LABELS}
        pd_table[name] = {r: {alt: _pd(cell) for alt, cell in alts.items()} for r, alts in cells.items()}
        evidence[name] = {r: {alt: int(cell[0]) for alt, cell in alts.items()} for r, alts in cells.items()}
    return pd_table, evidence


def _residual_mad_us(hist: Mapping[str, int], bin_us: float) -> float | None:
    """Median absolute deviation of delay residuals, from their histogram."""
    keys = np.array([int(key) for key in hist], dtype=float)
    counts = np.array(list(hist.values()), dtype=float)
    if counts.sum() < EVIDENCE_FLOOR:
        return None
    centres = (keys + 0.5) * bin_us
    return _weighted_median(np.abs(centres - _weighted_median(centres, counts)), counts)


def _delay_residual(merged: Mapping[str, Any]) -> tuple[float | None, str | None]:
    """The node's delay residual MAD and what it read: its tags, or with too few of them its claims."""
    for source, hist in (("tags", merged["tag_residual_hist"]), ("claims", merged["claim_residual_hist"])):
        mad = _figure(_residual_mad_us(hist, merged["residual_bin_us"]))
        if mad is not None:
            return mad, source
    return None, None


def _observed(merged: Mapping[str, Any]) -> dict[str, float | None]:
    """The detections' median SNR and 85th-percentile ground range, and the hours of buckets holding a detection.

    Each is None when no day of the window carried its counts.
    """
    snr, ranges, buckets = (merged.get(name) for name in ("detected_snr_hist", "detected_range_hist", "frame_buckets"))
    return {
        "snr_median_db": None if snr is None else _nearest_rank(snr, merged["snr_bin_db"], 50),
        "verified_range_p85_km": None if ranges is None else _nearest_rank(ranges, merged["range_bin_km"], 85),
        "hours_observed": None if buckets is None else _figure(buckets * merged["bucket_ms"] / _MS_PER_HOUR),
    }


def _nearest_rank(hist: Mapping[str, int], bin_width: float, percent: int) -> float | None:
    """The centre of the first bin, walking up, whose running count reaches ``ceil(percent / 100 * total)``.

    None when the histogram holds no counts.
    """
    keys = sorted(hist, key=int)
    cumulative = np.cumsum([int(hist[key]) for key in keys])
    if not keys or cumulative[-1] <= 0:
        return None
    rank = -(-percent * int(cumulative[-1]) // 100)
    return _figure((int(keys[np.searchsorted(cumulative, rank)]) + 0.5) * bin_width)


def _weighted_median(values: np.ndarray, counts: np.ndarray) -> float:
    """The median of each value repeated by its count: an even total averages the middle two."""
    order = np.argsort(values, kind="stable")
    ordered, cumulative = values[order], np.cumsum(counts[order])
    total = int(cumulative[-1])
    low = ordered[np.searchsorted(cumulative, (total + 1) // 2)]
    high = ordered[np.searchsorted(cumulative, total // 2 + 1)]
    return float(low + high) / 2.0


def _rank_status(
    opportunities: int,
    residual_mad: float | None,
    ceiling: float | None,
    short_of_half: bool,
    floor: float | None,
    tower_known: bool,
) -> str | None:
    # Suspect and no opportunities collide only at zero opportunities, and there
    # the known fault is the one to act on.
    if residual_mad is not None and residual_mad >= RESIDUAL_MAD_SUSPECT_US:
        return "config_suspect"
    if opportunities == 0:
        return "no_opportunities"
    if opportunities < MIN_RANK_OPPORTUNITIES or ceiling is None or floor is None or residual_mad is None:
        return "low_evidence"
    if ceiling < MIN_PLATEAU_SHARE or short_of_half:
        return "low_ceiling"
    return None if tower_known else "tower_unknown"


def _cells(geometry: Mapping[str, Any], class_offsets: Mapping[str, float]) -> _Cells:
    known, offset, cls, rows = [], [], [], []
    for index, (name, value) in enumerate(class_offsets.items()):
        for key, cell in geometry.get(name, {}).items():
            known.append(int(key) + _K_BIN_HALF_WIDTH_DB)
            offset.append(value)
            cls.append(index)
            rows.append(cell)
    counts = np.array(rows, dtype=float) if rows else np.zeros((0, 5))
    return _Cells(np.array(known), np.array(offset), np.array(cls, dtype=int), counts)


def _identifiable(known: np.ndarray, n: np.ndarray, d: np.ndarray) -> bool:
    """Two parameters need two K bins, and both outcomes somewhere."""
    return np.unique(known).size >= 2 and 0 < d.sum() < n.sum()


def _fit_cells(cells: _Cells, *, capped: bool | None = None) -> _Fit | None:
    n, d = cells.counts[:, 0], cells.counts[:, 1]
    if not _identifiable(cells.known, n, d):
        return None
    return _probit_mle(cells.known + cells.offset, n, d, capped=capped)


def _transition_in_data(cells: _Cells, fit: _Fit) -> bool:
    """Whether the fitted 50 % point sits within the margin of the evidence, and the cells show its curve."""
    x = cells.known + cells.offset
    n, d = cells.counts[:, 0], cells.counts[:, 1]
    held = x[n >= EVIDENCE_FLOOR]
    if not held.size:
        return False
    reach = _K_BIN_HALF_WIDTH_DB + min(TRANSITION_MARGIN_DB, TRANSITION_MARGIN_SLOPES * fit.slope)
    if not held.min() - reach <= -fit.a <= held.max() + reach:
        return False
    pd = _curve_pd(fit, x)
    gain = 2.0 * (_log_likelihood(pd, n, d) - _log_likelihood(np.full(x.size, d.sum() / n.sum()), n, d))
    return gain / _dispersion(pd, n, d) >= SHAPE_LR_MIN


def _past_the_weakest_cell(cells: _Cells, fit: _Fit) -> bool:
    """Whether a located fit's 50 % point sits under the bottom of the weakest cell with evidence."""
    held = (cells.known + cells.offset)[cells.counts[:, 0] >= EVIDENCE_FLOOR]
    return bool(-fit.a < held.min() - _K_BIN_HALF_WIDTH_DB)


def _past_the_strongest_cell(cells: _Cells, fit: _Fit) -> bool:
    """Whether a located fit's 50 % point sits over the top of the strongest cell with evidence.

    No ceiling stands there: a plateau needs evidence two slopes and 5 dB over that point.
    """
    held = (cells.known + cells.offset)[cells.counts[:, 0] >= EVIDENCE_FLOOR]
    return bool(-fit.a > held.max() + _K_BIN_HALF_WIDTH_DB)


def _plateau_above_the_fall(cells: _Cells) -> _Plateau | None:
    """The plateau of cells that stop above their 50 % point, or None unless they show it.

    The plateau is what the strong half of the evidence detects: pooled over all of it, a
    curve with most of its cells on the fall would pass on a plateau it dragged down.

    Weakest cells at half of it can still be a slow rise that never reached its plateau,
    so the edge stands on no plateau.  Pd is c * Phi with c at most 1, so a band reading
    0.5 has passed the 50 % point under any ceiling: the top of the first, walking up.
    """
    held = cells.counts[:, 0] >= EVIDENCE_FLOOR
    if not held.any():
        return None
    x = (cells.known + cells.offset)[held]
    order = np.argsort(x, kind="stable")
    x = x[order]
    n, d = cells.counts[held][order, :2].T
    strong = np.cumsum(n) >= n.sum() / 2.0
    weakest = x <= x[0] + WEAKEST_CELLS_DB
    share = float(d[strong].sum() / n[strong].sum())
    if share < MIN_PLATEAU_SHARE or d[weakest].sum() / n[weakest].sum() < share / 2.0:
        return None
    for start in x:
        band = (x >= start) & (x <= start + EDGE_BAND_DB)
        if d[band].sum() / n[band].sum() >= 0.5:
            return _Plateau(share, float(x[band][-1] + _K_BIN_HALF_WIDTH_DB))
    return None


def _probit_loss(z: np.ndarray, n: np.ndarray, d: np.ndarray) -> tuple[float, np.ndarray]:
    """Binomial negative log likelihood at probit z, and its derivative in z per cell."""
    log_hit, log_miss = log_ndtr(z), log_ndtr(-z)
    log_pdf = -0.5 * z * z - _LOG_SQRT_2PI
    loss = -(d @ log_hit + (n - d) @ log_miss)
    return float(loss), (n - d) * np.exp(log_pdf - log_miss) - d * np.exp(log_pdf - log_hit)


def _capped_loss(
    z: np.ndarray, ceiling_logit: float | np.ndarray, n: np.ndarray, d: np.ndarray
) -> tuple[float, np.ndarray, np.ndarray]:
    """``_probit_loss`` under a ceiling given as its logit, and the derivative in that logit per cell.

    An infinite logit is a ceiling of exactly 1.
    """
    log_ceiling, log_gap = log_expit(ceiling_logit), log_expit(-ceiling_logit)
    log_curve = log_ndtr(z)
    log_miss = np.logaddexp(log_gap, log_ceiling + log_ndtr(-z))
    log_pdf = -0.5 * z * z - _LOG_SQRT_2PI
    loss = -(d @ (log_ceiling + log_curve) + (n - d) @ log_miss)
    dz = (n - d) * np.exp(log_ceiling + log_pdf - log_miss) - d * np.exp(log_pdf - log_curve)
    dlogit = (n - d) * np.exp(log_ceiling + log_gap + log_curve - log_miss) - d * np.exp(log_gap)
    return float(loss), dz, dlogit


def _fisher_weight(z: np.ndarray, n: np.ndarray, ceiling_logit: np.ndarray) -> np.ndarray:
    """Fisher information of each cell about z, under its ceiling."""
    log_ceiling, log_gap = log_expit(ceiling_logit), log_expit(-ceiling_logit)
    log_miss = np.logaddexp(log_gap, log_ceiling + log_ndtr(-z))
    return n * np.exp(log_ceiling - z * z - 2.0 * _LOG_SQRT_2PI - log_ndtr(z) - log_miss)


def _minimise(
    objective: Callable[[np.ndarray], tuple[float, np.ndarray]],
    start: np.ndarray,
    bounds: Sequence[tuple[float | None, float | None]],
    gate: float = _CONVERGED_GRADIENT,
) -> np.ndarray | None:
    """The minimiser, or None when the optimiser did not converge.

    Converged means a vanishing projected gradient, not the optimiser's own flag:
    its line search aborts on float noise at the optimum, and its relative-reduction
    stop passes a stall.
    """
    result = minimize(
        objective,
        start,
        jac=True,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 5000, "maxfun": 50000, "ftol": 0.0, "gtol": 1e-9},
    )
    low = np.array([-np.inf if bound[0] is None else bound[0] for bound in bounds])
    high = np.array([np.inf if bound[1] is None else bound[1] for bound in bounds])
    projected = np.clip(result.x - result.jac, low, high) - result.x
    return result.x if np.abs(projected).max() <= gate else None


def _on_slope_bound(beta: float) -> bool:
    return beta <= _BETA_BOUNDS[0] * (1.0 + 1e-9) or beta >= _BETA_BOUNDS[1] * (1.0 - 1e-9)


def _log_likelihood(pd: np.ndarray, n: np.ndarray, d: np.ndarray) -> float:
    pd = np.clip(pd, _PD_LIMIT, 1.0 - _PD_LIMIT)
    return float((xlogy(d, pd) + xlogy(n - d, 1.0 - pd)).sum())


def _dispersion(pd: np.ndarray, n: np.ndarray, d: np.ndarray) -> float:
    """The cells' scatter about the curve, in units of binomial scatter, never under 1.

    Ten-second samples of one aircraft are not independent, and a node's bearings
    show through its curve: real cells run 15 to 35 times the binomial figure.
    """
    held = n >= EVIDENCE_FLOOR
    freedom = np.count_nonzero(held) - 3
    if freedom <= 0:
        return 1.0
    n, d, pd = n[held], d[held], pd[held]
    return max(1.0, 2.0 * (_log_likelihood(d / n, n, d) - _log_likelihood(pd, n, d)) / freedom)


def _curve_pd(fit: _Fit, x: np.ndarray) -> np.ndarray:
    return fit.ceiling * ndtr((x + fit.a) / fit.slope)


def _ceiling_stands(fit: _Fit, gain: float, x: np.ndarray, n: np.ndarray, d: np.ndarray) -> bool:
    """Whether the cells hold a plateau under 1, ``gain`` being the free ceiling's likelihood ratio."""
    held = x[n >= EVIDENCE_FLOOR]
    if not held.size or held.max() < _PLATEAU_SLOPES * fit.slope + PLATEAU_MIN_DB - fit.a:
        return False
    return gain / _dispersion(_curve_pd(fit, x), n, d) >= CEILING_LR_MIN


def _probit_mle(x: np.ndarray, n: np.ndarray, d: np.ndarray, *, capped: bool | None = None) -> _Fit | None:
    """(a, s, c) of ``c * Phi((x + a) / s)``, fitted as ``c * Phi(alpha + beta * (x - centre))``.

    ``capped`` True frees c, False holds it at 1, None leaves that to ``_ceiling_stands``.
    """
    total = n.sum()
    centre = (n @ x) / total
    centred = x - centre

    def uncapped_loss(theta: np.ndarray) -> tuple[float, np.ndarray]:
        loss, dz = _probit_loss(theta[0] + theta[1] * centred, n, d)
        return loss / total, np.array([dz.sum(), dz @ centred]) / total

    def capped_loss(theta: np.ndarray) -> tuple[float, np.ndarray]:
        loss, dz, dlogit = _capped_loss(theta[0] + theta[1] * centred, theta[2], n, d)
        return loss / total, np.array([dz.sum(), dz @ centred, dlogit.sum()]) / total

    def fit_at(theta: np.ndarray) -> _Fit:
        ceiling = float(expit(theta[2])) if theta.size == 3 else 1.0
        return _Fit(float(theta[0] / theta[1] - centre), float(1.0 / theta[1]), ceiling)

    bounds = [(None, None), _BETA_BOUNDS]
    held = free = None
    if capped is not True:
        held = _minimise(uncapped_loss, np.array([0.0, _BETA_START]), bounds)
    if capped is not False:
        found = [_minimise(capped_loss, np.array(start), [*bounds, (None, _LOGIT_MAX)]) for start in _CAPPED_STARTS]
        free = min((theta for theta in found if theta is not None), key=lambda t: capped_loss(t)[0], default=None)

    if capped is None:
        if held is None or free is None:
            return None
        gain = 2.0 * total * (uncapped_loss(held)[0] - capped_loss(free)[0])
        capped = _ceiling_stands(fit_at(free), gain, x, n, d)
    theta = free if capped else held
    if theta is None or _on_slope_bound(theta[1]):
        return None
    return fit_at(theta)


def _tobit_mle(
    x: np.ndarray,
    n: np.ndarray,
    tagged: np.ndarray,
    snr_sum: np.ndarray,
    snr_sq_sum: np.ndarray,
    floor: float,
) -> _Fit | None:
    """(b, s) of a normal SNR with mean ``x + b``, censored below ``floor``.

    Fitted in (shift, ratio) = ((b - b0) / s, s0 / s), where the likelihood is
    concave and the observed part reduces to one sum of squares about ``b0``, the
    observed mean residual.  ``s0`` is the observed spread, so both run near 1.
    """
    observed = tagged.sum()
    censored = n - tagged
    mean_residual = (snr_sum - tagged * x).sum() / observed
    mean = x + mean_residual
    squares = (snr_sq_sum - 2.0 * mean * snr_sum + tagged * mean * mean).sum()
    if squares <= 0.0:
        return None
    h_start = math.sqrt(observed / squares)
    cut = (floor - mean) * h_start
    total = n.sum()

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        shift, ratio = theta
        below, dz = _probit_loss(ratio * cut - shift, censored, censored)
        loss = below + observed * (0.5 * (ratio * ratio + shift * shift) - math.log(ratio))
        grad = np.array([observed * shift - dz.sum(), dz @ cut + observed * (ratio - 1.0 / ratio)])
        return loss / total, grad / total

    bounds = [(None, None), (_BETA_BOUNDS[0] / h_start, _BETA_BOUNDS[1] / h_start)]
    theta = _minimise(objective, np.array([0.0, 1.0]), bounds)
    if theta is None or _on_slope_bound(theta[1] * h_start):
        return None
    h = theta[1] * h_start
    return _Fit(float(mean_residual + theta[0] / h), float(1.0 / h))


def _jackknife_sigma_db(by_day: Mapping[str, Any], class_offsets: Mapping[str, float], full: _Fit) -> float | None:
    """Delete-one-day jackknife of ``a``, refitting c where the full fit found one.

    Whether a ceiling exists stays the full fit's call, because a delete-one flip would
    jump ``a`` by far more than the days scatter.  Its value is refitted: held fixed,
    the figure reads low wherever ``a`` and c trade off.

    A day with no usable cell is not a sample.  A day the fit can't do without is
    the most influential one, so a failed fit leaves no figure, not a smaller spread.
    """
    days = [_cells(by_day[day], class_offsets) for day in sorted(by_day)]
    days = [cells for cells in days if cells.known.size]
    count = len(days)
    if count < MIN_JACKKNIFE_DAYS:
        return None
    estimates = []
    for left_out in range(count):
        rest = days[:left_out] + days[left_out + 1 :]
        rest_cells = _Cells(*(np.concatenate(column) for column in zip(*rest, strict=True)))
        fit = _fit_cells(rest_cells, capped=full.ceiling < 1.0)
        if fit is None:
            return None
        estimates.append(fit.a)
    spread = np.array(estimates) - np.mean(estimates)
    return math.sqrt((count - 1) / count * float(spread @ spread))


def _class_tally(cells: _Cells, class_count: int) -> np.ndarray:
    """Opportunities and hits, one row per class."""
    return np.stack([np.bincount(cells.cls, cells.counts[:, field], class_count) for field in (0, 1)], axis=1)


def _anchored(reference: int, tallies: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Masks of the nodes that enter the joint fit and the classes it can place.

    ``tallies`` holds opportunities and hits by node and class.  Working out from
    the reference class, a node joins through a placed class with both outcomes on
    it, and a class is placed once the joined nodes hold enough of it.  So a class
    never vouches for itself: a node only it reaches counts for it only after other
    nodes have placed it.
    """
    n, d = tallies[..., 0], tallies[..., 1]
    links = (d > 0) & (d < n)
    placed = np.arange(n.shape[1]) == reference
    while True:
        joined = links[:, placed].any(axis=1)
        pooled_n, pooled_d = n[joined].sum(axis=0), d[joined].sum(axis=0)
        enough = (
            (pooled_n >= MIN_CLASS_OPPORTUNITIES)
            & (pooled_d >= MIN_CLASS_OUTCOMES)
            & (pooled_n - pooled_d >= MIN_CLASS_OUTCOMES)
        )
        new = links[joined].any(axis=0) & enough & ~placed
        if not new.any():
            return joined, placed
        placed |= new


def _range_figures(
    merged: Mapping[str, Any],
    class_offsets: Mapping[str, float],
    fit: _Fit | None,
    located: bool,
    grid: Mapping[str, Any] | None,
) -> tuple[dict[str, float | None], dict[str, str]]:
    range_km: dict[str, float | None] = dict.fromkeys(CLASSES)
    bound: dict[str, str] = dict.fromkeys(CLASSES, "evidence")
    usable = np.array([cell[0] >= EVIDENCE_FLOOR for cell in merged["bearing"]])
    # A curve is averaged over bearings; rings walked alone need none.
    if fit is None or grid is None or (math.isfinite(fit.a) and not usable.any()):
        return range_km, bound
    for name in CLASSES:
        if name in class_offsets:
            cells = _ring_cells(merged["class_ring"].get(name, {}), len(grid["ring_km"]))
            value, label = _class_range(cells, usable, grid, class_offsets[name], fit, located)
            range_km[name], bound[name] = _figure(value), label
    return range_km, bound


def _ring_cells(class_ring: Mapping[str, Sequence[int]], ring_count: int) -> np.ndarray:
    """``[n, d]`` of every ring on the grid, one row each."""
    cells = np.zeros((ring_count, 2))
    for key, cell in class_ring.items():
        if int(key) < ring_count:
            cells[int(key)] = cell
    return cells


def _ring_curve(usable: np.ndarray, grid: Mapping[str, Any], offset: float, fit: _Fit) -> np.ndarray:
    """Phi of each ring, averaged over its usable bearings inside the window; NaN with none left."""
    inside = usable & (grid["delay_us"] < grid["window_us"])
    bearings = inside.sum(axis=1)
    phi = ndtr((grid["known_db"] + offset + fit.a) / fit.slope)
    return np.where(bearings > 0, (phi * inside).sum(axis=1) / np.maximum(bearings, 1), np.nan)


def _class_plateau(cells: np.ndarray, curve: np.ndarray, pooled: float) -> float:
    """The most the class holds on two neighbouring rings at the curve's top, or ``pooled`` with no such ring.

    Pooling them all would read a fall inside the top, on a node whose curve stays
    there to the window's edge, as a lower plateau.  One ring alone runs high on noise.
    """
    top = cells[(curve >= ndtr(_PLATEAU_SLOPES)) & (cells[:, 0] >= EVIDENCE_FLOOR)]
    if not top.size:
        return pooled
    pairs = top[:-1] + top[1:] if len(top) > 1 else top
    return float((pairs[:, 1] / pairs[:, 0]).max())


def _class_range(
    cells: np.ndarray,
    usable: np.ndarray,
    grid: Mapping[str, Any],
    offset: float,
    fit: _Fit,
    located: bool,
) -> tuple[float | None, str]:
    """Where the class's Pd crosses 0.5, and what stopped the walk if not."""
    ring_km = grid["ring_km"]
    curve = _ring_curve(usable, grid, offset, fit)
    ceiling = _class_plateau(cells, curve, fit.ceiling)
    if ceiling <= 0.5:
        return None, "evidence"
    # A ring with no bearing left has no curve to hold it: what it measured stands alone.
    fitted = np.nan_to_num(ceiling * curve, nan=1.0)
    held = np.flatnonzero(cells[:, 0] >= EVIDENCE_FLOOR).tolist()
    outermost = None
    previous_pd = 0.0
    for ring, beyond in zip_longest(held, held[1:]):
        pd = min(float(fitted[ring]), cells[ring, 1] / cells[ring, 0])
        if pd < 0.5 <= fitted[ring] and beyond is not None:
            pooled_n, pooled_d = cells[ring] + cells[beyond]
            if pooled_d / pooled_n >= 0.5:
                continue
        if pd < 0.5:
            if outermost is None:
                return (0.0, "measured") if ring == 0 and located else (None, "evidence")
            inner = float(ring_km[outermost])
            if not located:
                return inner, "evidence"
            share = (previous_pd - 0.5) / (previous_pd - pd)
            return inner + share * (float(ring_km[ring]) - inner), "measured"
        outermost, previous_pd = ring, pd
    if outermost is None:
        return None, "evidence"
    cut_off = np.count_nonzero(usable & (grid["delay_us"][outermost] >= grid["window_us"]))
    return float(ring_km[outermost]), "window" if 2 * cut_off > np.count_nonzero(usable) else "evidence"
