"""Push production's node registry to the environments the detection mirror feeds.

The detection mirror (services/detection_mirror.py) brings real nodes' frames to
the test droplet and staging, but a node's registration, its configs, its owner
and its history stayed on production, so every page that reads the registry
(an owner's nodes, the admin Fleet and node pages) was empty there. This sends
that registry along, one way, to the same receivers with the same keys: a
receiver holds its key as its own RADAR_API_KEY, which is what
routes/registry_sync.py checks.

Sender. Inert unless the detection mirror has targets and REGISTRY_MIRROR is not
"off". Once a minute it reads the mirrored tables, compares each row with what
the target last accepted, and sends only the rows that changed and the keys
that disappeared. The first push after a start, and one every
REGISTRY_MIRROR_FULL_INTERVAL_S, is full: every row, plus the complete key list
of each table but node_events, so the receiver can drop rows production no
longer has. A target's state only advances when every chunk of a push landed;
a failed push is resent whole next minute, which upserts make harmless.

Receiver. `apply` upserts by each table's natural key, never by a surrogate id,
since ids differ between databases. A claim's owner travels as an address and
is bound to the receiver's account at that address, made if missing, so the
owner signing in there finds their nodes. Edits made on the receiver stand
until production changes that row or the next full push: production wins, and
nothing flows back.

Sign-in sessions go the same way, owner as address, so a person signed in on
production is signed in on a receiver by the same cookie: user_sessions holds
the SHA-256 of each token, never the token, and core/sessions.py resolves a
cookie by that hash, so a mirrored row signs the cookie in here as it does
there. The browser only presents the cookie to a receiver's host when
production set it on the parent domain (SESSION_COOKIE_DOMAIN). A session
ended on production leaves the next push as a delete; one ended on a receiver
comes back with production's next diff, as any local edit does. Sessions a
receiver opened itself are not production's and stay.

Copied: nodes, node_configs, node_claims, node_reports, node_events,
user_sessions. Not copied: node_tokens (credentials), node_claim_challenges
(live claim secrets), polled_radars (a receiver would start polling
third-party radars, and their secrets are encrypted under production's key),
node_contacts (personal details beyond an owner's address) and
retired_legacy_tokens (the JWT cookies from before sessions are signed with
production's key, which no receiver holds).

Columns are read from each side's own table metadata: the sender sends the
columns it has, the receiver writes the ones it has, so two environments a
migration apart still sync what they share.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import (
    Connection,
    DateTime,
    and_,
    bindparam,
    create_engine,
    delete,
    event,
    exists,
    insert,
    select,
    update,
)
from sqlalchemy.pool import NullPool

from config.constants import (
    DETECTION_MIRROR_LOG_INTERVAL_S,
    REGISTRY_MIRROR_CHUNK_BYTES,
    REGISTRY_MIRROR_FULL_INTERVAL_S,
    REGISTRY_MIRROR_INTERVAL_S,
    REGISTRY_MIRROR_TIMEOUT_S,
)
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, NodeReport
from core.users import UserSession
from services import detection_mirror

logger = logging.getLogger(__name__)

PATH = "/api/internal/registry/sync"


@dataclass(frozen=True)
class Table:
    name: str
    model: Any
    key: tuple[str, ...]
    # In the complete key list a full push sends. node_events is append-only on
    # production and goes with its node, so it is left out of the list rather
    # than shipping every event's key every six hours.
    listed: bool = True
    # Whether `user_id` names an account, which is an address on the wire and
    # bound to the receiver's account at that address.
    owned: bool = False
    # Columns kept off the wire: surrogate keys, which mean nothing in another
    # database. A session's id is a uuid the account's session list names it
    # by, so it travels, and a revocation on either side names the same one.
    skipped: frozenset[str] = frozenset({"id"})


# In foreign-key order: upserts run down this list and deletes up it.
TABLES = (
    Table("nodes", Node, ("node_id",)),
    Table("node_configs", NodeConfig, ("node_id", "version")),
    Table("node_claims", NodeClaim, ("node_id",), owned=True),
    Table("node_reports", NodeReport, ("node_id",)),
    Table("node_events", NodeEvent, ("node_id", "kind", "occurred_at", "actor"), listed=False),
    Table("user_sessions", UserSession, ("token_hash",), owned=True, skipped=frozenset()),
)

# An owner is an account id here and an address on the wire.
_OWNER = "owner_email"


def _columns(table: Table) -> list:
    return [c for c in table.model.__table__.columns if c.name not in table.skipped]


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _decode(column, value: Any) -> Any:
    if value is None:
        return None
    # A TypeDecorator such as UTCDateTime reports no python_type of its own.
    kind = getattr(column.type, "impl", column.type)
    if isinstance(kind, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def key_of(table: Table, row: dict) -> str:
    """A row's natural key as one string, the same on both ends."""
    return json.dumps([row.get(k) for k in table.key], separators=(",", ":"))


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


