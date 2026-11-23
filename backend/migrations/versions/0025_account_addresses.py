"""retired_addresses and pending_address_changes: the addresses an account
signed in with before its current one, and the one change of address it has
waiting for confirmation; and node_claims.declined: the address on file
declined the claim.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# Two new tables and a column with a default. Code from before this revision
# names none of them. While it serves after a rollback, a retired address is a
# stranger to it: signing in with one makes a second account, as before the
# address was retired, and a pending change can no longer be confirmed. It
# records no decline and clears none, so a claim it moves to another address
# keeps the flag the last address set: once this revision serves again, that
# claim is missing from its new address's dashboard until the node asks for
# its mail again. The mail the node asked for goes either way.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "retired_addresses",
        # Normalised, so the key is case-insensitive uniqueness. The width of
        # "user".email, where every one of these was an account's address.
        sa.Column("email", sa.String(length=320), nullable=False),
        # CHAR(36), as "user".id is: see 0001.
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("email"),
    )
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_retired_addresses_user_id", "retired_addresses", ["user_id"])

    op.create_table(
        "pending_address_changes",
        # The key, so an account has one pending change at most.
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        # SHA-256 hex of the mailed token, as magic_links keys it.
        sa.Column("handle", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index("ix_pending_address_changes_handle", "pending_address_changes", ["handle"], unique=True)

    # Every claim on file reads as not declined: nothing recorded a decline before this.
    op.add_column("node_claims", sa.Column("declined", sa.Boolean(), server_default="0", nullable=False))


def downgrade() -> None:
    # SQLite rewrites node_claims to drop the column; every row keeps the rest.
    # A declined claim is offered to its address again.
    with op.batch_alter_table("node_claims") as batch:
        batch.drop_column("declined")
    # Every retired address goes back to being nobody's, so the next sign-in or
    # claim with one makes a new account. Pending changes go too, and their
    # links can no longer be confirmed.
    op.drop_index("ix_pending_address_changes_handle", table_name="pending_address_changes")
    op.drop_table("pending_address_changes")
    op.drop_index("ix_retired_addresses_user_id", table_name="retired_addresses")
    op.drop_table("retired_addresses")
