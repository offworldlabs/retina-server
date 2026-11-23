import logging
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from core import users
from core.mender_mirror import SYNC_STATE_ID, MenderDevice, MenderSyncState
from services import mender


@pytest.fixture(autouse=True)
def _no_ambient_mender_source(monkeypatch):
    """Every test here starts on the API, whatever the shell exported: lookup_device
    reads MENDER_SOURCE per call, so an ambient value would switch the whole file."""
    monkeypatch.delenv("MENDER_SOURCE", raising=False)
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S", raising=False)


PUBKEY = "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"

# A real EC P-256 SPKI public key, so the fingerprint test pins a value rather than
# just a length. Derived independently of mender.py:
#   openssl ecparam -name prime256v1 -genkey -noout | openssl pkey -pubout
#   openssl pkey -pubin -outform DER | shasum -a 256
REAL_PUBKEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE6KZgT+OUiuwWJGsbVh5hkfrKHKK8\n"
    "Bm+wE+K4UfIAzoA6JbXn2IH8v6gSRhuUyOX1ZwM/4UzLmNu8hHROlRK8ag==\n"
    "-----END PUBLIC KEY-----\n"
)
EXPECTED_FINGERPRINT = "deb10bddcd61fe0040321e766b15ae97bc027368c9273aae0d2594bedc476258"


def _device(node_id: str, status: str = "accepted", auth_sets: list | None = None) -> dict:
    return {
        "id": "b7e2",
        "identity_data": {"node_id": node_id},
        "status": status,
        "auth_sets": auth_sets if auth_sets is not None else [],
    }


def _responds(payload, status_code: int = 200):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