# ── Sender ───────────────────────────────────────────────────────────────────


async def read_registry(session) -> dict[str, dict[str, dict]]:
    """Every mirrored row, per table and natural key, as it goes on the wire."""
    from core.users import User

    emails = {str(uid): email for uid, email in (await session.execute(select(User.id, User.email))).all()}
    registry: dict[str, dict[str, dict]] = {}
    for table in TABLES:
        columns = _columns(table)
        rows = {}
        for obj in await session.scalars(select(table.model)):
            row = {c.name: _encode(getattr(obj, c.key)) for c in columns}
            if table.owned:
                owner = row.pop("user_id", None)
                row[_OWNER] = emails.get(str(owner)) if owner else None
            rows[key_of(table, row)] = row
        registry[table.name] = rows
    return registry


@dataclass
class Target:
    """One receiver, and the digest of every row it has accepted, per table."""

    label: str
    url: str
    key: str
    accepted: dict[str, dict[str, str]] = field(default_factory=dict)
    full_at: float | None = None
    pushes: int = 0
    rows_sent: int = 0
    failed: int = 0
    healthy: bool = True
    logged_at: float = 0.0

    def stats(self) -> dict:
        return {
            "pushes": self.pushes,
            "rows_sent": self.rows_sent,
            "failed": self.failed,
            "healthy": self.healthy,
            "rows_held": sum(len(rows) for rows in self.accepted.values()),
        }


_targets: list[Target] = []


def configure_from_env(env=None) -> bool:
    """Arm the sender on the detection mirror's targets. Reports whether it is armed."""
    global _targets
    env = os.environ if env is None else env
    if (env.get("REGISTRY_MIRROR") or "").strip().lower() == "off":
        _targets = []
        return False
    _targets = [Target(label=t.label, url=t.url, key=t.key) for t in detection_mirror._parse_targets(env)]
    if _targets:
        logger.info("registry mirror armed, pushing the node registry to %s", ", ".join(t.label for t in _targets))
    return bool(_targets)


def stats() -> dict:
    return {t.label: t.stats() for t in _targets}


def plan_push(target: Target, registry: dict[str, dict[str, dict]], full: bool) -> dict:
    """What `target` has not accepted yet: changed rows, vanished keys and, when full, every key."""
    upserts: dict[str, list[dict]] = {}
    deletes: dict[str, list[str]] = {}
    for table in TABLES:
        rows = registry[table.name]
        held = {} if full else target.accepted.get(table.name, {})
        changed = [row for key, row in rows.items() if held.get(key) != _digest(row)]
        if changed:
            upserts[table.name] = changed
        gone = [key for key in target.accepted.get(table.name, {}) if key not in rows]
        if gone and not full:
            deletes[table.name] = gone
    push: dict[str, Any] = {"upserts": upserts, "deletes": deletes}
    if full:
        push["full_keys"] = {t.name: list(registry[t.name]) for t in TABLES if t.listed}
    return push


