"""Fold node_owners into node_claims: the owner sits beside the address that claimed it.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# A dropped table. Code from before this revision reads node_owners on every
# node heartbeat (claim_status), so a rollback across it needs
# `alembic downgrade 0014` before those heartbeats stop failing.
rollback_safety = "destructive"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # A database built by create_all rather than by the chain already has the
    # column and never had the old table. 0012 takes the same position.
    if not inspector.has_table("node_owners"):
        return

    # Before anything changes. node_owners took any id; node_claims takes only a
    # registered one, and this connection does not enforce the foreign key, so
    # an owner for an unregistered node would be copied in silently as a row
    # that breaks it. An ownership row is an authorisation fact, and losing one
    # quietly is worse than refusing to boot, which leaves the previous image
    # serving.
    orphans = bind.execute(
        sa.text("SELECT node_id FROM node_owners WHERE node_id NOT IN (SELECT node_id FROM nodes)")
    ).fetchall()
    if orphans:
        raise RuntimeError(
            f"{len(orphans)} node_owners row(s) name a node that never registered "
            f"({', '.join(row[0] for row in orphans[:5])}); node_claims cannot hold them. "
            "Register the node or clear the owner, then deploy again."
        )

    op.add_column("node_claims", sa.Column("user_id", sa.String(length=255), nullable=True))
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_node_claims_user_id", "node_claims", ["user_id"])

    # Owners whose node already carries a claim row gain the column; the rest
    # get a row of their own with no address, which is what an owner assigned
    # by an administrator looks like.
    op.execute(
        "UPDATE node_claims SET user_id = "
        "(SELECT user_id FROM node_owners WHERE node_owners.node_id = node_claims.node_id) "
        "WHERE node_id IN (SELECT node_id FROM node_owners)"
    )
    op.execute(
        "INSERT INTO node_claims (node_id, user_id, verified, undeliverable) "
        "SELECT node_id, user_id, 0, 0 FROM node_owners "
        "WHERE node_id NOT IN (SELECT node_id FROM node_claims)"
    )

    op.drop_index("ix_node_owners_user_id", table_name="node_owners")
    op.drop_table("node_owners")


def downgrade() -> None:
    # Recreates node_owners as 0001 built it and hands the owners back to it.
    op.create_table(
        "node_owners",
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index("ix_node_owners_user_id", "node_owners", ["user_id"])
    op.execute(
        "INSERT INTO node_owners (node_id, user_id) SELECT node_id, user_id FROM node_claims WHERE user_id IS NOT NULL"
    )
    # A row that existed only to carry an owner has nothing left to say, and
    # before this revision every claim row carried an address.
    op.execute("DELETE FROM node_claims WHERE email IS NULL")

    op.drop_index("ix_node_claims_user_id", table_name="node_claims")
    with op.batch_alter_table("node_claims") as batch:
        batch.drop_column("user_id")
