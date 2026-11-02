"""Fill the node activity record from the detection archive, now.

Usage, from backend/ (in the container: docker compose exec -w /app/backend server ...):
    python3 -m scripts.backfill_activity_days

Records every node day of the contributor window, today included, that the
detection archive holds on local disk or in the object store
(services/activity_backfill.py). The server does this by itself once, on its
first entitlement run, while the record holds no day before today; this runs
it whatever the record holds. Days already recorded are left as they are, so
running it twice is harmless. The next entitlement run counts the new days.

Reads the object store and window settings from the environment, which the
container has from backend/.env. Prints how many rows were new.

Exit code: 0 done, 1 it failed (the error is printed).
"""

import argparse
import asyncio
import logging
import sys

import core.users
from services.activity_backfill import backfill_activity_days


async def _run() -> int:
    try:
        return await backfill_activity_days()
    finally:
        await core.users.engine.dispose()


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Fill the node activity record from the detection archive.").parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        added = asyncio.run(_run())
    except Exception as exc:
        print(f"Activity backfill failed: {exc!r}", file=sys.stderr)
        return 1
    print(f"{added} new node activity day(s) recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
