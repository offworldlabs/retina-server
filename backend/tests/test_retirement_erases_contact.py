"""Retiring a node erases its owner's contact details.

The row is there so a fault has somewhere to go, and a retired node has none
left, nor a node its owner could clear the row from. Every retirement erases it
once the retirement has gone through; one that is refused or fails leaves it.
Polled radars are retired in the database, and their tests are in
test_polled_radars.py.
"""

import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

import core.users
from core import state
from core.nodes import Node, NodeContact
from routes import admin
from services import node_retirement
from services.node_contact_store import upsert_contact
from services.tasks import periodic

GONE = "ret1a2b3c4d"
OTHER = "ret9f8e7d6c"
SKIPPED = "ret2b3c4d5e"
FAILED = "ret3a4b5c6d"

CONTACT = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "phone": None, "country": "GB"}


async def _with_contact(session, *node_ids: str) -> None:
    for node_id in node_ids:
        session.add(Node(node_id=node_id, node_ref=f"nde{node_id[-12:]:0>12}", status="active"))
    await session.flush()
    for node_id in node_ids:
        await upsert_contact(session, node_id, dict(CONTACT))
    await session.commit()


async def _held(session) -> set[str]:
    """The nodes holding a contact, read afresh after the request wrote."""
    session.expire_all()
    return set((await session.execute(select(NodeContact.node_id))).scalars())


def _in_memory(*node_ids: str) -> None:
    """Custody alone makes a node held, so retirement has something to clear."""
    for node_id in node_ids:
        state.chain_entries[node_id] = [{"seq": 1}]


@pytest.fixture
def contacts_will_not_erase(node_session):
    """The database refuses the erase, as it does a writer that waited out the
    busy timeout behind another."""
    engine = node_session.bind.sync_engine

    def refuse(_conn, _cursor, statement, parameters, *_args):
        if statement.startswith("DELETE FROM node_contacts"):
            raise OperationalError(statement, parameters, sqlite3.OperationalError("database is locked"))

    event.listen(engine, "before_cursor_execute", refuse)
    yield
    event.remove(engine, "before_cursor_execute", refuse)


def _event(contains: str) -> dict:
    """The latest admin event whose message says this."""
    return next(e for e in admin._events if contains in e["message"])


class TestRetiringOneNode:
    async def test_it_erases_the_contact_and_no_other(self, node_client, node_session):
        await _with_contact(node_session, GONE, OTHER)
        _in_memory(GONE)

        r = node_client.delete(f"/api/admin/nodes/{GONE}/state")

        assert r.status_code == 200
        assert r.json()["contact_erased"] is True
        assert GONE not in state.chain_entries
        assert await _held(node_session) == {OTHER}
        # Whether, never what.
        assert CONTACT["email"] not in r.text

    async def test_a_node_that_held_no_contact_says_none_was_erased(self, node_client, node_session):
        await _with_contact(node_session, OTHER)

        r = node_client.delete(f"/api/admin/nodes/{GONE}/state")

        assert r.status_code == 200
        assert r.json()["contact_erased"] is False
        assert "contact_error" not in r.json()
        assert await _held(node_session) == {OTHER}

    async def test_a_node_still_connected_keeps_its_contact_until_it_is_retired(self, node_client, node_session):
        await _with_contact(node_session, GONE)
        state.connected_nodes[GONE] = {"status": "active"}

        assert node_client.delete(f"/api/admin/nodes/{GONE}/state").status_code == 409
        assert await _held(node_session) == {GONE}

        del state.connected_nodes[GONE]
        assert node_client.delete(f"/api/admin/nodes/{GONE}/state").json()["contact_erased"] is True
        assert await _held(node_session) == set()

    async def test_a_force_the_allowlist_refuses_keeps_the_contact(self, node_client, node_session, monkeypatch):
        await _with_contact(node_session, GONE)
        state.connected_nodes[GONE] = {"status": "active"}
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")

        assert node_client.delete(f"/api/admin/nodes/{GONE}/state?force=true").status_code == 403
        assert await _held(node_session) == {GONE}

        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "")
        assert node_client.delete(f"/api/admin/nodes/{GONE}/state?force=true").status_code == 200
        assert await _held(node_session) == set()

    async def test_a_retirement_that_fails_partway_keeps_the_contact(self, node_client, node_session, monkeypatch):
        """Half retired is not retired: the retire-stale pass that finishes it
        is the one that erases."""
        await _with_contact(node_session, GONE)
        _in_memory(GONE)

        def boom(_node_id):
            raise OSError("coverage file is not writable")

        with monkeypatch.context() as m:
            m.setattr(state.node_analytics, "retire_node", boom)
            assert node_client.delete(f"/api/admin/nodes/{GONE}/state").status_code == 500
        assert await _held(node_session) == {GONE}

        assert node_client.delete(f"/api/admin/nodes/{GONE}/state").json()["contact_erased"] is True
        assert await _held(node_session) == set()

    async def test_a_contact_the_database_will_not_erase_is_reported_and_the_retirement_stands(
        self, node_client, node_session, contacts_will_not_erase, caplog
    ):
        await _with_contact(node_session, GONE)
        _in_memory(GONE)

        with caplog.at_level(logging.ERROR, logger=node_retirement.__name__):
            r = node_client.delete(f"/api/admin/nodes/{GONE}/state")

        assert r.status_code == 207
        body = r.json()
        assert body["contact_erased"] is False
        assert "database is locked" in body["contact_error"]
        # Retired all the same.
        assert body["custody"]["chain_entries"] == 1
        assert GONE not in state.chain_entries
        assert await _held(node_session) == {GONE}
        assert any(GONE in record.getMessage() for record in caplog.records)
        recorded = _event(f"Retired node state for {GONE}")
        assert recorded["severity"] == "error"
        assert "not erased" in recorded["message"]