async def test_an_accepted_device_resolves(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/management/v2/devauth/devices")
        # No identity_data or status params on the wire: see lookup_device's docstring.
        assert "identity_data" not in request.url.params
        assert "status" not in request.url.params
        assert request.url.params["per_page"] == "500"
        return httpx.Response(
            200,
            json=[
                _device(
                    "ret1a2b3c4d",
                    auth_sets=[
                        {
                            "id": "as-1",
                            "status": "accepted",
                            "pubkey": REAL_PUBKEY,
                            "identity_data": {"node_id": "ret1a2b3c4d"},
                        }
                    ],
                )
            ],
        )

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    # Mender's own device id, distinct from node_id and the auth set's id. It is what
    # an operator searches the tenant by when a registration is refused, so it comes
    # off the device rather than the auth set and is asserted here for that reason.
    assert record.mender_device_id == "b7e2"
    assert record.auth_set_id == "as-1"
    assert record.auth_set_pubkey == REAL_PUBKEY
    assert record.auth_set_fingerprint == EXPECTED_FINGERPRINT


async def test_an_unknown_device_resolves_to_none(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("retdeadbeef") is None


async def test_a_device_with_another_identity_resolves_to_none(monkeypatch):
    """The fail-closed check from lookup_device's docstring: a response identity
    that does not match the request must not resolve."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("retffffffff", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": PUBKEY}])]),
    )
    assert await mender.lookup_device("ret1a2b3c4d") is None


async def test_a_pending_device_resolves_but_is_not_accepted(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds([_device("ret1a2b3c4d", status="pending")]))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "pending"


async def test_a_timeout_raises_mender_unreachable(monkeypatch):
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_5xx_raises_mender_unreachable(monkeypatch):
    monkeypatch.setattr(mender, "_transport", _responds({"error": "down"}, status_code=503))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_401_raises_mender_unreachable(monkeypatch):
    # Per the module docstring: a dead credential blocks the whole fleet.
    monkeypatch.setattr(mender, "_transport", _responds({"error": "unauthorized"}, status_code=401))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_json_200_raises_mender_unreachable(monkeypatch):
    """An error page served with a 200 is otherwise a bare JSONDecodeError; see
    lookup_device's comment for why this needs its own route to MenderUnreachable."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_json_object_body_raises_mender_unreachable(monkeypatch):
    """A JSON object rather than a device list, e.g. from an error response or a
    future envelope change; see lookup_device's comment for why this must not
    reach _record."""
    monkeypatch.setattr(mender, "_transport", _responds({"devices": []}))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_list_of_non_dict_elements_raises_mender_unreachable(monkeypatch):
    """A 200 carrying ["unauthorized"], or a future list of bare device ids, is
    skipped rather than matched; see lookup_device's comment for why a scan
    that ends with a skip and no match still raises rather than returning None."""
    monkeypatch.setattr(mender, "_transport", _responds(["unauthorized"]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_list_auth_sets_raises_mender_unreachable(monkeypatch):
    """auth_sets as a dict would iterate its string keys into s.get and raise
    AttributeError instead of the shared MenderUnreachable."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets={"status": "accepted"})]),
    )
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_an_auth_set_list_with_a_non_dict_element_raises_mender_unreachable(monkeypatch):
    """See _record's comment: a list containing a non-dict element fails the
    same way as auth_sets being a dict outright."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted"}, "garbage"])]),
    )
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_identity_data_as_a_string_raises_mender_unreachable(monkeypatch):
    """See _identity_node_id's comment for why a JSON-encoded identity_data
    string, rather than an object, must not reach .get. This is the only device
    in the response, so the skip in lookup_device's loop leaves no match and
    the scan still raises; see the malformed-device tests below for the case
    where a good match is found despite a skip elsewhere in the page."""
    device = _device("ret1a2b3c4d")
    device["identity_data"] = '{"node_id": "ret1a2b3c4d"}'
    monkeypatch.setattr(mender, "_transport", _responds([device]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_malformed_device_ahead_of_the_requested_one_still_resolves_it(monkeypatch):
    """See lookup_device's comment for why a malformed element is skipped
    rather than raised on the spot: one bad record earlier in the page must
    not stop the scan from reaching the device actually asked for."""
    bad = _device("ret00000000")
    bad["identity_data"] = '{"node_id": "ret00000000"}'
    good = _device(
        "ret1a2b3c4d",
        auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": PUBKEY, "identity_data": {"node_id": "ret1a2b3c4d"}}],
    )
    monkeypatch.setattr(mender, "_transport", _responds([bad, "garbage", good]))
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_id == "as-1"


async def test_a_malformed_device_with_no_match_raises_mender_unreachable(monkeypatch):
    """This test passes even with the skip-and-keep-scanning behaviour reverted,
    since an unconditional raise on any malformed device also raises here. It is
    kept as the guard against the opposite regression: dropping the `skipped`
    flag so a scan that skipped a record resolves to None like a clean miss;
    see lookup_device's comment for why that would be wrong."""
    bad = _device("ret00000000")
    bad["identity_data"] = '{"node_id": "ret00000000"}'
    other = _device("retffffffff")
    monkeypatch.setattr(mender, "_transport", _responds([other, bad]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_pem_pubkey_resolves_without_a_fingerprint(monkeypatch):
    """An OpenSSH-style key has no '-----' lines, so the whole string becomes the
    decode body, and its stripped length is still a multiple of 4; see
    _fingerprint's comment for why validate=True is what rejects it rather than
    the length check alone. The raw pubkey is still carried on the record even
    though it could not be fingerprinted: it is the one worth keeping for
    diagnosis."""
    ssh_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKrPxK2u4pJd3vN7QeR1sT9wXyZ0aBcDeFgHiJkLmNoP al@host"
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": ssh_key}])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_pubkey == ssh_key
    assert record.auth_set_fingerprint is None


async def test_mender_pat_set_after_import_is_picked_up(monkeypatch):
    """MENDER_PAT is read inside lookup_device, not at import, so a value set on
    the environment after the module has loaded (e.g. by load_dotenv() running
    later in backend/main.py) still reaches the request."""
    seen_auth = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_auth["header"] = request.headers.get("authorization")
        return httpx.Response(200, json=[])

    monkeypatch.setenv("MENDER_PAT", "late-token")
    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    await mender.lookup_device("ret1a2b3c4d")
    assert seen_auth["header"] == "Bearer late-token"


async def test_a_malformed_timeout_raises_mender_unreachable(monkeypatch):
    """See lookup_device's comment for why MENDER_TIMEOUT_S is read per call
    rather than at import; the mock transport is set so a build that regresses
    to an import-time read (env var not yet malformed) still hits an assertion,
    rather than falling through to a real request against MENDER_SERVER."""
    monkeypatch.setenv("MENDER_TIMEOUT_S", "not-a-number")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_an_empty_timeout_falls_back_to_the_default(monkeypatch):
    """A bare `MENDER_TIMEOUT_S=` line in .env, passed through by compose's
    env_file, is "" from os.getenv, not unset; see lookup_device's comment for
    why that must fall back to the default rather than reach float()."""
    monkeypatch.setenv("MENDER_TIMEOUT_S", "")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("ret1a2b3c4d") is None


async def test_an_empty_server_falls_back_to_the_default(monkeypatch):
    """Same set-but-empty case as MENDER_TIMEOUT_S above, see lookup_device's
    comment: a bare `MENDER_SERVER=` line in .env leaves the env var set to ""
    rather than unset, and the `or` fallback there is what keeps base_url at
    the default instead of reaching an empty string."""
    monkeypatch.setenv("MENDER_SERVER", "")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    assert await mender.lookup_device("ret1a2b3c4d") is None


async def test_a_non_string_pubkey_resolves_without_a_fingerprint(monkeypatch):
    """A pubkey that is not a string raises AttributeError out of str.splitlines,
    not the binascii/ValueError that an undecodable string raises. auth_set_pubkey
    is str | None, so the int itself must not land there either."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": 12345}])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_pubkey is None
    assert record.auth_set_fingerprint is None


async def test_more_than_one_accepted_set_is_refused_rather_than_resolved(monkeypatch):
    accepted = {"id": "as", "status": "accepted", "pubkey": PUBKEY, "identity_data": {"node_id": "ret1a2b3c4d"}}
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[accepted, dict(accepted, id="as2")])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "ambiguous"
    assert record.auth_set_id is None
    assert record.auth_set_pubkey is None and record.auth_set_fingerprint is None


async def test_accepted_status_with_no_accepted_auth_set_is_ambiguous(monkeypatch, caplog):
    """See _record's comment: a device claiming accepted at the top level while
    no auth set agrees is the same contradiction as more than one accepted set,
    so it resolves the same way rather than trusting the aggregate status."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", status="accepted", auth_sets=[{"id": "as-1", "status": "pending"}])]),
    )
    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "ambiguous"
    assert record.auth_set_id is None
    assert record.auth_set_pubkey is None and record.auth_set_fingerprint is None
    assert any("no accepted auth set" in message for message in caplog.messages)


