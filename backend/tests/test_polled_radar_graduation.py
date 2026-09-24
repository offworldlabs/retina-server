"""Tests for an administrator graduating a polled radar, or returning it to probation.

Graduation releases an unvetted operator's data into the solve, the archive and
the public map, so it applies only to the epoch the administrator was shown: a
box that took over the slot since, or a declaration that changed, is refused
rather than released on someone else's judgement. Each decision is recorded in
node_events with who made it and at which epoch.
"""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from core.nodes import Node, NodeEvent, PolledRadar
from core.users import ANONYMOUS_USER, DATABASE_URL, async_session_maker
from services import node_auth, probation, publication

_RADAR = "bla0a1b2c3d"
_OTHER = "bla1f2e3d4c"
_FLEET = "ret1a2b3c4d"
_ADMIN = f"admin:{ANONYMOUS_USER['email']}"
_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fence_on(monkeypatch):
    monkeypatch.delenv("POLLED_RADAR_PROBATION_ENABLED", raising=False)


def _run(coro):
    result = asyncio.run(coro)
    # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
    # restores one for the same reason.
    asyncio.set_event_loop(asyncio.new_event_loop())
    return result


def _seed(node_id: str = _RADAR, *, epoch: int = 2, trust_state: str = "probation") -> None:
    async def _go():
        async with async_session_maker() as session:
            session.add(Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), board_model="blah2"))
            await session.flush()
            session.add(
                PolledRadar(
                    node_id=node_id,
                    epoch=epoch,
                    endpoint_raw=f"http://{node_id}.example.com:3000/",
                    scheme="http",
                    host=f"{node_id}.example.com",
                    port=3000,
                    endpoint_key=f"{node_id}.example.com:3000",
                    auth_user="operator",
                    auth_secret_enc="not-a-real-ciphertext",
                    config_fingerprint="a" * 64,
                    probe_passed_at=_NOW,
                    endpoint_changed_at=_NOW,
                    trust_state=trust_state,
                    liveness="streaming",
                    last_frame_at=_NOW,
                )
            )
            await session.commit()

    _run(_go())
    probation.invalidate()
    publication.invalidate()


def _seed_fleet_node(node_id: str = _FLEET) -> None:
    async def _go():
        async with async_session_maker() as session:
            session.add(Node(node_id=node_id, node_ref=node_auth.mint_node_ref()))
            await session.commit()

    _run(_go())


def _row(node_id: str = _RADAR) -> tuple[int, str]:
    async def _go():
        async with async_session_maker() as session:
            radar = await session.get(PolledRadar, node_id)
            return radar.epoch, radar.trust_state

    return _run(_go())


def _events(node_id: str = _RADAR) -> list[tuple[str, int | None, str]]:
    async def _go():
        async with async_session_maker() as session:
            rows = await session.scalars(select(NodeEvent).where(NodeEvent.node_id == node_id).order_by(NodeEvent.id))
            return [(e.kind, e.epoch, e.actor) for e in rows]

    return _run(_go())


def _put(client, node_id: str = _RADAR, *, trust_state: str, epoch: int):
    return client.put(f"/api/admin/polled-radars/{node_id}/trust", json={"trust_state": trust_state, "epoch": epoch})


