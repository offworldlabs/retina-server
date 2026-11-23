"""A node's system constant predicted from what it is, fitted across the fleet.

The fleet's measured S_n are regressed on descriptors of the tower, the geometry and
the hardware:

    S_n = s_0 + sum_j beta_j x_j(n) + u_n + e_n,    u_n ~ N(0, tau^2),    e_n ~ N(0, sigma_n^2)

s_0 has a flat prior and every beta_j a Gaussian one, N(prior mean, prior SD^2), so a
term the fleet barely supports stays near its prior instead of fitting noise.  sigma_n
is the node's own jackknife figure, the median of the known ones where a node has none,
and 0 where no node has one.

- A descriptor is read the same way in the fit and in ``predict``.  For a category a
  string is taken as given, a bool is "true" or "false" and an int its decimal
  string; for a number only a finite int or float counts, and above 0 for a log10
  term.  Anything else is no value.
- A term is fitted only where the fleet supports it, judged again on every fit: a
  number needs 3 carriers that don't all agree, a category two levels held by 2 nodes
  each.  A level fewer than 2 nodes hold counts as no value.  The reference level is
  the one most carriers hold, ties to the first in sorted order.  Every other term is
  listed with its reason.
- Each column is centred over its carriers, and a missing value is 0, which is the
  fleet's average for that term: a node that sends nothing is predicted the fleet's
  average S.  A category value that is neither a listed level nor the reference has
  never been seen in the fleet, and its mean is that average too.
- tau is the restricted maximum likelihood estimate, with the slopes integrated over
  their priors and the flat intercept integrated out.  Given tau the coefficients are
  Gaussian, mean beta and covariance Sigma, so a prediction for x is ``x'beta`` with
  ``prediction_sigma_db = sqrt(tau^2 + max(x'Sigma x + missing, 0))``.  ``missing`` is what
  the terms with no known value could add: a number j its slope's second moment over
  the spread of its carriers, ``(beta_j^2 + Sigma_jj) spread_j^2``, spread being the
  sample SD of the encoded value; a category its levels' ``beta_L'C beta_L +
  tr(C Sigma_LL)`` with ``C = diag(p) - pp'`` over the level shares p; an unseen value
  that plus the term's prior variance.  Each missing term counts on its own: what two
  of them share is left out, and so is tau's own uncertainty.
- ``predict`` reads only the calibration document, so a stored, rounded document
  predicts exactly what was published from it.
- ``validate`` leaves one node out at a time, refits on the rest (support, centring and
  tau included) and predicts the node from its descriptors.  Beside it runs the same
  for the intercept alone, which shows whether the descriptors help at all.
- A fit whose tau sits at a bound of its range is not published: the model file then
  holds the validation and no calibration.

Rows arrive eligible, and are taken in node order, so the order they come in never
changes the output.  Every published float is rounded to 6 digits, and the same rows
give the same bytes.
"""

import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any, NamedTuple

import numpy as np
from scipy.linalg import cho_factor, cho_solve
from scipy.optimize import minimize_scalar

MODEL_FILE = "performance/system_constant.json"


class Term(NamedTuple):
    name: str
    kind: str  # "category", "linear" or "log10"
    prior_mean_db: float
    prior_sd_db: float


TERMS: tuple[Term, ...] = (
    Term("band", "category", 0.0, 6.0),
    Term("tower_eirp_dbm", "linear", 0.0, 0.5),
    Term("tower_antenna_height_m", "log10", 0.0, 6.0),
    Term("tower_polarisation", "category", 0.0, 3.0),
    Term("baseline_km", "log10", 0.0, 10.0),
    Term("sdr_model", "category", 0.0, 3.0),
    Term("lna_state", "category", 0.0, 3.0),
    Term("antenna_model", "category", 0.0, 3.0),
    Term("antenna_polarisation", "category", 0.0, 3.0),
    Term("local_adsb_receiver", "category", 0.0, 6.0),
    Term("notch_dab", "category", 0.0, 3.0),
    Term("notch_rf", "category", 0.0, 3.0),
    Term("gain_reduction_ref_db", "linear", 0.0, 0.5),
    Term("gain_reduction_surv_db", "linear", 0.0, 0.5),
    Term("antenna_gain_dbi", "linear", 1.0, 0.5),
    Term("feedline_loss_db", "linear", -1.0, 0.5),
)
# The category terms a config row holds as a Boolean column: their values are "true" and "false".
FLAG_TERMS = ("local_adsb_receiver", "notch_dab", "notch_rf")

