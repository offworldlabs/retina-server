"""node_claims.user_id and owned_since_user_id in the account's own spelling.

Revision ID: 0026
Revises: 0025
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# Data only: no table, column or index changes. Code from before this revision
# matches owner ids exactly against the account's own spelling, so it finds the
# rewritten ones where it missed them before. An administrator's assignment
# from that code, serving after a rollback, can store another spelling again,
# and nothing rewrites it until the same account is assigned again by code
# from this revision on (services/node_claim_store.py, set_owner).
rollback_safety = "additive"

_REWRITE = {
    "user_id": sa.text("UPDATE node_claims SET user_id = :owner WHERE node_id = :node_id"),
    "owned_since_user_id": sa.text("UPDATE node_claims SET owned_since_user_id = :owner WHERE node_id = :node_id"),
}


def upgrade() -> None:
    """Rewrite every owner id that is a UUID in another spelling as the
    account's own: the dashed lower-case form "user".id holds.

    The admin owner route used to store whichever spelling of the id it was
    sent, upper case or undashed say, and node_owners took any before 0015
    folded it in here. Every read by account matches
    node_claims.user_id exactly, so a node owned that way was missing from the
    account's node count and its nodes, and its owner read as no account.
    owned_since_user_id was written from the same value, and get_user_tenures
    compares the two, so both are rewritten together.

    No other column can hold another spelling. node_labels.user_id is written
    only with the signed-in owner's own id, and only while node_claims holds
    that exact id (services/owner_nodes.py), and the tables keyed to "user".id
    are written from the account's id.

    An id that is not a UUID at all names no account in any spelling, so it is
    left as it is, and counted in the boot log: start.sh prints what this
    writes.
    """
    bind = op.get_bind()
    claims = bind.execute(
        sa.text(
            "SELECT node_id, user_id, owned_since_user_id FROM node_claims "
            "WHERE user_id IS NOT NULL OR owned_since_user_id IS NOT NULL ORDER BY node_id"
        )
    ).fetchall()
    rewritten: set[str] = set()
    unreadable: list[str] = []
    for node_id, user_id, since_for in claims:
        for column, stored in (("user_id", user_id), ("owned_since_user_id", since_for)):
            if stored is None:
                continue
            owner = _account(stored)
            if owner is None:
                unreadable.append(node_id)
            elif owner != stored:
                bind.execute(_REWRITE[column], {"owner": owner, "node_id": node_id})
                rewritten.add(node_id)
    if rewritten:
        print(f"[0026] {len(rewritten)} claim(s) had an owner id in another spelling, now the account's own")
    if unreadable:
        nodes = sorted(set(unreadable))
        print(
            f"[0026] {len(unreadable)} owner id(s) are not UUIDs and were left as they were, "
            f"on {', '.join(map(repr, nodes[:5]))}{', ...' if len(nodes) > 5 else ''}"
        )


def _account(user_id: object) -> str | None:
    """A user id in its canonical spelling, or None for one that is not a UUID.

    A copy of 0024's: a revision imports nothing from the app.
    """
    try:
        return str(uuid.UUID(str(user_id)))
    except ValueError:
        return None


def downgrade() -> None:
    # Nothing to undo. Each rewritten id names the account its old spelling
    # named, and code from before this revision finds the account by the new
    # form, where it missed the old one. Putting the old spellings back would
    # need them recorded somewhere, and would only hide those nodes from their
    # accounts again.
    pass
