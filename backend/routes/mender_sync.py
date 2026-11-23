"""POST /api/internal/mender/snapshot: mender-auto-accept overwrites the Mender mirror.

The contract is docs/mender-snapshot.md. Each push carries the whole fleet and
replaces the mirror in one transaction; the tables are in core/mender_mirror.py.

A fault in the envelope refuses the push with 422. A fault in one device never
does: the device is dropped, or stored with a `snapshot_issue` that makes
registration refuse that node alone, and the rest of the fleet is applied. Any
image carries the Mender tenant token, so one board can enrol under a chosen
identity, and that must not keep the mirror stale for every node.

The handler takes the raw Request rather than a body model, because FastAPI
reads a declared body before any dependency runs, and nothing here is read
before the token is checked. For the same reason the route is left out of the
OpenAPI documents: the generated schema would describe no body.
"""

import hmac
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.mender_mirror import SYNC_STATE_ID, MenderDevice, MenderSyncState, load_sync_state
from core.users import get_async_session
from services.mender import _fingerprint

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)

SNAPSHOT_PATH = "/api/internal/mender/snapshot"
MAX_BODY_BYTES = 2 * 1024 * 1024
MIN_TOKEN_CHARS = 32
MAX_CLOCK_AHEAD = timedelta(seconds=300)
ACCEPTED = "accepted"

# Field caps from the contract table. SQLite does not enforce String lengths,
# so these are the only thing that does.
MAX_ID_CHARS = 64
MAX_PUBKEY_CHARS = 4096
MAX_DEVICE_TYPE_CHARS = 128
MAX_ARTIFACT_NAME_CHARS = 256
MAX_ISSUE_CHARS = 128
#: What an auth_status that is not a status token is stored as.
INVALID_STATUS = "invalid"
#: Devices with a fault logged one line each per push; the rest get one line.
MAX_LOGGED_DEVICES = 20
#: A refusal, or the short-token error, is logged at most once per window.
LOG_WINDOW_S = 60.0


def _now() -> datetime:
    return datetime.now(UTC)


def _monotonic() -> float:
    return time.monotonic()


# ── Dates ────────────────────────────────────────────────────────────────────

# The parser also takes a number of seconds written as a string, which is not
# ISO 8601, so the start of a date and time is required first.
_ISO_DATE_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt]")
_AWARE = TypeAdapter(AwareDatetime)


def _aware_utc(value: str) -> datetime:
    """An ISO 8601 date and time with a timezone, as UTC. ValueError otherwise.

    One parser for generated_at and inventory_updated_at, so the two cannot
    disagree on what counts.
    """
    if not _ISO_DATE_TIME.match(value):
        raise ValueError("not an ISO 8601 date and time")
    try:
        return _AWARE.validate_strings(value, strict=True).astimezone(UTC)
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from None
    except OverflowError:
        # Year 1 at a positive offset, or 9999 at a negative one: no UTC
        # timestamp holds it, and the column stores UTC.
        raise ValueError("outside the range of a UTC timestamp") from None


# ── Body ─────────────────────────────────────────────────────────────────────

# strict: no number is taken for a timestamp, and no coercion into a string.
_FORBID = ConfigDict(extra="forbid", strict=True)


class SnapshotDevice(BaseModel):
    """One device as sent. Only its keys and JSON types are checked here, as
    the envelope; the values are judged per device by _sort_out."""

    model_config = _FORBID

    mender_device_id: str
    node_id: str | None
    auth_status: str
    auth_set_id: str | None
    auth_set_pubkey: str | None
    device_type: str | None
    artifact_name: str | None
    inventory_updated_at: str | None


