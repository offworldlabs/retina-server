"""The daily node performance run and the task that starts it, on archive files the repo's writers wrote.

Ids, towers and positions are invented.  A day of the fixture is one frame: 300 typed
aircraft around the receiver, the near ones mostly tagged and the far ones mostly not,
with every tag's delay on its true bistatic delay, plus 1000 rows at 5.0 dB that set
the threshold.  One aggregator row lists each aircraft for the day's nodes.
"""

import asyncio
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from retina_analytics.empirical_coverage import EmpiricalCoverageState
from scipy.special import ndtr
from sqlalchemy import create_engine

import main
from config.constants import PERFORMANCE_RETRY_S
from core import state, task_registry
from core.nodes import Node, NodeClaim, NodeConfig
from core.users import Base
from services import adsb_writer, aircraft_db, parquet_writer, performance_job, performance_store, tower_match
from services import performance_data as pdata
from services import performance_fit as pfit
from services.tasks import performance_refresh

BACKEND_DIR = Path(__file__).resolve().parents[1]
NODE = "radar-alpha"
OTHER = "radar-beta"
SYNTHETIC = "synth-one"
TODAY = date(2026, 3, 12)
YESTERDAY = date(2026, 3, 11)
AIRCRAFT = 300
TAGGED = 139
PINNED_KEYS = {
    "schema",
    "node_id",
    "window",
    "opportunities",
    "detections",
    "detections_claims",
    "detections_tags",
    "denominator",
    "numerator",
    "system_constant_db",
    "system_constant_sigma_db",
    "threshold_db",
    "pd_by_class_range_alt",
    "evidence_by_class_range_alt",
    "pd_by_bearing",
    "tag_delay_residual_mad_us",
    "range_50pct_km",
    "range_50pct_bound",
    "classes_with_evidence",
    "tower",
    "rank_status",
}


def config_row(node_id=NODE, callsign="QAAA", tx_lat=10.2, **over):
    row = {"node_id": node_id, "version": 1, "created_at": datetime(2026, 1, 1, tzinfo=UTC), "superseded_at": None}
    row.update(rx_lat=10.0, rx_lon=20.0, rx_alt_ft=300.0, tx_lat=tx_lat, tx_lon=20.0, tx_alt_ft=1000.0)
    row.update(tx_callsign=callsign, fc_hz=200e6, fs_hz=2e6, cpi_s=0.5, max_range_km=59.96)
    row.update(delay_tolerance_us=5.0, doppler_tolerance_hz=5.0)
    return {**row, **over}


def claim_row(since_written_for, updated_at):
    since = datetime(2026, 1, 1, tzinfo=UTC)
    row = {"node_id": NODE, "user_id": "owner-two", "owned_since": since, "owned_since_user_id": since_written_for}
    return {**row, "updated_at": updated_at}


def licence(callsign, eirp_dbm):
    row = {"callsign": callsign, "frequency_mhz": 200.0, "band": "VHF", "distance_km": 2.0, "eirp_dbm": eirp_dbm}
    return {**row, "licence_type": "Broadcasting", "measured": False}


def tower_finder(request):
    # 60.0 is the power the route fills in for a licence without one: matched, with no EIRP.
    records = [licence("QAAA", 81.2), licence("QBBB", 74.5), licence("QNUL", 60.0)]
    return httpx.Response(200, json={"towers": records})


