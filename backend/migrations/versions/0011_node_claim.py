"""The address a node was claimed with, and its one pending challenge.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Two new tables and nothing else. Code from before this revision has no
# queries that name them, as 0002 says of its three.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "node_claims",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("verified", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("undeliverable", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_table(
        "node_claim_challenges",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        # SHA-256 hex, so 64 characters.
        sa.Column("handle", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("node_id"),
    )
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_node_claim_challenges_email", "node_claim_challenges", ["email"])


def downgrade() -> None:
    # Drops every confirmed address with it. The bindings in `node_owners`
    # survive, so an owner keeps their nodes; what is lost is the record of
    # which address each was claimed with, and every outstanding challenge.
    op.drop_index("ix_node_claim_challenges_email", table_name="node_claim_challenges")
    op.drop_table("node_claim_challenges")
    op.drop_table("node_claims")
