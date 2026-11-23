"""WebSocket and SSE live-streaming endpoints."""

import asyncio
import hmac
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect, status
from fastapi.requests import HTTPConnection
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from config.constants import AIRCRAFT_FLUSH_INTERVAL_S
from core import sessions, state
from core.auth import get_user_nodes
from core.data_access import (
    RECHECK_INTERVAL_S,
    DataAccessDenied,
    Principal,
    check_data_access,
    refuse_upgrade,
    require_data_access,
    stream_slot,
    ws_data_access,
)
from core.origins import foreign_origin
from core.users import ANONYMOUS_USER, AUTH_BYPASS, read_user_from_token
from services import owner_feed, viewport
from services.tasks.aircraft_flush import WS_SEND_TIMEOUT_S, owner_bytes

router = APIRouter(tags=["radar"])

_WS_AUTH_TOKEN = os.getenv("WS_AUTH_TOKEN", "")

# How long an open owner feed goes between checks of its cookie when nothing
# wakes it first. Every sign-out in core/sessions.py wakes it at once, so this
# only bounds a session that expires, or one ended behind that module's back.
SESSION_RECHECK_S = 30.0
# How long closing a signed-out owner feed may take. By then it is out of the
# broadcast, so a client too wedged to answer costs nothing but this.
_SIGNED_OUT_CLOSE_TIMEOUT_S = 5.0

# How long an event stream goes without an event before it sends a comment
# line instead. nginx closes an /api/ read idle for 120 s
# (deploy/nginx/snippets/api.conf), and a real-only stream has nothing to send
# for as long as no flush writes a new frame.
SSE_KEEPALIVE_S = 15.0
# What core/state.py holds for the real-only frame until the first flush
# writes one: a placeholder, not a frame, so no stream is sent it.
_NO_REAL_FRAME_YET = b'{"now":0,"aircraft":[],"messages":0}'


async def _authenticate_ws(ws: WebSocket) -> bool:
    """Validate WebSocket token if WS_AUTH_TOKEN is configured.

    Guards the two public feeds. The owner feed does not take it: its session
    cookie is the credential, and a shared token adds nothing to that.

    Returns True if the connection is authorized (or auth is disabled).
    Closes the socket and returns False on failure.
    """
    if not _WS_AUTH_TOKEN:
        return True
    token = ws.query_params.get("token", "")
    if not token or not hmac.compare_digest(token, _WS_AUTH_TOKEN):
        await ws.close(code=1008, reason="Unauthorized")
        return False
    return True


def _new_real_frame(sent: bytes | None) -> bytes | None:
    """The real-only frame, when a flush has written one other than `sent`; else None.

    Every flush stores a new bytes object (services/tasks/aircraft_flush.py),
    so a frame is new exactly when it is not the object sent last.
    """
    frame = state.latest_real_aircraft_json_bytes
    if frame is sent or frame == _NO_REAL_FRAME_YET:
        return None
    return frame


async def _refusal_now(conn: HTTPConnection) -> DataAccessDenied | None:
    """The gate's refusal of an open stream's caller now, or None while it may go on.

    A check that fails for any other reason keeps the stream until the next
    one, as the owner feed does: a key or an entitlement ends by a write to
    the database the check could not read, so a failed read is no sign that
    one did.
    """
    try:
        await check_data_access(conn)
    except DataAccessDenied as denied:
        return denied
    except Exception:
        logging.warning("Could not re-check an open stream's data access", exc_info=True)
    return None


async def _until_disconnect(ws: WebSocket) -> None:
    """Read, and drop, whatever the client sends, until it goes."""
    while (await ws.receive())["type"] != "websocket.disconnect":
        pass