def chunk_push(push: dict, limit: int = REGISTRY_MIRROR_CHUNK_BYTES) -> list[dict]:
    """`push` split into bodies under `limit` bytes, tables in foreign-key order.

    Deletes and the full key list ride on the last chunk, after every upsert
    has landed, so a node is never deleted before the rows that replace it or
    listed before it exists.
    """
    chunks: list[dict] = []
    current: dict[str, list[dict]] = {}
    size = 0
    for table in TABLES:
        for row in push["upserts"].get(table.name, []):
            row_size = len(json.dumps(row, default=str))
            if current and size + row_size > limit:
                chunks.append({"upserts": current})
                current, size = {}, 0
            current.setdefault(table.name, []).append(row)
            size += row_size
    last: dict[str, Any] = {"upserts": current, "deletes": push["deletes"]}
    if "full_keys" in push:
        last["full_keys"] = push["full_keys"]
    chunks.append(last)
    for index, chunk in enumerate(chunks):
        chunk["seq"] = index
        chunk["last"] = index == len(chunks) - 1
    return chunks


async def push_to_target(client, target: Target, registry: dict, now: float) -> bool:
    """Send `target` what it lacks. True when there was nothing to send or all of it landed."""
    full = target.full_at is None or now - target.full_at >= REGISTRY_MIRROR_FULL_INTERVAL_S
    push = plan_push(target, registry, full)
    if not full and not push["upserts"] and not push["deletes"]:
        return True
    rows = sum(len(r) for r in push["upserts"].values())
    try:
        for chunk in chunk_push(push):
            response = await client.post(f"{target.url}{PATH}", json=chunk, headers={"X-API-Key": target.key})
            response.raise_for_status()
    except Exception as exc:
        target.failed += 1
        _note(target, False, exc)
        return False
    target.accepted = {name: {key: _digest(row) for key, row in rows_.items()} for name, rows_ in registry.items()}
    if full:
        target.full_at = now
    target.pushes += 1
    target.rows_sent += rows
    _note(target, True, None)
    return True


async def mirror_task() -> None:
    """Push once a minute. Returns at once when unarmed."""
    if not _targets:
        return
    from core.users import async_session_maker

    async with httpx.AsyncClient(timeout=REGISTRY_MIRROR_TIMEOUT_S) as client:
        while True:
            try:
                async with async_session_maker() as session:
                    registry = await read_registry(session)
                now = time.monotonic()
                await asyncio.gather(
                    *(push_to_target(client, t, registry, now) for t in _targets), return_exceptions=True
                )
            except Exception:
                logger.exception("registry mirror could not read the registry")
            await asyncio.sleep(REGISTRY_MIRROR_INTERVAL_S)


def _note(target: Target, ok: bool, exc: Exception | None) -> None:
    """A target's transitions at once, a continuing fault once a minute."""
    if ok:
        if not target.healthy:
            target.healthy = True
            logger.warning("registry mirror to %s recovered (%s)", target.label, target.stats())
        return
    now = time.monotonic()
    if target.healthy:
        target.healthy = False
        target.logged_at = now
        logger.warning("registry mirror to %s failing: %s (%s)", target.label, exc, target.stats())
        return
    if now - target.logged_at >= DETECTION_MIRROR_LOG_INTERVAL_S:
        target.logged_at = now
        logger.warning("registry mirror to %s still failing: %s (%s)", target.label, exc, target.stats())


# ── Receiver ─────────────────────────────────────────────────────────────────


def _held_path() -> Path:
    from core.database import DATABASE_URL

    # Beside the database it describes, so a test database gets its own.
    return Path(DATABASE_URL.split(":///", 1)[1]).parent / "registry_mirror.json"


def _read_held() -> dict[str, list[str]]:
    """The keys production last listed, per table: the rows a full push may remove."""
    try:
        return json.loads(_held_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_held(held: dict[str, list[str]]) -> None:
    path = _held_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(held))
    tmp.replace(path)