class World:
    """An archive, a registry and a data folder, and a run over them."""

    def __init__(self, root: Path):
        self.root = root
        self.data = root / "coverage_data"
        self.engine = create_engine(f"sqlite:///{root / 'registry.db'}")
        Base.metadata.create_all(self.engine, tables=[Node.__table__, NodeConfig.__table__, NodeClaim.__table__])

    def registry(self, *configs, claim=None):
        with self.engine.begin() as connection:
            if configs:
                connection.execute(NodeConfig.__table__.insert(), list(configs))
            connection.execute(NodeClaim.__table__.delete())
            if claim is not None:
                connection.execute(NodeClaim.__table__.insert(), claim)

    def day(self, day, *nodes, config=None, skew_us=0.0):
        """One frame per node and the aggregator's rows for all of them, at noon of ``day``."""
        site = pdata._site(config or config_row())
        index = np.arange(AIRCRAFT)
        hexes = [f"{0xA00000 + i:06x}" for i in index]
        bearing = np.radians(np.where(index % 2, 135.0, 45.0))
        ground_km = np.linspace(10.0, 20.0, AIRCRAFT)
        lat = 10.0 + ground_km * np.cos(bearing) / 110.6
        lon = 20.0 + ground_km * np.sin(bearing) / 109.6
        look = site.look(lat, lon, 12000 * 0.3048)
        known_db = site.known_db(look.rt_m, look.rr_m)
        tagged = index[index * 0.6180339887 % 1.0 < ndtr((known_db - np.median(known_db)) / 2.0)]
        fix = {"alt_baro": 12000}
        t_ms = int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp() * 1000)
        delays = look.delay_us[tagged] + skew_us * (tagged % 5 - 2)
        frame = {
            "timestamp": t_ms,
            "delay": [*delays.tolist(), *[30.0] * 1000],
            "doppler": [-50.0] * (tagged.size + 1000),
            "snr": [12.0] * tagged.size + [5.0] * 1000,
            "adsb": [{"hex": hexes[i], "lat": lat[i], "lon": lon[i], **fix} for i in tagged] + [None] * 1000,
            "_signing_mode": "unknown",
            "_signature_valid": False,
        }
        flushed = datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
        for node in nodes:
            parquet_writer.write_detections_parquet(
                node_id=node, frames=[frame], base_dir=self.root / "archive", write_ts=flushed
            )
        common = {"ts_ms": t_ms, "seen_ms": t_ms, "gs": 450.0, "track": 90.0, "flight": None, **fix}
        real = {"world": "real", "source": "aggregator", "source_node_id": ",".join(sorted(nodes))}
        rows = [{**common, **real, "hex": h, "lat": lat[i], "lon": lon[i]} for i, h in enumerate(hexes)]
        rows.append({**rows[0], "hex": "b00000", "world": "sim"})
        adsb_writer.write_adsb_parquet(rows=rows, base_dir=self.root / "adsb", write_ts=flushed)

    def roots(self):
        root = self.root
        return {"archive_root": root / "archive", "adsb_root": root / "adsb", "registry": root / "registry.db"}

    def run(self, today=TODAY, handler=tower_finder, data_dir=None):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return performance_job.run_daily(
                **self.roots(),
                data_dir=data_dir or self.data,
                tower_url="http://towers.example.com",
                today=today,
                session=client,
            )

    def file(self, node=NODE, kind="", data_dir=None):
        return json.loads(((data_dir or self.data) / f"performance_{node}{kind}.json").read_text())

    def table(self, day, node=NODE):
        return self.data / "performance" / "days" / day.isoformat() / f"{node}.json"

    def everything(self):
        files = (path for path in self.data.rglob("*") if path.is_file() and path.name != "last_run.json")
        return {str(path.relative_to(self.data)): path.read_bytes() for path in files}


@pytest.fixture
def world(tmp_path, monkeypatch):
    # The clock of tower_match's refresh, so a run on TODAY reads as already refreshed.
    monkeypatch.setattr(tower_match, "_now", lambda: datetime(2026, 3, 12, 2, 30, tzinfo=UTC))
    monkeypatch.setattr(tower_match.time, "sleep", lambda _s: None)
    monkeypatch.setattr(aircraft_db, "DB_DIR", tmp_path / "aircraft_db")
    for name in ("_db", "_db_identity", "_bad_identity"):
        monkeypatch.setattr(aircraft_db, name, None)
    path = tmp_path / "aircraft_db" / aircraft_db.FILE_NAME
    path.parent.mkdir()
    hexes = np.arange(0xA00000, 0xA00000 + AIRCRAFT, dtype=np.uint32)
    type_idx = np.zeros(AIRCRAFT, dtype=np.uint16)
    np.savez(path, hex=hexes, type_idx=type_idx, types=np.array(["B738"]), schema=np.array(1))
    built = World(tmp_path)
    monkeypatch.setattr(performance_store, "DATA_DIR", built.data)
    yield built
    monkeypatch.setattr(performance_store, "DATA_DIR", tmp_path / "missing")
    performance_store.reload()