MIN_FIT_NODES = 4
MIN_VALIDATION_NODES = 5
MIN_CARRIERS = 3
MIN_LEVEL_HOLDERS = 2
TAU_GRID_DB = np.geomspace(0.1, 40.0, 41)

_DIGITS = 6
_SPREAD = 1e-9
_TAU_XATOL = 1e-6


class _Support(NamedTuple):
    """A term the fit keeps, and what its columns are centred on."""

    term: Term
    carriers: int
    centres: tuple[float, ...]  # a number's mean, or each listed level's share
    levels: tuple[str, ...] = ()
    reference: str | None = None
    spread: float = 0.0  # a number's sample SD over its carriers


class _Posterior(NamedTuple):
    objective: float
    beta: np.ndarray
    covariance: np.ndarray
    effective_parameters: float


class _Held(NamedTuple):
    node_id: str
    measured: float
    predicted: float
    sigma: float
    tau: float
    inside: bool
    inside_tau: bool


def fit(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The calibration document for ``rows``, or None under 4 rows.

    ``fitted_on`` and ``class_offsets_db`` stay None; ``model_file`` fills them in.
    """
    return _fit(rows, TERMS)


def predict(calibration: Mapping[str, Any], descriptors: Mapping[str, Any]) -> tuple[float, float]:
    """``(mean_db, prediction_sigma_db)`` for a node with ``descriptors``, from the document's own numbers.

    A term with no known value is the fleet's average in the mean, and its band holds
    what the term's values across the fleet could add.
    """
    covariance = np.array(calibration["covariance_db2"], dtype=float)
    x = [1.0]
    beta = [calibration["intercept_db"]]
    missing = 0.0
    for support, coefficients in _published(calibration):
        value = _value(support.term.kind, descriptors.get(support.term.name))
        reading = _reading(support, value)
        start = len(x)
        x.extend(_columns(support, value))
        beta.extend(coefficients)
        if reading != "known":
            block = covariance[start : len(x), start : len(x)]
            missing += _missing_variance(support, np.array(coefficients, dtype=float), block)
        if reading == "unseen":
            missing += support.term.prior_sd_db**2
    x_row = np.array(x)
    mean = float(x_row @ np.array(beta, dtype=float))
    tau = float(calibration["tau_db"])
    return mean, math.sqrt(tau * tau + max(float(x_row @ covariance @ x_row) + missing, 0.0))


def unused(calibration: Mapping[str, Any], descriptors: Mapping[str, Any]) -> dict[str, str]:
    """Each of ``descriptors`` that ``predict`` takes as no known value, and how.

    "dropped" where the model has no such term (``dropped_terms`` says why), "absent"
    where the value is none the term reads, "unseen" where no fitted level holds it.
    """
    kept = {support.term.name: support for support, _ in _published(calibration)}
    found = {}
    for name, raw in descriptors.items():
        support = kept.get(name)
        reading = "dropped" if support is None else _reading(support, _value(support.term.kind, raw))
        if reading != "known":
            found[name] = reading
    return found


def validate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Each node predicted by a fit on the others, against the intercept alone; None under 5 rows."""
    rows = _in_order(rows)
    if len(rows) < MIN_VALIDATION_NODES:
        return None
    held = _left_out(rows, TERMS)
    baseline = _left_out(rows, ())
    return {
        "loo": [
            {
                "node_id": entry.node_id,
                "measured_db": _figure(entry.measured),
                "predicted_db": _figure(entry.predicted),
                "prediction_sigma_db": _figure(entry.sigma),
                "tau_db": _figure(entry.tau),
                "inside": entry.inside,
                "inside_tau": entry.inside_tau,
            }
            for entry in held
        ],
        "inside": sum(entry.inside for entry in held),
        "inside_tau": sum(entry.inside_tau for entry in held),
        "of": len(held),
        "rmse_db": _rmse(held),
        "baseline": {
            "inside": sum(entry.inside for entry in baseline),
            "of": len(baseline),
            "rmse_db": _rmse(baseline),
        },
    }


def model_file(rows: Sequence[Mapping[str, Any]], class_offsets: Mapping[str, float], today: date) -> dict[str, Any]:
    """The document the daily run writes to ``MODEL_FILE``."""
    rows = _in_order(rows)
    calibration = fit(rows)
    if calibration is None:
        status = "too_few_nodes"
    elif calibration["tau_at_bound"]:
        # A tau the search ran into an end of its range for is no estimate; on the floor it claims the
        # descriptors explain every node, which a few nodes can't show.
        status, calibration = "tau_at_bound", None
    else:
        status = "fitted"
        calibration["fitted_on"] = today.isoformat()
        calibration["class_offsets_db"] = {name: _figure(offset) for name, offset in class_offsets.items()}
    return {
        "schema": 1,
        "status": status,
        "fitted_on": today.isoformat(),
        "nodes": len(rows),
        "calibration": calibration,
        "validation": validate(rows),
    }


# ── Fit ──────────────────────────────────────────────────────────────────────


def _fit(rows: Sequence[Mapping[str, Any]], terms: Iterable[Term]) -> dict[str, Any] | None:
    """The calibration document over ``terms``: every term in TERMS for ``fit``, none for the baseline."""
    rows = _in_order(rows)
    return _calibrate(rows, terms) if len(rows) >= MIN_FIT_NODES else None


def _calibrate(rows: Sequence[Mapping[str, Any]], terms: Iterable[Term]) -> dict[str, Any]:
    kept: list[_Support] = []
    values: list[list[Any]] = []
    dropped: dict[str, str] = {}
    for term in terms:
        column = [_value(term.kind, (row.get("descriptors") or {}).get(term.name)) for row in rows]
        support = _support(term, column)
        if isinstance(support, str):
            dropped[term.name] = support
        else:
            kept.append(support)
            values.append(column)
    design = np.array(
        [
            [1.0] + [cell for support, column in zip(kept, values) for cell in _columns(support, column[index])]
            for index in range(len(rows))
        ]
    )
    # A number is one column, a category one per listed level, each with the term's prior.
    priors = [support.term for support in kept for _ in range(len(support.levels) or 1)]
    prior_mean = np.array([0.0] + [term.prior_mean_db for term in priors])
    precision = np.array([0.0] + [term.prior_sd_db**-2 for term in priors])
    measured = np.array([float(row["system_constant_db"]) for row in rows])
    noise = np.square(_sigmas(rows))

    def posterior(tau: float) -> _Posterior:
        return _posterior(design, measured, noise + tau * tau, precision, prior_mean)

    tau = _reml_tau(lambda tau: posterior(tau).objective)
    result = posterior(tau)
    covariance = (result.covariance + result.covariance.T) / 2.0
    return {
        "schema": 1,
        "kind": "retina-fleet-calibration",
        "fitted_on": None,
        "window": [min(row["window"][0] for row in rows), max(row["window"][1] for row in rows)],
        "nodes": len(rows),
        "intercept_db": _figure(result.beta[0]),
        "terms": _term_docs(kept, result.beta),
        "covariance_db2": [[_figure(cell) for cell in line] for line in covariance],
        "tau_db": _figure(tau),
        "tau_at_bound": bool(min(tau - TAU_GRID_DB[0], TAU_GRID_DB[-1] - tau) <= _TAU_XATOL),
        "effective_parameters": _figure(result.effective_parameters),
        "dropped_terms": dropped,
        "threshold_db": _median(row.get("threshold_db") for row in rows),
        "processing_gain_db": _median(row.get("processing_gain_db") for row in rows),
        "class_offsets_db": None,
    }


def _support(term: Term, column: Sequence[Any]) -> _Support | str:
    """What the fitted rows support of ``term``, or the reason it is dropped."""
    present = [value for value in column if value is not None]
    if not present:
        return "no fitted node carries it"
    if term.kind == "category":
        holders = {level: count for level, count in Counter(present).items() if count >= MIN_LEVEL_HOLDERS}
        if len(holders) < 2:
            return f"needs two levels held by {MIN_LEVEL_HOLDERS} nodes each"
        reference = min(holders, key=lambda level: (-holders[level], level))
        levels = tuple(sorted(level for level in holders if level != reference))
        carriers = sum(holders.values())
        return _Support(term, carriers, tuple(holders[level] / carriers for level in levels), levels, reference)
    if len(present) < MIN_CARRIERS:
        return f"carried by {len(present)} fitted nodes, needs {MIN_CARRIERS}"
    if max(present) - min(present) <= _SPREAD:
        return f"one value across its {len(present)} carriers"
    return _Support(term, len(present), (math.fsum(present) / len(present),), spread=statistics.stdev(present))


def _columns(support: _Support, value: Any) -> list[float]:
    """A term's design columns for one node: centred, and 0 where the term has no value it knows."""
    if support.term.kind != "category":
        return [0.0 if value is None else value - support.centres[0]]
    if value is None or (value != support.reference and value not in support.levels):
        return [0.0] * len(support.levels)
    return [float(value == level) - share for level, share in zip(support.levels, support.centres)]


def _reading(support: _Support, value: Any) -> str:
    """How ``value`` reads: "known", "absent" where it is None, "unseen" for a category value no level holds."""
    if value is None:
        return "absent"
    if support.term.kind == "category" and value != support.reference and value not in support.levels:
        return "unseen"
    return "known"


def _missing_variance(support: _Support, coefficients: np.ndarray, block: np.ndarray) -> float:
    """What a term with no known value adds to a prediction's variance, ``block`` its own covariance."""
    if support.term.kind != "category":
        return float((coefficients[0] ** 2 + block[0, 0]) * support.spread**2)
    shares = np.array(support.centres, dtype=float)
    mix = np.diag(shares) - np.outer(shares, shares)
    return float(coefficients @ mix @ coefficients + np.trace(mix @ block))


def _value(kind: str, raw: Any) -> Any:
    """A descriptor as the fit encodes it: a category's string, a number or its log10; None where it has none."""
    if kind == "category":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        if isinstance(raw, int):
            try:
                return str(raw)
            except ValueError:  # beyond the interpreter's int-to-str digit limit
                return None
        return raw if isinstance(raw, str) else None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    try:
        if not math.isfinite(raw):
            return None
    except OverflowError:  # an int too large for a float
        return None
    if kind == "log10":
        return math.log10(raw) if raw > 0 else None
    return float(raw)


def _posterior(
    design: np.ndarray, measured: np.ndarray, variance: np.ndarray, precision: np.ndarray, prior_mean: np.ndarray
) -> _Posterior:
    """The coefficients' posterior at one tau, and -2 log of the restricted likelihood up to a constant."""
    weighted = design.T / variance
    information = weighted @ design
    factor = cho_factor(information + np.diag(precision))
    covariance = cho_solve(factor, np.eye(len(precision)))
    beta = covariance @ (weighted @ measured + precision * prior_mean)
    residual = measured - design @ beta
    shrink = beta - prior_mean
    objective = (
        float(np.sum(np.log(variance)))
        + 2.0 * float(np.sum(np.log(np.diag(factor[0]))))
        + float(residual @ (residual / variance))
        + float(shrink @ (precision * shrink))
    )
    return _Posterior(objective, beta, covariance, float(np.trace(covariance @ information)))


def _reml_tau(objective: Callable[[float], float]) -> float:
    """The grid's best tau, or the bounded minimum between its neighbours where that is lower."""
    grid = [objective(float(tau)) for tau in TAU_GRID_DB]
    best = int(np.argmin(grid))
    bracket = (float(TAU_GRID_DB[max(best - 1, 0)]), float(TAU_GRID_DB[min(best + 1, len(grid) - 1)]))
    found = minimize_scalar(objective, bounds=bracket, method="bounded", options={"xatol": _TAU_XATOL})
    if float(found.fun) < grid[best]:
        return float(found.x)
    return float(TAU_GRID_DB[best])


def _term_docs(kept: Sequence[_Support], beta: np.ndarray) -> list[dict[str, Any]]:
    docs = []
    position = 1
    for support in kept:
        term = support.term
        doc: dict[str, Any] = {
            "name": term.name,
            "kind": term.kind,
            "prior_mean_db": _figure(term.prior_mean_db),
            "prior_sd_db": _figure(term.prior_sd_db),
            "nodes": support.carriers,
        }
        if term.kind == "category":
            doc["reference"] = support.reference
            doc["levels"] = [
                {"value": level, "centre": _figure(share), "coefficient_db": _figure(beta[position + offset])}
                for offset, (level, share) in enumerate(zip(support.levels, support.centres))
            ]
            position += len(support.levels)
        else:
            doc["centre"] = _figure(support.centres[0])
            doc["spread"] = _figure(support.spread)
            doc["coefficient_db"] = _figure(beta[position])
            position += 1
        docs.append(doc)
    return docs


def _published(calibration: Mapping[str, Any]) -> Iterable[tuple[_Support, list[float]]]:
    """Each term of a calibration document as the fit kept it, with its coefficients."""
    for doc in calibration["terms"]:
        term = Term(doc["name"], doc["kind"], doc["prior_mean_db"], doc["prior_sd_db"])
        if term.kind == "category":
            levels = doc["levels"]
            centres = tuple(level["centre"] for level in levels)
            values = tuple(level["value"] for level in levels)
            support = _Support(term, doc["nodes"], centres, values, doc["reference"])
            yield support, [level["coefficient_db"] for level in levels]
        else:
            yield _Support(term, doc["nodes"], (doc["centre"],), spread=doc["spread"]), [doc["coefficient_db"]]


# ── Validation ───────────────────────────────────────────────────────────────


def _left_out(rows: Sequence[Mapping[str, Any]], terms: Iterable[Term]) -> list[_Held]:
    """Every row predicted by the fit on the other rows."""
    terms = tuple(terms)
    held = []
    for index, row in enumerate(rows):
        others = [*rows[:index], *rows[index + 1 :]]
        fold = _calibrate(others, terms)
        predicted, sigma = predict(fold, row.get("descriptors") or {})
        measured = float(row["system_constant_db"])
        noise = _known_sigma(row)
        if noise is None:
            noise = _sigma_fallback(others)
        miss = abs(measured - predicted)
        tau = fold["tau_db"]
        held.append(
            _Held(row["node_id"], measured, predicted, sigma, tau, miss <= math.hypot(sigma, noise), miss <= tau)
        )
    return held


def _rmse(held: Sequence[_Held]) -> float:
    return _figure(math.sqrt(math.fsum((entry.measured - entry.predicted) ** 2 for entry in held) / len(held)))


# ── Rows ─────────────────────────────────────────────────────────────────────


def _in_order(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Node order, and two rows of one node by their content, so no input order reaches the output."""
    return sorted(rows, key=lambda row: (str(row["node_id"]), json.dumps(row, sort_keys=True, default=str)))


def _sigmas(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    """Each row's sigma_n: its own where known, else the median of the known ones, else 0."""
    fallback = _sigma_fallback(rows)
    return [fallback if sigma is None else sigma for sigma in map(_known_sigma, rows)]


def _sigma_fallback(rows: Sequence[Mapping[str, Any]]) -> float:
    known = [sigma for sigma in map(_known_sigma, rows) if sigma is not None]
    return statistics.median(known) if known else 0.0


def _known_sigma(row: Mapping[str, Any]) -> float | None:
    sigma = _finite(row.get("sigma_db"))
    return sigma if sigma is not None and sigma >= 0 else None


def _median(values: Iterable[Any]) -> float | None:
    known = [value for value in map(_finite, values) if value is not None]
    return _figure(statistics.median(known)) if known else None


def _finite(raw: Any) -> float | None:
    """``raw`` as a float, or None where it is no finite number; a bool is none."""
    if isinstance(raw, bool):
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _figure(value: Any) -> float:
    return round(float(value), _DIGITS)