async def _owners(emails: set[str]) -> dict[str, str | None]:
    """The receiver's account id for each owner address, made where missing."""
    from core.users import get_or_create_magic_link_user

    owners: dict[str, str | None] = {}
    for email in emails:
        try:
            owners[email] = str((await get_or_create_magic_link_user(email)).id)
        except Exception as exc:
            # A deactivated account here: the node arrives unowned rather than
            # bound to an account this environment has closed.
            logger.warning("registry mirror leaves %s's nodes unowned here: %r", email, exc)
            owners[email] = None
    return owners


def _where_key(table: Table, key: str):
    values = json.loads(key)
    columns = [table.model.__table__.c[name] for name in table.key]
    decoded = [_decode(column, value) for column, value in zip(columns, values, strict=True)]
    return and_(*(column == value for column, value in zip(columns, decoded, strict=True)))


def _receiver_engine():
    """A synchronous engine over this environment's database, for `apply`'s thread.

    The write runs on a thread, not on the event loop: an async session holds
    SQLite's write lock from its first flush to its commit, and every await in
    between waits on the loop, which on a busy server stalls for seconds at a
    time. A push held the lock past the 5 s every other writer waits for, and
    the mail outbox failed with "database is locked" within seconds of each
    push. On a thread the transaction runs start to finish without yielding.

    BEGIN IMMEDIATE takes the write lock up front. A deferred transaction that
    reads and then writes cannot wait for a writer that committed after its
    read began: SQLite fails it at once, busy_timeout or not. Taken first, the
    lock is waited for like any other. NullPool for the reason
    services/publication.py gives: no connection outlives its call or its thread.
    """
    global _engine
    if _engine is None:
        from core.database import DATABASE_URL
        from core.users import _set_sqlite_pragmas

        engine = create_engine(f"sqlite:///{DATABASE_URL.split('///', 1)[1]}", poolclass=NullPool)

        @event.listens_for(engine, "connect")
        def _connect(dbapi_conn, conn_rec):
            # The driver's own implicit BEGIN would come before ours.
            dbapi_conn.isolation_level = None
            _set_sqlite_pragmas(dbapi_conn, conn_rec)

        @event.listens_for(engine, "begin")
        def _begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")

        _engine = engine
    return _engine


_engine = None


def _reset_for_tests() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def _delete(conn: Connection, table: Table, key: str) -> None:
    if table.model is Node:
        node_id = json.loads(key)[0]
        for dependent in reversed(TABLES[1:]):
            if hasattr(dependent.model, "node_id"):
                conn.execute(delete(dependent.model).where(dependent.model.node_id == node_id))
    conn.execute(delete(table.model).where(_where_key(table, key)))


def _key_matches(table: Table):
    """The row with this bound key, NULLs equal, as `_where_key` matches it."""
    t = table.model.__table__
    return and_(*(t.c[k].is_not_distinct_from(bindparam(f"k_{k}", type_=t.c[k].type)) for k in table.key))


def _upsert(conn: Connection, table: Table, names: tuple[str, ...], rows: list[dict]) -> None:
    """Update the rows that exist and insert the rest: two statements, however many rows.

    Each runs as one executemany, so the write lock is held for a handful of
    driver calls rather than an ORM round per row. A column left out of the
    insert takes its server default; every mirrored column that has a Python
    default has the same one on the server.
    """
    t = table.model.__table__
    params = [{**{f"k_{k}": row[k] for k in table.key}, **{f"v_{n}": row[n] for n in names}} for row in rows]
    values = {n: bindparam(f"v_{n}", type_=t.c[n].type) for n in names if n not in table.key}
    if values:
        conn.execute(update(t).where(_key_matches(table)).values(values), params)
    source = select(*(bindparam(f"v_{n}", type_=t.c[n].type) for n in names)).where(
        ~exists().where(_key_matches(table))
    )
    conn.execute(insert(t).from_select([t.c[n] for n in names], source), params)