def test_a_run_publishes_the_real_nodes_and_nothing_of_the_simulated_world(world):
    world.registry(config_row(), config_row(OTHER, "QNUL"), config_row(SYNTHETIC, "QAAA", tx_lat=11.0))
    world.day(YESTERDAY, NODE, OTHER, SYNTHETIC)

    marker = world.run()

    assert (marker["nodes"], marker["failed"]) == (2, 0)
    doc = world.file()
    assert doc.keys() >= PINNED_KEYS
    assert doc["schema"] == 1 and doc["node_id"] == NODE and doc["window"] == ["2026-03-11", "2026-03-11"]
    assert (doc["opportunities"], doc["detections"], doc["detections_tags"]) == (AIRCRAFT, TAGGED, TAGGED)
    assert doc["detections_claims"] == 0
    assert (doc["denominator"], doc["numerator"], doc["threshold_db"]) == ("aggregator", "tags", 5.0)
    assert doc["classes_with_evidence"] == ["narrowbody"]
    assert doc["tower"] == {"callsign": "QAAA", "eirp_dbm": 81.2, "status": "matched"}
    assert doc["system_constant_db"] is not None

    table = json.loads(world.table(YESTERDAY).read_text())
    assert (table["opportunities"], table["detections_tags"]) == (AIRCRAFT, TAGGED)

    no_power = world.file(OTHER)
    assert no_power["tower"] == {"callsign": "QNUL", "eirp_dbm": None, "status": "matched"}
    assert no_power["system_constant_db"] is None

    performance_store.reload()
    assert set(performance_store.all()) == {NODE, OTHER}
    assert not any(SYNTHETIC in name for name in world.everything())


def test_a_second_run_adds_nothing(world, monkeypatch):
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    world.run()
    before = world.everything()
    read = []

    def reading(real):
        return lambda node, day, **kwargs: read.append(day) or real(node, day, **kwargs)

    monkeypatch.setattr(pdata, "snr_histogram", reading(pdata.snr_histogram))
    monkeypatch.setattr(pdata, "node_day_table", reading(pdata.node_day_table))

    world.run(handler=lambda request: pytest.fail("the towers were asked twice in a day"))

    assert world.everything() == before
    assert YESTERDAY not in read
    assert len(read) == 6  # one listing of each day with no table


_KILLED_RUN = """
import os, sys
from datetime import date
from services import performance_job

target, today, *roots = sys.argv[1:]
replace = os.replace
os.replace = lambda source, path: os._exit(9) if str(path).endswith(target) else replace(source, path)
names = ("archive_root", "adsb_root", "registry", "data_dir")
performance_job.run_daily(
    **dict(zip(names, roots)), tower_url="http://towers.example.com", today=date.fromisoformat(today)
)
"""


def test_a_killed_run_leaves_the_previous_file_and_the_next_run_completes(world):
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    world.run()
    path = world.data / f"performance_{NODE}.json"
    path.write_bytes(b'{"previous": true}')
    marker = (world.data / "performance" / "last_run.json").read_bytes()
    roots = world.roots()
    argv = [path.name, TODAY.isoformat(), roots["archive_root"], roots["adsb_root"], roots["registry"], world.data]

    killed = subprocess.run([sys.executable, "-c", _KILLED_RUN, *map(str, argv)], cwd=BACKEND_DIR)

    assert killed.returncode == 9
    assert path.read_bytes() == b'{"previous": true}'
    assert len(list(world.data.glob(f".performance_{NODE}.json.*.tmp"))) == 1
    assert (world.data / "performance" / "last_run.json").read_bytes() == marker
    an_hour_ago = time.time() - 3601
    for left in world.data.glob(".*.tmp"):
        os.utime(left, (an_hour_ago, an_hour_ago))

    world.run()

    assert world.file()["opportunities"] == AIRCRAFT
    assert list(world.data.glob(".*.tmp")) == []


