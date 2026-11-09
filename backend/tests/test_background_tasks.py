"""Tests for background tasks — aircraft flush, periodic tasks."""

import time

import pytest

from core import state

# ── _real_only_dict ─────────────────────────────────────────────────


class TestRealOnlyDict:
    def test_filters_synthetic_nodes(self):
        from services.tasks.aircraft_flush import _real_only_dict

        state.connected_nodes["real-1"] = {"is_synthetic": False, "status": "active"}
        state.connected_nodes["synth-1"] = {"is_synthetic": True, "status": "active"}
        data = {
            "now": time.time(),
            "aircraft": [
                {"hex": "R1", "node_id": "real-1", "multinode": False},
                {"hex": "S1", "node_id": "synth-1", "multinode": False},
            ],
            "detection_arcs": [
                {"node_id": "real-1", "arc": []},
                {"node_id": "synth-1", "arc": []},
            ],
        }
        result = _real_only_dict(data)
        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "R1"
        assert len(result["detection_arcs"]) == 1
        assert result["detection_arcs"][0]["node_id"] == "real-1"

    def test_includes_multinode_with_real_contributor(self):
        from services.tasks.aircraft_flush import _real_only_dict

        state.connected_nodes["real-1"] = {"is_synthetic": False, "status": "active"}
        state.connected_nodes["synth-1"] = {"is_synthetic": True, "status": "active"}
        data = {
            "now": time.time(),
            "aircraft": [
                {
                    "hex": "M1",
                    "node_id": "synth-1",
                    "multinode": True,
                    "contributing_node_ids": ["synth-1", "real-1"],
                },
            ],
            "detection_arcs": [],
        }
        result = _real_only_dict(data)
        assert len(result["aircraft"]) == 1
        assert result["aircraft"][0]["hex"] == "M1"

    def test_empty_data(self):
        from services.tasks.aircraft_flush import _real_only_dict

        result = _real_only_dict({"now": 0})
        assert result["aircraft"] == []
        assert result["detection_arcs"] == []
        assert result["messages"] == 0


# ── broadcast_aircraft ───────────────────────────────────────────────────────


class TestBroadcastAircraft:
    @pytest.mark.asyncio
    async def test_updates_state(self):
        from services.tasks.aircraft_flush import broadcast_aircraft

        # Synthetic-prefixed id: it is published as itself, so the served bytes
        # differ from this frame only in the field name.  An unregistered real
        # id has no node_ref and is dropped at the publication boundary.
        data = {
            "now": time.time(),
            "aircraft": [{"hex": "BC01", "node_id": "test-n1"}],
            "detection_arcs": [],
            "ground_truth": {},
        }
        import orjson

        published_bytes = orjson.dumps({**data, "aircraft": [{"hex": "BC01", "node_ref": "test-n1"}]})
        await broadcast_aircraft(data)
        assert state.latest_aircraft_json == data
        assert state.latest_aircraft_json_bytes == published_bytes
        assert state.latest_real_aircraft_json_bytes != b""


# ── Stale task detection helpers ─────────────────────────────────────────────


class TestTaskTimestamps:
    def test_task_last_success_updated(self):
        """Verify that task_last_success is a dict that can track timestamps."""
        state.task_last_success["test_task"] = time.time()
        assert "test_task" in state.task_last_success
        state.task_last_success.pop("test_task", None)

    def test_task_error_counts(self):
        """Verify task_error_counts tracks errors."""
        orig = state.task_error_counts.get("test_task", 0)
        state.task_error_counts["test_task"] = orig + 1
        assert state.task_error_counts["test_task"] == orig + 1
        state.task_error_counts.pop("test_task", None)

    def test_bump_task_error_is_atomic_under_threads(self):
        """N threads bumping the same key concurrently must end at N.

        The bare ``task_error_counts[name] += 1`` this replaced is a
        read-modify-write; the bump sites run on the event loop, the frame
        workers and the solver threads at once, so a lost update turned a
        failing task into a quiet one.
        """
        import threading

        state.task_error_counts.pop("race_task", None)
        n_threads, per_thread = 16, 500
        start = threading.Barrier(n_threads)

        def worker():
            start.wait()
            for _ in range(per_thread):
                state.bump_task_error("race_task")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert state.task_error_counts["race_task"] == n_threads * per_thread
        finally:
            state.task_error_counts.pop("race_task", None)


