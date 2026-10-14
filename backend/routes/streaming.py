"""WebSocket and SSE live-streaming endpoints."""

import asyncio
import hmac
import logging
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core import sessions, state
from core.auth import get_user_nodes
from core.origins import foreign_origin
from core.users import ANONYMOUS_USER, AUTH_BYPASS, read_user_from_token
from services import owner_feed
from services.tasks.aircraft_flush import owner_bytes

router = APIRouter(tags=["radar"])

_WS_AUTH_TOKEN = os.getenv("WS_AUTH_TOKEN", "")

# How long an open owner feed goes between checks of its cookie when nothing
# wakes it first. Every sign-out in core/sessions.py wakes it at once, so this
# only bounds a session that expires, or one ended behind that module's back.
SESSION_RECHECK_S = 30.0
# How long closing a signed-out owner feed may take. By then it is out of the
# broadcast, so a client too wedged to answer costs nothing but this.
_SIGNED_OUT_CLOSE_TIMEOUT_S = 5.0


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


@router.websocket("/ws/aircraft")
async def websocket_aircraft(ws: WebSocket):
    """Full aircraft feed — all nodes including synthetic simulation fleet."""
    if not await _authenticate_ws(ws):
        return
    await ws.accept()
    state.ws_clients.add(ws)
    logging.info("WebSocket client connected (%d total)", len(state.ws_clients))
    try:
        # Probed and sent from the public pair, which is what
        # latest_aircraft_json_bytes serialises: this socket is unauthenticated,
        # and the unredacted dict beside it belongs to the owner feed below.
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
    """
    if not await _authenticate_ws(ws):
        return
    await ws.accept()
    state.ws_live_clients.add(ws)
    logging.info("WS live client connected (%d total)", len(state.ws_live_clients))
    try:
        # Send current snapshot immediately so the map doesn't start blank
        if state.latest_real_aircraft_json_bytes:
            await ws.send_text(state.latest_real_aircraft_json_bytes.decode())
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        # NOT a client disconnect — a bug here (e.g. in payload filtering)
        # used to be swallowed and read as one.
        logging.debug("websocket handler error", exc_info=True)
    finally:
        state.ws_live_clients.discard(ws)
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


@router.get("/api/radar/stream")
async def sse_aircraft_stream():
    async def _generate():
        last_hash = ""
        while True:
            # Unauthenticated, so both the change probe and the body come from
            # the public pair.
            data = state.latest_aircraft_json_public
            current_hash = str(data.get("now", 0))
            if current_hash != last_hash:
                yield b"data: " + state.latest_aircraft_json_bytes + b"\n\n"
                last_hash = current_hash
            await asyncio.sleep(1)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