def test_a_run_waits_for_another_run_and_leaves_its_temp_file_alone(world, caplog):
    caplog.set_level("INFO", logger="services.performance_job")
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    lock = world.data / "performance" / ".job.lock"
    lock.parent.mkdir(parents=True)
    held = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX)
    live = world.data / f".performance_{NODE}.json.{'a' * 32}.tmp"
    live.write_text("{}")
    marker = world.data / "performance" / "last_run.json"
    run = threading.Thread(target=world.run, daemon=True)
    run.start()
    try:
        run.join(timeout=1.0)
        assert run.is_alive()
        assert not (world.data / "tower_match.json").exists() and not marker.exists()
        assert "waiting for another run to end" in caplog.text
    finally:
        os.close(held)
        run.join(timeout=60)

    assert json.loads(marker.read_text())["nodes"] == 1
    assert live.read_text() == "{}"


def test_a_handover_inside_the_window_gives_an_owner_file_of_the_later_whole_days(world):
    # Its start was written for the last owner, so the stamp of the write that changed hands is the tenure.
    world.registry(config_row(), claim=claim_row("owner-one", datetime(2026, 3, 9, 15, tzinfo=UTC)))
    for day in (date(2026, 3, 9), date(2026, 3, 10), YESTERDAY):
        world.day(day, NODE)

    world.run()

    assert world.file()["window"] == ["2026-03-09", "2026-03-11"]
    assert world.file()["opportunities"] == 3 * AIRCRAFT
    owner = world.file(kind=".owner")
    assert owner["window"] == ["2026-03-10", "2026-03-11"]
    assert owner["opportunities"] == 2 * AIRCRAFT
    performance_store.reload()
    assert performance_store.get_owner(NODE) == owner

    world.registry(claim=claim_row("owner-two", datetime(2026, 3, 11, 15, tzinfo=UTC)))

    world.run()

    assert not (world.data / f"performance_{NODE}.owner.json").exists()


_AUDITED_RUN = """
import json, sys

opened = []
sys.addaudithook(lambda event, args: opened.append(str(args[0])) if event == "sqlite3.connect" else None)
from scripts import node_performance

code = node_performance.main(sys.argv[1:])
print(json.dumps({"code": code, "opened": opened, "loaded": sorted({"main", "core.state"} & set(sys.modules))}))
"""


def test_the_child_opens_the_registry_read_only_and_loads_neither_main_nor_the_app_state(world, capsys):
    world.registry(config_row())
    roots = world.roots()
    argv = ["--daily", "--registry", roots["registry"], "--data-dir", world.data]
    argv += ["--archive-root", roots["archive_root"], "--adsb-root", roots["adsb_root"]]

    run = subprocess.run(
        [sys.executable, "-c", _AUDITED_RUN, *map(str, argv)],
        cwd=BACKEND_DIR,
        # Nothing listens here: no test reaches a tower service.
        env={**os.environ, "TOWER_FINDER_URL": "http://127.0.0.1:9"},
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout.splitlines()[-1])
    assert report["code"] == 0
    assert report["loaded"] == []
    assert len(report["opened"]) == 1 and report["opened"][0].endswith("registry.db?mode=ro")
    assert json.loads((world.data / "performance" / "last_run.json").read_text())["nodes"] == 1

    from scripts import node_performance

    with pytest.raises(SystemExit):
        node_performance.main(["--daily", "--node", NODE])
    assert "--daily takes no --node" in capsys.readouterr().err