# ── analytics_refresh pacing ─────────────────────────────────────────────────


class TestAnalyticsRefreshPacing:
    """The refresh loop must hold a period, not a gap.

    e2e/specs/nodes.spec.ts waits a fixed window for a newly registered node
    to reach /api/radar/nodes, and derives that window from this interval. Under
    fixed-delay the period is the interval PLUS the cycle cost, so the window is
    only wide enough while the cycle happens to be cheap.
    """

    async def _run_one_cycle(self, monkeypatch, work_s: float, interval_s: float, fail: bool = False):
        """Drive the task through exactly one cycle; return the delay it then asked for.

        The cycle's cost is charged to a fake clock; this helper must not sleep for
        real, which would load the suite's other timing-sensitive tests.
        """
        import asyncio

        from services.tasks import analytics_refresh as mod

        class Clock:
            """Stands in for the module's `time`; the loop uses monotonic() and time()."""

            def __init__(self):
                self.now = 1000.0

            def monotonic(self):
                return self.now

            def time(self):
                return 1_700_000_000.0

        clock = Clock()
        monkeypatch.setattr(mod, "time", clock)
        monkeypatch.setattr(mod, "ANALYTICS_REFRESH_INTERVAL_S", interval_s)

        def advance_the_clock():
            clock.now += work_s
            if fail:
                raise RuntimeError("cycle failed late")

        monkeypatch.setattr(mod, "_refresh_analytics_and_nodes", advance_the_clock)
        monkeypatch.setattr(state.node_analytics, "maybe_auto_save", lambda: None)

        import routes.admin

        monkeypatch.setattr(routes.admin, "check_node_health", lambda: None)

        sleeps: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *a, **kw):
            sleeps.append(delay)
            # Two calls: the task's own startup sleep, then the pacing sleep we
            # are measuring. Break out rather than loop for ever.
            if len(sleeps) >= 2:
                raise asyncio.CancelledError
            return await real_sleep(0)

        class AsyncioShim:
            """Only what the task resolves through `asyncio`.

            Patching the real module's `sleep` would hand the fake to every other
            coroutine on the loop and cancel it.
            """

            def __init__(self):
                self.sleep = fake_sleep

            def get_event_loop(self):
                return asyncio.get_event_loop()

        monkeypatch.setattr(mod, "asyncio", AsyncioShim())

        # Both are process-global. setitem, not delitem: delitem(raising=False)
        # records no undo for an absent key, so the loop's own write would outlive
        # the test. setitem records the prior value or its absence either way, and
        # the undo restores that rather than whatever the loop last wrote.
        had_errors = state.task_error_counts.get("analytics_refresh", 0)
        monkeypatch.setitem(state.task_last_success, "analytics_refresh", 0.0)
        monkeypatch.setitem(state.task_error_counts, "analytics_refresh", had_errors)

        with pytest.raises(asyncio.CancelledError):
            await mod.analytics_refresh_task()

        assert len(sleeps) == 2, sleeps
        if fail:
            assert state.task_error_counts.get("analytics_refresh", 0) == had_errors + 1
            assert state.task_last_success.get("analytics_refresh") == 0.0
        else:
            # Pacing alone would also hold for a cycle that threw, so pin the cycle
            # to the success path before trusting the delay.
            assert state.task_last_success.get("analytics_refresh") == clock.time()
            assert state.task_error_counts.get("analytics_refresh", 0) == had_errors
        return sleeps[1]

    async def test_the_sleep_absorbs_the_cycle_cost(self, monkeypatch):
        work_s, interval_s = 8.0, 30.0
        delay = await self._run_one_cycle(monkeypatch, work_s, interval_s)

        # Fixed-delay would ask for the whole interval regardless of the work.
        assert delay == pytest.approx(interval_s - work_s), (
            f"asked for {delay:.3f}s after {work_s:.2f}s of work; "
            f"period would be {delay + work_s:.3f}s against a {interval_s}s interval"
        )

    async def test_a_cycle_longer_than_the_interval_still_leaves_a_gap(self, monkeypatch):
        delay = await self._run_one_cycle(monkeypatch, work_s=45.0, interval_s=30.0)
        assert delay == pytest.approx(3.0)  # the floor, not zero

    async def test_a_failed_cycle_waits_the_whole_interval(self, monkeypatch):
        """A failure must not be paced: nothing else here backs off.

        Pacing one would retry a broken dependency every (time-to-fail + floor),
        so a cycle failing late would go from a 30 s gap to a 3 s one.
        """
        delay = await self._run_one_cycle(monkeypatch, work_s=28.0, interval_s=30.0, fail=True)
        assert delay == pytest.approx(30.0)