class Snapshot(BaseModel):
    model_config = _FORBID

    generated_at: datetime
    # Printable ASCII only: it is written into log lines and alert messages.
    source: Annotated[str, StringConstraints(pattern=r"^[\x20-\x7e]{1,128}$")]
    devices: list[SnapshotDevice]

    @field_validator("generated_at", mode="plain")
    @classmethod
    def _aware_and_not_ahead_of_the_server(cls, value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError("generated_at must be a string")
        when = _aware_utc(value)
        # A far-future stamp would win every later 409 check and freeze the mirror.
        if when - _now() > MAX_CLOCK_AHEAD:
            raise ValueError("generated_at is more than 300 s ahead of the server clock")
        return when


def _echoable(value: Any) -> bool:
    """Whether an error's input is a single value that can be sent back.

    A dict or list input is the enclosing object, a model validator's or a
    missing field's, and bytes are the whole body, a json_invalid error's: both
    would echo far more than what was wrong. NaN and infinities are valid input
    to the JSON parser but cannot be written back out.
    """
    if isinstance(value, float):
        return math.isfinite(value)
    return value is None or isinstance(value, str | int | bool)


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    """FastAPI's 422 shape, with the input kept only where it is the offending value."""
    detail = []
    for error in exc.errors(include_url=False, include_context=False):
        entry: dict[str, Any] = {"type": error["type"], "loc": ["body", *error["loc"]], "msg": error["msg"]}
        if _echoable(error.get("input")):
            entry["input"] = error.get("input")
        detail.append(entry)
    return detail


# ── Devices ──────────────────────────────────────────────────────────────────

# fullmatch: with re.match, "$" would let a trailing newline through.
_STATUS_TOKEN = re.compile(r"[a-z][a-z_]{0,31}")


def _shown(value: str) -> str:
    """A sent string as it goes into a log line: cut short, and repr'd so it cannot break the line."""
    return repr(value[:MAX_ID_CHARS])


@dataclass
class _Entry:
    """One device as it is stored, and what was wrong with it as sent."""

    index: int
    mender_device_id: str
    node_id: str | None
    auth_status: str
    auth_set_id: str | None
    auth_set_pubkey: str | None
    auth_set_fingerprint: str | None
    device_type: str | None
    artifact_name: str | None
    inventory_updated_at: datetime | None
    # The device's own fields that make it unusable for registration.
    field_issues: list[str]
    # The value behind an issue, logged with the issue and only when it is.
    issue_notes: list[str]
    # Changed on the way in and logged, but no issue: the node can still register.
    notes: list[str]
    repeated: bool = False
    shared_by: int = 0

    @property
    def issue(self) -> str | None:
        """The snapshot_issue stored, or None when the entry is clean.

        Issues between devices come first, so the cut never hides a collision.
        """
        issues = []
        if self.repeated:
            issues.append("mender_device_id repeated in the snapshot")
        if self.shared_by:
            issues.append(f"node_id shared by {self.shared_by} devices")
        return "; ".join([*issues, *self.field_issues])[:MAX_ISSUE_CHARS] or None


def _blank_to_none(value: str | None) -> str | None:
    """An empty or whitespace-only auth set field as null. It names no auth set,
    and kept as sent, "" would be fingerprinted as zero bytes."""
    return value if value and not value.isspace() else None


def _cut(value: str | None, cap: int, name: str, notes: list[str]) -> str | None:
    if value is not None and len(value) > cap:
        notes.append(f"{name} cut from {len(value)} to {cap} characters")
        return value[:cap]
    return value


def _entry(index: int, device: SnapshotDevice) -> _Entry:
    """The device as stored, its mender_device_id already judged usable."""
    issues: list[str] = []
    issue_notes: list[str] = []
    notes: list[str] = []

    node_id = device.node_id or None
    if node_id is not None and len(node_id) > MAX_ID_CHARS:
        node_id = None
        issues.append("node_id too long")

    auth_status = device.auth_status
    if not _STATUS_TOKEN.fullmatch(auth_status):
        issue_notes.append(f"auth_status {_shown(auth_status)} stored as {INVALID_STATUS!r}")
        auth_status = INVALID_STATUS
        issues.append("auth_status not a status token")

    # Blank before the caps: blank is null whatever its length.
    auth_set_id = _blank_to_none(device.auth_set_id)
    if auth_set_id is not None and len(auth_set_id) > MAX_ID_CHARS:
        auth_set_id = None
        issues.append("auth_set_id too long")
    auth_set_pubkey = _blank_to_none(device.auth_set_pubkey)
    if auth_set_pubkey is not None and len(auth_set_pubkey) > MAX_PUBKEY_CHARS:
        auth_set_pubkey = None
        issues.append("auth_set_pubkey too long")
    if auth_status == ACCEPTED and (auth_set_id is None or auth_set_pubkey is None):
        issues.append("accepted without an auth set")

    fingerprint = None
    if auth_set_pubkey is not None:
        try:
            fingerprint = _fingerprint(auth_set_pubkey)
        except ValueError:
            # Undecodable base64, binascii.Error included. Stored without a
            # fingerprint, as services/mender.py's _record does.
            notes.append("auth_set_pubkey undecodable, stored without a fingerprint")

    inventory_updated_at = None
    if device.inventory_updated_at is not None:
        try:
            inventory_updated_at = _aware_utc(device.inventory_updated_at)
        except ValueError:
            notes.append(
                f"inventory_updated_at {_shown(device.inventory_updated_at)} is not ISO 8601 "
                "with a timezone, stored null"
            )

    return _Entry(
        index=index,
        mender_device_id=device.mender_device_id,
        node_id=node_id,
        auth_status=auth_status,
        auth_set_id=auth_set_id,
        auth_set_pubkey=auth_set_pubkey,
        auth_set_fingerprint=fingerprint,
        device_type=_cut(device.device_type, MAX_DEVICE_TYPE_CHARS, "device_type", notes),
        artifact_name=_cut(device.artifact_name, MAX_ARTIFACT_NAME_CHARS, "artifact_name", notes),
        inventory_updated_at=inventory_updated_at,
        field_issues=issues,
        issue_notes=issue_notes,
        notes=notes,
    )


@dataclass
class _Sorted:
    """The entries to store, and the devices dropped as (index, id, why)."""

    entries: list[_Entry]
    dropped: list[tuple[int, str, str]]

    @property
    def accepted(self) -> int:
        return sum(1 for entry in self.entries if entry.issue is None)


def _sort_out(devices: list[SnapshotDevice]) -> _Sorted:
    """Judge each device on its own. Never raises for what a device holds."""
    dropped: list[tuple[int, str, str]] = []
    entries: list[_Entry] = []
    first_at: dict[str, _Entry] = {}
    for index, device in enumerate(devices):
        device_id = device.mender_device_id
        if not device_id or len(device_id) > MAX_ID_CHARS:
            reason = f"dropped, mender_device_id is {len(device_id)} characters, not 1 to {MAX_ID_CHARS}"
            dropped.append((index, device_id, reason))
            continue
        first = first_at.get(device_id)
        if first is not None:
            first.repeated = True
            dropped.append((index, device_id, f"dropped, mender_device_id repeats devices[{first.index}]"))
            continue
        entry = _entry(index, device)
        first_at[device_id] = entry
        entries.append(entry)

    holders = Counter(entry.node_id for entry in entries if entry.node_id is not None)
    for entry in entries:
        if entry.node_id is not None and holders[entry.node_id] > 1:
            entry.shared_by = holders[entry.node_id]
    return _Sorted(entries, dropped)


def _faults(sorted_out: _Sorted, stored_issues: dict[str, str | None]) -> list[tuple[int, str, str]]:
    """One log line's worth per device with something to say on this push, by index.

    Dropped devices and notes are logged on every push that sends them. An
    issue is logged only when it is new or differs from the one stored before
    this push, or a device that stays faulty would repeat its line every sweep.
    """
    faults = list(sorted_out.dropped)
    for entry in sorted_out.entries:
        issue = entry.issue
        if issue is not None and stored_issues.get(entry.mender_device_id) != issue:
            parts = [f"stored with issue {issue!r}", *entry.issue_notes, *entry.notes]
        elif entry.notes:
            parts = ["stored" if issue is None else "stored with its issue unchanged", *entry.notes]
        else:
            continue
        faults.append((entry.index, entry.mender_device_id, "; ".join(parts)))
    faults.sort(key=lambda fault: fault[0])
    return faults


def _log_faults(faults: list[tuple[int, str, str]]) -> None:
    # Capped: a body of thousands of bad devices must not flood the log.
    for index, device_id, detail in faults[:MAX_LOGGED_DEVICES]:
        logger.warning("mender snapshot devices[%d] %s: %s", index, _shown(device_id), detail)
    rest = len(faults) - MAX_LOGGED_DEVICES
    if rest > 0:
        logger.warning("mender snapshot: %d more devices with a fault, not logged one by one", rest)


# ── Throttled lines ──────────────────────────────────────────────────────────


class _Throttle:
    """A line logged at once, then at most once per LOG_WINDOW_S with the count held back.

    Anyone can send a request this route refuses, and nothing else logs one line
    per request (uvicorn runs at --log-level warning), so a line each would let
    one address rotate the log away. One window per line, never per address, so
    memory stays fixed. admit() never awaits, so no other request runs inside it.
    """

    def __init__(self) -> None:
        # None, not 0.0: the monotonic clock counts from boot, so 0.0 would hold
        # back the first line on a host up for less than a window.
        self.logged_at: float | None = None
        self.held = 0
        self.last_held = ""

    def admit(self, detail: str = "") -> tuple[int, str] | None:
        """None when this one is held back. Otherwise it is logged now, with what
        this returns: the count held back since the last line, and the detail
        of the last of them ("" when none was)."""
        now = _monotonic()
        if self.logged_at is not None and now - self.logged_at < LOG_WINDOW_S:
            self.held += 1
            self.last_held = detail
            return None
        admitted = (self.held, self.last_held)
        self.logged_at, self.held, self.last_held = now, 0, ""
        return admitted


_refusals = _Throttle()
_short_token = _Throttle()


def _reset_for_tests() -> None:
    """Restore the throttles to boot values. Tests only."""
    global _refusals, _short_token
    _refusals, _short_token = _Throttle(), _Throttle()


def _log_refused(client: str) -> None:
    """The client address only: never the token or the header."""
    admitted = _refusals.admit(client)
    if admitted is None:
        return
    held, last = admitted
    if held:
        logger.warning(
            "mender snapshot refused: missing or wrong bearer token from %s"
            " (%d more refused since the last such line, the last from %s)",
            client,
            held,
            last,
        )
    else:
        logger.warning("mender snapshot refused: missing or wrong bearer token from %s", client)


def _log_short_token() -> None:
    admitted = _short_token.admit()
    if admitted is None:
        return
    message = "MENDER_SYNC_TOKEN is shorter than %d characters, so %s answers 404; set a longer one"
    held = admitted[0]
    if held:
        logger.error(message + " (%d more requests since the last such line)", MIN_TOKEN_CHARS, SNAPSHOT_PATH, held)
    else:
        logger.error(message, MIN_TOKEN_CHARS, SNAPSHOT_PATH)


# ── Token and body reads ─────────────────────────────────────────────────────


def _configured_token() -> bytes | None:
    """MENDER_SYNC_TOKEN as bytes, or None while the route should not exist."""
    token = (os.getenv("MENDER_SYNC_TOKEN") or "").strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_CHARS:
        _log_short_token()
        return None
    return token.encode()


def _presented_token(request: Request) -> bytes | None:
    """The bearer credentials as sent, or None when the header is missing or malformed."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, credentials = values[0].partition(" ")
    if scheme.lower() != "bearer":
        return None
    credentials = credentials.strip(" ")
    # Starlette decodes header values as latin-1, so this gives back the bytes on
    # the wire. compare_digest refuses a str holding non-ASCII with a TypeError.
    return credentials.encode("latin-1")


async def _read_capped(request: Request) -> bytes | None:
    """The body, or None as soon as it is known to exceed MAX_BODY_BYTES.

    The app-wide middleware checks Content-Length against its own, larger cap
    and never sees a chunked body, so both are checked here.
    """
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = None
    if declared is not None and declared > MAX_BODY_BYTES:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# ── Apply ────────────────────────────────────────────────────────────────────

# A write that changes nothing, run first in the transaction. It takes SQLite's
# write lock, which a SELECT does not (the driver opens no transaction for one),
# so a concurrent push waits here until this one commits or rolls back, and the
# stored generated_at read next cannot change before this push writes its own.
# It takes the lock with zero rows matched as well, before the first snapshot.
_TAKE_WRITE_LOCK = (
    update(MenderSyncState)
    .where(MenderSyncState.id == SYNC_STATE_ID)
    .values(id=MenderSyncState.id)
    .execution_options(synchronize_session=False)
)


def _row_values(entry: _Entry, previous: Any, at: datetime) -> dict[str, Any]:
    """The row an entry is written as. `previous` is its stored row, or None."""
    accepted = entry.auth_status == ACCEPTED
    if previous is None:
        accepted_at = at if accepted else None
        auth_set_changed_at = at if entry.auth_set_id is not None else None
    else:
        was_accepted = previous.auth_status == ACCEPTED
        if accepted and not was_accepted:
            accepted_at = at
        elif was_accepted and not accepted:
            accepted_at = None
        else:
            accepted_at = previous.accepted_at
        changed = entry.auth_set_id != previous.auth_set_id
        auth_set_changed_at = at if changed else previous.auth_set_changed_at
    return {
        "mender_device_id": entry.mender_device_id,
        "node_id": entry.node_id,
        "auth_status": entry.auth_status,
        "auth_set_id": entry.auth_set_id,
        "auth_set_pubkey": entry.auth_set_pubkey,
        "auth_set_fingerprint": entry.auth_set_fingerprint,
        "auth_set_changed_at": auth_set_changed_at,
        "accepted_at": accepted_at,
        "last_confirmed_at": at,
        "device_type": entry.device_type,
        "artifact_name": entry.artifact_name,
        "inventory_updated_at": entry.inventory_updated_at,
        "present": True,
        "snapshot_issue": entry.issue,
    }


async def _apply_devices(
    session: AsyncSession, entries: list[_Entry], at: datetime
) -> tuple[int, dict[str, str | None]]:
    """Write the entries over the mirror. Returns the rows now absent, and the
    snapshot_issue each stored row had before this push.

    node_id is unique among present rows without an issue, and SQLite checks
    that as each row is written. So rows leave the index before any row joins
    it: absent rows first, then entries with an issue, written in full, then
    clean entries whose node_id changes drop the old one, and only then are
    clean entries written. Their node_ids are unique, since a shared one is an
    issue.
    """
    stored = (
        await session.execute(
            select(
                MenderDevice.mender_device_id,
                MenderDevice.node_id,
                MenderDevice.auth_status,
                MenderDevice.auth_set_id,
                MenderDevice.accepted_at,
                MenderDevice.auth_set_changed_at,
                MenderDevice.present,
                MenderDevice.snapshot_issue,
            )
        )
    ).all()
    previous = {row.mender_device_id: row for row in stored}
    listed = {entry.mender_device_id for entry in entries}

    absent = [row for row in stored if row.mender_device_id not in listed]
    newly_absent = [{"mender_device_id": row.mender_device_id, "present": False} for row in absent if row.present]
    if newly_absent:
        await session.execute(update(MenderDevice), newly_absent)

    known = [entry for entry in entries if entry.mender_device_id in previous]
    flagged = [entry for entry in known if entry.issue is not None]
    clean = [entry for entry in known if entry.issue is None]
    if flagged:
        await session.execute(
            update(MenderDevice), [_row_values(entry, previous[entry.mender_device_id], at) for entry in flagged]
        )

    moving = [
        {"mender_device_id": entry.mender_device_id, "node_id": None}
        for entry in clean
        if (old := previous[entry.mender_device_id].node_id) is not None and old != entry.node_id
    ]
    if moving:
        await session.execute(update(MenderDevice), moving)

    if clean:
        await session.execute(
            update(MenderDevice), [_row_values(entry, previous[entry.mender_device_id], at) for entry in clean]
        )
    inserts = [_row_values(entry, None, at) for entry in entries if entry.mender_device_id not in previous]
    if inserts:
        await session.execute(insert(MenderDevice), inserts)
    return len(absent), {row.mender_device_id: row.snapshot_issue for row in stored}


def _iso_z(when: datetime) -> str:
    return when.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _alert_after_commit(snapshot: Snapshot, previous_source: str | None, previous_count: int | None) -> None:
    # Resolved at call time so a test's patch of services.alerting.send_alert is
    # the function called, as in routes/node_register.py.
    from services.alerting import send_alert

    count = len(snapshot.devices)
    if previous_source is not None and previous_source != snapshot.source:
        message = f"mender snapshot source changed from {previous_source!r} to {snapshot.source!r}"
        logger.warning("%s", message)
        send_alert(
            "mender_snapshot_source_changed",
            message,
            {"previous_source": previous_source, "source": snapshot.source},
        )
    # More than a 20% drop, in integers: exactly 20% does not alert.
    if previous_count and count * 5 < previous_count * 4:
        message = f"mender snapshot lists {count} devices, down from {previous_count} (source {snapshot.source!r})"
        logger.warning("%s", message)
        send_alert(
            "mender_snapshot_count_drop",
            message,
            {"previous_count": previous_count, "device_count": count, "source": snapshot.source},
        )


# ── Route ────────────────────────────────────────────────────────────────────


@router.post(SNAPSHOT_PATH)
async def receive_snapshot(request: Request, session: AsyncSession = Depends(get_async_session)) -> JSONResponse:
    expected = _configured_token()
    if expected is None:
        # Raised rather than built, so the body is the one an unknown route gets.
        raise HTTPException(status_code=404)

    presented = _presented_token(request)
    if presented is None or not hmac.compare_digest(presented, expected):
        _log_refused(request.client.host if request.client else "unknown")
        return JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    body = await _read_capped(request)
    if body is None:
        return JSONResponse({"detail": f"Request body too large (max {MAX_BODY_BYTES} bytes)"}, status_code=413)

    try:
        snapshot = Snapshot.model_validate_json(body)
    except ValidationError as exc:
        return JSONResponse({"detail": _validation_detail(exc)}, status_code=422)
    # Before the lock: it reads nothing from the database.
    sorted_out = _sort_out(snapshot.devices)

    await session.execute(_TAKE_WRITE_LOCK)
    state = await load_sync_state(session)
    if state is not None and snapshot.generated_at <= state.generated_at:
        stored_at = state.generated_at
        await session.rollback()
        logger.info(
            "mender snapshot from %r refused: generated_at %s is not later than the stored %s",
            snapshot.source,
            _iso_z(snapshot.generated_at),
            _iso_z(stored_at),
        )
        return JSONResponse({"detail": "generated_at is not later than the stored snapshot's"}, status_code=409)

    previous_source = state.source if state is not None else None
    previous_count = state.device_count if state is not None else None
    absent, stored_issues = await _apply_devices(session, sorted_out.entries, snapshot.generated_at)
    received_at = _now()
    if state is None:
        state = MenderSyncState(id=SYNC_STATE_ID)
        session.add(state)
    state.last_snapshot_at = received_at
    state.generated_at = snapshot.generated_at
    state.device_count = len(snapshot.devices)
    state.source = snapshot.source
    await session.commit()

    accepted = sorted_out.accepted
    rejected = len(snapshot.devices) - accepted
    logger.info(
        "mender snapshot from %r applied: %d devices, %d accepted, %d rejected, %d absent, generated_at %s",
        snapshot.source,
        len(snapshot.devices),
        accepted,
        rejected,
        absent,
        _iso_z(snapshot.generated_at),
    )
    _log_faults(_faults(sorted_out, stored_issues))
    _alert_after_commit(snapshot, previous_source, previous_count)
    return JSONResponse(
        {
            "accepted": accepted,
            "rejected": rejected,
            "absent": absent,
            "generated_at": _iso_z(snapshot.generated_at),
        }
    )