async def apply(chunk: dict) -> dict:
    """Write one chunk of a push. Returns what changed, per table.

    Owners are resolved first, on the loop, in their own short transactions:
    making an account is core.users' business and is async. The registry write
    itself goes to a thread (see `_receiver_engine`).
    """
    upserts = chunk.get("upserts") or {}
    owned = (row for table in TABLES if table.owned for row in upserts.get(table.name, []))
    owners = await _owners({row[_OWNER] for row in owned if row.get(_OWNER)})
    return await asyncio.to_thread(_apply, chunk, owners)


def _apply(chunk: dict, owners: dict[str, str | None]) -> dict:
    upserts: dict[str, list[dict]] = chunk.get("upserts") or {}
    full_keys = chunk.get("full_keys")
    written: dict[str, int] = {}
    skipped: dict[str, int] = {}
    removed: dict[str, int] = {}

    # Decoded before the lock is taken, grouped by the columns each row carries
    # (one group, unless the sender is a migration apart mid-push).
    batches: list[tuple[Table, dict[tuple[str, ...], list[dict]]]] = []
    for table in TABLES:
        columns = {c.name: c for c in _columns(table)}
        groups: dict[tuple[str, ...], list[dict]] = {}
        for row in upserts.get(table.name, []):
            if table.owned:
                row = dict(row)
                row["user_id"] = owners.get(row.pop(_OWNER, None) or "")
                if row["user_id"] is None and not columns["user_id"].nullable:
                    # A session of an account this environment has closed, or
                    # of nobody: a claim arrives unowned, a session cannot.
                    skipped[table.name] = skipped.get(table.name, 0) + 1
                    continue
            values = {name: _decode(columns[name], row[name]) for name in columns if name in row}
            groups.setdefault(tuple(sorted(values)), []).append(values)
        if groups:
            batches.append((table, groups))

    with _receiver_engine().begin() as conn:
        for table, groups in batches:
            if table.model is Node:
                groups = _without_taken_refs(conn, groups, skipped)
            for names, rows in groups.items():
                if rows:
                    _upsert(conn, table, names, rows)
                    written[table.name] = written.get(table.name, 0) + len(rows)

        for table in reversed(TABLES):
            for key in (chunk.get("deletes") or {}).get(table.name, []):
                _delete(conn, table, key)
                removed[table.name] = removed.get(table.name, 0) + 1

        if full_keys is not None:
            held = _read_held()
            for table in reversed(TABLES):
                if table.name not in full_keys:
                    continue
                listed = set(full_keys[table.name])
                for key in held.get(table.name, []):
                    if key not in listed:
                        _delete(conn, table, key)
                        removed[table.name] = removed.get(table.name, 0) + 1
    if full_keys is not None:
        _write_held({name: list(keys) for name, keys in full_keys.items()})
    return {"written": written, "removed": removed, "skipped": skipped}


def _without_taken_refs(
    conn: Connection, groups: dict[tuple[str, ...], list[dict]], skipped: dict[str, int]
) -> dict[tuple[str, ...], list[dict]]:
    """Nodes whose ref a different node here already publishes under, dropped and named.

    node_ref is unique, so such a row cannot land; skipping it keeps the rest
    of the chunk from failing with it. One query for the whole chunk.
    """
    refs = {row["node_ref"] for rows in groups.values() for row in rows if row.get("node_ref")}
    if not refs:
        return groups
    holders = dict(conn.execute(select(Node.node_ref, Node.node_id).where(Node.node_ref.in_(refs))).all())
    kept: dict[tuple[str, ...], list[dict]] = {}
    for names, rows in groups.items():
        for row in rows:
            holder = holders.get(row.get("node_ref"))
            if holder is not None and holder != row.get("node_id"):
                logger.warning(
                    "registry mirror skips node %s: ref %s is held here by %s",
                    row.get("node_id"),
                    row["node_ref"],
                    holder,
                )
                skipped["nodes"] = skipped.get("nodes", 0) + 1
                continue
            kept.setdefault(names, []).append(row)
    return kept