# ── aircraft_db_refresh ──────────────────────────────────────────────────────


class TestAircraftDbRefresh:
    @pytest.fixture(autouse=True)
    def own_database(self, monkeypatch, tmp_path):
        from services import aircraft_db

        monkeypatch.setattr(aircraft_db, "DB_DIR", tmp_path / "db")
        for name in ("_db", "_db_identity", "_bad_identity"):
            monkeypatch.setattr(aircraft_db, name, None)

    @staticmethod
    def _point_at(monkeypatch, mictronics_url: str, faa_url: str):
        from services.tasks import aircraft_db_refresh as mod

        monkeypatch.setenv("AIRCRAFT_DB_MICTRONICS_URL", mictronics_url)
        monkeypatch.setenv("AIRCRAFT_DB_FAA_URL", faa_url)
        return mod

    async def test_the_child_downloads_builds_and_cleans_up(self, monkeypatch, tmp_path):
        import functools
        import threading
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

        from services import aircraft_db
        from tests.test_aircraft_db import write_sources

        write_sources(tmp_path / "src", {"A00001": "C172"}, [("A00002", "0000001"), ("A00001", "0000001")])
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path / "src"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        mod = self._point_at(monkeypatch, f"{base}/indexedDB_old.zip", f"{base}/ReleasableAircraft.zip")
        try:
            assert await mod.run_refresh() is True
        finally:
            server.shutdown()
            server.server_close()

        assert aircraft_db.current().types_for(["a00001", "a00002"]) == ["C172", "C172"]
        assert not (tmp_path / "db" / "download").exists()

    async def test_an_unreachable_source_keeps_the_last_good_file(self, monkeypatch, tmp_path):
        import socket

        from services import aircraft_db
        from tests.test_aircraft_db import write_sources

        out = tmp_path / "db" / aircraft_db.FILE_NAME
        aircraft_db.build(*write_sources(tmp_path / "src", {"A00001": "C172"}, [("A00001", "0000001")]), out)
        before = out.read_bytes()
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{closed.getsockname()[1]}/x.zip"
        mod = self._point_at(monkeypatch, url, url)

        assert await mod.run_refresh() is False
        assert out.read_bytes() == before
        assert aircraft_db.current().types_for(["a00001"]) == ["C172"]

    async def test_a_child_past_the_timeout_is_killed(self, monkeypatch, tmp_path):
        import os

        from services.tasks import aircraft_db_refresh as mod

        download = tmp_path / "db" / "download"
        download.mkdir(parents=True)
        pid_file = tmp_path / "pid"
        monkeypatch.setattr(mod, "_child_command", lambda: ["/bin/sh", "-c", f"echo $$ > {pid_file}; exec sleep 60"])

        started = time.monotonic()
        assert await mod.run_refresh(timeout_s=3.0) is False
        assert time.monotonic() - started < 30  # not the child's own 60 s
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert not download.exists()

    async def test_a_shutdown_stops_the_child(self, monkeypatch, tmp_path):
        import asyncio
        import os

        from services.tasks import aircraft_db_refresh as mod

        download = tmp_path / "db" / "download"
        download.mkdir(parents=True)
        pid_file = tmp_path / "pid"
        monkeypatch.setattr(mod, "_child_command", lambda: ["/bin/sh", "-c", f"echo $$ > {pid_file}; exec sleep 60"])

        task = asyncio.create_task(mod.run_refresh(timeout_s=30.0))
        async with asyncio.timeout(20):
            while not (pid_file.exists() and pid_file.read_text().strip()):
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert not download.exists()

    async def _run_task(
        self, monkeypatch, tmp_path, *, enabled=True, file_age_s=None, garbage=False, ok=False, builds=False
    ):
        import os
        import types

        from services import aircraft_db
        from services.tasks import aircraft_db_refresh as mod
        from tests.test_aircraft_db import write_sources

        monkeypatch.setenv("AIRCRAFT_DB_ENABLED", "1" if enabled else "0")
        monkeypatch.setenv("AIRCRAFT_DB_START_DELAY_S", "120")
        monkeypatch.setattr(mod, "AIRCRAFT_DB_REFRESH_INTERVAL_S", 604800)
        monkeypatch.setattr(mod, "AIRCRAFT_DB_RETRY_S", 21600)
        monkeypatch.setitem(state.task_last_success, "aircraft_db_refresh", 0.0)
        monkeypatch.setitem(state.task_error_counts, "aircraft_db_refresh", 0)
        path = tmp_path / "db" / aircraft_db.FILE_NAME
        if file_age_s is not None:
            aircraft_db.build(*write_sources(tmp_path / "src", {"A00001": "C172"}, [("A00001", "0000001")]), path)
            os.utime(path, (time.time() - file_age_s,) * 2)
        if garbage:
            path.write_bytes(b"not a database")

        class Stop(BaseException):  # past the task's except Exception
            pass

        runs: list[bool] = []
        sleeps: list[float] = []

        async def run_refresh():
            runs.append(True)
            if len(runs) == 3:
                raise Stop
            if builds:
                aircraft_db.build(*write_sources(tmp_path / "built", {"A00001": "C172"}, [("A00001", "0000001")]), path)
            return ok

        async def sleep(delay):
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise Stop

        monkeypatch.setattr(mod, "run_refresh", run_refresh)
        monkeypatch.setattr(mod, "asyncio", types.SimpleNamespace(sleep=sleep))
        try:
            await mod.aircraft_db_refresh_task()
        except Stop:
            pass
        return runs, sleeps

    async def test_the_task_refreshes_only_what_is_due(self, monkeypatch, tmp_path):
        download = tmp_path / "db" / "download"
        download.mkdir(parents=True)
        runs, sleeps = await self._run_task(monkeypatch, tmp_path, enabled=False)
        assert (runs, sleeps) == ([], [])
        assert not download.exists()

        runs, sleeps = await self._run_task(monkeypatch, tmp_path, file_age_s=3600)
        assert runs == []
        assert sleeps == [120, pytest.approx(601200, abs=60)]
        assert state.task_last_success["aircraft_db_refresh"] > 0.0

        runs, sleeps = await self._run_task(monkeypatch, tmp_path, garbage=True)
        assert runs == [True]
        assert sleeps == [120, 21600]
        assert state.task_last_success["aircraft_db_refresh"] == 0.0

        (tmp_path / "db" / "aircraft_db.npz").unlink()
        runs, sleeps = await self._run_task(monkeypatch, tmp_path, ok=True)
        assert runs == [True]
        assert sleeps == [120, 21600]
        assert state.task_error_counts["aircraft_db_refresh"] == 1

        runs, sleeps = await self._run_task(monkeypatch, tmp_path, ok=True, builds=True)
        assert runs == [True]
        assert sleeps == [120, pytest.approx(604800, abs=60)]
        assert state.task_error_counts["aircraft_db_refresh"] == 0
        assert state.task_last_success["aircraft_db_refresh"] > 0.0