async def _send_real_frames(ws: WebSocket) -> None:
    """Send an open socket each real-only frame a flush writes, until it goes or is refused.

    Looks for a new frame every AIRCRAFT_FLUSH_INTERVAL_S, the flush's own
    cadence, and asks the gate again every RECHECK_INTERVAL_S; a refusal
    closes the socket 1008 with its reason. The receive side is watched
    beside the loop, so a client that goes ends it at once rather than at its
    next send. A send or close the client does not take within
    WS_SEND_TIMEOUT_S ends the socket, as the broadcast drops a client that
    misses its send, so a wedged client holds its stream no longer than that.
    """
    loop = asyncio.get_running_loop()
    gone = asyncio.create_task(_until_disconnect(ws))
    sent = None
    recheck_at = loop.time() + RECHECK_INTERVAL_S
    try:
        while not gone.done():
            if (frame := _new_real_frame(sent)) is not None:
                await asyncio.wait_for(ws.send_text(frame.decode()), timeout=WS_SEND_TIMEOUT_S)
                sent = frame
            if loop.time() >= recheck_at:
                if (denied := await _refusal_now(ws)) is not None:
                    reason = denied.reason
                    await asyncio.wait_for(
                        ws.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason), timeout=WS_SEND_TIMEOUT_S
                    )
                    return
                recheck_at = loop.time() + RECHECK_INTERVAL_S
            await asyncio.wait((gone,), timeout=AIRCRAFT_FLUSH_INTERVAL_S)
    finally:
        gone.cancel()
        await asyncio.gather(gone, return_exceptions=True)


async def _serve_real_frames(ws: WebSocket, principal: Principal) -> None:
    """Hold one of the principal's streams for as long as the socket is open, and feed it.

    The slot is taken before accept, so one past the tier's limit is refused
    with the 429 `stream_limit` a REST call gets and no socket opens
    (refuse_upgrade), and given back however the socket ends: a disconnect, a
    refusal, an error, or the handler cancelled.
    """
    async with AsyncExitStack() as held:
        try:
            await held.enter_async_context(stream_slot(principal))
        except DataAccessDenied as denied:
            refuse_upgrade(denied)
        await ws.accept()
        logging.info("WS real-only client connected (%s)", principal.kind)
        try:
            await _send_real_frames(ws)
        except WebSocketDisconnect:
            pass
        except Exception:
            # NOT a client disconnect — a bug here used to be swallowed and
            # read as one.
            logging.debug("websocket handler error", exc_info=True)
        finally:
            logging.info("WS real-only client disconnected (%s)", principal.kind)


