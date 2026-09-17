"""node_claims and node_claim_challenges, from the migrated schema up.

The route's own behaviour is pinned elsewhere. What is tested here is what only
the store can get wrong: which writes clear the verified flag, which handle a
displaced challenge gives back, and the address check that keeps a late click
or a stale bounce off a corrected address.

Nothing here commits, matching the store: the routes own their transactions.
"""

from services.node_claim_store import (
    clear_claim,
    drop_challenge,
    mark_undeliverable,
    mark_verified,
    put_challenge,
    read_challenge,
    read_claim,
    set_claim_address,
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
