"""The hardware a node runs on: its SDR and the gain it runs at, its antenna
and feedline, whether a local ADS-B receiver shares the site, and its two notch
filters, each of the two pairs held whole or not at all.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

# Eleven nullable columns with no default, and two checks over them. Code from
# before this revision has no query that names them, and every existing row
# reads null, which is what "the node did not say" means and what both checks
# allow.
rollback_safety = "additive"


def upgrade() -> None:
    # One table rebuild for all of it: SQLite cannot add a table CHECK to an
    # existing table, so batch mode copies node_configs with the columns and the
    # checks in place and swaps it in. The checks are core.nodes.NodeConfig's:
    # each pair is both its columns or neither.
    with op.batch_alter_table("node_configs") as batch:
        batch.add_column(sa.Column("sdr_model", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("gain_reduction_ref_db", sa.Float(), nullable=True))
        batch.add_column(sa.Column("gain_reduction_surv_db", sa.Float(), nullable=True))
        batch.add_column(sa.Column("lna_state", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("antenna_model", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("antenna_gain_dbi", sa.Float(), nullable=True))
        batch.add_column(sa.Column("antenna_polarisation", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("feedline_loss_db", sa.Float(), nullable=True))
        batch.add_column(sa.Column("local_adsb_receiver", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("notch_dab", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("notch_rf", sa.Boolean(), nullable=True))
        batch.create_check_constraint(
            "ck_node_configs_gain_reduction_pair",
            "(gain_reduction_ref_db IS NULL) = (gain_reduction_surv_db IS NULL)",
        )
        batch.create_check_constraint("ck_node_configs_notch_filters_pair", "(notch_dab IS NULL) = (notch_rf IS NULL)")


def downgrade() -> None:
    # SQLite rewrites the table to drop a column, so all eleven go in one pass,
    # after the two checks: the rewrite carries a named check over, and these
    # name columns that are about to go. What every row held in them goes too:
    # this table is the only place the database keeps what a node reported
    # about its hardware.
    with op.batch_alter_table("node_configs") as batch:
        batch.drop_constraint("ck_node_configs_notch_filters_pair", type_="check")
        batch.drop_constraint("ck_node_configs_gain_reduction_pair", type_="check")
        batch.drop_column("notch_rf")
        batch.drop_column("notch_dab")
        batch.drop_column("local_adsb_receiver")
        batch.drop_column("feedline_loss_db")
        batch.drop_column("antenna_polarisation")
        batch.drop_column("antenna_gain_dbi")
        batch.drop_column("antenna_model")
        batch.drop_column("lna_state")
        batch.drop_column("gain_reduction_surv_db")
        batch.drop_column("gain_reduction_ref_db")
        batch.drop_column("sdr_model")
