"""Print a node's detection tables for a date range, or run the daily job that publishes every node's.

Reads the detection archive and the ADS-B store, the node's config rows and the
aircraft type database, and runs the node performance core over the window:
Pd by range, differential delay, altitude, aircraft class and type, then the
fit's figures. Nothing is written, and the app is not started.

Run it from backend/:

    .venv/bin/python -m scripts.node_performance --node <id> --from YYYY-MM-DD --to YYYY-MM-DD
    .venv/bin/python -m scripts.node_performance --node <id> --from ... --to ... --json --tobit
    .venv/bin/python -m scripts.node_performance --daily

--daily is the run services/performance_job.py describes, the one thing here that
writes, under --data-dir (default coverage_data). The server starts it as a child
process. It covers every node and the trailing window, so it takes no --node, --from
or --to. It reads owners from the registry and types from the database the server
keeps, so it takes no --configs or --aircraft-db. It exits 0 when the run reached its
marker, whatever became of single nodes, and non-zero otherwise: a run where a node
failed and no other came through with a table stops short of the marker.

--from and --to are inclusive UTC days. --opportunities node counts the node's
own ADS-B rows instead of the aggregator's: those figures have no rank, so the
rank status, system constant and its bound, Pd ceiling and 50 % ranges are left out. --numerator forces
tags or claims in place of the window's own. Configs come from the registry
(read-only; --registry, default the app's database) or from a JSON list of
config rows (--configs). Aircraft types come from --aircraft-db, else the
database the server keeps; without either, every aircraft is of unknown type.

Exit status: 0 figures printed; 1 no data (no opportunities in the window, or a
synthetic node), one line on stderr and nothing on stdout; 2 bad input (an
argument, no config rows for the node, an unreadable registry, configs file or
aircraft database, a window day whose files may be in object storage only), one
line on stderr.
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
from services import aircraft_db, archive_reader, compaction, object_store, performance_job
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
    parser.add_argument("--data-dir", type=Path, default=_BACKEND_DATA, help="where --daily writes")
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


def window_report(
    args: argparse.Namespace,
    configs: Sequence[Mapping[str, Any]],
    types_for: Callable[[Sequence[str]], list[str | None]],
) -> dict[str, Any] | None:
    """The window's figures, or None when there is nothing to count."""
    days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
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

    performance = pfit.fit_node(merged, grid=pdata.ring_grid(pdata.config_on(configs, args.end)))
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
