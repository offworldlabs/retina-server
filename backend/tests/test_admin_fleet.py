"""The admin fleet read: GET /api/admin/fleet.

One row per node across the registry and the Mender mirror, with the claim and
contact state of each and a header saying how fresh the mirror is. Rows are
written straight into a migrated database, the mirror's as a snapshot would
leave them, and read back through the route as an administrator is served in a
deployment, as tests/test_admin_claims.py does.
"""

import asyncio
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from core import sessions, users
from core.mender_mirror import MenderDevice, MenderSyncState
from core.nodes import Node, NodeClaim, NodeContact
from core.users import ACCESS_ASSERTION_HEADER, User
from services import node_auth

OPERATOR = "operator@example.com"
ASSERTION = "an-access-assertion"
AS_ADMIN = {ACCESS_ASSERTION_HEADER: ASSERTION}

ADA = "ada@example.com"
ADA_NEW = "ada.new@example.com"
SITE = "site@example.com"

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
T0_EPOCH = 1792152000.0


class _Access:
    """Stands in for core.users.access_identity, verifying ASSERTION as OPERATOR."""

    def is_configured(self) -> bool:
        return True

    async def identity(self, token: str | None) -> str | None:
        return OPERATOR if token == ASSERTION else None


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    monkeypatch.setattr(users, "access_identity", _Access())
    monkeypatch.delenv("MENDER_SOURCE", raising=False)
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)


def node(node_id: str, **fields) -> Node:
    return Node(node_id=node_id, node_ref=node_auth.mint_node_ref(), **fields)


def device(mender_device_id: str, node_id: str | None, **fields) -> MenderDevice:
    fields.setdefault("auth_status", "accepted")
    fields.setdefault("last_confirmed_at", T0)
    return MenderDevice(mender_device_id=mender_device_id, node_id=node_id, **fields)


def snapshot(age_s: float | None, **fields) -> MenderSyncState | None:
    """The sync state a snapshot received `age_s` ago leaves, or none at all."""
    if age_s is None:
        return None
    received = datetime.now(UTC) - timedelta(seconds=age_s)
    fields.setdefault("generated_at", received - timedelta(seconds=3))
    fields.setdefault("device_count", 0)
    fields.setdefault("source", "mender-auto-accept@central")
    return MenderSyncState(id=1, last_snapshot_at=received, **fields)


async def seed(session, *rows) -> None:
    """Commit `rows`, registry rows first so the claim and contact keys hold."""
    for row in rows:
        if row is not None:
            session.add(row)
        await session.flush()
    await session.commit()


async def account(session, email: str) -> str:
    user = User(email=email, name=email.split("@")[0], is_verified=True)
    session.add(user)
    await session.commit()
    return str(user.id)


