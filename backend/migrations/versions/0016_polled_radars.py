"""polled_radars and their endpoint history: what polling a stock blah2 radar adds to its node.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# Two new tables and nothing else. Code from before this revision has no queries
# that name them, as 0002 says of its three.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "polled_radars",
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("endpoint_raw", sa.String(length=2048), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("endpoint_key", sa.String(length=320), nullable=False),
        sa.Column("auth_user", sa.String(length=255), nullable=True),
        # Fernet ciphertext, never the secret itself.
        sa.Column("auth_secret_enc", sa.Text(), nullable=True),
        sa.Column("last_resolved_ip", sa.String(length=45), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("probe_passed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoint_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trust_state", sa.String(length=16), server_default="probation", nullable=False),
        sa.Column("unprotected", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("liveness", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("last_frame_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_config_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
        sa.PrimaryKeyConstraint("node_id"),
    )
    # Named to match what `unique=True, index=True` on the model produces, or the
    # schema comparison in tests/test_migrations.py sees two different databases.
    op.create_index("ix_polled_radars_endpoint_key", "polled_radars", ["endpoint_key"], unique=True)

    op.create_table(
        "polled_radar_endpoint_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("old_key", sa.String(length=320), nullable=True),
        sa.Column("new_key", sa.String(length=320), nullable=False),
        sa.Column("resolved_ip", sa.String(length=45), nullable=True),
        sa.Column("changed_by", sa.String(length=255), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["polled_radars.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_polled_radar_endpoint_history_node_id", "polled_radar_endpoint_history", ["node_id"])


def downgrade() -> None:
    # Drops every polled radar's endpoint and credential. Their nodes, configs
    # and claims stay, as nodes that no longer stream.
    op.drop_index("ix_polled_radar_endpoint_history_node_id", table_name="polled_radar_endpoint_history")
    op.drop_table("polled_radar_endpoint_history")
    op.drop_index("ix_polled_radars_endpoint_key", table_name="polled_radars")
    op.drop_table("polled_radars")