async def test_a_non_ascii_mender_server_raises_mender_unreachable(monkeypatch):
    """See lookup_device's comment: httpx.InvalidURL (an IDNA encoding failure
    on a non-ASCII host) inherits Exception rather than HTTPError in httpx
    0.27, so a MENDER_SERVER this malformed would otherwise 500 the register
    handler instead of returning the shared 403."""
    monkeypatch.setenv("MENDER_SERVER", "http://☃.com")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_garbage_mender_server_raises_mender_unreachable(monkeypatch):
    """See lookup_device's comment: a MENDER_SERVER with no recognisable
    scheme reaches urllib as a bare ValueError, not an httpx.HTTPError."""
    monkeypatch.setenv("MENDER_SERVER", "not a url at all")
    monkeypatch.setattr(mender, "_transport", _responds([]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_string_node_id_raises_mender_unreachable(monkeypatch):
    """See _identity_node_id's comment: a node_id that is present but not a
    string can never equal the requested string, so without the guard the
    device would pass for an ordinary miss and a scan that finds nothing else
    would wrongly return None instead of raising."""
    device = _device("ret1a2b3c4d")
    device["identity_data"] = {"node_id": 12345}
    monkeypatch.setattr(mender, "_transport", _responds([device]))
    with pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device("ret1a2b3c4d")


async def test_a_non_string_auth_set_id_resolves_without_it(monkeypatch):
    """See _record's comment: auth_set_id is str | None, same as
    auth_set_pubkey above it, so a value that is not a string must not reach
    it as-is."""
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", auth_sets=[{"id": 12345, "status": "accepted", "pubkey": PUBKEY}])]),
    )
    record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "accepted"
    assert record.auth_set_id is None


async def test_an_accepted_auth_set_with_a_non_accepted_status_warns_but_resolves_to_its_own_status(
    monkeypatch, caplog
):
    """See _record's comment for why this direction warns rather than
    refuses, unlike the top-level-accepted/no-accepted-auth-set contradiction
    above: auth_status still carries the device's own status."""
    accepted_set = {"id": "as-1", "status": "accepted", "pubkey": PUBKEY}
    monkeypatch.setattr(
        mender,
        "_transport",
        _responds([_device("ret1a2b3c4d", status="pending", auth_sets=[accepted_set])]),
    )
    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None
    assert record.auth_status == "pending"
    assert record.auth_set_id == "as-1"
    assert any("accepted auth set but device status" in message for message in caplog.messages)


async def test_a_full_page_logs_a_warning_and_still_resolves(monkeypatch, caplog):
    """A device past _PER_PAGE resolves to the same None as one Mender has never
    heard of; the warning is what tells the two apart, so it must fire on a full
    page. _PER_PAGE is pinned down to keep the fixture to two devices rather than
    five hundred."""
    monkeypatch.setattr(mender, "_PER_PAGE", 2)
    devices = [_device("ret1a2b3c4d", status="pending"), _device("ret2b3c4d5e", status="pending")]
    monkeypatch.setattr(mender, "_transport", _responds(devices))
    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device("ret1a2b3c4d")
    assert record is not None and record.auth_status == "pending"
    assert any("filled the page" in message for message in caplog.messages)


# ── MENDER_SOURCE ────────────────────────────────────────────────────────────
#
# The mirror tests seed node_session's migrated database directly, so they do
# not depend on the snapshot route. Every one serves a 500 from the Mender
# transport and asserts nothing reached it: a mirror read that fell through to
# the API would fail there as well as on its answer.

NODE_ID = "ret1a2b3c4d"
# The mirror's clock, frozen: ages are rounded up, so a real clock would turn
# a snapshot seeded 1000 s old into 1001 s by the time it is read.
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
# The API's view of NODE_ID that _mirror_row's defaults describe, so shadow
# agrees unless a test changes one side.
ACCEPTED_ON_THE_API = [_device(NODE_ID, auth_sets=[{"id": "as-1", "status": "accepted", "pubkey": REAL_PUBKEY}])]
# Pending with no auth set, as a row for it carries none: against a rejected
# row, only auth_status differs.
PENDING_ON_THE_API = [_device(NODE_ID, status="pending")]


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    monkeypatch.setattr(mender, "_now", lambda: NOW)


def _serve(monkeypatch, payload, status_code: int = 200) -> list[httpx.Request]:
    """Answer every Mender request with payload, and return the requests as they arrive."""
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status_code, json=payload)

    monkeypatch.setattr(mender, "_transport", httpx.MockTransport(handler))
    return calls