def fleet(client) -> dict:
    r = client.get("/api/admin/fleet", headers=AS_ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def by_id(body: dict) -> dict[str | None, dict]:
    return {row["node_id"]: row for row in body["rows"]}


class TestPresence:
    async def test_each_state_is_told_apart(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            node("ret0123abcd"),
            node("ret1a2b3c4d"),
            device("md-1", "ret00000000"),
            device("md-3", "ret1a2b3c4d", present=False),
            device("md-4", "retdeadbeef", auth_status="pending"),
            device("md-5", "bench-board-7"),
        )

        body = fleet(node_client)
        rows = by_id(body)

        assert {nid: row["presence"] for nid, row in rows.items()} == {
            "ret00000000": "both",
            # A node with any mirror row is both, present or not.
            "ret1a2b3c4d": "both",
            "ret0123abcd": "registry_only",
            "retdeadbeef": "mender_only",
            "bench-board-7": "mender_only",
        }
        assert body["counts"] == {"both": 2, "registry_only": 1, "mender_only": 2}

    async def test_a_registry_only_row_carries_nothing_from_the_mirror(self, node_session, node_client):
        await seed(node_session, node("ret0123abcd", status="retired", first_seen_at=T0, last_seen_at=T0))

        row = by_id(fleet(node_client))["ret0123abcd"]

        assert row["status"] == "retired"
        assert row["node_ref"].startswith("nde")
        mirror_fields = (
            "mender_device_id",
            "auth_status",
            "device_type",
            "artifact_name",
            "inventory_updated_at",
            "present",
        )
        assert {k: row[k] for k in mirror_fields} == dict.fromkeys(mirror_fields)
        assert row["never_registered"] is False

    async def test_a_mender_only_row_carries_nothing_from_the_registry(self, node_session, node_client):
        await seed(node_session, device("md-4", "retdeadbeef", auth_status="pending", device_type="pi5-v3-arm64"))

        row = by_id(fleet(node_client))["retdeadbeef"]

        assert (row["node_ref"], row["status"], row["first_seen_at"], row["last_seen_at"]) == (None,) * 4
        assert (row["claim_email"], row["claim_verified"], row["contact_email"], row["email_state"]) == (
            None,
            None,
            None,
            "none",
        )
        assert (row["mender_device_id"], row["auth_status"], row["device_type"], row["present"]) == (
            "md-4",
            "pending",
            "pi5-v3-arm64",
            True,
        )

    async def test_the_mirror_fields_of_a_both_row_are_the_devices(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            device(
                "md-1",
                "ret00000000",
                device_type="pi5-v3-arm64",
                artifact_name="owl-os-1.4.2",
                inventory_updated_at=T0,
            ),
        )

        row = by_id(fleet(node_client))["ret00000000"]

        assert (
            row["mender_device_id"],
            row["auth_status"],
            row["device_type"],
            row["artifact_name"],
            row["present"],
        ) == ("md-1", "accepted", "pi5-v3-arm64", "owl-os-1.4.2", True)


class TestNeverRegistered:
    async def test_only_a_ret_node_known_to_mender_alone_is(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            node("ret0123abcd"),
            device("md-1", "ret00000000"),
            device("md-4", "retdeadbeef", auth_status="pending"),
            device("md-5", "bench-board-7"),
            device("md-6", None),
        )

        rows = by_id(fleet(node_client))

        assert {nid: row["never_registered"] for nid, row in rows.items()} == {
            "retdeadbeef": True,
            "bench-board-7": False,
            # Registered, so not "never": both and registry_only are false.
            "ret00000000": False,
            "ret0123abcd": False,
            None: False,
        }


class TestDevicesWithNoNodeId:
    async def test_a_present_one_is_listed_and_an_absent_one_is_not(self, node_session, node_client):
        await seed(
            node_session,
            device("md-here", None, auth_status="pending"),
            device("md-gone", None, present=False),
        )

        rows = fleet(node_client)["rows"]

        assert [(r["node_id"], r["mender_device_id"], r["presence"]) for r in rows] == [
            (None, "md-here", "mender_only")
        ]

    async def test_several_are_each_listed(self, node_session, node_client):
        await seed(node_session, device("md-b", None), device("md-a", None))

        assert [r["mender_device_id"] for r in fleet(node_client)["rows"]] == ["md-a", "md-b"]


class TestSeveralMirrorRowsForOneNodeId:
    async def test_the_present_one_is_shown_though_an_absent_one_is_newer(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            device("md-old", "ret00000000", last_confirmed_at=T0 - timedelta(days=2), artifact_name="owl-os-1.4.2"),
            device(
                "md-new",
                "ret00000000",
                last_confirmed_at=T0,
                present=False,
                artifact_name="owl-os-1.3.0",
            ),
        )

        body = fleet(node_client)

        assert len(body["rows"]) == 1
        assert (body["rows"][0]["mender_device_id"], body["rows"][0]["present"]) == ("md-old", True)
        assert body["rows"][0]["artifact_name"] == "owl-os-1.4.2"

    async def test_with_none_present_the_latest_confirmed_is_shown(self, node_session, node_client):
        await seed(
            node_session,
            device("md-a", "ret00000000", present=False, last_confirmed_at=T0 - timedelta(days=3)),
            device("md-b", "ret00000000", present=False, last_confirmed_at=T0),
            device("md-c", "ret00000000", present=False, last_confirmed_at=T0 - timedelta(days=1)),
        )

        body = fleet(node_client)

        assert [(r["mender_device_id"], r["present"]) for r in body["rows"]] == [("md-b", False)]
        assert body["counts"] == {"both": 0, "registry_only": 0, "mender_only": 1}


class TestSnapshotIssue:
    async def test_a_flagged_device_shows_its_issue(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            device("md-1", "ret00000000", snapshot_issue="auth_set_pubkey is not a PEM public key"),
            device("md-2", "retdeadbeef", snapshot_issue="node_id shared by 2 devices"),
            device("md-3", None, snapshot_issue="node_id is empty"),
        )

        rows = fleet(node_client)["rows"]

        assert {(r["node_id"], r["mender_device_id"]): r["snapshot_issue"] for r in rows} == {
            ("ret00000000", "md-1"): "auth_set_pubkey is not a PEM public key",
            ("retdeadbeef", "md-2"): "node_id shared by 2 devices",
            (None, "md-3"): "node_id is empty",
        }

    async def test_a_clean_device_and_a_registry_only_row_have_none(self, node_session, node_client):
        await seed(node_session, node("ret00000000"), node("ret0123abcd"), device("md-1", "ret00000000"))

        rows = by_id(fleet(node_client))

        assert rows["ret00000000"]["snapshot_issue"] is None
        assert rows["ret0123abcd"]["snapshot_issue"] is None

    async def test_a_shared_node_id_shows_the_flagged_row_though_the_clean_one_is_newer(
        self, node_session, node_client
    ):
        await seed(
            node_session,
            node("ret00000000"),
            device(
                "md-a",
                "ret00000000",
                last_confirmed_at=T0 - timedelta(hours=1),
                snapshot_issue="node_id shared by 2 devices",
            ),
            device("md-z", "ret00000000", last_confirmed_at=T0, snapshot_issue="node_id shared by 2 devices"),
            device("md-m", "ret00000000", last_confirmed_at=T0 + timedelta(hours=1)),
        )

        body = fleet(node_client)

        assert len(body["rows"]) == 1
        assert body["rows"][0]["mender_device_id"] == "md-z"
        assert body["rows"][0]["snapshot_issue"] == "node_id shared by 2 devices"
        assert body["counts"] == {"both": 1, "registry_only": 0, "mender_only": 0}

    async def test_a_pair_both_flagged_shows_the_one_confirmed_last_then_the_greater_id(
        self, node_session, node_client
    ):
        issue = "node_id shared by 2 devices"
        await seed(
            node_session,
            device("md-a", "ret1a2b3c4d", last_confirmed_at=T0 - timedelta(hours=1), snapshot_issue=issue),
            device("md-b", "ret1a2b3c4d", last_confirmed_at=T0, snapshot_issue=issue),
            device("md-c", "retdeadbeef", snapshot_issue=issue),
            device("md-d", "retdeadbeef", snapshot_issue=issue),
        )

        rows = by_id(fleet(node_client))

        assert rows["ret1a2b3c4d"]["mender_device_id"] == "md-b"
        assert rows["retdeadbeef"]["mender_device_id"] == "md-d"

    async def test_a_present_clean_row_is_shown_over_an_absent_flagged_one(self, node_session, node_client):
        await seed(
            node_session,
            device(
                "md-gone",
                "ret1a2b3c4d",
                present=False,
                last_confirmed_at=T0 + timedelta(hours=1),
                snapshot_issue="node_id shared by 2 devices",
            ),
            device("md-here", "ret1a2b3c4d", last_confirmed_at=T0 - timedelta(days=1)),
        )

        row = by_id(fleet(node_client))["ret1a2b3c4d"]

        assert (row["mender_device_id"], row["present"], row["snapshot_issue"]) == ("md-here", True, None)

    async def test_an_issue_does_not_rank_an_absent_row(self, node_session, node_client):
        await seed(
            node_session,
            device(
                "md-flagged",
                "ret1a2b3c4d",
                present=False,
                last_confirmed_at=T0 - timedelta(days=1),
                snapshot_issue="node_id shared by 2 devices",
            ),
            device("md-latest", "ret1a2b3c4d", present=False, last_confirmed_at=T0),
        )

        row = by_id(fleet(node_client))["ret1a2b3c4d"]

        assert (row["mender_device_id"], row["snapshot_issue"]) == ("md-latest", None)

    async def test_devices_with_no_node_id_are_still_listed_present_only(self, node_session, node_client):
        await seed(
            node_session,
            device("md-here", None, snapshot_issue="node_id is empty"),
            device("md-gone", None, present=False, snapshot_issue="node_id is empty"),
        )

        rows = fleet(node_client)["rows"]

        assert [(r["mender_device_id"], r["snapshot_issue"]) for r in rows] == [("md-here", "node_id is empty")]


class TestEmailState:
    async def test_a_verified_claim_reads_claimed_verified(self, node_session, node_client):
        user_id = await account(node_session, ADA)
        await seed(
            node_session,
            node("ret00000000"),
            NodeClaim(node_id="ret00000000", user_id=user_id, email=ADA, verified=True),
        )

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["claim_email"], row["claim_verified"], row["contact_email"]) == (
            "claimed_verified",
            ADA,
            True,
            None,
        )

    async def test_a_verified_claim_wins_over_a_contact(self, node_session, node_client):
        user_id = await account(node_session, ADA)
        await seed(
            node_session,
            node("ret00000000"),
            NodeClaim(node_id="ret00000000", user_id=user_id, email=ADA, verified=True),
            NodeContact(node_id="ret00000000", email=SITE),
        )

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["contact_email"]) == ("claimed_verified", SITE)

    async def test_an_unverified_claim_with_a_contact_reads_contact_only(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            NodeClaim(node_id="ret00000000", email=ADA, verified=False),
            NodeContact(node_id="ret00000000", email=SITE),
        )

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["claim_email"], row["claim_verified"], row["contact_email"]) == (
            "contact_only",
            ADA,
            False,
            SITE,
        )

    async def test_an_unverified_claim_without_a_contact_reads_none(self, node_session, node_client):
        await seed(node_session, node("ret00000000"), NodeClaim(node_id="ret00000000", email=ADA, verified=False))

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["claim_email"], row["claim_verified"], row["contact_email"]) == (
            "none",
            ADA,
            False,
            None,
        )

    async def test_a_contact_alone_reads_contact_only(self, node_session, node_client):
        await seed(node_session, node("ret00000000"), NodeContact(node_id="ret00000000", email=SITE))

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["claim_email"], row["claim_verified"], row["contact_email"]) == (
            "contact_only",
            None,
            None,
            SITE,
        )

    async def test_a_contact_with_no_address_reads_none(self, node_session, node_client):
        await seed(node_session, node("ret00000000"), NodeContact(node_id="ret00000000", first_name="Ada"))

        assert by_id(fleet(node_client))["ret00000000"]["email_state"] == "none"

    async def test_neither_reads_none(self, node_session, node_client):
        await seed(node_session, node("ret00000000"))

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["email_state"], row["claim_email"], row["claim_verified"], row["contact_email"]) == (
            "none",
            None,
            None,
            None,
        )