class TestTrustDecision:
    def test_graduating_answers_with_the_state_the_radar_now_holds(self, client):
        _seed()
        r = _put(client, trust_state="graduated", epoch=2)
        assert r.status_code == 200
        assert r.json() == {"node_id": _RADAR, "epoch": 2, "trust_state": "graduated"}
        assert _row() == (2, "graduated")

    def test_graduation_lifts_the_fence_without_waiting_for_the_cache(self, client):
        _seed()
        assert probation.in_probation(_RADAR)
        _put(client, trust_state="graduated", epoch=2)
        assert not probation.in_probation(_RADAR)

    def test_graduation_reaches_the_public_surfaces_without_waiting_for_their_cache(self, client):
        # The private set caches the probation answer it was built with.
        _seed()
        assert publication.is_private(_RADAR)
        _put(client, trust_state="graduated", epoch=2)
        assert not publication.is_private(_RADAR)

    def test_returning_to_probation_closes_the_fence_without_waiting_for_the_cache(self, client):
        _seed(trust_state="graduated")
        assert not probation.in_probation(_RADAR)
        r = _put(client, trust_state="probation", epoch=2)
        assert r.status_code == 200
        assert probation.in_probation(_RADAR)
        assert publication.is_private(_RADAR)

    def test_the_caches_are_expired_only_once_the_decision_is_committed(self, client, monkeypatch):
        """Expired before the commit, a reader in between would cache the old state again."""
        seen = []

        def invalidate():
            with closing(sqlite3.connect(DATABASE_URL.split("///", 1)[1])) as conn:
                seen.append(conn.execute("SELECT trust_state FROM polled_radars").fetchone()[0])

        _seed()
        monkeypatch.setattr(probation, "invalidate", invalidate)
        monkeypatch.setattr(publication, "invalidate", invalidate)
        _put(client, trust_state="graduated", epoch=2)
        assert seen == ["graduated", "graduated"]

    @pytest.mark.parametrize(("held", "asked"), [("probation", "graduated"), ("graduated", "probation")])
    def test_a_moved_epoch_is_refused_and_changes_nothing(self, client, held, asked):
        """The administrator looked at epoch 2; the slot has since moved on to 3."""
        _seed(epoch=3, trust_state=held)
        r = _put(client, trust_state=asked, epoch=2)
        assert r.status_code == 409
        assert "epoch 3" in r.json()["detail"]
        assert _row() == (3, held)
        assert _events() == []

    def test_the_state_the_radar_already_holds_is_answered_and_not_recorded(self, client):
        _seed(trust_state="graduated")
        r = _put(client, trust_state="graduated", epoch=2)
        assert r.status_code == 200
        assert r.json() == {"node_id": _RADAR, "epoch": 2, "trust_state": "graduated"}
        assert _events() == []

    @pytest.mark.parametrize("node_id", [_FLEET, "blaffff0000"])
    def test_a_node_that_is_not_a_polled_radar_is_not_found(self, client, node_id):
        _seed_fleet_node()
        r = _put(client, node_id, trust_state="graduated", epoch=1)
        assert r.status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"trust_state": "blocked", "epoch": 2},
            {"trust_state": "graduated", "epoch": 0},
            {"trust_state": "graduated"},
        ],
    )
    def test_a_malformed_decision_is_refused(self, client, body):
        _seed()
        r = client.put(f"/api/admin/polled-radars/{_RADAR}/trust", json=body)
        assert r.status_code == 422
        assert _row() == (2, "probation")

    def test_each_decision_is_recorded_with_its_administrator_and_epoch(self, client):
        _seed()
        _put(client, trust_state="graduated", epoch=2)
        _put(client, trust_state="probation", epoch=2)
        assert _events() == [("graduated", 2, _ADMIN), ("returned_to_probation", 2, _ADMIN)]


class TestListing:
    def test_lists_each_polled_radar_with_its_trust_and_decisions_newest_first(self, client):
        _seed()
        _seed(_OTHER, epoch=1, trust_state="probation")
        _seed_fleet_node()
        _put(client, trust_state="graduated", epoch=2)
        _put(client, trust_state="probation", epoch=2)

        r = client.get("/api/admin/polled-radars")
        assert r.status_code == 200
        body = r.json()
        assert body["probation_enabled"] is True
        radars = {radar["node_id"]: radar for radar in body["radars"]}
        assert set(radars) == {_RADAR, _OTHER}

        radar = radars[_RADAR]
        assert set(radar) == {
            "node_id",
            "node_ref",
            "endpoint",
            "liveness",
            "last_frame_at",
            "epoch",
            "trust_state",
            "events",
        }
        assert radar["node_ref"].startswith("nde")
        assert radar["endpoint"] == f"{_RADAR}.example.com:3000"
        assert (radar["liveness"], radar["epoch"], radar["trust_state"]) == ("streaming", 2, "probation")
        assert radar["last_frame_at"] is not None
        assert [(e["kind"], e["epoch"], e["actor"]) for e in radar["events"]] == [
            ("returned_to_probation", 2, _ADMIN),
            ("graduated", 2, _ADMIN),
        ]
        assert all(e["at"] for e in radar["events"])
        assert radars[_OTHER]["events"] == []

    def test_reports_the_fence_switched_off(self, client, monkeypatch):
        monkeypatch.setenv("POLLED_RADAR_PROBATION_ENABLED", "0")
        assert client.get("/api/admin/polled-radars").json() == {"probation_enabled": False, "radars": []}


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/admin/polled-radars", {}),
        ("put", f"/api/admin/polled-radars/{_RADAR}/trust", {"json": {"trust_state": "graduated", "epoch": 2}}),
    ],
)
def test_both_routes_are_behind_require_admin(client, method, path, kwargs):
    _seed()
    with patch("core.users.AUTH_BYPASS", False):
        r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 401
    assert _row() == (2, "probation")
