"""The job's parallel and provisional runs, on the synthetic scenario of tests/perf_truth_fixtures.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import perf_eval
from services import performance_truth as pt
from services import performance_truth_job as job
from tests.perf_truth_fixtures import (
    CONFIG,
    NODE,
    T0,
    clutter_frames,
    detections_of,
    fixes_for,
    registry,
    seen_frames,
    truth_of,
    write_archive,
    write_truth_files,
)

OTHER = "radar-beta"


@pytest.fixture
def two_node_setup(tmp_path: Path):
    """Two nodes with the same geometry, one aircraft each sees, in one window."""
    root = tmp_path / "perf"
    (root / "registry").mkdir(parents=True)
    other = dict(CONFIG, node_id=OTHER)
    registry(root / "registry" / "users.db", configs=(CONFIG, other))
    archive_root, adsb_root, fov_dir = tmp_path / "archive", tmp_path / "adsb", tmp_path / "fov"
    for d in (archive_root, adsb_root, fov_dir):
        d.mkdir()
    window = job.window_start(T0)
    fixes = fixes_for("a1b2c3", window + 20 * 60_000, window + 40 * 60_000, 10.12, 20.12, gs_kt=80.0, track=270.0)
    truth = truth_of(fixes)
    sites = pt.Sites([CONFIG])
    seen = seen_frames(sites, truth, "a1b2c3", window + 20 * 60_000, window + 40 * 60_000)
    clutter = clutter_frames(window, window + job.WINDOW_MS, 2000)
    write_truth_files(root / "truth", fixes, window, window + job.WINDOW_MS)
    flushed = datetime.fromtimestamp((window + job.WINDOW_MS) / 1000.0, tz=UTC) + timedelta(minutes=20)
    write_archive(archive_root, detections_of(seen + clutter), flushed)
    write_archive(archive_root, detections_of(seen[::2] + clutter), flushed, node_id=OTHER)
    settings = job.Settings(archive_root=archive_root, adsb_root=adsb_root, fov_dir=fov_dir, paths=job.Paths(root))
    return settings, window


def _summaries(settings: job.Settings, window: int) -> dict[str, dict]:
    return {
        node: job.read_json(job.window_dir(settings.paths, window) / f"{node}.summary.json") for node in (NODE, OTHER)
    }


def test_parallel_workers_give_the_sequential_results(two_node_setup):
    settings, window = two_node_setup
    now = window + 2 * job.WINDOW_MS
    job.run(settings, now_ms=now, backfill_from_ms=window, workers=1, aggregate=False)
    sequential = _summaries(settings, window)
    record = job.read_json(job.window_dir(settings.paths, window) / "WINDOW.json")
    assert record["workers"] == 1 and record["status"] == "ok"
    job.run(settings, now_ms=now + 60_000, workers=2, force=True, aggregate=False)
    parallel = _summaries(settings, window)
    record = job.read_json(job.window_dir(settings.paths, window) / "WINDOW.json")
    assert record["workers"] == 2 and record["status"] == "ok"
    for node in (NODE, OTHER):
        assert parallel[node]["gates"] == sequential[node]["gates"]
        assert parallel[node]["bootstrap"] == sequential[node]["bootstrap"]
    # The second node saw every other frame only, so it detected less.
    assert parallel[OTHER]["gates"]["geometric"]["validated"] <= parallel[NODE]["gates"]["geometric"]["validated"]
    assert parallel[NODE]["gates"]["geometric"]["validated"] == parallel[NODE]["gates"]["geometric"]["n"]


def test_open_window_is_scored_provisionally_then_complete(two_node_setup):
    settings, window = two_node_setup
    # Two hours into the window: open, scored provisionally.
    report = job.run(settings, now_ms=window + 2 * 3_600_000, backfill_from_ms=window, workers=1)
    assert [(s["window"], s["reason"]) for s in report["scored"]] == [(job.window_label(window), "provisional")]
    ledger = job.load_ledger(settings.paths)
    assert ledger["windows"][job.window_label(window)]["status"] == "provisional"
    record = job.read_json(job.window_dir(settings.paths, window) / "WINDOW.json")
    assert record["provisional"] is True and record["status"] == "provisional"
    assert _summaries(settings, window)[NODE]["provisional"] is True
    latest = job.read_json(settings.paths.out / "latest.json")
    assert latest["latest_window_provisional"] is True
    assert latest["provisional_windows"] == [job.window_label(window)]
    assert latest["windows_processed"] == 0  # provisional windows are not complete ones
    assert latest["nodes"][NODE]["spans"]["latest"]["provisional"] is True
    assert latest["nodes"][NODE]["spans"]["day"] is None
    page = (settings.paths.out / "index.html").read_text()
    assert "provisional" in page
    # Once complete, the same window is scored again as complete and the flag clears.
    report = job.run(settings, now_ms=window + 2 * job.WINDOW_MS, workers=1)
    reasons = {s["window"]: s["reason"] for s in report["scored"]}
    assert reasons[job.window_label(window)] == "complete"
    assert job.load_ledger(settings.paths)["windows"][job.window_label(window)]["status"] == "ok"
    latest = job.read_json(settings.paths.out / "latest.json")
    assert latest["windows_processed"] == 1
    # The next window is open now and scored provisionally in the same run.
    assert reasons.get(job.window_label(window + job.WINDOW_MS)) == "provisional"
    assert latest["latest_window_provisional"] is True
    assert latest["nodes"][NODE]["spans"]["day"]["windows"] == 1


def test_provisional_scoring_can_be_switched_off(two_node_setup):
    settings, window = two_node_setup
    report = job.run(
        settings, now_ms=window + 2 * 3_600_000, backfill_from_ms=window, provisional=False, aggregate=False
    )
    assert report["scored"] == []


def test_forced_open_window_is_recorded_provisional(two_node_setup):
    settings, window = two_node_setup
    report = job.run(
        settings, now_ms=window + 3_600_000, only_windows=[window], force=True, provisional=False, aggregate=False
    )
    assert [s["reason"] for s in report["scored"]] == ["new"]
    assert job.load_ledger(settings.paths)["windows"][job.window_label(window)]["status"] == "provisional"


def test_cli_parses_workers_and_provisional_flags():
    args = perf_eval.build_parser().parse_args(
        [
            "run",
            "--archive-root",
            "a",
            "--adsb-root",
            "b",
            "--fov-dir",
            "c",
            "--perf-root",
            "d",
            "--workers",
            "2",
            "--no-provisional",
        ]
    )
    assert args.workers == 2 and args.no_provisional is True
    defaults = perf_eval.build_parser().parse_args(
        ["run", "--archive-root", "a", "--adsb-root", "b", "--fov-dir", "c", "--perf-root", "d"]
    )
    assert defaults.workers == job.DEFAULT_WORKERS and defaults.no_provisional is False