class TestClaimEmail:
    """As the admin claim read gives an owner's address: the account's, as it is
    now, over the one the claim was made with."""

    async def test_an_owned_claim_reads_the_accounts_current_address(self, node_session, node_client):
        user_id = await account(node_session, ADA_NEW)
        await seed(
            node_session,
            node("ret00000000"),
            NodeClaim(node_id="ret00000000", user_id=user_id, email=ADA, verified=True),
        )

        assert by_id(fleet(node_client))["ret00000000"]["claim_email"] == ADA_NEW

    async def test_a_claim_without_an_owner_reads_its_own_address(self, node_session, node_client):
        await seed(node_session, node("ret00000000"), NodeClaim(node_id="ret00000000", email=ADA))

        assert by_id(fleet(node_client))["ret00000000"]["claim_email"] == ADA

    async def test_an_owner_with_no_account_row_reads_the_claims_address(self, node_session, node_client):
        await seed(
            node_session,
            node("ret00000000"),
            NodeClaim(node_id="ret00000000", user_id=str(uuid.uuid4()), email=ADA, verified=False),
        )

        assert by_id(fleet(node_client))["ret00000000"]["claim_email"] == ADA


class TestHeader:
    async def test_a_fresh_snapshot(self, node_session, node_client, monkeypatch):
        monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "300")
        state = snapshot(60, device_count=22)
        assert state is not None
        await seed(node_session, state)

        mirror = fleet(node_client)["mirror"]

        assert mirror["stale"] is False
        assert mirror["stale_reason"] is None
        assert mirror["age_s"] == pytest.approx(60, abs=5)
        assert mirror["max_age_s"] == 300
        assert mirror["last_snapshot_at"] == state.last_snapshot_at.timestamp()
        assert mirror["generated_at"] == state.generated_at.timestamp()
        assert (mirror["source"], mirror["device_count"]) == ("mender-auto-accept@central", 22)

    async def test_the_default_limit_is_five_minutes(self, node_session, node_client):
        await seed(node_session, snapshot(60))

        mirror = fleet(node_client)["mirror"]

        assert (mirror["max_age_s"], mirror["stale"]) == (300, False)

    async def test_an_old_snapshot_is_stale(self, node_session, node_client, monkeypatch):
        monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "300")
        await seed(node_session, snapshot(3600))

        mirror = fleet(node_client)["mirror"]

        assert mirror["stale"] is True
        assert re.fullmatch(r"last snapshot 36\d\d s ago, limit 300 s", mirror["stale_reason"])
        assert mirror["age_s"] == pytest.approx(3600, abs=5)
        assert mirror["max_age_s"] == 300

    async def test_a_snapshot_from_the_future_is_stale_with_a_negative_age(
        self, node_session, node_client, monkeypatch
    ):
        monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "300")
        await seed(node_session, snapshot(-3600))

        mirror = fleet(node_client)["mirror"]

        assert mirror["stale"] is True
        assert re.fullmatch(
            r"last snapshot is 36\d\d s in the future, the server clock went back", mirror["stale_reason"]
        )
        assert mirror["age_s"] == pytest.approx(-3600, abs=5)
        assert mirror["max_age_s"] == 300

    async def test_a_little_clock_jitter_ahead_is_fresh_and_the_age_is_still_negative(self, node_session, node_client):
        await seed(node_session, snapshot(-5))

        mirror = fleet(node_client)["mirror"]

        assert (mirror["stale"], mirror["stale_reason"]) == (False, None)
        assert mirror["age_s"] == pytest.approx(-5, abs=3)

    async def test_no_snapshot_is_stale_with_nothing_to_report(self, node_client):
        mirror = fleet(node_client)["mirror"]

        assert mirror == {
            "last_snapshot_at": None,
            "age_s": None,
            "max_age_s": 300,
            "stale": True,
            "stale_reason": "no snapshot yet",
            "generated_at": None,
            "source": None,
            "device_count": None,
            "mender_source": "api",
        }

    @pytest.mark.parametrize("bad", ["soon", "0", "-5", "nan", "inf"])
    async def test_a_malformed_limit_is_null_and_stale_even_when_fresh(
        self, node_session, node_client, monkeypatch, bad
    ):
        monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", bad)
        await seed(node_session, snapshot(1))

        mirror = fleet(node_client)["mirror"]

        assert mirror["max_age_s"] is None
        assert mirror["stale"] is True
        assert "MENDER_MIRROR_MAX_AGE_S" in mirror["stale_reason"]
        assert mirror["age_s"] == pytest.approx(1, abs=5)

    @pytest.mark.parametrize("source", ["api", "shadow", "mirror"])
    async def test_mender_source_is_echoed(self, node_client, monkeypatch, source):
        monkeypatch.setenv("MENDER_SOURCE", f" {source.upper()} ")

        assert fleet(node_client)["mirror"]["mender_source"] == source

    async def test_a_misconfigured_mender_source_is_echoed_as_it_is(self, node_client, monkeypatch):
        monkeypatch.setenv("MENDER_SOURCE", "mirorr")

        assert fleet(node_client)["mirror"]["mender_source"] == "mirorr"