def _mender_must_not_be_asked(monkeypatch) -> list[httpx.Request]:
    return _serve(monkeypatch, {"error": "the mirror read asked Mender"}, status_code=500)


def _mirror_row(
    auth_status: str = "accepted",
    *,
    node_id: str = NODE_ID,
    mender_device_id: str = "b7e2",
    auth_set_id: str | None = "as-1",
    fingerprint: str = EXPECTED_FINGERPRINT,
    present: bool = True,
    snapshot_issue: str | None = None,
) -> MenderDevice:
    """One mender_devices row. The auth set is carried only while accepted, as
    the snapshot route stores it."""
    accepted = auth_status == "accepted"
    return MenderDevice(
        mender_device_id=mender_device_id,
        node_id=node_id,
        auth_status=auth_status,
        auth_set_id=auth_set_id if accepted else None,
        auth_set_pubkey=REAL_PUBKEY if accepted else None,
        auth_set_fingerprint=fingerprint if accepted else None,
        last_confirmed_at=NOW,
        present=present,
        snapshot_issue=snapshot_issue,
    )


async def _seed(session, *devices: MenderDevice, age_s: float | None = 0.0) -> None:
    """Mirror rows and a sync state whose last snapshot is age_s old at NOW,
    committed; negative is ahead of NOW. age_s=None leaves the state out: no
    snapshot has ever arrived."""
    session.add_all(devices)
    if age_s is not None:
        received = NOW - timedelta(seconds=age_s)
        session.add(
            MenderSyncState(
                id=SYNC_STATE_ID,
                last_snapshot_at=received,
                generated_at=received - timedelta(seconds=1),
                device_count=len(devices),
                source="mender-auto-accept@test",
            )
        )
    await session.commit()


def _sqlite(session, sql: str) -> None:
    """Run sql on a connection of the test's own, as another writer would."""
    with closing(sqlite3.connect(session.get_bind().url.database)) as connection, connection:
        connection.execute(sql)


# mirror


async def test_mirror_an_accepted_row_resolves_without_asking_mender(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    monkeypatch.delenv("MENDER_PAT", raising=False)
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())

    record = await mender.lookup_device(NODE_ID, node_session)

    assert record == mender.MenderDeviceRecord("b7e2", NODE_ID, "accepted", "as-1", REAL_PUBKEY, EXPECTED_FINGERPRINT)
    assert calls == []


@pytest.mark.parametrize("auth_status", ["pending", "rejected", "preauthorized", "noauth", "ambiguous"])
async def test_mirror_a_row_resolves_with_its_status_as_stored(monkeypatch, node_session, auth_status):
    """Passed through for registration to refuse as it refuses the API's answer.
    "ambiguous" above all: the sender reduced it, and nothing here could redo that."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(auth_status))

    record = await mender.lookup_device(NODE_ID, node_session)

    assert record is not None
    assert record.auth_status == auth_status
    assert record.auth_set_id is None and record.auth_set_pubkey is None and record.auth_set_fingerprint is None
    assert calls == []


async def test_mirror_a_node_the_snapshot_did_not_list_resolves_to_none(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(node_id="retffffffff"), _mirror_row(node_id=None, mender_device_id="c9f3"))

    assert await mender.lookup_device(NODE_ID, node_session) is None
    assert calls == []


async def test_mirror_a_device_gone_from_the_fleet_resolves_to_none(monkeypatch, node_session):
    """Kept with present false, as a decommissioned device is, and refused."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(present=False))

    assert await mender.lookup_device(NODE_ID, node_session) is None


