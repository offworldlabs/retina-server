"""user_sessions and retired_legacy_tokens: sign-in sessions held on the server.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# Two new tables. Code from before this revision has no queries that name them.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        # CHAR(36), as "user".id is: see 0001.
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        # SHA-256 hex, so 64 characters. The token itself is never stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(length=120), server_default="", nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Named to match what `index=True` on the model produces, or the schema
    # comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    op.create_table(
        "retired_legacy_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        # Null for a row that retires every cookie of the account at once.
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retired_legacy_tokens_user_id", "retired_legacy_tokens", ["user_id"])
    op.create_index("ix_retired_legacy_tokens_token_hash", "retired_legacy_tokens", ["token_hash"], unique=True)
    op.create_index("ix_retired_legacy_tokens_expires_at", "retired_legacy_tokens", ["expires_at"])


def downgrade() -> None:
    # Signs out everybody who signed in since, as the code before this reads
    # auth_token as a JWT and a session token is not one. The record of which
    # JWT cookies were retired goes too, so any of those still inside its seven
    # days works again on the older code.
    op.drop_index("ix_retired_legacy_tokens_expires_at", table_name="retired_legacy_tokens")
    op.drop_index("ix_retired_legacy_tokens_token_hash", table_name="retired_legacy_tokens")
    op.drop_index("ix_retired_legacy_tokens_user_id", table_name="retired_legacy_tokens")
    op.drop_table("retired_legacy_tokens")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_token_hash", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