class TestRetireStale:
    async def test_retired_nodes_lose_their_contacts_and_skipped_or_failed_ones_keep_them(
        self, node_client, node_session, monkeypatch
    ):
        await _with_contact(node_session, GONE, SKIPPED, FAILED, OTHER)
        _in_memory(GONE, SKIPPED, FAILED)
        real_retire = node_retirement.retire_node

        def retire(node_id, **kwargs):
            if node_id == SKIPPED:
                state.connected_nodes[SKIPPED] = {"status": "active"}
            if node_id == FAILED:
                raise OSError("coverage file is not writable")
            return real_retire(node_id, **kwargs)

        monkeypatch.setattr(node_retirement, "retire_node", retire)

        r = node_client.post("/api/admin/nodes/retire-stale")

        assert r.status_code == 207
        body = r.json()
        retired = {entry["node_id"]: entry for entry in body["retired"]}
        assert retired[GONE]["contact_erased"] is True
        assert [entry["node_id"] for entry in body["skipped"]] == [SKIPPED]
        assert [entry["node_id"] for entry in body["failed"]] == [FAILED]
        assert await _held(node_session) == {SKIPPED, FAILED, OTHER}

    async def test_a_clean_sweep_erases_and_answers_200(self, node_client, node_session):
        await _with_contact(node_session, GONE, OTHER)
        _in_memory(GONE)

        r = node_client.post("/api/admin/nodes/retire-stale")

        assert r.status_code == 200
        assert {e["node_id"]: e["contact_erased"] for e in r.json()["retired"]}[GONE] is True
        assert await _held(node_session) == {OTHER}

    async def test_contacts_the_database_will_not_erase_make_the_sweep_partial(
        self, node_client, node_session, contacts_will_not_erase
    ):
        """Every node retired and none failed, but a contact left behind is not
        a clean sweep, and a caller reading only the status must see that."""
        await _with_contact(node_session, GONE)
        _in_memory(GONE)

        r = node_client.post("/api/admin/nodes/retire-stale")

        assert r.status_code == 207
        body = r.json()
        assert body["failed"] == []
        entry = next(e for e in body["retired"] if e["node_id"] == GONE)
        assert entry["contact_erased"] is False
        assert "database is locked" in entry["contact_error"]
        assert await _held(node_session) == {GONE}
        recorded = _event("stale node(s)")
        assert recorded["severity"] == "error"
        assert GONE in recorded["meta"]["contact_not_erased"]


def _heard(seconds_ago: float, now: float) -> str:
    return datetime.fromtimestamp(now - seconds_ago, timezone.utc).isoformat()


class TestDisposableSweep:
    QUIET = "e2e-quiet-0001"
    HEARD = "e2e-heard-0001"

    async def test_the_task_erases_the_contacts_of_what_it_retires(self, node_session, monkeypatch):
        monkeypatch.setenv("NODE_FORCE_RETIRE_PREFIXES", "e2e-")
        monkeypatch.setattr(node_retirement, "_stale_last_pass", set())
        monkeypatch.setattr(periodic, "DISPOSABLE_SWEEP_INTERVAL_S", 0.0)
        monkeypatch.setattr(core.users, "async_session_maker", async_sessionmaker(node_session.bind))
        await _with_contact(node_session, self.QUIET, self.HEARD)
        now = time.time()
        state.connected_nodes[self.QUIET] = {
            "status": "disconnected",
            "last_heartbeat": _heard(node_retirement.DISPOSABLE_QUIET_S + 60, now),
        }
        state.connected_nodes[self.HEARD] = {"status": "active", "last_heartbeat": _heard(0, now)}

        task = asyncio.create_task(periodic.retire_disposable_nodes_task())
        try:
            for _ in range(500):
                await asyncio.sleep(0.01)
                if "retire_disposable_nodes" in state.task_last_success:
                    break
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert self.QUIET not in state.connected_nodes
        assert await _held(node_session) == {self.HEARD}
