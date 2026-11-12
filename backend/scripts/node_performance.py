"""Print a node's detection tables for a date range, or run the daily job that publishes every node's.

Reads the detection archive and the ADS-B store, the node's config rows and the
aircraft type database, and runs the node performance core over the window:
Pd by range, differential delay, altitude, aircraft class and type, then the
fit's figures. Nothing is written, and the app is not started.

Run it from backend/:

    .venv/bin/python -m scripts.node_performance --node <id> --from YYYY-MM-DD --to YYYY-MM-DD
    .venv/bin/python -m scripts.node_performance --node <id> --from ... --to ... --json --tobit
    .venv/bin/python -m scripts.node_performance --daily
    .venv/bin/python -m scripts.node_performance --system-constant [--calibration] [--json]
    .venv/bin/python -m scripts.node_performance --predict --band UHF --tower-eirp-dbm 80 [--json]

--daily is the run services/performance_job.py describes, the one thing here that
writes, under --data-dir (default coverage_data). The server starts it as a child
process. It covers every node and the trailing window, so it takes no --node, --from
or --to. It reads owners from the registry and types from the database the server
keeps, so it takes no --configs or --aircraft-db. It exits 0 when the run reached its
marker, whatever became of single nodes, and non-zero otherwise: a run where a node
failed and no other came through with a table stops short of the marker.

--system-constant prints the fleet's stored system constant model, the file --daily keeps
at performance/system_constant.json under --data-dir: the fit, its terms, tau and the
leave-one-node-out table. Where the fit published no calibration it prints the status and
why, and the table when 5 or more nodes were fitted. With --calibration it prints the
calibration document alone, the JSON tower-finder reads; --json prints the whole file as
stored. --predict reads the same file
and predicts S for a node that doesn't exist from the term flags it is given (one per
descriptor, as --help lists), with tau and each class's S + sigma_k - T. --band is read
as the job reads a config row's band, upper case. A flag the model has no term for, a
value it can't read and a category value the fleet never held each get a line on
stderr, and the prediction takes the fleet average there; the band then holds what a
term of the model varies across the fleet, and an unseen value's prior too. All take
only --data-dir, --json and the flags named here. They exit 0 whenever the file exists
and parses as a JSON object, except --calibration and --predict, which exit 1 when no
calibration is published (too few fitted nodes, or tau at a bound of its range). All
exit 1 when there is no file or it doesn't parse as one, and 2 when it can't be read.

--from and --to are inclusive UTC days. --opportunities node counts the node's
own ADS-B rows instead of the aggregator's: those figures have no rank, so the
rank status, system constant and its bound, Pd ceiling and 50 % ranges are left out. --numerator forces
tags or claims in place of the window's own. Configs come from the registry
(read-only; --registry, default the app's database) or from a JSON list of
config rows (--configs). Aircraft types come from --aircraft-db, else the
database the server keeps; without either, every aircraft is of unknown type.
The power of the window's tower comes from its config row's match, else from the
match --daily keeps under --data-dir (tower_match.json). Without either the tower
is unknown, as a line on stderr says when that file is absent, and there is no
system constant. A window is whole days on one tower, as the job's are: one with a
day that saw any other tower is refused.

Exit status: 0 figures printed; 1 no data (no opportunities in the window, or a
synthetic node), one line on stderr and nothing on stdout; 2 bad input (an
argument, no config rows for the node, an unreadable registry, configs file or
aircraft database, a window day whose files may be in object storage only, a
window over two towers), one line on stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config.aircraft_classes import UNKNOWN, class_for_type
from config.constants import performance_settings
from core.database import _DB_PATH
from services import (
    aircraft_db,
    archive_reader,
    compaction,
    object_store,
    performance_job,
    performance_predict,
    tower_match,
)
from services import performance_data as pdata
from services import performance_fit as pfit

_BACKEND_DATA = Path(__file__).resolve().parents[1] / "coverage_data"
# The note's altitude rows; each is a run of the core's bins.
_ALT_ROWS = {"0-2": ("0-2",), "2-20": ("2-5", "5-10", "10-20"), "20-30": ("20-30",), "30-45": ("30-45",)}
_RANK_KEYS = (
    "rank_status",
    "system_constant_db",
    "system_constant_bound",
    "system_constant_sigma_db",
    "pd_ceiling",
    "range_50pct_km",
    "range_50pct_bound",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node_performance", description=__doc__.split("\n\n")[0])
    parser.add_argument("--daily", action="store_true", help="run the daily job, writing every node's files")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_BACKEND_DATA,
        help="where --daily writes, and keeps towers and the system constant model",
    )
    parser.add_argument("--node", help="node id")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, metavar="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", type=date.fromisoformat, metavar="YYYY-MM-DD")
    parser.add_argument("--opportunities", choices=("aggregator", "node"), default="aggregator")
    parser.add_argument("--numerator", choices=("tags", "claims"), default=None)
    parser.add_argument("--tobit", action="store_true", help="add the censored-regression cross-check")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    parser.add_argument("--archive-root", type=Path, default=_BACKEND_DATA / "archive")
    parser.add_argument("--adsb-root", type=Path, default=_BACKEND_DATA / "adsb")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--registry", type=Path, help="SQLite registry holding node_configs")
    source.add_argument("--configs", type=Path, help="JSON list of node_configs rows")
    parser.add_argument("--aircraft-db", type=Path, help="aircraft type database file")
    parser.add_argument("--system-constant", action="store_true", help="print the fleet's stored system constant model")
    parser.add_argument("--calibration", action="store_true", help="with --system-constant: the calibration alone")
    parser.add_argument("--predict", action="store_true", help="predict S for a node from the term flags below")
    terms = parser.add_argument_group("--predict terms")
    for term in performance_predict.TERMS:
        kind = float if term.kind != "category" else int if term.name == "lna_state" else str
        choices = ("true", "false") if term.name in performance_predict.FLAG_TERMS else None
        terms.add_argument(
            "--" + term.name.replace("_", "-"), dest=term.name, type=kind, choices=choices, help=f"{term.kind} term"
        )
    return parser


def _registry_rows(path: Path, node: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM node_configs WHERE node_id = ? ORDER BY version", (node,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _config_rows(args: argparse.Namespace) -> list[Mapping[str, Any]]:
    if args.configs is not None:
        with open(args.configs) as handle:
            rows = json.load(handle)
        if not isinstance(rows, list):
            raise ValueError(f"{args.configs} is not a list of config rows")
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not {"node_id", "created_at"} <= row.keys():
                raise ValueError(f"{args.configs}: row {index} is not a config row with node_id and created_at")
            # The reader's own parse of it, so a row let through here is one the reader can date.
            try:
                pdata._created_ms(row["created_at"])
            except (TypeError, ValueError):
                created = json.dumps(row["created_at"])
                raise ValueError(f"{args.configs}: row {index} has created_at {created}, not an ISO date") from None
        return [row for row in rows if row["node_id"] == args.node]
    return _registry_rows(args.registry or _DB_PATH, args.node)


def _types_for(args: argparse.Namespace) -> Callable[[Sequence[str]], list[str | None]]:
    if args.aircraft_db is not None:
        return aircraft_db.load(args.aircraft_db).types_for
    database = aircraft_db.current()
    if database is not None:
        return database.types_for
    print("node_performance: no aircraft database, every type is unknown", file=sys.stderr)
    return lambda hexes: [None] * len(hexes)


def _rows(cells: Mapping[str, Sequence[int]]) -> list[dict[str, Any]]:
    return [
        {"label": label, "opportunities": n, "detections": d, "pd": round(d / n, 4)}
        for label, (n, d) in cells.items()
        if n
    ]


def _by_label(cells: Mapping[str, Sequence[int]]) -> list[dict[str, Any]]:
    return _rows({label: cells[label] for label in sorted(cells, key=lambda label: float(label.split("-")[0]))})


def _by_pd(cells: Mapping[str, Sequence[int]]) -> list[dict[str, Any]]:
    rows = _rows(cells)
    return sorted(rows, key=lambda row: (row["label"] == UNKNOWN, -row["pd"], row["label"]))


def _by_alt(by_alt: Mapping[str, Sequence[int]]) -> list[dict[str, Any]]:
    cells = {}
    for label, bins in _ALT_ROWS.items():
        found = [by_alt.get(name, (0, 0)) for name in bins]
        cells[label] = [sum(cell[0] for cell in found), sum(cell[1] for cell in found)]
    return _rows(cells)


def _by_class(merged: Mapping[str, Any]) -> list[dict[str, Any]]:
    cells: dict[str, list[int]] = {}
    for icao_type, (n, d) in merged["by_type"].items():
        cell = cells.setdefault(class_for_type(icao_type), [0, 0])
        cell[0] += n
        cell[1] += d
    # What no type covers is unknown: not taken from class_range_alt, which drops what is off its grid.
    unknown = cells.setdefault(UNKNOWN, [0, 0])
    unknown[0] += merged["opportunities"] - sum(cell[0] for cell in cells.values())
    unknown[1] += merged["detections"] - sum(cell[1] for cell in cells.values())
    return _by_pd(cells)


def _refuse_days_not_local(args: argparse.Namespace, days: Sequence[date]) -> None:
    """ValueError naming the first of ``days`` that local disk, all this reads, may not hold in full.

    Only with an object store: the lifecycle then deletes uploaded detections
    flushed before archive_reader.local_deletion_cutoff, and an ADS-B partition
    with no usable local compact may be read from object storage, as
    compaction.read_plan says. A day's ADS-B rows land in its own partition and
    the ones either side; a neighbour with no folder holds none of them, as a
    compacted day keeps its compact.json, and so its folder, for good.
    """
    if not object_store.is_enabled():
        return
    now = time.time()
    for day in days:
        if pdata.detections_may_have_left_disk(day, now):
            raise ValueError(f"{day}: its detections may have been deleted from local disk after their upload")
        for offset in (-1, 0, 1):
            partition = day + timedelta(days=offset)
            directory = compaction.day_dir(str(args.adsb_root), partition)
            if offset and not os.path.isdir(directory):
                continue
            try:
                if compaction.read_footer_manifest(os.path.join(directory, compaction.COMPACT_NAME)) is not None:
                    continue
            except OSError:
                pass
            fetchable = archive_reader.fetchable(compaction.ADSB, partition, int(now * 1000))
            names = compaction.raw_part_names(directory)
            # read_plan's own loads, in its order, first: it warns of a broken file, and as this
            # configures no logging, the warning would be a second line on stderr.
            try:
                if fetchable:
                    compaction.load_pending(compaction.ADSB, directory, partition)
                    compaction.load_sidecar(compaction.ADSB, directory, partition)
            except compaction.ManifestError:
                plan = compaction.ReadPlan(cold=False, sidecar=None, names=[], broken=True)
            else:
                plan = compaction.read_plan(compaction.ADSB, directory, partition, names, fetchable=fetchable)
            if plan.broken or plan.cold:
                why = "has a broken compact.json or upload record" if plan.broken else "may be in object storage only"
                raise ValueError(f"{day}: the ADS-B partition of {partition} {why}")


def _refuse_two_towers(
    configs: Sequence[Mapping[str, Any]], days: Sequence[date], end_config: Mapping[str, Any]
) -> None:
    """ValueError naming the last of ``days`` that saw a tower other than ``end_config``'s, as the job walks back.

    The system constant is fitted against one tower's power, and the job never
    publishes a fit over two.
    """
    end_tower = {tower_match.tower_key(end_config)}
    for day in reversed(days):
        if performance_job.towers_on(configs, day) != end_tower:
            raise ValueError(f"{day}: the transmitter tower changed inside the window")


def _stored_tower(args: argparse.Namespace, config: Mapping[str, Any]) -> dict[str, Any] | None:
    """``config``'s tower as the job takes it: the row's match, else the one --daily stored; None when neither has it.

    An absent file reads as empty, and says so on stderr where the row has no match.
    """
    path = args.data_dir / "tower_match.json"
    if config.get("tower_match_status") != "matched" and not path.exists():
        print(f"node_performance: no {path}, the tower is unknown", file=sys.stderr)
    return tower_match.tower_for(config, tower_match.load(path))


def window_report(
    args: argparse.Namespace,
    configs: Sequence[Mapping[str, Any]],
    types_for: Callable[[Sequence[str]], list[str | None]],
) -> dict[str, Any] | None:
    """The window's figures, or None when there is nothing to count."""
    days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
    end_config = pdata.config_on(configs, args.end)
    _refuse_two_towers(configs, days, end_config)
    _refuse_days_not_local(args, days)
    histogram: dict[str, int] = {}
    for day in days:
        for key, count in pdata.snr_histogram(args.node, day, archive_root=args.archive_root).items():
            histogram[key] = histogram.get(key, 0) + count
    t_n = pfit.threshold_db(histogram)

    tables = [
        pdata.node_day_table(
            args.node,
            day,
            configs=configs,
            archive_root=args.archive_root,
            adsb_root=args.adsb_root,
            types_for=types_for,
            class_for=class_for_type,
            opportunities=args.opportunities,
            threshold_db=t_n,
            in_fov=None,
        )
        for day in days
    ]
    if any(table is None for table in tables):
        return None
    merged = pdata.merge_tables(tables, numerator=args.numerator)
    if merged["opportunities"] == 0:
        return None
    if args.numerator == "claims" and any(t["opportunities"] and not t["claim_columns"] for t in tables):
        print("node_performance: some files have no claim columns, their rows read as misses", file=sys.stderr)

    performance = pfit.fit_node(merged, tower=_stored_tower(args, end_config), grid=pdata.ring_grid(end_config))
    if args.opportunities == "node":
        for key in _RANK_KEYS:
            del performance[key]
    report = {
        "node_id": merged["node_id"],
        "window": merged["window"],
        "days": merged["days"],
        "denominator": merged["denominator"],
        "numerator": merged["numerator"],
        "threshold_db": t_n,
        "opportunities": merged["opportunities"],
        "detections": merged["detections"],
        "detections_claims": merged["detections_claims"],
        "detections_tags": merged["detections_tags"],
        "pd": round(merged["detections"] / merged["opportunities"], 4),
        "by_range": _by_label(merged["by_range"]),
        "by_delay": _by_label(merged["by_delay"]),
        "by_alt": _by_alt(merged["by_alt"]),
        "by_class": _by_class(merged),
        "by_type": _by_pd(merged["by_type"]),
        "tag_delay_residual_mad_us": performance["tag_delay_residual_mad_us"],
        "delay_residual_source": performance["delay_residual_source"],
        "performance": performance,
    }
    if args.tobit:
        report["tobit"] = pfit.tobit_check(merged)
    return report