async def test_mirror_a_board_enrolled_again_resolves_to_its_present_device(monkeypatch, node_session):
    """The node_id's old device stays behind, absent, beside the new one."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(
        node_session,
        _mirror_row(present=False, auth_set_id="as-old"),
        _mirror_row(mender_device_id="c9f3", auth_set_id="as-new"),
    )

    record = await mender.lookup_device(NODE_ID, node_session)

    assert record is not None
    assert (record.mender_device_id, record.auth_set_id) == ("c9f3", "as-new")


async def test_mirror_with_no_snapshot_yet_raises_mender_unreachable(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=None)

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == "mirror stale (no snapshot yet)"
    assert calls == []


async def test_mirror_a_stale_snapshot_raises_with_its_age_and_the_limit(monkeypatch, node_session):
    """An accepted row in a stale mirror is not an answer: the device may have
    been rejected since, and nothing would say so."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=1000)

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == "mirror stale (last snapshot 1000 s ago, limit 300 s)"
    assert calls == []


async def test_mirror_a_snapshot_just_past_the_limit_never_reads_as_within_it(monkeypatch, node_session):
    """Rounded up, as stale_reason rounds: 300.4 s rounded to nearest would
    refuse while saying 300 s against a limit of 300 s."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=300.4)

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == "mirror stale (last snapshot 301 s ago, limit 300 s)"


@pytest.mark.parametrize(("ahead_s", "shown_s"), [(30.5, 31), (31, 31), (3600, 3600)], ids=str)
async def test_mirror_a_snapshot_ahead_of_the_clock_raises_as_the_clock_went_back(
    monkeypatch, node_session, ahead_s, shown_s
):
    """This server's clock stepped back past the last push, so how long ago
    that push was is no longer known: an accepted row is not an answer, however
    new the snapshot looks."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=-ahead_s)

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == f"mirror stale (last snapshot is {shown_s} s in the future, the server clock went back)"
    assert calls == []


@pytest.mark.parametrize("ahead_s", [1, 29.9, 30], ids=str)
async def test_mirror_a_snapshot_up_to_30_s_ahead_is_fresh(monkeypatch, node_session, ahead_s):
    """Clock jitter, not a step back."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=-ahead_s)

    record = await mender.lookup_device(NODE_ID, node_session)
    assert record is not None and record.auth_status == "accepted"


async def test_mirror_staleness_follows_mender_mirror_max_age_s(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=100)

    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "60")
    with pytest.raises(mender.MenderUnreachable, match=r"^mirror stale \(last snapshot 100 s ago, limit 60 s\)$"):
        await mender.lookup_device(NODE_ID, node_session)

    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "120")
    record = await mender.lookup_device(NODE_ID, node_session)
    assert record is not None and record.auth_status == "accepted"


async def test_mirror_answers_again_once_a_new_snapshot_lands(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(), age_s=1000)
    with pytest.raises(mender.MenderUnreachable, match="mirror stale"):
        await mender.lookup_device(NODE_ID, node_session)

    await node_session.execute(update(MenderSyncState).values(last_snapshot_at=NOW))
    await node_session.commit()

    record = await mender.lookup_device(NODE_ID, node_session)
    assert record is not None and record.auth_status == "accepted"


@pytest.mark.parametrize("raw", ["five minutes", "0", "-300", "nan", "inf"])
async def test_mirror_a_malformed_max_age_raises_naming_the_variable(monkeypatch, node_session, raw):
    """No fallback to the default: see mirror_max_age_s for why. Reported as
    a stale mirror, as every reason stale_reason gives is."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", raw)
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())

    with pytest.raises(
        mender.MenderUnreachable,
        match=rf"^mirror stale \(MENDER_MIRROR_MAX_AGE_S={re.escape(repr(raw))} is not a (number|positive number of seconds)\)$",
    ):
        await mender.lookup_device(NODE_ID, node_session)
    assert calls == []


@pytest.mark.parametrize("table", ["mender_devices", "mender_sync_state"])
async def test_mirror_a_database_error_raises_mender_unreachable(monkeypatch, node_session, table):
    """Fails closed, and with the driver's message rather than SQLAlchemy's
    statement dump, since the reason goes out in an alert."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())
    await node_session.execute(text(f"DROP TABLE {table}"))
    await node_session.commit()

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == f"mirror unreadable: no such table: {table}"
    assert calls == []


async def test_mirror_an_unreadable_snapshot_time_raises_mender_unreachable(monkeypatch, node_session):
    """SQLite returns what it cannot parse as a bare ValueError, which would
    otherwise be a 500 for the one caller whose lookup read it."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())
    _sqlite(node_session, "UPDATE mender_sync_state SET last_snapshot_at = 'yesterday'")

    with pytest.raises(mender.MenderUnreachable, match="^mirror unreadable: "):
        await mender.lookup_device(NODE_ID, node_session)


def _mirror_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage().startswith("mender mirror refuses ")
    ]


# What _record answers for a device it refuses rather than choosing for.
AMBIGUOUS = mender.MenderDeviceRecord("b7e2", NODE_ID, "ambiguous", None, None, None)


