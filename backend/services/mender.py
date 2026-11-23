"""The in-request Mender lookup, from the source MENDER_SOURCE names: the live
management API, or the mirror that mender-auto-accept's snapshots fill
(core/mender_mirror.py).

One function, so registration has one external dependency and tests have one seam.
Every failure to get an answer, from Mender or from the mirror, raises
MenderUnreachable rather than resolving to "unknown device": the two are the same
403 on the wire, but only one of them means every enrolment in the fleet is
blocked, and that difference belongs in an alert.
"""

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core import users
from core.mender_mirror import (
    MENDER_SOURCES,
    MenderDevice,
    load_sync_state,
    mender_source,
    stale_reason,
)

logger = logging.getLogger(__name__)

# The page size the client-side filter needs to see the whole fleet in one request.
# Phase 1 targets twelve nodes, comfortably inside one page; a fleet past 500 needs
# pagination here, not a bigger number.
_PER_PAGE = 500

# Overridden by tests with an httpx.MockTransport. None means httpx's default.
_transport: httpx.AsyncBaseTransport | None = None


class MenderUnreachable(Exception):
    """Mender could not be asked. Distinct from Mender answering no."""


def _now() -> datetime:
    """The clock the mirror's staleness is judged by. Tests freeze it."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class MenderDeviceRecord:
    """What one device's entry in the management API reduces to.

    The accepted set's raw pubkey is carried alongside its fingerprint. The Mender
    mirror (D26) needs the pubkey itself to populate `mender_devices.auth_set_pubkey`,
    which is free to write as the sweep runs and expensive to backfill across the
    fleet afterwards, and registration signing (D31) cannot verify anything without
    it either. The fingerprint cannot stand in for either: it is a SHA-256 over the
    DER SPKI, so it confirms a key you already hold but cannot verify a signature.
    """

    mender_device_id: str
    node_id: str
    auth_status: str
    auth_set_id: str | None
    auth_set_pubkey: str | None
    auth_set_fingerprint: str | None


def _fingerprint(pem: str) -> str:
    """SHA-256 over the DER SPKI, which is the base64 body of the PEM decoded."""
    body = "".join(line for line in pem.splitlines() if "-----" not in line)
    # validate=True: without it, b64decode silently drops characters outside the
    # base64 alphabet instead of raising, so a non-PEM key (an OpenSSH line, say)
    # would fingerprint the wrong bytes rather than hit the except clause below.
    return hashlib.sha256(base64.b64decode(body, validate=True)).hexdigest()


def _str_or_none(value: object) -> str | None:
    """Coerce a value onto a field typed str | None: anything else is a malformed record."""
    return value if isinstance(value, str) else None


def _identity_node_id(device: dict) -> str:
    """The device's identity_data.node_id, or "" if there is no identity_data.

    deviceauth's older API carried identity data as a JSON-encoded string; that
    or a list here would raise AttributeError out of .get, the same shape
    failure closed for the device list and for auth_sets. A node_id that is
    present but not a string is the same shape failure: it can never equal the
    requested string, so left unchecked it would pass for an ordinary miss
    rather than the malformed record it is.
    """
    identity_data = device.get("identity_data")
    if identity_data is not None and not isinstance(identity_data, dict):
        raise MenderUnreachable(f"device {device.get('id')} has malformed identity_data")
    node_id = (identity_data or {}).get("node_id", "")
    if not isinstance(node_id, str):
        raise MenderUnreachable(f"device {device.get('id')} has a non-string node_id")
    return node_id


def _record(device: dict) -> MenderDeviceRecord:
    node_id = _identity_node_id(device)
    auth_sets = device.get("auth_sets") or []
    if not isinstance(auth_sets, list) or not all(isinstance(s, dict) for s in auth_sets):
        # auth_sets as a dict would iterate its string keys into s.get and raise
        # AttributeError; a list containing a non-dict element fails the same way.
        # Both are Mender answering something unintelligible, not "no accepted set".
        raise MenderUnreachable(f"device {device.get('id')} has a malformed auth_sets")
    accepted = [s for s in auth_sets if s.get("status") == "accepted"]
    if len(accepted) > 1:
        # The phase 1 ADR (claude-shared/docs/decisions/2026-08-03-node-server-phase-1.md)
        # refuses rather than choosing: picking one would silently bless whichever key
        # happened to sort first on a device with two live identities.
        logger.warning("mender device %s has %d accepted auth sets", device.get("id"), len(accepted))
        return MenderDeviceRecord(device.get("id", ""), node_id, "ambiguous", None, None, None)
    if not accepted:
        status = device.get("status", "pending")
        if status == "accepted":
            # The device's own status says accepted while nothing in auth_sets
            # agrees: the same contradiction as more than one accepted set, so
            # it gets the same refusal rather than trusting the aggregate.
            logger.warning("mender device %s is accepted with no accepted auth set", device.get("id"))
            return MenderDeviceRecord(device.get("id", ""), node_id, "ambiguous", None, None, None)
        return MenderDeviceRecord(device.get("id", ""), node_id, status, None, None, None)
    auth_set = accepted[0]
    status = device.get("status", "accepted")
    if status != "accepted":
        # The opposite contradiction to the one above (an accepted auth set
        # while the top-level status disagrees) is not symmetric with it in
        # consequence, so it is not refused the same way: trusting the
        # aggregate above would admit a node with no key identified, whereas
        # here the aggregate is the conservative reading, and auth_status
        # below carries `status` rather than "accepted", so a caller gating
        # registration on auth_status already fails closed on it exactly as
        # refusing outright would. mender-auto-accept sweeps every 30 seconds,
        # so a device caught here is ordinarily mid bring-up rather than
        # faulty; the warning exists only to make the disagreement visible.
        logger.warning("mender device %s has an accepted auth set but device status %s", device.get("id"), status)
    pubkey = auth_set.get("pubkey")
    # auth_set_id and auth_set_pubkey are both str | None, so a value that is
    # not a string (a malformed record) must not reach either field as-is. A
    # pubkey that fails to decode below is kept regardless: it is exactly the
    # one worth carrying for diagnosis.
    auth_set_id = _str_or_none(auth_set.get("id"))
    auth_set_pubkey = _str_or_none(pubkey)
    fingerprint = None
    if pubkey:
        try:
            fingerprint = _fingerprint(pubkey)
        except (ValueError, AttributeError):
            # ValueError covers undecodable base64 (binascii.Error is a subclass);
            # AttributeError covers a pubkey that is not a string at all.
            logger.warning("mender device %s has an undecodable pubkey", device.get("id"))
    return MenderDeviceRecord(
        device.get("id", ""),
        node_id,
        status,
        auth_set_id,
        auth_set_pubkey,
        fingerprint,
    )


async def lookup_device(node_id: str, session: AsyncSession | None = None) -> MenderDeviceRecord | None:
    """Resolve node_id against the Mender source. None means it does not know the device.

    MENDER_SOURCE is read per call:
    - `api`, the default: the management API, live (_lookup_api).
    - `mirror`: the mender_devices table, with no call to Mender (_lookup_mirror).
    - `shadow`: the API answers, and its answer or its MenderUnreachable is what
      the caller gets. The mirror is then read as `mirror` would read it and the
      two are compared in the log, but nothing the mirror says reaches the caller.
    Any other value raises MenderUnreachable before anything is asked: guessing a
    mode would quietly change what gates registration.

    `session` is the caller's, for the mirror read. Without one, the read opens
    its own.
    """
    source = mender_source()
    if source == "mirror":
        return await _lookup_mirror(node_id, session)
    if source not in MENDER_SOURCES:
        raise MenderUnreachable(f"unknown MENDER_SOURCE '{source}'")
    record = await _lookup_api(node_id)
    if source == "shadow":
        await _compare_with_mirror(node_id, record, session)
    return record


async def _lookup_mirror(node_id: str, session: AsyncSession | None) -> MenderDeviceRecord | None:
    """Resolve node_id from the mirror. None means the last snapshot did not list it.

    Fails closed: a stale mirror (any stale_reason: no snapshot, too old,
    ahead of a clock that went back, a malformed MENDER_MIRROR_MAX_AGE_S) and
    a database error both raise MenderUnreachable, never None. An entry the
    last snapshot could not store cleanly is not a reason to block the fleet:
    it resolves to "ambiguous", which refuses that node_id alone.
    """
    try:
        if session is not None:
            # No autoflush: a read on the caller's session must not write
            # whatever the caller has pending, or fail on it.
            with session.no_autoflush:
                return await _read_mirror(session, node_id)
        async with users.async_session_maker() as own_session:
            return await _read_mirror(own_session, node_id)
    except (SQLAlchemyError, ValueError) as exc:
        # ValueError as well: SQLite hands back a stored timestamp it cannot
        # parse as a bare ValueError, not wrapped by SQLAlchemy.
        reason = exc.orig if isinstance(exc, DBAPIError) else exc
        raise MenderUnreachable(f"mirror unreadable: {reason}") from exc


async def _read_mirror(session: AsyncSession, node_id: str) -> MenderDeviceRecord | None:
    # The state before the device, so the row read is no older than the
    # snapshot just judged fresh.
    state = await load_sync_state(session)
    reason = stale_reason(state, _now())
    if reason is not None:
        raise MenderUnreachable(f"mirror stale ({reason})")
    # Columns rather than MenderDevice objects: an entity the session already
    # holds comes back as held rather than as stored, and the session may be
    # the caller's. Ordered so the device named below is the same on every read.
    rows = (
        await session.execute(
            select(
                MenderDevice.mender_device_id,
                MenderDevice.auth_status,
                MenderDevice.auth_set_id,
                MenderDevice.auth_set_pubkey,
                MenderDevice.auth_set_fingerprint,
                MenderDevice.snapshot_issue,
            )
            .where(MenderDevice.node_id == node_id, MenderDevice.present.is_(True))
            .order_by(MenderDevice.mender_device_id)
        )
    ).all()
    if not rows:
        # Never listed, or listed once and gone from every snapshot since.
        return None
    row = rows[0]
    if len(rows) == 1 and row.snapshot_issue is None:
        # auth_status as the sender reduced it, ambiguity included: there is
        # nothing here to recompute it from.
        return MenderDeviceRecord(
            row.mender_device_id,
            node_id,
            row.auth_status,
            row.auth_set_id,
            row.auth_set_pubkey,
            row.auth_set_fingerprint,
        )
    # An entry with an issue, or more than one device claiming the node_id
    # (the partial unique index allows that once one is flagged). Refused
    # rather than choosing, as _record refuses two accepted auth sets, and in
    # its shape: the lowest device id, no auth set. A refusal for this node
    # alone, so it is logged here rather than alerted as Mender unreachable.
    logger.warning(
        "mender mirror refuses %s as ambiguous: %s",
        node_id,
        ", ".join(_refused_entry(r.mender_device_id, r.snapshot_issue) for r in rows),
    )
    return MenderDeviceRecord(row.mender_device_id, node_id, "ambiguous", None, None, None)


def _shown(value: str) -> str:
    """A sender's string as it goes into a log line: cut short, and repr'd so it cannot break the line."""
    return repr(value[:64])


def _refused_entry(mender_device_id: str, issue: str | None) -> str:
    return f"{_shown(mender_device_id)} ({_shown(issue) if issue else 'no issue'})"


# What shadow compares. Not the pubkey: the fingerprint stands for it, and
# keys stay out of the log.
_SHADOW_FIELDS = ("mender_device_id", "auth_status", "auth_set_id", "auth_set_fingerprint")


def _shadow_summary(record: MenderDeviceRecord | None) -> str:
    if record is None:
        return "none"
    fingerprint = record.auth_set_fingerprint[:16] if record.auth_set_fingerprint else None
    return f"{record.auth_status} device={record.mender_device_id} set={record.auth_set_id} fp={fingerprint}"


def _shadow_agrees(api: MenderDeviceRecord | None, mirror: MenderDeviceRecord | None) -> bool:
    if api is None or mirror is None:
        return api is mirror
    return all(getattr(api, field) == getattr(mirror, field) for field in _SHADOW_FIELDS)


async def _compare_with_mirror(node_id: str, api: MenderDeviceRecord | None, session: AsyncSession | None) -> None:
    """Log how the mirror's answer compares with the API's. Never raises."""
    try:
        mirror = await _lookup_mirror(node_id, session)
    except MenderUnreachable as exc:
        logger.warning("mender shadow could not read the mirror for %s: %s", node_id, exc)
        return
    except Exception as exc:
        # Not expected, hence the traceback. Still only a log line: in shadow
        # the API's answer decides, so the mirror cannot be what refuses.
        logger.warning("mender shadow could not read the mirror for %s: %r", node_id, exc, exc_info=True)
        return
    if _shadow_agrees(api, mirror):
        logger.info("mender shadow agrees for %s", node_id)
    else:
        logger.warning(
            "mender shadow disagrees for %s: api=%s mirror=%s",
            node_id,
            _shadow_summary(api),
            _shadow_summary(mirror),
        )


async def _lookup_api(node_id: str) -> MenderDeviceRecord | None:
    """Resolve node_id against the live management API. None means Mender does not know it.

    The filtering is client side because deviceauth has no identity filter:
    GET /devices declares status, id, page and per_page and ignores any other
    query parameter rather than rejecting it, so a filter that looks right
    returns the whole first page instead of nothing. The identity check before
    the return is what makes that fail closed, and it is the part to keep
    whatever the query becomes.

    No status filter either, deliberately. Asking only for accepted devices
    collapses "Mender has never heard of this board" and "this board is enrolled
    and waiting to be accepted" into the same None, and those want different
    operator actions. The wire cannot tell them apart (both are the shared 403),
    but the log can, and during bring-up most of the fleet is pending.
    """
    # Read per call, not at import: backend/main.py calls load_dotenv() after its
    # service imports, so a module-level read here would see an empty MENDER_PAT
    # on a non-compose run and misreport a dead credential as MenderUnreachable.
    #
    # `or` rather than getenv's own default, on this line and MENDER_TIMEOUT_S
    # below: a variable that is set but empty (a bare `MENDER_SERVER=` line in
    # .env, passed through by compose's env_file) is "" from getenv, not None,
    # so getenv's own default never fires.
    mender_server = (os.getenv("MENDER_SERVER") or "https://hosted.mender.io").rstrip("/")
    mender_pat = os.getenv("MENDER_PAT", "")
    try:
        mender_timeout_s = float(os.getenv("MENDER_TIMEOUT_S") or "3.0")
    except ValueError as exc:
        # A stray value here should not turn into an uncaught 500.
        raise MenderUnreachable(f"malformed MENDER_TIMEOUT_S: {exc}") from exc
    headers = {"Authorization": f"Bearer {mender_pat}"}
    try:
        async with httpx.AsyncClient(base_url=mender_server, timeout=mender_timeout_s, transport=_transport) as client:
            response = await client.get(
                "/api/management/v2/devauth/devices", params={"per_page": _PER_PAGE}, headers=headers
            )
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        # A malformed MENDER_SERVER does not necessarily raise httpx.HTTPError:
        # httpx.InvalidURL (a non-ASCII host that fails IDNA encoding, a
        # malformed IPv6 literal) inherits Exception rather than HTTPError in
        # httpx 0.27, and a value with no recognisable scheme reaches urllib
        # as a bare ValueError. Both are as much a failure to reach Mender as
        # anything HTTPError already covers, and must not escape as an
        # uncaught exception.
        raise MenderUnreachable(str(exc)) from exc
    if response.status_code != 200:
        # A 401 lands here too, per the module docstring: it blocks the whole fleet.
        raise MenderUnreachable(f"status {response.status_code}")
    # A 200 does not guarantee a device list: neither of the next two checks is
    # an httpx.HTTPError, so each needs its own explicit route to MenderUnreachable
    # rather than surfacing as an uncaught exception downstream.
    try:
        payload = response.json()
    except ValueError as exc:
        # A 200 with a body that is not JSON at all, e.g. an error page from
        # something in front of Mender.
        raise MenderUnreachable(f"non-JSON response body: {exc}") from exc
    if not isinstance(payload, list):
        # A JSON object body, from an error response or a future envelope change,
        # would otherwise iterate its string keys into _record and fail there
        # instead of here.
        raise MenderUnreachable(f"expected a list of devices, got {type(payload).__name__}")
    if len(payload) >= _PER_PAGE:
        # A full page means the fleet may have grown past what one request returns,
        # and a real device sitting beyond it resolves to the same None as one
        # Mender has never heard of. This is what would tell the two apart.
        logger.warning("mender device list filled the page (%d devices); the fleet may exceed _PER_PAGE", len(payload))
    # A malformed element (not a dict, or an unparseable identity_data) is skipped rather than raised on
    # the spot: the loop runs over every device in the page, so raising here would let one unrelated bad
    # record block every node's registration. The skip is forgiven only if the scan still finds its
    # match; otherwise the skipped record could have been the one asked for (see the module docstring).
    skipped = False
    for device in payload:
        if not isinstance(device, dict):
            # E.g. ["unauthorized"], or a future list of bare device ids.
            logger.warning("mender device list contains a non-object element of type %s", type(device).__name__)
            skipped = True
            continue
        try:
            identity = _identity_node_id(device)
        except MenderUnreachable:
            logger.warning("mender device %s has malformed identity_data", device.get("id"))
            skipped = True
            continue
        if identity != node_id:
            continue
        record = _record(device)
        # Deliberately redundant now that node_id is matched above: this is the
        # fail-closed guard the docstring describes, kept for when the query
        # changes and might rely on a filter deviceauth silently ignores.
        if record.node_id == node_id:
            return record
    if skipped:
        raise MenderUnreachable("a malformed device in the list could not be checked against the requested node_id")
    return None
