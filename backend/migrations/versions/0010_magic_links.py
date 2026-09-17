"""Outstanding sign-in links, one row per unredeemed token.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# One new table and nothing else. Code from before this revision has no queries
# that name it, as 0002 says of its three.
rollback_safety = "additive"


def upgrade() -> None:
    # A database built by create_all rather than by the chain already has this
    # table, and creating it again fails. 0001 takes the same position for the
    # four baseline tables: a table that is already correct makes recording the
    # revision the whole job.
    if sa.inspect(op.get_bind()).has_table("magic_links"):
        return

    op.create_table(
        "magic_links",
        # SHA-256 hex, so 64 characters. The token itself is never stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_magic_links_email", "magic_links", ["email"])


def downgrade() -> None:
    # Drops every outstanding link. Anyone mid-sign-in has to ask for another;
    # nothing else is lost, since a redeemed link leaves its session behind in
    # the cookie rather than anything here.
    op.drop_index("ix_magic_links_email", table_name="magic_links")
    op.drop_table("magic_links")