async def test_mirror_an_entry_with_an_issue_resolves_as_ambiguous(monkeypatch, node_session, caplog):
    """The snapshot kept the device but could not store it cleanly. An answer
    for this node_id, refused as not accepted, rather than MenderUnreachable,
    which would alert as if the whole fleet were blocked. The auth set it
    carries does not reach the record."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row(snapshot_issue="auth_set_pubkey too long"))

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == AMBIGUOUS
    assert _mirror_warnings(caplog) == [
        f"mender mirror refuses {NODE_ID} as ambiguous: 'b7e2' ('auth_set_pubkey too long')"
    ]
    assert calls == []


async def test_mirror_devices_sharing_a_node_id_resolve_as_ambiguous_naming_the_lowest_id(
    monkeypatch, node_session, caplog
):
    """Both flagged, as the snapshot route flags every device a node_id is
    shared by, so the unique index lets both stay present. Stored highest id
    first: the device named is the lowest id, not whichever row came back
    first."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    issue = "node_id shared by 2 devices"
    await _seed(
        node_session,
        _mirror_row(mender_device_id="c9f3", auth_set_id="as-2", snapshot_issue=issue),
        _mirror_row(snapshot_issue=issue),
    )

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == AMBIGUOUS
    assert _mirror_warnings(caplog) == [
        f"mender mirror refuses {NODE_ID} as ambiguous: 'b7e2' ('{issue}'), 'c9f3' ('{issue}')"
    ]


async def test_mirror_a_flagged_device_beside_a_clean_one_resolves_as_ambiguous(monkeypatch, node_session, caplog):
    """The clean one is not chosen: the flagged one claims the node_id as well."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(
        node_session,
        _mirror_row(),
        _mirror_row(mender_device_id="c9f3", auth_set_id="as-2", snapshot_issue="accepted without an auth set"),
    )

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == AMBIGUOUS
    assert _mirror_warnings(caplog) == [
        f"mender mirror refuses {NODE_ID} as ambiguous: 'b7e2' (no issue), 'c9f3' ('accepted without an auth set')"
    ]


async def test_mirror_two_present_devices_for_one_node_are_refused_rather_than_chosen(
    monkeypatch, node_session, caplog
):
    """The partial unique index forbids two clean ones; the reader still
    refuses, as _record refuses two accepted auth sets, if the index were ever
    missing."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await node_session.execute(text("DROP INDEX ux_mender_devices_present_node_id"))
    await _seed(node_session, _mirror_row(mender_device_id="c9f3"), _mirror_row())

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == AMBIGUOUS
    assert _mirror_warnings(caplog) == [
        f"mender mirror refuses {NODE_ID} as ambiguous: 'b7e2' (no issue), 'c9f3' (no issue)"
    ]


async def test_mirror_refusal_log_keeps_a_forged_id_and_issue_on_one_line(monkeypatch, node_session, caplog):
    """The sender chooses the device id and the issue text: a newline in either
    must not start a second log line, and neither is logged at full length."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    forged_id = "b7e2\nWARNING mender mirror refuses other as ambiguous"
    await _seed(
        node_session,
        _mirror_row(mender_device_id=forged_id, snapshot_issue="bad\nissue " + "x" * 100),
    )

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        await mender.lookup_device(NODE_ID, node_session)

    [line] = _mirror_warnings(caplog)
    assert "\n" not in line
    assert line.startswith(f"mender mirror refuses {NODE_ID} as ambiguous: 'b7e2\\nWARNING ")
    assert "'bad\\nissue " in line
    assert "x" * 60 not in line


async def test_mirror_an_issue_refuses_its_own_node_id_alone(monkeypatch, node_session, caplog):
    """Flagged devices under another node_id, and a flagged one under this
    node_id that has since left the fleet, change nothing for the clean,
    present one."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    issue = "node_id shared by 2 devices"
    await _seed(
        node_session,
        _mirror_row(),
        _mirror_row(mender_device_id="a0d1", present=False, snapshot_issue=issue),
        _mirror_row(node_id="retffffffff", mender_device_id="c9f3", snapshot_issue=issue),
        _mirror_row(node_id="retffffffff", mender_device_id="d4e5", snapshot_issue=issue),
    )

    with caplog.at_level(logging.WARNING, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == mender.MenderDeviceRecord("b7e2", NODE_ID, "accepted", "as-1", REAL_PUBKEY, EXPECTED_FINGERPRINT)
    assert _mirror_warnings(caplog) == []


async def test_mirror_reads_a_row_as_stored_not_as_the_session_holds_it(monkeypatch, node_session):
    """The session is the caller's and may already hold the device: a rejection
    the next snapshot wrote must not be answered with the accepted copy."""
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())
    held = await node_session.get(MenderDevice, "b7e2")
    assert held is not None and held.auth_status == "accepted"

    _sqlite(node_session, "UPDATE mender_devices SET auth_status = 'rejected', auth_set_id = NULL")

    record = await mender.lookup_device(NODE_ID, node_session)
    assert record is not None and record.auth_status == "rejected"


