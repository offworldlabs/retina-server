"""Drop node_location_privacy: a node's location privacy is its registration choice alone.

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# A dropped table. Code from before this revision reads it on every refresh of
# the private-node set and serves the override routes, so a rollback across it
# needs `alembic downgrade 0019`.
rollback_safety = "destructive"


def upgrade() -> None:
    # Each row is a node whose published state may change with this revision,
    # and the table is the only record of it, so the rows go to the boot log
    # first: start.sh prints what the migration writes to stdout.
    for node_id, private in op.get_bind().execute(sa.text("SELECT node_id, private FROM node_location_privacy")):
        print(f"[0020] Dropping location privacy override for {node_id!r} (private={bool(private)})")
    op.drop_table("node_location_privacy")


def downgrade() -> None:
    # Recreates the table as 0006 built it, empty: rows the upgrade dropped do
    # not come back.
    op.create_table(
        "node_location_privacy",
        sa.Column("node_id", sa.String(length=255), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False),
        sa.Column("set_by", sa.String(length=255), server_default="", nullable=False),
        sa.Column("set_at", sa.Float(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )
