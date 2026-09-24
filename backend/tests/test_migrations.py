"""Migrations are exercised as a subprocess, the way a deploy runs them.

Importing Alembic in-process would share this interpreter's already-imported
`core.users`, and with it the module-level `engine` bound to whichever database
existed at import time. The subprocess gets a clean import and an explicit
RETINA_DB_PATH, which is the only way the round trip can be trusted.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from tests.migration_helpers import BACKEND, ROLLBACK_AHEAD_SENTINEL, _alembic

MIGRATE_PY = BACKEND.parent / "deploy" / "migrate.py"


def _migrate(db_path: Path, *, cwd: Path = BACKEND, argv: list[str] | None = None) -> subprocess.CompletedProcess:
    """deploy/migrate.py as start.sh runs it, or `argv` in its place."""
    env = os.environ | {"RETINA_ENV": "test", "RETINA_DB_PATH": str(db_path)}
    return subprocess.run(  # noqa: S603
        [sys.executable, *(argv or [str(MIGRATE_PY)])],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_migrations_have_exactly_one_head(tmp_path):
    """Two branches that each add a revision off the same parent will merge
    without a textual conflict: different files, nothing overlapping, nothing a
    review would show. What they leave behind is an Alembic tree with two
    heads, and `alembic upgrade head` refusing to choose between them.

    That refusal reads "Multiple head revisions are present", which is not the
    wording the `elif` in deploy/start.sh tolerates, so it falls through to the
    refuse-to-boot `else` below and no environment starts until the heads are
    reconciled. The rest of this file goes red too, along with conftest.py's
    node schema template and every test behind it, but those all fail on
    `returncode == 0` with Alembic's reason left on the subprocess's stdout,
    where the assertion message does not carry it. This test sits first so it
    is the first failure of the run, and it prints the colliding revisions. The
    fix is to renumber the later one onto the other's head, or `alembic merge`.
    """
    # `heads` is answered from migrations/versions/ through the ScriptDirectory
    # alone and never imports env.py, so nothing is created at this path.
    # _alembic requires one all the same.
    result = _alembic("heads", db_path=tmp_path / "unused.db")
    # Both streams: Alembic's `util.err` puts a clean failure, a missing
    # alembic.ini say, on stdout behind a "FAILED:" prefix, and only an
    # uncaught traceback such as a dangling down_revision reaches stderr.
    # Reporting one of the two is how the failures described above lose their
    # reason.
    assert result.returncode == 0, result.stdout + result.stderr

    heads = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(heads) == 1, result.stdout


def test_upgrade_downgrade_upgrade_round_trips(tmp_path):
    """The downgrade is run rather than assumed.

    A downgrade nobody has executed is a downgrade that does not work, and the
    cost of finding that out rises with every revision added after it.
    """
    db = tmp_path / "round_trip.db"

    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    down = _alembic("downgrade", "base", db_path=db)
    assert down.returncode == 0, down.stderr

    again = _alembic("upgrade", "head", db_path=db)
    assert again.returncode == 0, again.stderr


def _schema(db_path: Path) -> dict:
    """Structure of a SQLite file, normalised for comparison.

    Compared as sets rather than as `.schema` text: SQLAlchemy and Alembic emit
    the same structure with different whitespace and constraint ordering, and a
    text diff would fail on both without a single column being wrong.
    `alembic_version` is excluded because only one side of the comparison has it.
    """
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        tables = [
            name
            for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        out = {}
        for table in tables:
            columns = {
                (name, kind.upper(), bool(notnull), default, bool(pk))
                for name, kind, notnull, default, pk in con.execute(
                    'SELECT name, type, "notnull", dflt_value, pk FROM pragma_table_info(?)', (table,)
                )
            }
            indexes = set()
            for index_name, unique in con.execute('SELECT name, "unique" FROM pragma_index_list(?)', (table,)):
                if index_name.startswith("sqlite_autoindex"):
                    continue  # the implicit index behind a UNIQUE constraint
                index_columns = tuple(
                    column
                    for (column,) in con.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index_name,))
                )
                indexes.add((index_name, bool(unique), index_columns))
            foreign_keys = {
                (referenced_table, from_col, to_col, on_update, on_delete)
                for referenced_table, from_col, to_col, on_update, on_delete in con.execute(
                    'SELECT "table", "from", "to", on_update, on_delete FROM pragma_foreign_key_list(?)', (table,)
                )
            }
            out[table] = (columns, indexes, foreign_keys)
        return out
    finally:
        con.close()


def _create_all(db_path: Path) -> subprocess.CompletedProcess:
    """Build a database from the models, the way the test suite does, for comparison."""
    env = os.environ | {
        "RETINA_ENV": "test",
        "RETINA_DB_PATH": str(db_path),
        "RETINA_SCHEMA_SOURCE": "create_all",
    }
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import asyncio; import core.nodes;  # noqa: F401  registers the tables\n"
            "from core.users import create_db_and_tables; "
            "asyncio.run(create_db_and_tables())",
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_migrations_produce_the_schema_create_all_produces(tmp_path):
    """Drift here passes the round trip and diverges on a fresh deploy."""
    migrated = tmp_path / "migrated.db"
    direct = tmp_path / "direct.db"

    up = _alembic("upgrade", "head", db_path=migrated)
    assert up.returncode == 0, up.stderr
    built = _create_all(direct)
    assert built.returncode == 0, built.stderr

    assert _schema(migrated) == _schema(direct)


def test_upgrading_a_pre_alembic_database_succeeds(tmp_path):
    """The state all three droplets were in before Alembic: the four baseline
    auth tables, no alembic_version, and no node tables.

    Built from 0001's own tables rather than by create_all, which builds what
    the models declare today and stops matching once a revision drops one of
    the four. Without the early return in 0001 this fails on `table user
    already exists`, and every deploy after the guard lands would refuse to boot.
    """
    import sqlite3

    db = tmp_path / "pre_existing.db"
    base = _alembic("upgrade", "0001", db_path=db)
    assert base.returncode == 0, base.stderr
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE alembic_version")
        con.commit()
    finally:
        con.close()

    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    stamped = _alembic("current", db_path=db)
    assert stamped.returncode == 0, stamped.stderr
    assert "head" in stamped.stdout, stamped.stdout + stamped.stderr


# ── 0015: node_owners folded into node_claims ────────────────────────────────


def _at_0014_with_owners(db: Path, owners: str) -> None:
    """A database at 0014 holding three registered nodes, two claim rows and
    the given node_owners rows, as SQL values."""
    import sqlite3

    base = _alembic("upgrade", "0014", db_path=db)
    assert base.returncode == 0, base.stdout + base.stderr
    con = sqlite3.connect(db)
    try:
        con.executescript(
            "INSERT INTO nodes (node_id, node_ref) VALUES "
            "('retclaimed', 'ndeclaimed00000'), ('retassigned', 'ndeassigned0000'), ('retoffered', 'ndeoffered00000');"
            "INSERT INTO node_claims (node_id, email, verified) VALUES "
            "('retclaimed', 'ada@example.com', 1), ('retoffered', 'bob@example.com', 0);"
            f"INSERT INTO node_owners (node_id, user_id) VALUES {owners};"
        )
        con.commit()
    finally:
        con.close()


def _query(db: Path, sql: str) -> list[tuple]:
    import sqlite3

    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_0015_folds_every_owner_into_the_claims(tmp_path):
    """An owner whose node already had a claim row gains the column on it; one
    whose node had none, an administrator's assignment, gets a row with no
    address. A claim nobody owns is left unowned."""
    db = tmp_path / "fold.db"
    _at_0014_with_owners(db, "('retclaimed', 'user-a'), ('retassigned', 'user-b')")

    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr

    assert _query(db, "SELECT node_id, user_id, email, verified FROM node_claims ORDER BY node_id") == [
        ("retassigned", "user-b", None, 0),
        ("retclaimed", "user-a", "ada@example.com", 1),
        ("retoffered", None, "bob@example.com", 0),
    ]
    assert _query(db, "SELECT name FROM sqlite_master WHERE name = 'node_owners'") == []


def test_0015_refuses_an_owner_for_a_node_that_never_registered(tmp_path):
    """node_claims holds only registered nodes, and the migration connection
    does not enforce the key, so such a row would otherwise be copied in
    silently. Refused before anything changes: an ownership row is an
    authorisation fact, and a failed upgrade leaves the previous image serving."""
    db = tmp_path / "orphan.db"
    _at_0014_with_owners(db, "('retclaimed', 'user-a'), ('legacy-tcp-node', 'user-b')")

    up = _alembic("upgrade", "head", db_path=db)

    assert up.returncode != 0
    assert "legacy-tcp-node" in up.stdout + up.stderr
    assert _query(db, "SELECT version_num FROM alembic_version") == [("0014",)]
    assert _query(db, "SELECT node_id, user_id FROM node_owners ORDER BY node_id") == [
        ("legacy-tcp-node", "user-b"),
        ("retclaimed", "user-a"),
    ]
    assert "user_id" not in {row[1] for row in _query(db, "PRAGMA table_info(node_claims)")}


def test_0015_downgrade_hands_the_owners_back(tmp_path):
    db = tmp_path / "back.db"
    _at_0014_with_owners(db, "('retclaimed', 'user-a'), ('retassigned', 'user-b')")
    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr

    down = _alembic("downgrade", "0014", db_path=db)
    assert down.returncode == 0, down.stdout + down.stderr

    assert _query(db, "SELECT node_id, user_id FROM node_owners ORDER BY node_id") == [
        ("retassigned", "user-b"),
        ("retclaimed", "user-a"),
    ]
    # The row that only carried an owner goes; every claim row before 0015 had
    # an address.
    assert _query(db, "SELECT node_id, email, verified FROM node_claims ORDER BY node_id") == [
        ("retclaimed", "ada@example.com", 1),
        ("retoffered", "bob@example.com", 0),
    ]
    assert "user_id" not in {row[1] for row in _query(db, "PRAGMA table_info(node_claims)")}


def test_rollback_ahead_sentinel_matches_alembics_wording(tmp_path):
    """deploy/start.sh greps a failed deploy/migrate.py's output (Alembic's own
    `upgrade head`) for the literal substring ROLLBACK_AHEAD_SENTINEL to tell a
    tolerable rollback apart from a genuine migration failure (see the comment
    above that `elif`). Alembic controls the wording, not us, so this
    reproduces the actual scenario a rolled-back image sees: a database stamped
    ahead of the revisions its (older) migrations/versions/ directory knows
    about.

    A future Alembic release rewording the message fails this test loudly in
    CI, instead of start.sh's grep silently no longer matching in production.
    """
    db = tmp_path / "ahead.db"
    up = _alembic("upgrade", "head", db_path=db)
    assert up.returncode == 0, up.stderr

    # An "older image": the same backend tree minus the newest revision. Only
    # core/ (env.py imports core.users and core.nodes to build target_metadata)
    # and migrations/ are needed; core/__init__.py does not import its sibling
    # modules eagerly, so copying the two files env.py touches is enough.
    old_image = tmp_path / "old_image"
    shutil.copytree(BACKEND / "core", old_image / "core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(BACKEND / "migrations", old_image / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    # The newest revision, found rather than named: revisions are numbered
    # 000N_*.py so the last by filename is the head. Naming one here meant that
    # adding a revision on top of it left the older one's removal orphaning every
    # revision after it, and Alembic then raised a bare KeyError on the missing
    # down_revision instead of the message this test exists to pin.
    versions = sorted((old_image / "migrations" / "versions").glob("[0-9]*.py"))
    versions[-1].unlink()
    shutil.copyfile(BACKEND / "alembic.ini", old_image / "alembic.ini")

    result = _migrate(db, cwd=old_image)

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert ROLLBACK_AHEAD_SENTINEL in combined, combined


def test_migrate_reports_the_revision_it_reached(tmp_path):
    """start.sh's migration step: the upgrade, then the revision it left behind."""
    result = _migrate(tmp_path / "fresh.db")

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"Database is now at:\n\w+ \(head\)$", result.stdout.strip()), result.stdout