def _figure(value: float | None, spec: str, unit: str) -> str:
    return "none" if value is None else f"{value:{spec}} {unit}"


def _noted(line: str, note: str | None) -> str:
    return line if note is None else f"{line} ({note})"


def _table(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return [f"  {row['label']:<28}{row['pd']:6.2f}{row['opportunities']:10d}" for row in rows]


def render_text(report: Mapping[str, Any]) -> str:
    performance = report["performance"]
    lines = [
        f"{report['node_id']} {report['window'][0]}..{report['window'][1]}",
        f"denominator {report['denominator']}, numerator {report['numerator']}, "
        f"threshold {_figure(report['threshold_db'], '.2f', 'dB')}",
        f"opportunities {report['opportunities']}, detections {report['detections']} "
        f"(tags {report['detections_tags']}, claims {report['detections_claims']})",
        "",
        f"  {'all in-window opportunities':<28}{report['pd']:6.2f}{report['opportunities']:10d}",
        "",
        "by R_r (km)",
        *_table(report["by_range"]),
        "by differential delay (us)",
        *_table(report["by_delay"]),
        "by altitude (kft)",
        *_table(report["by_alt"]),
        "by class",
        *_table(report["by_class"]),
        f"by type (at least {pfit.EVIDENCE_FLOOR} opportunities)",
        *_table([row for row in report["by_type"] if row["opportunities"] >= pfit.EVIDENCE_FLOOR]),
        "",
        _noted(
            f"delay residual MAD {_figure(report['tag_delay_residual_mad_us'], '.2f', 'us')}",
            report["delay_residual_source"],
        ),
    ]
    if report["denominator"] == "aggregator":
        lines += [
            f"rank status {performance['rank_status'] or 'none'}",
            _noted(
                f"system constant {_figure(performance['system_constant_db'], '.1f', 'dB')}",
                performance["system_constant_bound"],
            ),
            f"Pd ceiling {_figure(performance['pd_ceiling'], '.2f', 'of opportunities')}",
        ]
        for name, km in performance["range_50pct_km"].items():
            lines.append(f"range 50% {name} {_figure(km, '.1f', 'km')} ({performance['range_50pct_bound'][name]})")
    if "tobit" in report:
        tobit = report["tobit"]
        lines.append(
            f"tobit offset {_figure(tobit['offset_db'], '.1f', 'dB')}, sigma {_figure(tobit['sigma_db'], '.1f', 'dB')}; "
            f"probit offset {_figure(tobit['probit_offset_db'], '.1f', 'dB')}, "
            f"slope {_figure(tobit['probit_slope_db'], '.1f', 'dB')}"
        )
    return "\n".join(lines)


def _yield_to_the_server() -> None:
    """If the container runs out of memory, the kernel takes this process, not the server."""
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")


def _run_daily(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    refused = (("node", "--node"), ("start", "--from"), ("end", "--to"), ("configs", "--configs"))
    for name, flag in (*refused, ("aircraft_db", "--aircraft-db")):
        if getattr(args, name) is not None:
            parser.error(f"--daily takes no {flag}")
    logging.basicConfig(level=logging.INFO)
    _yield_to_the_server()
    performance_job.run_daily(
        archive_root=args.archive_root,
        adsb_root=args.adsb_root,
        registry=args.registry or _DB_PATH,
        data_dir=args.data_dir,
        tower_url=performance_settings().tower_finder_url,
    )
    return 0


class _NoModel(Exception):
    pass


def _stored_model(data_dir: Path) -> dict[str, Any]:
    """The stored model; _NoModel where there is none or it doesn't parse as a JSON object."""
    path = data_dir / performance_predict.MODEL_FILE
    try:
        model = json.loads(path.read_text())
    except FileNotFoundError:
        raise _NoModel(f"no fitted system constant model in {path}") from None
    except (ValueError, RecursionError) as exc:
        raise _NoModel(f"no system constant model in {path}: it doesn't parse ({exc})") from None
    if not isinstance(model, dict):
        raise _NoModel(f"no system constant model in {path}: it is not a JSON object")
    return model


def _refusal(model: Mapping[str, Any], data_dir: Path) -> str | None:
    """Why the stored model publishes no calibration, or None when it does."""
    path = data_dir / performance_predict.MODEL_FILE
    if model.get("status") == "tau_at_bound":
        return f"no calibration in {path}: tau sits at a bound of its range on {model.get('nodes')} nodes, so none is published"
    if model.get("calibration") is None:
        return f"no fitted system constant model in {path} ({model.get('status')}, {model.get('nodes')} nodes)"
    return None


def _db(value: float | None, spec: str = ".2f") -> str:
    return _figure(value, spec, "dB")


def render_model(model: Mapping[str, Any], refusal: str | None = None) -> str:
    """The stored model: its calibration, or its status and ``refusal`` without one; the validation if it has one."""
    calibration = model["calibration"]
    if calibration is None:
        lines = [f"status {model['status']}, fitted on {model['fitted_on']}, {model['nodes']} nodes", str(refusal)]
    else:
        lines = [
            f"fitted on {model['fitted_on']}, {model['nodes']} nodes, "
            f"{calibration['effective_parameters']:.2f} effective parameters",
            f"intercept {_db(calibration['intercept_db'])}",
            f"tau {_db(calibration['tau_db'])}",
            "terms",
        ]
        for term in calibration["terms"]:
            if term["kind"] == "category":
                lines.append(f"  {term['name']} ({term['nodes']} nodes), against {term['reference']}")
                lines += [f"    {level['value']:<20}{level['coefficient_db']:+.2f} dB" for level in term["levels"]]
            else:
                per = "per unit" if term["kind"] == "linear" else "per decade"
                lines.append(f"  {term['name']} ({term['nodes']} nodes)   {term['coefficient_db']:+.3f} dB {per}")
        lines += [f"  dropped {name}: {reason}" for name, reason in calibration["dropped_terms"].items()]
    validation = model["validation"]
    if validation is None:
        return "\n".join([*lines, "validation: needs 5 fitted nodes"])
    lines += ["leave-one-node-out", f"  {'node':<14}{'measured':>10}{'predicted':>16}{'inside':>8}{'inside tau':>12}"]
    for held in validation["loo"]:
        predicted = f"{held['predicted_db']:.2f} +/- {held['prediction_sigma_db']:.2f}"
        lines.append(
            f"  {held['node_id']:<14}{held['measured_db']:>10.2f}{predicted:>16}"
            f"{'yes' if held['inside'] else 'no':>8}{'yes' if held['inside_tau'] else 'no':>12}"
        )
    baseline = validation["baseline"]
    return "\n".join(
        [
            *lines,
            f"{validation['inside']} of {validation['of']} inside the band, "
            f"{validation['inside_tau']} of {validation['of']} inside tau",
            f"rmse {_db(validation['rmse_db'])}; intercept only {_db(baseline['rmse_db'])}, "
            f"{baseline['inside']} of {baseline['of']} inside the band",
        ]
    )


def _given_terms(args: argparse.Namespace) -> dict[str, Any]:
    return {
        term.name: getattr(args, term.name)
        for term in performance_predict.TERMS
        if getattr(args, term.name) is not None
    }


def _descriptors(args: argparse.Namespace) -> dict[str, Any]:
    """The term flags given, as ``predict`` takes them: the band named as the job names a config row's."""
    given = _given_terms(args)
    if "band" in given:
        given["band"] = performance_job._band({"tower_band": given["band"]})
    return given


def _unused_lines(
    calibration: Mapping[str, Any], descriptors: Mapping[str, Any], args: argparse.Namespace
) -> list[str]:
    """A line for each given term the prediction can't read as a value the fleet knows."""
    lines = []
    for name, reading in performance_predict.unused(calibration, descriptors).items():
        if reading == "dropped":
            lines.append(f"{name} is not in the model ({calibration['dropped_terms'][name]}): fleet average used")
        elif reading == "unseen":
            lines.append(f"{name} {descriptors[name]} is not seen in the fleet: prior added to the band")
        else:
            lines.append(f"{name} {getattr(args, name)} is no value the model reads: fleet average used")
    return lines


def _prediction(calibration: Mapping[str, Any], descriptors: Mapping[str, Any]) -> dict[str, Any]:
    mean, sigma = performance_predict.predict(calibration, descriptors)
    threshold = calibration["threshold_db"]
    return {
        "system_constant_db": mean,
        "prediction_sigma_db": sigma,
        "tau_db": calibration["tau_db"],
        "threshold_db": threshold,
        "class_margin_db": {
            name: None if threshold is None else mean + offset - threshold
            for name, offset in calibration["class_offsets_db"].items()
        },
    }


def render_prediction(prediction: Mapping[str, Any]) -> str:
    lines = [
        f"system constant {prediction['system_constant_db']:.2f} \u00b1 {_db(prediction['prediction_sigma_db'])}",
        f"tau {_db(prediction['tau_db'])} (its own uncertainty is not in the \u00b1)",
        f"threshold {_db(prediction['threshold_db'])}",
    ]
    for name, margin in prediction["class_margin_db"].items():
        lines.append(f"S + \u03c3_k - T  {name:<20}{_db(margin)}")
    return "\n".join(lines)


def _run_model(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    mode = "--predict" if args.predict else "--system-constant"
    if args.system_constant and args.predict:
        parser.error("--system-constant takes no --predict")
    if args.calibration and not args.system_constant:
        parser.error("--calibration needs --system-constant")
    for name in _given_terms(args):
        if not args.predict:
            parser.error(f"--{name.replace('_', '-')} needs --predict")
    refused = (("daily", "--daily"), ("node", "--node"), ("start", "--from"), ("end", "--to"))
    for name, flag in (
        *refused,
        ("configs", "--configs"),
        ("registry", "--registry"),
        ("aircraft_db", "--aircraft-db"),
    ):
        if getattr(args, name) not in (None, False):
            parser.error(f"{mode} takes no {flag}")
    try:
        model = _stored_model(args.data_dir)
        refusal = _refusal(model, args.data_dir)
        if refusal and (args.predict or args.calibration):
            raise _NoModel(refusal)
        calibration = model["calibration"]
        if args.predict:
            descriptors = _descriptors(args)
            prediction = _prediction(calibration, descriptors)
            for line in _unused_lines(calibration, descriptors, args):
                print(f"node_performance: {line}", file=sys.stderr)
            print(json.dumps(prediction, indent=2, allow_nan=False) if args.json else render_prediction(prediction))
        elif args.calibration:
            print(json.dumps(calibration, indent=2, allow_nan=False))
        else:
            print(json.dumps(model, indent=2, allow_nan=False) if args.json else render_model(model, refusal))
    except _NoModel as exc:
        print(f"node_performance: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, LookupError, TypeError) as exc:
        print(f"node_performance: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.system_constant or args.predict or args.calibration or _given_terms(args):
        return _run_model(parser, args)
    if args.daily:
        return _run_daily(parser, args)
    for name, flag in (("node", "--node"), ("start", "--from"), ("end", "--to")):
        if getattr(args, name) is None:
            parser.error(f"{flag} is required")
    if args.start > args.end:
        parser.error("--from is after --to")
    try:
        configs = _config_rows(args)
        if not configs:
            raise ValueError(f"no config rows for node {args.node}")
        report = window_report(args, configs, _types_for(args))
    except (OSError, ValueError, sqlite3.Error, aircraft_db.AircraftDbError) as exc:
        print(f"node_performance: {exc}", file=sys.stderr)
        return 2
    if report is None:
        print(f"node_performance: no opportunities for {args.node} in {args.start}..{args.end}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, allow_nan=False) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
