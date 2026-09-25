"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
<%
    # `just new-migration` passes `-x rollback_safety=...`. Anything else gets a
    # value test_migrations.py refuses, so the declaration cannot be forgotten.
    x_args = dict(arg.split("=", 1) for arg in (getattr(config.cmd_opts, "x", None) or []) if "=" in arg)
    rollback_safety = x_args.get("rollback_safety", "TODO")
%>
import sqlalchemy as sa
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None

# additive or destructive: docs/runbook.md, "Declaring a revision's rollback safety".
rollback_safety = "${rollback_safety}"


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