def test_migrate_boots_when_the_revision_cannot_be_read(tmp_path):
    """The revision report is diagnostic: failing to read it is logged, not fatal."""
    unreadable = (
        "import runpy\n"
        "from alembic import command, util\n"
        "def unreadable(*args, **kwargs):\n"
        "    raise util.CommandError('database is locked')\n"
        "command.current = unreadable\n"
        f"runpy.run_path({str(MIGRATE_PY)!r}, run_name='__main__')\n"
    )
    result = _migrate(tmp_path / "fresh.db", argv=["-c", unreadable])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Could not determine current revision (non-fatal)" in result.stdout
    assert "database is locked" in result.stdout


VALID_ROLLBACK_SAFETY = {"additive", "destructive"}


def _graded_revisions() -> list[Path]:
    """Exactly the files deploy/classify-migration-gap.py grades.

    Its `_versions_at` takes every `*.py` under versions/ bar `__init__.py`, so
    the two tests below cover that set rather than the one our naming
    convention describes.
    """
    return sorted(p for p in (BACKEND / "migrations" / "versions").glob("*.py") if p.name != "__init__.py")


def test_every_migration_is_numbered_for_chain_order():
    """`_versions_at` in deploy/classify-migration-gap.py returns its listing
    `sorted()`, and the classifier then treats the last element as the head of
    the chain: `_revision_of(here[-1])` is the revision it reports the database
    as being left at, and `_revision_of(restored[-1])` is the downgrade target
    it tells an operator to run. Filename sort is chain order only while every
    revision is named `NNNN_`.

    alembic.ini sets no file_template, so `alembic revision -m "..."` writes a
    12-hex prefix, and ASCII sorts digits before letters. Such a file lands at
    the end of the listing whatever its place in the chain, and the report then
    names the wrong revision and computes the wrong downgrade target, with
    nothing about it looking wrong. file_template cannot fix this either, since
    the number is chosen per revision with `--rev-id`, so the enforcement is
    here. Failing at authoring time costs a `git mv`; the alternative is finding
    out during a production rollback.

    The declaration gate below is the other half of this: it makes such a file
    declare its safety, but says nothing about where it sorts.
    """
    versions = _graded_revisions()
    assert versions, "no revisions found under migrations/versions/"

    for path in versions:
        assert re.match(r"^\d{4}_", path.name), (
            f"{path.name} is not named NNNN_<slug>.py, so deploy/classify-migration-gap.py "
            "cannot derive chain order from the filename sort"
        )


def test_every_migration_declares_rollback_safety():
    """deploy/classify-migration-gap.py reads this constant out of each revision
    to decide whether a rollback across it is safe to serve, and grades a
    missing one destructive. That default is deliberately loud, but it is only
    loud at rollback time, on production, when someone is already having a bad
    day. Failing here costs a line in the revision instead.

    Both the set and the pattern are the classifier's, deliberately: a gate that
    covers less than the consumer grades is a gate with a hole in it. A revision
    named the default way slips past a `[0-9]*.py` glob, as would one written
    `rollback_safety = 'additive'` past a stricter regex, and the first anyone
    hears of either is the classifier grading it undeclared, hence destructive,
    on the next production rollback.
    """
    versions = _graded_revisions()
    assert versions, "no revisions found under migrations/versions/"

    for path in versions:
        match = re.search(r'^rollback_safety\s*=\s*["\'](\w+)["\']', path.read_text(), re.MULTILINE)
        assert match, f"{path.name} does not declare rollback_safety"
        assert match.group(1) in VALID_ROLLBACK_SAFETY, (
            f"{path.name} declares rollback_safety = {match.group(1)!r}, "
            f"expected one of {sorted(VALID_ROLLBACK_SAFETY)}"
        )


# ── 0016: polled radars ──────────────────────────────────────────────────────