class TestTimesAreEpochSeconds:
    async def test_every_time_field(self, node_session, node_client):
        later = T0 + timedelta(minutes=5)
        await seed(
            node_session,
            node("ret00000000", first_seen_at=T0, last_seen_at=later),
            device("md-1", "ret00000000", inventory_updated_at=later),
            snapshot(10),
        )

        body = fleet(node_client)
        row = body["rows"][0]

        assert row["first_seen_at"] == T0_EPOCH
        assert row["last_seen_at"] == T0_EPOCH + 300
        assert row["inventory_updated_at"] == T0_EPOCH + 300
        assert isinstance(row["first_seen_at"], float)
        assert abs(body["mirror"]["last_snapshot_at"] - time.time()) < 60
        assert abs(body["mirror"]["generated_at"] - time.time()) < 60

    async def test_a_time_not_recorded_is_null(self, node_session, node_client):
        await seed(node_session, node("ret00000000", last_seen_at=None), device("md-1", "ret00000000"))

        row = by_id(fleet(node_client))["ret00000000"]

        assert (row["last_seen_at"], row["inventory_updated_at"]) == (None, None)


class TestOrder:
    async def test_by_node_id_with_unidentified_devices_last_by_device_id(self, node_session, node_client):
        await seed(
            node_session,
            node("ret0123abcd"),
            node("ret00000000"),
            device("md-z", None),
            device("md-a", None),
            device("md-9", "bench-board-7"),
            device("md-8", "retdeadbeef"),
        )

        rows = fleet(node_client)["rows"]

        assert [(r["node_id"], r["mender_device_id"]) for r in rows] == [
            ("bench-board-7", "md-9"),
            ("ret00000000", None),
            ("ret0123abcd", None),
            ("retdeadbeef", "md-8"),
            (None, "md-a"),
            (None, "md-z"),
        ]

    async def test_an_empty_fleet(self, node_client):
        body = fleet(node_client)

        assert body["rows"] == []
        assert body["counts"] == {"both": 0, "registry_only": 0, "mender_only": 0}


class TestOnlyAnAdministratorMay:
    def test_no_credentials_is_401(self, client):
        r = client.get("/api/admin/fleet")

        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    def test_a_signed_in_account_is_403(self, client):
        made: User = asyncio.run(users.get_or_create_magic_link_user(ADA))
        token, _ = asyncio.run(sessions.open_session(made, user_agent="Mozilla/5.0 (X11; Linux x86_64)", ip=None))

        r = client.get("/api/admin/fleet", headers={"Cookie": f"{sessions.COOKIE_NAME}={token}"})

        assert r.status_code == 403
        assert r.json() == {"detail": "Admin access required"}
