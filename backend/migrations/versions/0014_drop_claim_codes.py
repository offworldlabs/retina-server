"""Drop claim_codes, which nothing reads since nodes are claimed by email.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# A dropped table. Code from before this revision serves the claim-code routes
# and redeems codes on the TCP HELLO, both of which query it, so a rollback
# across it needs `alembic downgrade`.
rollback_safety = "destructive"


def upgrade() -> None:
    op.drop_index("ix_claim_codes_user_id", table_name="claim_codes")
    op.drop_table("claim_codes")


def downgrade() -> None:
    # Recreates the table as 0001 built it, empty: rows the upgrade dropped do
    # not come back.
    op.create_table(
        "claim_codes",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.Column("used_by_node_id", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_index("ix_claim_codes_user_id", "claim_codes", ["user_id"])