def test_0016_downgrade_drops_both_polled_radar_tables(tmp_path):
    db = tmp_path / "polled.db"
    up = _alembic("upgrade", "0016", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr
    tables = "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'polled_radar%' ORDER BY name"
    assert _query(db, tables) == [("polled_radar_endpoint_history",), ("polled_radars",)]

    down = _alembic("downgrade", "0015", db_path=db)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _query(db, tables) == []


# ── 0017: the record of a polled radar's network moves ───────────────────────


def test_0017_downgrade_drops_the_network_move_columns(tmp_path):
    db = tmp_path / "moves.db"
    up = _alembic("upgrade", "0017", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr
    columns = "SELECT name FROM pragma_table_info('polled_radars') WHERE name LIKE '%network_move%' ORDER BY name"
    assert _query(db, columns) == [("last_network_move_at",), ("network_moves",)]

    down = _alembic("downgrade", "0016", db_path=db)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _query(db, columns) == []
    # SQLite rewrites the table to drop a column, so the rest of it has to
    # come back: the endpoint's unique index above all, which is what stops
    # one radar being registered twice.
    assert ("epoch",) in _query(db, "SELECT name FROM pragma_table_info('polled_radars')")
    indexes = "SELECT name, \"unique\" FROM pragma_index_list('polled_radars') ORDER BY name"
    assert ("ix_polled_radars_endpoint_key", 1) in _query(db, indexes)
    assert _query(db, "SELECT \"table\" FROM pragma_foreign_key_list('polled_radars')") == [("nodes",)]


# ── 0019: node_events ────────────────────────────────────────────────────────


def test_0019_downgrade_drops_node_events(tmp_path):
    db = tmp_path / "events.db"
    up = _alembic("upgrade", "0019", db_path=db)
    assert up.returncode == 0, up.stdout + up.stderr
    tables = "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'node_events'"
    assert _query(db, tables) == [("node_events",)]
    # Keyed to nodes, not to polled_radars: the record outlives a registration.
    assert _query(db, "SELECT \"table\" FROM pragma_foreign_key_list('node_events')") == [("nodes",)]

    down = _alembic("downgrade", "0018", db_path=db)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _query(db, tables) == []