async def test_mirror_leaves_the_callers_pending_writes_alone(monkeypatch, node_session):
    """Read through the caller's session without flushing it: the caller's
    writes stay the caller's to flush, and a conflict in them must not come
    back as the mirror being unreadable."""
    from core.nodes import Node

    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())
    pending = Node(node_id="ret0123abcd", node_ref="nde1234567800", board_model="raspberrypi5-4gb")
    node_session.add(pending)

    record = await mender.lookup_device(NODE_ID, node_session)

    assert record is not None and record.auth_status == "accepted"
    assert pending in node_session.new


async def test_mirror_without_a_session_opens_one_of_its_own(monkeypatch, node_session):
    monkeypatch.setenv("MENDER_SOURCE", "mirror")
    _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())
    monkeypatch.setattr(users, "async_session_maker", async_sessionmaker(node_session.bind, expire_on_commit=False))

    record = await mender.lookup_device(NODE_ID)

    assert record is not None and record.auth_status == "accepted"


# shadow

SHADOW_LOG = "mender shadow"


def _shadow_lines(caplog, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level and SHADOW_LOG in r.getMessage()]


async def test_shadow_agreement_is_logged_and_the_api_answers(monkeypatch, node_session, caplog):
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    calls = _serve(monkeypatch, ACCEPTED_ON_THE_API)
    await _seed(node_session, _mirror_row())

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == mender.MenderDeviceRecord("b7e2", NODE_ID, "accepted", "as-1", REAL_PUBKEY, EXPECTED_FINGERPRINT)
    assert len(calls) == 1
    assert _shadow_lines(caplog, logging.INFO) == [f"mender shadow agrees for {NODE_ID}"]
    assert _shadow_lines(caplog, logging.WARNING) == []


# The mirror side as _mirror_row arguments rather than rows: a row built at
# collection would be one ORM object, detached after its first test.
@pytest.mark.parametrize(
    ("api_devices", "mirror_row"),
    [
        (ACCEPTED_ON_THE_API, {"node_id": "retffffffff"}),
        ([], {}),
        (PENDING_ON_THE_API, {"auth_status": "rejected"}),
        (ACCEPTED_ON_THE_API, {"auth_set_id": "as-2"}),
        (ACCEPTED_ON_THE_API, {"fingerprint": "0" * 64}),
        (ACCEPTED_ON_THE_API, {"mender_device_id": "c9f3"}),
        (ACCEPTED_ON_THE_API, {"snapshot_issue": "auth_set_pubkey too long"}),
    ],
    ids=["mirror-none", "api-none", "auth-status", "auth-set-id", "fingerprint", "mender-device-id", "snapshot-issue"],
)
async def test_shadow_a_disagreement_is_a_warning_and_the_api_still_answers(
    monkeypatch, node_session, caplog, api_devices, mirror_row
):
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, api_devices)
    await _seed(node_session, _mirror_row(**mirror_row))

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    _serve(monkeypatch, api_devices)
    assert record == await mender._lookup_api(NODE_ID)
    [warning] = _shadow_lines(caplog, logging.WARNING)
    assert warning.startswith(f"mender shadow disagrees for {NODE_ID}: api=")
    assert " mirror=" in warning
    assert _shadow_lines(caplog, logging.INFO) == []
    # Summaries only: no key material in the log.
    assert "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE" not in caplog.text


async def test_shadow_a_flagged_entry_against_an_accepted_answer_is_a_disagreement(monkeypatch, node_session, caplog):
    """Everything on the row matches the API's but its issue: the mirror would
    refuse what the API admits, and that is what the warning is for."""
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, ACCEPTED_ON_THE_API)
    await _seed(node_session, _mirror_row(snapshot_issue="node_id shared by 2 devices"))

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == mender.MenderDeviceRecord("b7e2", NODE_ID, "accepted", "as-1", REAL_PUBKEY, EXPECTED_FINGERPRINT)
    assert _shadow_lines(caplog, logging.WARNING) == [
        f"mender shadow disagrees for {NODE_ID}: "
        f"api=accepted device=b7e2 set=as-1 fp={EXPECTED_FINGERPRINT[:16]} "
        "mirror=ambiguous device=b7e2 set=None fp=None"
    ]
    assert _shadow_lines(caplog, logging.INFO) == []


async def test_shadow_a_flagged_entry_agrees_with_an_api_that_refuses_it_the_same_way(
    monkeypatch, node_session, caplog
):
    """Accepted with no accepted auth set: _record calls it ambiguous, and the
    snapshot route stores it with an issue. Same refusal, same shape."""
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, [_device(NODE_ID, status="accepted", auth_sets=[{"id": "as-1", "status": "pending"}])])
    await _seed(node_session, _mirror_row(auth_set_id=None, snapshot_issue="accepted without an auth set"))

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record == AMBIGUOUS
    assert _shadow_lines(caplog, logging.INFO) == [f"mender shadow agrees for {NODE_ID}"]
    assert _shadow_lines(caplog, logging.WARNING) == []