def test_a_tower_change_leaves_only_whole_days_on_the_end_tower(world, monkeypatch):
    changed = datetime(2026, 3, 9, 12, tzinfo=UTC)
    new = config_row(callsign="QBBB", tx_lat=10.4, version=2, created_at=changed)
    world.registry(config_row(superseded_at=changed), new)
    world.day(date(2026, 3, 8), NODE)
    world.day(date(2026, 3, 9), NODE)
    world.data.mkdir()
    path = world.data / f"performance_{NODE}.json"
    path.write_bytes(b'{"previous": true}')
    asked = []

    def finder(request):
        asked.append(request.url.params["lat"])
        return tower_finder(request)

    world.run(today=date(2026, 3, 10), handler=finder)

    assert sorted(asked) == ["10.2000", "10.4000"]
    assert path.read_bytes() == b'{"previous": true}'  # the last day saw two towers

    world.day(date(2026, 3, 10), NODE, config=new)
    world.day(YESTERDAY, NODE, config=new)
    entered, fit_fleet = [], pfit.fit_fleet
    monkeypatch.setattr(pfit, "fit_fleet", lambda runs: entered.extend(runs.values()) or fit_fleet(runs))

    world.run()

    doc = world.file()
    assert doc["window"] == ["2026-03-10", "2026-03-11"]
    assert doc["opportunities"] == 2 * AIRCRAFT
    assert doc["tower"] == {"callsign": "QBBB", "eirp_dbm": 74.5, "status": "matched"}
    assert [merged["days"] for merged in entered] == [["2026-03-08"], ["2026-03-10", "2026-03-11"]]


def test_a_day_without_files_gets_no_table_and_a_table_outlives_its_files(world):
    world.registry(config_row())
    world.day(date(2026, 3, 10), NODE)
    days = world.data / "performance" / "days"
    (days / "2026-02-04").mkdir(parents=True)
    (days / "2026-02-05").mkdir()

    world.run()

    table = world.table(date(2026, 3, 10))
    before = table.read_bytes()
    assert not world.table(YESTERDAY).exists()
    assert sorted(path.name for path in days.iterdir()) == ["2026-02-05", "2026-03-10"]

    shutil.rmtree(world.root / "archive")
    world.run()

    assert table.read_bytes() == before
    assert not world.table(YESTERDAY).exists()


def test_a_day_whose_adsb_could_not_be_read_fills_in_and_a_quiet_day_stays_as_written(world):
    quiet = world.table(date(2026, 3, 10))
    world.registry(config_row())
    world.day(date(2026, 3, 10), NODE)
    shutil.rmtree(world.root / "adsb")
    world.day(YESTERDAY, NODE)
    parts = list((world.root / "adsb").rglob("part-*.parquet"))
    try:
        for part in parts:
            part.chmod(0)
        world.run()
    finally:
        for part in parts:
            part.chmod(0o644)

    assert json.loads(world.table(YESTERDAY).read_text())["opportunities"] == 0
    assert world.file()["rank_status"] == "no_opportunities"
    # Without the job's indent, so a run that wrote this table again would change its bytes.
    written = json.dumps(json.loads(quiet.read_text())).encode()
    quiet.write_bytes(written)

    world.run()

    assert json.loads(world.table(YESTERDAY).read_text())["opportunities"] == AIRCRAFT
    assert world.file()["opportunities"] == AIRCRAFT
    assert quiet.read_bytes() == written


def test_the_class_table_is_fitted_when_missing_or_old_and_a_failed_fit_keeps_what_there_was(world, monkeypatch):
    world.registry(config_row(), config_row("radar-bent"))
    world.day(YESTERDAY, NODE)
    world.day(YESTERDAY, "radar-bent", skew_us=5.0)
    path = world.data / "performance" / "class_table.json"
    entered = []

    def nothing_anchors(runs):
        entered[:] = [merged["node_id"] for merged in runs.values()]

    monkeypatch.setattr(pfit, "fit_fleet", nothing_anchors)
    world.run()

    assert world.file("radar-bent")["rank_status"] == "config_suspect"
    assert entered == [NODE]
    assert not path.exists()
    on_defaults = world.file()["system_constant_db"]
    assert on_defaults is not None

    fitted = {"schema": 1, "class_offsets_db": {"narrowbody": 3.0}, "nodes": 1, "opportunities": AIRCRAFT}
    monkeypatch.setattr(pfit, "fit_fleet", lambda runs: fitted)
    world.run()

    assert json.loads(path.read_text()) == {**fitted, "fitted_on": "2026-03-12"}
    assert world.file()["system_constant_db"] == pytest.approx(on_defaults - 3.0, abs=0.01)

    path.write_text(json.dumps({**fitted, "fitted_on": "2026-02-11"}))
    monkeypatch.setattr(pfit, "fit_fleet", lambda runs: pytest.fail("a table under 30 days old was refitted"))
    world.run()

    path.write_text(json.dumps({**fitted, "fitted_on": "2026-02-10"}))
    entered.clear()
    monkeypatch.setattr(pfit, "fit_fleet", nothing_anchors)
    world.run()

    assert entered == [NODE]
    assert json.loads(path.read_text())["fitted_on"] == "2026-02-10"
    assert world.file()["system_constant_db"] == pytest.approx(on_defaults - 3.0, abs=0.01)


