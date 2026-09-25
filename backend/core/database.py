"""Where the database is.

Imports nothing beyond the standard library: every boot's migration reads this,
and the app's models cost that step most of its time (see migrations/env.py).
"""

import os
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
# RETINA_DB_PATH exists so tests and one-off migrations can point at a scratch
# file. It is unset in every deployed environment, where the path derives from
# this module's own location and lands inside the backend-data volume.
# A relative override is resolved against the current working directory, which
# is not the same for every caller: alembic runs with cwd backend/, while the
# application may not. Resolving here makes the same override mean the same
# file regardless of caller.
_DB_PATH = Path(os.getenv("RETINA_DB_PATH") or _DATA_DIR / "users.db").resolve()
DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"