@pytest.mark.parametrize(
    ("age_s", "reason"),
    [
        (None, "mirror stale (no snapshot yet)"),
        (1000, "mirror stale (last snapshot 1000 s ago, limit 300 s)"),
        (-31, "mirror stale (last snapshot is 31 s in the future, the server clock went back)"),
    ],
    ids=["empty", "stale", "future"],
)
async def test_shadow_a_mirror_it_cannot_use_is_a_warning_and_the_api_answers(
    monkeypatch, node_session, caplog, age_s, reason
):
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, ACCEPTED_ON_THE_API)
    await _seed(node_session, _mirror_row(), age_s=age_s)

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record is not None and record.auth_status == "accepted"
    assert _shadow_lines(caplog, logging.WARNING) == [
        f"mender shadow could not read the mirror for {NODE_ID}: {reason}"
    ]


async def test_shadow_a_malformed_max_age_or_a_broken_table_is_only_a_warning(monkeypatch, node_session, caplog):
    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, ACCEPTED_ON_THE_API)
    await _seed(node_session, _mirror_row())

    monkeypatch.setenv("MENDER_MIRROR_MAX_AGE_S", "soon")
    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        assert (await mender.lookup_device(NODE_ID, node_session)).auth_status == "accepted"
    monkeypatch.delenv("MENDER_MIRROR_MAX_AGE_S")
    await node_session.execute(text("DROP TABLE mender_devices"))
    await node_session.commit()
    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        assert (await mender.lookup_device(NODE_ID, node_session)).auth_status == "accepted"

    first, second = _shadow_lines(caplog, logging.WARNING)
    assert first == (
        f"mender shadow could not read the mirror for {NODE_ID}: "
        "mirror stale (MENDER_MIRROR_MAX_AGE_S='soon' is not a number)"
    )
    assert (
        second
        == f"mender shadow could not read the mirror for {NODE_ID}: mirror unreadable: no such table: mender_devices"
    )


async def test_shadow_nothing_the_mirror_read_raises_reaches_the_caller(monkeypatch, node_session, caplog):
    async def _broken(*_args, **_kwargs):
        raise RuntimeError("a bug in the mirror read")

    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, ACCEPTED_ON_THE_API)
    monkeypatch.setattr(mender, "_read_mirror", _broken)

    with caplog.at_level(logging.INFO, logger=mender.logger.name):
        record = await mender.lookup_device(NODE_ID, node_session)

    assert record is not None and record.auth_status == "accepted"
    [warning] = _shadow_lines(caplog, logging.WARNING)
    assert warning == f"mender shadow could not read the mirror for {NODE_ID}: RuntimeError('a bug in the mirror read')"


async def test_shadow_with_mender_down_raises_and_leaves_the_mirror_unread(monkeypatch, node_session, caplog):
    """Alerts work as in api: the API's MenderUnreachable is the caller's, even
    with a mirror that could have answered."""
    reads: list[object] = []

    async def _spy(*args, **_kwargs):
        reads.append(args)

    monkeypatch.setenv("MENDER_SOURCE", "shadow")
    _serve(monkeypatch, {"error": "down"}, status_code=503)
    await _seed(node_session, _mirror_row())
    monkeypatch.setattr(mender, "_read_mirror", _spy)

    with caplog.at_level(logging.INFO, logger=mender.logger.name), pytest.raises(mender.MenderUnreachable):
        await mender.lookup_device(NODE_ID, node_session)
    assert reads == []
    assert SHADOW_LOG not in caplog.text


# unknown and default


@pytest.mark.parametrize("raw", ["mirrror", " Mirrors ", "off"])
async def test_an_unknown_source_raises_before_anything_is_asked(monkeypatch, node_session, raw):
    monkeypatch.setenv("MENDER_SOURCE", raw)
    calls = _mender_must_not_be_asked(monkeypatch)
    await _seed(node_session, _mirror_row())

    with pytest.raises(mender.MenderUnreachable) as raised:
        await mender.lookup_device(NODE_ID, node_session)
    assert str(raised.value) == f"unknown MENDER_SOURCE '{raw.strip().lower()}'"
    assert calls == []


@pytest.mark.parametrize("raw", [None, "", "API ", " Api\n"], ids=["unset", "empty", "upper-trailing", "mixed"])
async def test_the_api_is_the_default_whatever_its_spelling(monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv("MENDER_SOURCE", raw)
    calls = _serve(monkeypatch, ACCEPTED_ON_THE_API)

    record = await mender.lookup_device(NODE_ID)

    assert record is not None and record.auth_set_fingerprint == EXPECTED_FINGERPRINT
    assert len(calls) == 1
