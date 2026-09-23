"""node_reports: what each node last said about itself on its heartbeat.

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

# One new table. Code from before this revision has no queries that name it.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "node_reports",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("boot_id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("state_since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("uptime_s", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("health", sa.JSON(), nullable=True),
        sa.Column("versions", sa.JSON(), nullable=True),
        sa.Column("errors", sa.JSON(), server_default="[]", nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("node_id"),
    )


def downgrade() -> None:
    op.drop_table("node_reports")