def test_a_learned_field_of_view_narrows_the_opportunities(world):
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    world.run()
    assert world.file()["opportunities"] == AIRCRAFT

    narrowed = world.root / "narrowed"
    narrowed.mkdir()
    learned = EmpiricalCoverageState(10.0, 20.0, max_range_km=100.0, prior_azimuth_deg=45.0, prior_width_deg=20.0)
    learned.save_to_file(str(narrowed / f"empirical_{NODE}.json"))

    world.run(data_dir=narrowed)

    assert world.file(data_dir=narrowed)["opportunities"] == AIRCRAFT // 2


class Stop(BaseException):
    """Ends the task's endless loop from inside a sleep."""


def _sleeps_until(monkeypatch, last_delay_s):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if delay == last_delay_s or len(sleeps) == 3:
            raise Stop

    monkeypatch.setattr(performance_refresh.asyncio, "sleep", sleep)
    return sleeps


def _last_run(world, finished_at):
    path = world.data / "performance" / "last_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"finished_at": finished_at, "nodes": 1, "failed": 0}))


async def test_the_child_runs_nice_daily_and_on_the_system_memory_pool(monkeypatch):
    seen = {}
    real = asyncio.create_subprocess_exec

    async def spawn(*command, cwd, env):
        seen.update(command=list(command), cwd=cwd, pool=env["ARROW_DEFAULT_MEMORY_POOL"])
        return await real(sys.executable, "-c", "raise SystemExit(0)")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    assert await performance_refresh.run_refresh() is True

    assert seen == {
        "command": ["nice", "-n", "10", sys.executable, "-m", "scripts.node_performance", "--daily"],
        "cwd": BACKEND_DIR,
        "pool": "system",
    }


async def test_a_child_past_its_timeout_is_killed_and_the_store_reloaded(world, monkeypatch):
    _last_run(world, "2026-03-11T02:31:00+00:00")
    now = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(performance_refresh, "_now", lambda: now)
    doc = {
        "schema": 1,
        "node_id": NODE,
        "window": ["2026-03-11", "2026-03-11"],
        "rank_status": None,
        "tower": {"callsign": None, "eirp_dbm": None, "status": None},
    }
    pid_file = world.root / "pid"
    script = (
        f"echo '{json.dumps(doc)}' > {world.data / f'performance_{NODE}.json'}; echo $$ > {pid_file}; exec sleep 60"
    )
    monkeypatch.setattr(performance_refresh, "_child_command", lambda: ["/bin/sh", "-c", script])
    monkeypatch.setenv("PERFORMANCE_JOB_TIMEOUT_S", "2")
    monkeypatch.setenv("PERFORMANCE_JOB_START_DELAY_S", "120")
    monkeypatch.setitem(state.task_error_counts, "performance_refresh", 0)
    sleeps = _sleeps_until(monkeypatch, PERFORMANCE_RETRY_S)

    started = time.monotonic()
    with pytest.raises(Stop):
        await performance_refresh.performance_refresh_task()

    assert time.monotonic() - started < 30  # not the child's own 60 s
    assert sleeps == [120, 21600]
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert performance_store.get(NODE) == doc
    assert state.task_error_counts["performance_refresh"] == 1


