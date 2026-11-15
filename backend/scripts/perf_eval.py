"""Score node detection performance against an independent ADS-B denominator.

    python -m scripts.perf_eval run --archive-root R --adsb-root A --fov-dir F --perf-root P [options]
    python -m scripts.perf_eval aggregate --perf-root P
    python -m scripts.perf_eval status --perf-root P

``run`` scores every complete three-hour window since the evaluation started that
the ledger does not hold, reconciles the two most recent held ones, and renders
the outputs (services/performance_truth_job.py says what complete means and
what is written). Options:

    --nodes ID[,ID]        score these nodes only (others keep their files)
    --backfill-from T      move the evaluation start back to T (ISO date or time, UTC)
    --window T             score the one window starting at T, whatever the ledger says
    --force                score every due window again, inputs changed or not
    --max-windows N        at most N windows this run (default 16; a backfill continues next run)
    --truth-source S       recorder (default), aggregator (the archive's ADSBHub rows) or both
    --workers N            score a window's nodes in N processes (default 3)
    --no-provisional       do not score the open window on what has arrived so far
    --no-aggregate         skip the outputs
    --now T                pretend the clock reads T (tests)

Exit status 0 when every scored node succeeded, 1 when a node failed (its
window is recorded as partial and retried next run), 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from services import performance_truth_job as job


def _ms(value: str) -> int:
    text = value.strip()
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="perf_eval", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="score due windows and render the outputs")
    run.add_argument("--archive-root", type=Path, required=True)
    run.add_argument("--adsb-root", type=Path, required=True)
    run.add_argument("--fov-dir", type=Path, required=True)
    run.add_argument("--perf-root", type=Path, required=True)
    run.add_argument("--nodes", default="")
    run.add_argument("--backfill-from", type=_ms, default=None)
    run.add_argument("--window", type=_ms, default=None)
    run.add_argument("--force", action="store_true")
    run.add_argument("--max-windows", type=int, default=16)
    run.add_argument("--truth-source", choices=("recorder", "aggregator", "both"), default="recorder")
    run.add_argument("--workers", type=int, default=job.DEFAULT_WORKERS)
    run.add_argument("--no-provisional", action="store_true")
    run.add_argument("--no-aggregate", action="store_true")
    run.add_argument("--now", type=_ms, default=None)

    agg = sub.add_parser("aggregate", help="render the outputs from the windows already scored")
    agg.add_argument("--perf-root", type=Path, required=True)

    status = sub.add_parser("status", help="print the ledger")
    status.add_argument("--perf-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    paths = job.Paths(args.perf_root)
    if args.command == "status":
        ledger = job.load_ledger(paths)
        windows = ledger.get("windows", {})
        print(
            f"evaluation start: {job.iso(ledger['evaluation_start_ms']) if ledger.get('evaluation_start_ms') else '-'}"
        )
        print(f"last run: {job.iso(ledger['last_run_ms']) if ledger.get('last_run_ms') else '-'}")
        print(f"windows: {len(windows)} ({sum(1 for w in windows.values() if w.get('status') == 'ok')} ok)")
        for label in sorted(windows)[-12:]:
            entry = windows[label]
            nodes = entry.get("nodes", {})
            codes = {}
            for code in nodes.values():
                codes[code] = codes.get(code, 0) + 1
            print(f"  {label} {entry.get('status'):8} {entry.get('reason', ''):10} {json.dumps(codes, sort_keys=True)}")
        return 0
    if args.command == "aggregate":
        configs = job.registry_configs(paths.registry) if paths.registry.exists() else None
        report = job.build_outputs(paths, int(datetime.now(UTC).timestamp() * 1000), configs)
        print(json.dumps(report))
        return 0
    settings = job.Settings(
        archive_root=args.archive_root,
        adsb_root=args.adsb_root,
        fov_dir=args.fov_dir,
        paths=paths,
        truth_source=args.truth_source,
    )
    nodes = [n for n in args.nodes.split(",") if n] or None
    only = [job.window_start(args.window)] if args.window is not None else None
    report = job.run(
        settings,
        now_ms=args.now,
        nodes=nodes,
        backfill_from_ms=args.backfill_from,
        only_windows=only,
        force=args.force or only is not None,
        max_windows=args.max_windows,
        aggregate=not args.no_aggregate,
        workers=max(1, args.workers),
        provisional=not args.no_provisional,
    )
    for scored in report["scored"]:
        codes = {}
        for info in scored["nodes"].values():
            codes[info["status"]] = codes.get(info["status"], 0) + 1
        print(f"{scored['window']} {scored['reason']:10} {scored['status']:8} {json.dumps(codes, sort_keys=True)}")
    if report.get("aggregate"):
        print("outputs:", json.dumps(report["aggregate"]))
    if report["failed"]:
        print("FAILED:", json.dumps(report["failed"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