@router.websocket("/ws/aircraft")
async def websocket_aircraft(ws: WebSocket):
    """The whole network's aircraft feed, behind the data-access gate (core/data_access.py).

    WS_AUTH_TOKEN, when set, is asked for first, as on the live feed. Then the
    gate decides before anything is sent: an account key in `?apikey=`, the
    console's session from the console's own pages, an administrator or the
    radar key. Anything else is refused before accept with the HTTP status,
    headers and body a REST call gets, and no socket opens.

    Internal access, an administrator or the radar key, keeps the feed as it
    was: the whole fleet, the synthetic simulation one included (the /sim
    map), through the broadcast in services/tasks/aircraft_flush.py. A key or
    a session is sent the real-only frame instead, by a loop of its own
    (_send_real_frames), as one of its tier's streams open at once; it never
    joins the broadcast.
    """
    if not await _authenticate_ws(ws):
        return
    principal = await ws_data_access(ws)
    if not principal.internal:
        await _serve_real_frames(ws, principal)
        return
    await ws.accept()
    state.ws_clients.add(ws)
    logging.info("WebSocket client connected (%d total)", len(state.ws_clients))
    try:
        # Probed and sent from the public pair, which is what
        # latest_aircraft_json_bytes serialises: internal access is shown the
        # published feed, as every reader of this socket always was, and the
        # unredacted dict beside it belongs to the owner feed below.
        if state.latest_aircraft_json_public.get("aircraft"):
            await ws.send_text(state.latest_aircraft_json_bytes.decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # NOT a client disconnect — a bug here (e.g. in payload filtering)
        # used to be swallowed and read as one.
        logging.debug("websocket handler error", exc_info=True)
    finally:
        state.ws_clients.discard(ws)
        logging.info("WebSocket client disconnected (%d remaining)", len(state.ws_clients))


@router.websocket("/ws/aircraft/live")
async def websocket_aircraft_live(ws: WebSocket):
    """Real-node-only aircraft feed — excludes synthetic simulation nodes.
    Used by app.retina.fm, which shows live hardware only.

    Cut to the client's viewport (services/viewport.py): nothing is sent until
    it sends {"type": "viewport", "bbox": [south, west, north, east]}, and each
    such message replaces the last and is answered with the latest frame for
    it at once, or by the next broadcast when the last such answer was less
    than VIEWPORT_ANSWER_INTERVAL_S ago. Any other message is ignored.
    """
    if not await _authenticate_ws(ws):
        return
    await ws.accept()
    state.ws_live_clients.add(ws)
    logging.info("WS live client connected (%d total)", len(state.ws_live_clients))
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            box = viewport.viewport_from_message(message.get("text"))
            if box is not None:
                viewport.watch(ws, box)
                await viewport.answer(ws)
    except WebSocketDisconnect:
        pass
    except Exception:
        # NOT a client disconnect — a bug here (e.g. in payload filtering)
        # used to be swallowed and read as one.
        logging.debug("websocket handler error", exc_info=True)
    finally:
        state.ws_live_clients.discard(ws)
        viewport.forget(ws)
        logging.info("WS live client disconnected (%d remaining)", len(state.ws_live_clients))


async def _signed_in_as(cookie: str | None, account_id: uuid.UUID) -> bool:
    """Whether the cookie still signs in the account it opened the socket as.

    Read-only: an open socket is no use of its session, so this neither slides
    the session nor records a visit.
    """
    resolved = await sessions.resolve(cookie, slide=False)
    return resolved is not None and resolved.user.id == account_id and resolved.user.is_active


async def _close_when_signed_out(
    ws: WebSocket, cookie: str | None, account_id: uuid.UUID, watch: sessions.AccountWatch
) -> None:
    """Close an owner feed with 1008 once its cookie no longer signs its account in.

    Checks at once, for a sign-out that committed between the handshake and
    the watch, then each time the account's watch wakes or SESSION_RECHECK_S
    passes. A check that fails keeps the socket until the next one: a session
    ends only by a write to the database the check could not read, so a failed
    read is no sign that one did.
    """
    while True:
        try:
            if not await _signed_in_as(cookie, account_id):
                break
        except Exception:
            logging.warning("Could not check an owner feed's session", exc_info=True)
        await watch.wait(SESSION_RECHECK_S)
    # Out of the broadcast before the close, so no frame is built for it after.
    owner_feed.forget(ws)
    watch.release()
    try:
        await asyncio.wait_for(ws.close(code=1008, reason="Unauthorized"), timeout=_SIGNED_OUT_CLOSE_TIMEOUT_S)
    except Exception:
        logging.debug("Could not close a signed-out owner feed", exc_info=True)
    logging.info("WS owner client signed out (%d remaining)", len(state.ws_owner_clients))


@router.websocket("/ws/aircraft/owner")
async def websocket_aircraft_owner(ws: WebSocket):
    """Per-owner aircraft feed — only aircraft/arcs from the caller's own nodes.

    Authenticated by the auth_token cookie alone, the same session as the
    dashboard (sent automatically on same-origin WS handshakes); WS_AUTH_TOKEN
    is not asked for here. Unlike the public feeds this is true server-side
    data isolation: the connection never receives data for nodes the user
    doesn't own.

    Neither the session nor what it owns is settled once at the handshake. The
    broadcast reads ownership again before every frame, so a node released or
    handed on drops out of the next one (services/owner_feed.py). And a socket
    a cookie admitted is closed with 1008 as soon as that cookie stops signing
    its account in: signed out, revoked, signed out everywhere, or the account
    deactivated (_close_when_signed_out).

    A handshake from a page that is not the console's is refused before the
    cookie is read (core/origins.py).
    """
    if foreign_origin(ws.headers.get("origin")):
        await ws.close(code=1008, reason="Forbidden origin")
        return
    cookie = None
    account_id = None
    if AUTH_BYPASS:
        # The anonymous-admin bypass is opted into: that admin owns whatever
        # rows are assigned to the all-zero uuid (usually nothing), and has no
        # session that could end.
        user_id = ANONYMOUS_USER["id"]
    else:
        cookie = ws.cookies.get(sessions.COOKIE_NAME)
        user = await read_user_from_token(cookie)
        if user is None or not user.is_active:
            await ws.close(code=1008, reason="Unauthorized")
            return
        account_id = user.id
        user_id = str(user.id)
    owned = set(await get_user_nodes(user_id))

    await ws.accept()
    owner_feed.admit(ws, user_id, owned)
    logging.info("WS owner client connected (%d nodes, %d total)", len(owned), len(state.ws_owner_clients))
    watch = None
    guard = None
    try:
        if account_id is not None:
            watch = sessions.watch_account(account_id)
            guard = asyncio.create_task(_close_when_signed_out(ws, cookie, account_id, watch))
        if state.latest_aircraft_json.get("aircraft"):
            snapshot = owner_bytes(state.latest_aircraft_json, owned)
            await ws.send_text(snapshot.decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # NOT a client disconnect — a bug here (e.g. in payload filtering)
        # used to be swallowed and read as one.
        logging.debug("websocket handler error", exc_info=True)
    finally:
        # Everything that must not outlive the socket goes before the await
        # below, which a cancelled handler may not get past.
        if guard is not None:
            guard.cancel()
        if watch is not None:
            watch.release()
        owner_feed.forget(ws)
        logging.info("WS owner client disconnected (%d remaining)", len(state.ws_owner_clients))
        if guard is not None:
            # Unwound before the handler returns, so no check outlives the socket.
            (outcome,) = await asyncio.gather(guard, return_exceptions=True)
            if isinstance(outcome, Exception):
                logging.error("Owner feed session guard failed", exc_info=outcome)


class _SlotHoldingStream(StreamingResponse):
    """A StreamingResponse that gives its stream slot back when it ends, however it ends.

    Given back here rather than in the body's generator, whose `finally`
    never runs when the client goes away before the first chunk is asked of it.
    """

    def __init__(self, content: AsyncIterator[bytes], slot: AsyncExitStack, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._slot = slot

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._slot.aclose()


async def _aircraft_events(request: Request, principal: Principal) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    last_hash = ""
    sent = None
    written_at = loop.time()
    recheck_at = loop.time() + RECHECK_INTERVAL_S
    while True:
        if principal.internal:
            # Both the change probe and the body come from the public pair,
            # the published feed, as they always have.
            current_hash = str(state.latest_aircraft_json_public.get("now", 0))
            event = state.latest_aircraft_json_bytes if current_hash != last_hash else None
            last_hash = current_hash
        elif (event := _new_real_frame(sent)) is not None:
            sent = event
        if event is not None:
            yield b"data: " + event + b"\n\n"
            written_at = loop.time()
        elif loop.time() - written_at >= SSE_KEEPALIVE_S:
            yield b": keepalive\n\n"
            written_at = loop.time()
        if not principal.internal and loop.time() >= recheck_at:
            if await _refusal_now(request) is not None:
                return
            recheck_at = loop.time() + RECHECK_INTERVAL_S
        await asyncio.sleep(AIRCRAFT_FLUSH_INTERVAL_S)


@router.get("/api/radar/stream")
async def sse_aircraft_stream(request: Request, principal: Principal = Depends(require_data_access)):
    """Server-sent events of the aircraft feed, behind the data-access gate (core/data_access.py).

    Internal access, an administrator or the radar key, gets the feed as it
    was: the whole fleet's published frame each time its `now` moves. A key
    or a session gets the real-only frame each time a flush writes one, as
    one of its tier's streams open at once, and is asked about again every
    RECHECK_INTERVAL_S; a refusal ends the stream. Every refusal at the start,
    one past the stream limit included, answers as HTTP before any body.
    Either stream sends a `: keepalive` comment after SSE_KEEPALIVE_S without
    an event.
    """
    slot = AsyncExitStack()
    # Taken before the response starts, so `stream_limit` is a 429 the caller
    # sees; the response gives it back.
    await slot.enter_async_context(stream_slot(principal))
    return _SlotHoldingStream(
        _aircraft_events(request, principal),
        slot,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
