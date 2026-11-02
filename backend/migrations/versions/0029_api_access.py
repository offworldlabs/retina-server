"""tiers, entitlements, api_keys and node_activity_days: who may use the keyed
data API and within what limits, and the per-day node activity the contributor
rule counts.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# Four new tables and the two tier rows. Code from before this revision has
# no queries that name them.
rollback_safety = "additive"

# A copy of core/api_access.py's DEFAULT_TIERS: a revision imports nothing
# from the app. tests/test_migrations.py holds the two to the same rows.
_TIERS = [
    {
        "id": 1,
        "name": "contributor",
        "requests_per_minute": 60,
        "max_concurrent_streams": 2,
        "max_history_span_s": 86400,
    },
    {
        "id": 2,
        "name": "customer",
        "requests_per_minute": 600,
        "max_concurrent_streams": 10,
        "max_history_span_s": 604800,
    },
]


def upgrade() -> None:
    tiers = op.create_table(
        "tiers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_streams", sa.Integer(), nullable=False),
        sa.Column("max_history_span_s", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.bulk_insert(tiers, _TIERS)

    op.create_table(
        "entitlements",
        sa.Column("id", sa.String(length=36), nullable=False),
        # CHAR(36), as "user".id is: see 0001.
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tier_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lapsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('contributor', 'customer')", name="ck_entitlements_kind"),
        sa.CheckConstraint("source IN ('uptime', 'manual', 'stripe')", name="ck_entitlements_source"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tier_id"], ["tiers.id"]),
    )
    op.create_index("ix_entitlements_user_id", "entitlements", ["user_id"])
    # One open contributor row per account. A lapsed row is history, and the
    # next qualification opens a new one beside it.
    op.create_index(
        "ux_entitlements_open_contributor",
        "entitlements",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'contributor' AND lapsed_at IS NULL"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.CHAR(36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        # SHA-256 hex of the key's secret, which is never stored.
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"], unique=True)

    op.create_table(
        "node_activity_days",
        # No foreign key to nodes: a node reporting over TCP alone has no
        # registry row, and SQLite's INSERT OR IGNORE does not ignore a foreign
        # key failure.
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("node_id", "day"),
    )


def downgrade() -> None:
    # Every key, entitlement and day of activity goes. A later upgrade starts
    # them empty and seeds the default tiers again.
    op.drop_table("node_activity_days")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ux_entitlements_open_contributor", table_name="entitlements")
    op.drop_index("ix_entitlements_user_id", table_name="entitlements")
    op.drop_table("entitlements")
    op.drop_table("tiers")
