"""node_claims and node_claim_challenges, from the migrated schema up.

The route's own behaviour is pinned elsewhere. What is tested here is what only
the store can get wrong: which writes clear the verified flag, which handle a
displaced challenge gives back, and the address check that keeps a late click
or a stale bounce off a corrected address.

Nothing here commits, matching the store: the routes own their transactions.
The exception is a test where another connection writes in the middle, which
has to see the rows first.
"""

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from core.nodes import Node, NodeClaim, NodeClaimChallenge
from services.node_claim_store import (
    clear_claim,
    drop_challenge,
    mark_open_undeliverable,
    mark_undeliverable,
    mark_verified,
    open_claims,
    put_challenge,
    read_challenge,
    read_claim,
    set_claim_address,
    set_owner,
)

ADA = "ada@example.com"
GRACE = "grace@example.com"

# Far enough ahead that no test is racing the clock.
EXPIRES_AT = 4102444800.0


async def test_nominating_an_address_stores_it_unverified(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)

    row = await read_claim(node_session, seeded_node.node_id)
    assert (row.email, row.verified, row.undeliverable) == (ADA, False, False)


async def test_a_node_that_never_nominated_has_no_row(seeded_node, node_session):
    assert await read_claim(node_session, seeded_node.node_id) is None


async def test_a_new_address_drops_the_verified_flag(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await mark_verified(node_session, seeded_node.node_id, ADA)

    await set_claim_address(node_session, seeded_node.node_id, GRACE)

    row = await read_claim(node_session, seeded_node.node_id)
    assert (row.email, row.verified) == (GRACE, False)


async def test_rewriting_the_same_address_leaves_verified_alone(seeded_node, node_session):
    """The spec's "writing the address it already holds is accepted and changes
    nothing": a node resending its configuration must not unverify itself."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await mark_verified(node_session, seeded_node.node_id, ADA)

    await set_claim_address(node_session, seeded_node.node_id, ADA)

    row = await read_claim(node_session, seeded_node.node_id)
    assert (row.email, row.verified) == (ADA, True)


async def test_marking_verified_for_a_superseded_address_does_nothing(seeded_node, node_session):
    """A click that arrives after the address was corrected must not confirm the
    correction."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await set_claim_address(node_session, seeded_node.node_id, GRACE)

    await mark_verified(node_session, seeded_node.node_id, ADA)

    row = await read_claim(node_session, seeded_node.node_id)
    assert (row.email, row.verified) == (GRACE, False)


async def test_marking_undeliverable_for_a_superseded_address_does_nothing(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await set_claim_address(node_session, seeded_node.node_id, GRACE)

    assert await mark_undeliverable(node_session, seeded_node.node_id, ADA) is None

    row = await read_claim(node_session, seeded_node.node_id)
    assert row.undeliverable is False


async def test_verifying_spends_the_challenge(seeded_node, node_session):
    """A row that outlives its redemption is a link that can be walked into
    twice, and nothing downstream should have to notice the node has an owner
    for the second click to fail."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    await mark_verified(node_session, seeded_node.node_id, ADA)

    assert await read_challenge(node_session, seeded_node.node_id) is None
    assert (await read_claim(node_session, seeded_node.node_id)).verified is True


async def test_verifying_a_superseded_address_leaves_the_live_challenge_alone(seeded_node, node_session):
    """The late click is for an address that has been replaced, so the challenge
    it must not touch is the one belonging to the replacement."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await set_claim_address(node_session, seeded_node.node_id, GRACE)
    await put_challenge(node_session, seeded_node.node_id, GRACE, "handle-two", EXPIRES_AT)

    await mark_verified(node_session, seeded_node.node_id, ADA)

    assert (await read_challenge(node_session, seeded_node.node_id)).handle == "handle-two"


async def test_a_bounce_drops_the_challenge_so_the_node_reads_unclaimed(seeded_node, node_session):
    """Nobody clicks a link at an address that does not exist, and a challenge
    left standing would leave the node reading pending for ever."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    assert await mark_undeliverable(node_session, seeded_node.node_id, ADA) == "handle-one"

    assert await read_challenge(node_session, seeded_node.node_id) is None


async def test_a_bounce_on_a_node_with_no_challenge_gives_back_no_handle(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)

    assert await mark_undeliverable(node_session, seeded_node.node_id, ADA) is None

    row = await read_claim(node_session, seeded_node.node_id)
    assert row.undeliverable is True


async def test_a_bounce_checks_the_address_as_it_writes_not_off_a_row_already_loaded(seeded_node, node_session):
    """The row in the session still says the bounced address, but another
    request has committed a new one since, with a challenge of its own. The
    replacement keeps both, and no flag."""
    node_id = seeded_node.node_id
    await set_claim_address(node_session, node_id, ADA)
    await put_challenge(node_session, node_id, ADA, "handle-one", EXPIRES_AT)
    await node_session.commit()
    # Held, as services/claim_links.py holds the claim it read: the session
    # keeps a loaded row only while something refers to it.
    loaded = (await read_claim(node_session, node_id), await read_challenge(node_session, node_id))
    claim, challenge = loaded
    assert claim is not None and challenge is not None
    assert (claim.email, challenge.handle) == (ADA, "handle-one")

    other = sqlite3.connect(node_session.bind.url.database)
    try:
        other.execute("UPDATE node_claims SET email = ? WHERE node_id = ?", (GRACE, node_id))
        other.execute(
            "UPDATE node_claim_challenges SET email = ?, handle = ? WHERE node_id = ?", (GRACE, "handle-two", node_id)
        )
        other.commit()

        assert await mark_undeliverable(node_session, node_id, ADA) is None
        await node_session.commit()

        assert other.execute("SELECT email, undeliverable FROM node_claims").fetchall() == [(GRACE, 0)]
        assert other.execute("SELECT email, handle FROM node_claim_challenges").fetchall() == [(GRACE, "handle-two")]
    finally:
        other.close()


async def _node_offering(node_session, node_id: str, email: str, **claim) -> None:
    node_session.add(Node(node_id=node_id, node_ref=f"nde{node_id[3:]}00", board_model="raspberrypi5-4gb"))
    node_session.add(NodeClaim(node_id=node_id, email=email, **({"verified": False, "undeliverable": False} | claim)))
    await put_challenge(node_session, node_id, email, f"handle-{node_id}", EXPIRES_AT)


async def test_only_open_claims_on_the_address_are_marked_and_lose_their_challenge(seeded_node, node_session):
    """Open is unverified, unowned and not flagged already. A confirmed address
    has proved itself, an owner outranks the address, a flagged one was dealt
    with, and another address is another person."""
    stamped = datetime(2026, 1, 1, tzinfo=UTC)
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-open", EXPIRES_AT)
    await _node_offering(node_session, "ret9f8e7d6c", ADA, verified=True)
    await _node_offering(node_session, "ret2b3c4d5e", ADA, user_id="some-account")
    await _node_offering(node_session, "ret0123abcd", ADA, undeliverable=True, updated_at=stamped)
    await _node_offering(node_session, "retc0ffee00", GRACE)
    everyone = [seeded_node.node_id, "ret9f8e7d6c", "ret2b3c4d5e", "ret0123abcd", "retc0ffee00"]

    assert await open_claims(node_session, ADA) == [seeded_node.node_id]
    assert await mark_open_undeliverable(node_session, ADA, everyone) == {seeded_node.node_id: "handle-open"}

    # Read off the database rather than off the rows the session loaded.
    claims = await node_session.execute(select(NodeClaim.node_id, NodeClaim.undeliverable, NodeClaim.updated_at))
    flags = {node_id: (undeliverable, updated_at) for node_id, undeliverable, updated_at in claims}
    assert {node_id: undeliverable for node_id, (undeliverable, _) in flags.items()} == {
        seeded_node.node_id: True,
        "ret9f8e7d6c": False,
        "ret2b3c4d5e": False,
        "ret0123abcd": True,
        "retc0ffee00": False,
    }
    assert flags["ret0123abcd"][1] == stamped
    challenges = await node_session.execute(select(NodeClaimChallenge.node_id, NodeClaimChallenge.handle))
    assert dict(challenges.tuples().all()) == {node_id: f"handle-{node_id}" for node_id in everyone[1:]}


async def test_marking_open_claims_leaves_nodes_it_was_not_given(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    assert await mark_open_undeliverable(node_session, ADA, []) == {}
    assert await mark_open_undeliverable(node_session, ADA, ["ret9f8e7d6c"]) == {}

    claim = await read_claim(node_session, seeded_node.node_id)
    challenge = await read_challenge(node_session, seeded_node.node_id)
    assert claim is not None and claim.undeliverable is False
    assert challenge is not None and challenge.handle == "handle-one"


async def test_an_open_claim_with_no_challenge_is_marked_with_no_handle(seeded_node, node_session):
    await set_claim_address(node_session, seeded_node.node_id, ADA)

    assert await mark_open_undeliverable(node_session, ADA, [seeded_node.node_id]) == {seeded_node.node_id: None}

    claim = await read_claim(node_session, seeded_node.node_id)
    assert claim is not None and claim.undeliverable is True


async def test_a_different_address_after_a_bounce_starts_clean(seeded_node, node_session):
    """The flag belongs to the address, not to the node: correcting a typo must
    not leave the node carrying the dead address's verdict."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await mark_undeliverable(node_session, seeded_node.node_id, ADA)

    await set_claim_address(node_session, seeded_node.node_id, GRACE)

    row = await read_claim(node_session, seeded_node.node_id)
    assert (row.email, row.undeliverable, row.verified) == (GRACE, False, False)


async def test_marking_a_node_with_no_claim_does_nothing(seeded_node, node_session):
    await mark_verified(node_session, seeded_node.node_id, ADA)

    assert await read_claim(node_session, seeded_node.node_id) is None


async def test_a_second_challenge_replaces_the_first_and_returns_its_handle(seeded_node, node_session):
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    displaced = await put_challenge(node_session, seeded_node.node_id, GRACE, "handle-two", EXPIRES_AT)

    assert displaced == "handle-one"
    row = await read_challenge(node_session, seeded_node.node_id)
    assert (row.email, row.handle) == (GRACE, "handle-two")


async def test_the_first_challenge_displaces_nothing(seeded_node, node_session):
    assert await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT) is None


async def test_dropping_a_challenge_returns_its_handle_and_then_none(seeded_node, node_session):
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    assert await drop_challenge(node_session, seeded_node.node_id) == "handle-one"
    assert await drop_challenge(node_session, seeded_node.node_id) is None
    assert await read_challenge(node_session, seeded_node.node_id) is None


async def test_clearing_a_claim_leaves_no_row_and_no_challenge(seeded_node, node_session):
    """Release: the address goes with the binding rather than staying behind for
    the next owner to read."""
    await set_claim_address(node_session, seeded_node.node_id, ADA)
    await mark_verified(node_session, seeded_node.node_id, ADA)
    await put_challenge(node_session, seeded_node.node_id, ADA, "handle-one", EXPIRES_AT)

    await clear_claim(node_session, seeded_node.node_id)

    assert await read_claim(node_session, seeded_node.node_id) is None
    assert await read_challenge(node_session, seeded_node.node_id) is None


async def test_clearing_a_node_that_has_no_claim_is_not_an_error(seeded_node, node_session):
    await clear_claim(node_session, seeded_node.node_id)


# ── set_owner and the spelling of an owner id ────────────────────────────────

ADA_ID = "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"
BOB_ID = "0b7e9d2c-1a3f-4e6d-9c8b-7a6f5e4d3c2b"
OWNED_SINCE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

# Spellings uuid.UUID reads as one account. The first is the account's own.
SPELLINGS = {
    "own": lambda i: i,
    "upper-undashed": lambda i: i.replace("-", "").upper(),
    "braced": lambda i: f"{{{i}}}",
}


async def _owned_claim(session, node_id: str, owner: str, since_for: str) -> None:
    """A claim Ada confirmed by clicking, with its owner ids written as given."""
    session.add(
        NodeClaim(
            node_id=node_id,
            user_id=owner,
            email=ADA,
            verified=True,
            undeliverable=False,
            declined=False,
            updated_at=OWNED_SINCE,
            owned_since=OWNED_SINCE,
            owned_since_user_id=since_for,
        )
    )
    await session.flush()


async def _reread(session, node_id: str) -> NodeClaim:
    session.expire_all()
    row = await read_claim(session, node_id)
    assert row is not None
    return row


@pytest.mark.parametrize("stored", SPELLINGS.values(), ids=SPELLINGS.keys())
@pytest.mark.parametrize("given", SPELLINGS.values(), ids=SPELLINGS.keys())
async def test_assigning_the_owner_it_has_in_any_spelling_keeps_the_claim(seeded_node, node_session, stored, given):
    """Whatever spelling is on file and whichever is given, it is the same
    account: the address it was claimed with, its confirmation and when it
    was taken all stay, and only the ids take the account's own form."""
    node_id = seeded_node.node_id
    await _owned_claim(node_session, node_id, stored(ADA_ID), stored(ADA_ID))
    await put_challenge(node_session, node_id, ADA, "handle-one", EXPIRES_AT)

    await set_owner(node_session, node_id, given(ADA_ID))

    row = await _reread(node_session, node_id)
    assert (row.user_id, row.owned_since_user_id) == (ADA_ID, ADA_ID)
    assert (row.email, row.verified, row.undeliverable, row.declined) == (ADA, True, False, False)
    assert (row.updated_at, row.owned_since) == (OWNED_SINCE, OWNED_SINCE)
    assert (await read_challenge(node_session, node_id)).handle == "handle-one"


async def test_a_start_written_for_an_earlier_owner_keeps_naming_them(seeded_node, node_session):
    """An owner moved by code that does not know owned_since_user_id leaves it
    naming the owner before. Assigning the current owner again leaves it so,
    in that account's own spelling, and the start stays theirs."""
    node_id = seeded_node.node_id
    await _owned_claim(node_session, node_id, ADA_ID.upper(), BOB_ID.upper())

    await set_owner(node_session, node_id, ADA_ID)

    row = await _reread(node_session, node_id)
    assert (row.user_id, row.owned_since_user_id, row.owned_since) == (ADA_ID, BOB_ID, OWNED_SINCE)


@pytest.mark.parametrize("given", SPELLINGS.values(), ids=SPELLINGS.keys())
async def test_a_new_owner_is_stored_as_the_accounts_own_id(seeded_node, node_session, given):
    """A change of owner in any spelling: the claim starts afresh, under the id
    every read by account matches."""
    node_id = seeded_node.node_id
    await _owned_claim(node_session, node_id, ADA_ID, ADA_ID)

    await set_owner(node_session, node_id, given(BOB_ID))

    row = await _reread(node_session, node_id)
    assert (row.user_id, row.owned_since_user_id, row.email, row.verified) == (BOB_ID, BOB_ID, None, False)
    assert row.owned_since > OWNED_SINCE
