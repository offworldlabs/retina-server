"""Drop invites, which nothing has read since sign-in stopped going through OAuth.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# A dropped table. Code from before this revision serves the admin invite
# routes, which query it, so a rollback across it needs `alembic downgrade`.
rollback_safety = "destructive"


def upgrade() -> None:
    op.drop_index("ix_invites_email", table_name="invites")
    op.drop_table("invites")


def downgrade() -> None:
    # Recreates the table as 0001 built it, empty: rows the upgrade dropped do
    # not come back.
    op.create_table(
        "invites",
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("used_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_invites_email", "invites", ["email"])
