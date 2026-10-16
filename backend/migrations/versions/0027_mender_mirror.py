"""mender_devices and mender_sync_state: the Mender fleet as mender-auto-accept
last posted it, and when it last did.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

# Two new tables. Code from before this revision has no queries that name
# either of them. While it serves after a rollback, registration asks the
# Mender API directly, so once the server holds no Mender token every
# registration fails as Mender unreachable; snapshots get a 404.
rollback_safety = "additive"


def upgrade() -> None:
    op.create_table(
        "mender_devices",
        sa.Column("mender_device_id", sa.String(length=64), nullable=False),
        # No foreign key to nodes: Mender can know a device the registry does not.
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("auth_status", sa.String(length=32), nullable=False),
        sa.Column("auth_set_id", sa.String(length=64), nullable=True),
        sa.Column("auth_set_pubkey", sa.Text(), nullable=True),
        sa.Column("auth_set_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("auth_set_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_type", sa.String(length=128), nullable=True),
        sa.Column("artifact_name", sa.String(length=256), nullable=True),
        sa.Column("inventory_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("present", sa.Boolean(), server_default="1", nullable=False),
        # Why the latest snapshot's entry cannot be used for registration, or
        # null: a duplicate node_id, say, refuses that node and not the push.
        sa.Column("snapshot_issue", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("mender_device_id"),
    )
    # Present rows with no snapshot_issue only: a device kept after it left
    # the fleet, or one whose latest entry is unusable, does not block another
    # under the same node_id. NULLs never collide.
    op.create_index(
        "ux_mender_devices_present_node_id",
        "mender_devices",
        ["node_id"],
        unique=True,
        sqlite_where=sa.text("present = 1 AND snapshot_issue IS NULL"),
    )

    op.create_table(
        "mender_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_count", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        # A single row.
        sa.CheckConstraint("id = 1", name="ck_mender_sync_state_single_row"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # The mirror goes whole. A later upgrade starts it empty, and registration
    # in mirror mode refuses until the next snapshot lands.
    op.drop_table("mender_sync_state")
    op.drop_index("ux_mender_devices_present_node_id", table_name="mender_devices")
    op.drop_table("mender_devices")