async def test_a_run_where_every_node_raises_exits_non_zero_and_never_stamps_health(world, monkeypatch, caplog):
    world.registry(config_row(), config_row(OTHER, "QNUL"))
    # The child reads the window of its own clock, and a table that won't parse makes its node raise.
    day = datetime.now(UTC).date() - timedelta(days=2)
    for node in (NODE, OTHER):
        world.table(day, node).parent.mkdir(parents=True, exist_ok=True)
        world.table(day, node).write_text("not a table")
    child = [sys.executable, "-m", "scripts.node_performance", "--daily", "--data-dir", str(world.data)]
    child += ["--registry", str(world.roots()["registry"])]
    monkeypatch.setattr(performance_refresh, "_child_command", lambda: child)
    monkeypatch.setenv("TOWER_FINDER_URL", "http://127.0.0.1:9")
    monkeypatch.setitem(state.task_error_counts, "performance_refresh", 0)
    monkeypatch.setattr(state, "task_last_success", {})
    _sleeps_until(monkeypatch, None)  # past the retry, where a marker would read as a good run

    with pytest.raises(Stop):
        await performance_refresh.performance_refresh_task()

    assert "exited with code 1" in caplog.text
    assert state.task_error_counts["performance_refresh"] == 2
    assert state.task_last_success == {}


async def test_a_run_that_is_not_due_stamps_health_and_starts_no_child(world, monkeypatch):
    _last_run(world, "2026-03-11T02:31:00+00:00")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: pytest.fail("a child was started"))
    monkeypatch.setenv("PERFORMANCE_JOB_START_DELAY_S", "120")
    monkeypatch.setitem(state.task_last_success, "performance_refresh", 0.0)
    # Before today's slot the slot in force is yesterday's, which that run followed.
    now = datetime(2026, 3, 12, 1, 0, tzinfo=UTC)
    monkeypatch.setattr(performance_refresh, "_now", lambda: now)
    sleeps = _sleeps_until(monkeypatch, 5400.0)

    with pytest.raises(Stop):
        await performance_refresh.performance_refresh_task()

    assert sleeps == [120, 5400.0]
    assert state.task_last_success["performance_refresh"] > 0.0


async def test_the_boot_serves_the_files_and_the_started_task_reports_to_the_stale_check(world, monkeypatch):
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    world.run()
    monkeypatch.setenv("PERFORMANCE_JOB_START_DELAY_S", "0")
    monkeypatch.setattr(state, "task_last_success", {})
    monkeypatch.setattr(main, "restore_snapshot", lambda: {})
    monkeypatch.setattr(main, "save_snapshot", lambda: None)
    monkeypatch.setattr(main.state.node_analytics, "save_coverage_maps", lambda: None)
    for name in (  # noqa: SIM905
        "reputation_evaluator prune_synthetic_nodes retire_disposable_nodes_task adsb_truth_fetcher "
        "aircraft_flush_task feed_gc_task archive_flush_task track_flush_task adsb_snapshot_task "
        "archive_lifecycle_task users_backup_task analytics_refresh_task coverage_constraints_task "
        "storage_refresh_task health_monitor_task heartbeat_task frame_processor_loop"
    ).split():
        monkeypatch.setattr(main, name, lambda *_: asyncio.Event().wait())
    monkeypatch.setattr(main.detection_mirror, "mirror_task", lambda: asyncio.Event().wait())
    async with main.lifespan(main.app):
        assert performance_store.get(NODE) == world.file()
        async with asyncio.timeout(5):
            while "performance_refresh" not in state.task_last_success:
                await asyncio.sleep(0.01)
        state.task_last_success["performance_refresh"] -= 2 * 86400 + 1
        assert "performance_refresh" in task_registry.get_stale_tasks()


def test_a_file_that_cannot_be_read_keeps_its_value_and_one_for_another_node_is_left_out(world):
    world.registry(config_row())
    world.day(YESTERDAY, NODE)
    world.run()
    path = world.data / f"performance_{NODE}.json"
    shutil.copy(path, world.data / f"performance_{OTHER}.json")
    performance_store.reload()
    assert performance_store.get(NODE) == world.file() and performance_store.get(OTHER) is None
    path.chmod(0)
    try:
        performance_store.reload()
    finally:
        path.chmod(0o644)
    assert performance_store.get(NODE) == world.file()
