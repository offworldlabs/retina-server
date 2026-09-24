"""Upgrade the database to head, then report the revision it is at.

Run by deploy/start.sh from /app/backend. One interpreter for both, because
each Alembic command loads migrations/env.py and with it the app's models.

The upgrade goes through Alembic's own command line, so it fails as
`alembic upgrade head` does, with the message start.sh greps for a rollback.
The revision report is diagnostic only (the root logger is pinned to WARN, so
nothing else says what the boot left the database at) and never fails the boot.
"""

import io
import sys
import traceback

from alembic import command
from alembic.config import Config, main

main(argv=["upgrade", "head"])

current = io.StringIO()
try:
    command.current(Config("alembic.ini", stdout=current))
except Exception:
    print("[migrate.py] Could not determine current revision (non-fatal):")
    traceback.print_exc(file=sys.stdout)
else:
    print(f"[migrate.py] Database is now at:\n{current.getvalue().rstrip()}")
