"""node_labels: what an owner calls their node, on their own pages only; and
node_claims.owned_since: when the current owner took the node.

Revision ID: 0024
Revises: 0023
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# One new table and two nullable columns. Code from before this revision names
# none of them. An owner it sets while serving after a rollback leaves the two
# columns written for somebody else, or not at all, and the owner reads then
# take the row's updated_at, which that code stamps with the owner
# (core/nodes.py, NodeClaim).
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "node_labels",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.add_column("node_claims", sa.Column("owned_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("node_claims", sa.Column("owned_since_user_id", sa.String(length=255), nullable=True))
    _backfill_owned_since()


def _backfill_owned_since() -> None:
    """Give every owned row a start no earlier than its owner's true one.

    Nothing recorded when an owner took a node. Since 0015, every write that
    sets node_claims.user_id stamps updated_at with it: set_owner, the click
    that binds (through mark_verified, on a row whose address is the
    clicker's), and polled radar registration. Later writes only move it on.
    So updated_at is never earlier than the start of the current ownership on
    a row whose owner one of them set, and it is the start for these rows:

    * no address: an administrator's assignment, or a row 0015 inserted for an
      owner, stamped by the column default as 0015 ran;
    * a confirmed address that is the owner's own: they claimed it by clicking;
    * a polled radar's: registration (0016, after 0015) writes the row with
      the owner's address, confirmed or not.

    Any other owned row has an address that is not its owner's confirmed one,
    which is what 0015 leaves where it folded an administrator's assignment
    onto a claim row: it set user_id without touching updated_at, so the
    row's last write can predate the assignment. Nothing says when that was,
    so such a row starts now, and its owner is shown nothing recorded before
    this migration.
    """
    bind = op.get_bind()
    accounts = {
        account: email.lower()
        for user_id, email in bind.execute(sa.text('SELECT id, email FROM "user"'))
        if (account := _account(user_id)) is not None and email
    }
    claims = bind.execute(
        sa.text(
            "SELECT node_id, user_id, email, verified, updated_at, "
            "EXISTS (SELECT 1 FROM polled_radars WHERE polled_radars.node_id = node_claims.node_id) "
            "FROM node_claims WHERE user_id IS NOT NULL"
        )
    ).fetchall()
    # Stored as UTCDateTime stores it: naive UTC.
    now = datetime.now(UTC).replace(tzinfo=None)
    for node_id, user_id, email, verified, updated_at, polled in claims:
        account = _account(user_id)
        claimed = (
            email is not None and bool(verified) and account is not None and accounts.get(account) == email.lower()
        )
        if updated_at is not None and (email is None or claimed or polled):
            bind.execute(
                sa.text(
                    "UPDATE node_claims SET owned_since = updated_at, owned_since_user_id = user_id "
                    "WHERE node_id = :node_id"
                ),
                {"node_id": node_id},
            )
            continue
        bind.execute(
            sa.text(
                "UPDATE node_claims SET owned_since = :now, owned_since_user_id = user_id WHERE node_id = :node_id"
            ).bindparams(sa.bindparam("now", type_=sa.DateTime())),
            {"node_id": node_id, "now": now},
        )
        # The owner's view of this node's history starts at this deploy, which
        # is worth a line in the boot log: start.sh prints what this writes.
        print(f"[0024] {node_id!r}: no record of when its owner took it; their history starts now")


def _account(user_id: object) -> str | None:
    """A user id in its canonical spelling, or None for one that is not a UUID.

    users.id and node_claims.user_id both hold the canonical form today; this
    reads an older hex spelling as the same account rather than as a stranger.
    """
    try:
        return str(uuid.UUID(str(user_id)))
    except ValueError:
        return None


def downgrade() -> None:
    # SQLite rewrites node_claims to drop the columns; every row keeps its
    # owner, address and updated_at.
    with op.batch_alter_table("node_claims") as batch:
        batch.drop_column("owned_since_user_id")
        batch.drop_column("owned_since")
    # Drops every owner's label.
    op.drop_table("node_labels")
