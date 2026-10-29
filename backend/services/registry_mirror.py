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

Copied: nodes, node_configs, node_claims, node_reports, node_events. Not copied:
node_tokens (credentials), node_claim_challenges (live claim secrets),
polled_radars (a receiver would start polling third-party radars, and their
secrets are encrypted under production's key) and node_contacts (personal
details beyond an owner's address).

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
from sqlalchemy import DateTime, and_, delete, select

from config.constants import (
    DETECTION_MIRROR_LOG_INTERVAL_S,
    REGISTRY_MIRROR_CHUNK_BYTES,
    REGISTRY_MIRROR_FULL_INTERVAL_S,
    REGISTRY_MIRROR_INTERVAL_S,
    REGISTRY_MIRROR_TIMEOUT_S,
)
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, NodeReport
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


# In foreign-key order: upserts run down this list and deletes up it.
TABLES = (
    Table("nodes", Node, ("node_id",)),
    Table("node_configs", NodeConfig, ("node_id", "version")),
    Table("node_claims", NodeClaim, ("node_id",)),
    Table("node_reports", NodeReport, ("node_id",)),
    Table("node_events", NodeEvent, ("node_id", "kind", "occurred_at", "actor"), listed=False),
)

# Surrogate keys, which mean nothing in another database.
_SKIPPED = {"id"}
# A claim's owner is an account id here and an address on the wire.
_OWNER = "owner_email"


def _columns(table: Table) -> list:
    return [c for c in table.model.__table__.columns if c.name not in _SKIPPED]


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
            if table.model is NodeClaim:
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


async def _delete(session, table: Table, key: str) -> None:
    if table.model is Node:
        node_id = json.loads(key)[0]
        for dependent in reversed(TABLES[1:]):
            await session.execute(delete(dependent.model).where(dependent.model.node_id == node_id))
    await session.execute(delete(table.model).where(_where_key(table, key)))


async def apply(session, chunk: dict) -> dict:
    """Write one chunk of a push. Returns what changed, per table."""
    upserts: dict[str, list[dict]] = chunk.get("upserts") or {}
    claims = upserts.get("node_claims", [])
    owners = await _owners({row[_OWNER] for row in claims if row.get(_OWNER)})

    written: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for table in TABLES:
        columns = {c.name: c for c in _columns(table)}
        for row in upserts.get(table.name, []):
            if table.model is NodeClaim:
                row = dict(row)
                row["user_id"] = owners.get(row.pop(_OWNER, None) or "")
            values = {name: _decode(columns[name], row[name]) for name in columns if name in row}
            if table.model is Node and await _ref_taken(session, values):
                skipped[table.name] = skipped.get(table.name, 0) + 1
                continue
            existing = await session.scalar(select(table.model).where(_where_key(table, key_of(table, row))))
            if existing is None:
                session.add(table.model(**values))
            else:
                for name, value in values.items():
                    setattr(existing, columns[name].key, value)
            written[table.name] = written.get(table.name, 0) + 1
        await session.flush()

    removed: dict[str, int] = {}
    for table in reversed(TABLES):
        for key in (chunk.get("deletes") or {}).get(table.name, []):
            await _delete(session, table, key)
            removed[table.name] = removed.get(table.name, 0) + 1

    full_keys = chunk.get("full_keys")
    if full_keys is not None:
        held = _read_held()
        for table in reversed(TABLES):
            if table.name not in full_keys:
                continue
            listed = set(full_keys[table.name])
            for key in held.get(table.name, []):
                if key not in listed:
                    await _delete(session, table, key)
                    removed[table.name] = removed.get(table.name, 0) + 1
    await session.commit()
    if full_keys is not None:
        _write_held({name: list(keys) for name, keys in full_keys.items()})
    return {"written": written, "removed": removed, "skipped": skipped}


async def _ref_taken(session, values: dict) -> bool:
    """Whether a different node here already publishes under this row's ref.

    node_ref is unique, so the row cannot land; it is skipped and named rather
    than failing the chunk it came in.
    """
    ref = values.get("node_ref")
    if not ref:
        return False
    holder = await session.scalar(select(Node.node_id).where(Node.node_ref == ref))
    if holder is None or holder == values.get("node_id"):
        return False
    logger.warning("registry mirror skips node %s: ref %s is held here by %s", values.get("node_id"), ref, holder)
    return True
