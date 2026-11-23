"""The keyed live streams: the real fleet's aircraft over a websocket and as server-sent events.

Both stand behind the data-access gate (core/data_access.py) and read the
live-search index (services/live_search.py), so they serve the real fleet
only, to every caller, as readsb rows narrowed by the live filters that
routes/data_v1.py parses. Each polls the index every POLL_INTERVAL_S and sends
when a flush has replaced it, so a stream costs one filtered search per flush.

An open stream holds one of its caller's stream slots (data_access.stream_slot)
for as long as it is open, and asks check_data_access again every
data_access.RECHECK_INTERVAL_S: a key revoked, or an entitlement lapsed or
ended, closes it within one re-check. A re-check that cannot be read keeps the
stream until the next one, as the owner feed does (routes/streaming.py): access
ends only by a write the failed read did not see, so the failure is no sign of
one.

No key reaches a log line, a message, a close reason or an event: nothing here
logs the URL, the query or the headers, and refusals carry the gate's reason
and message, which never repeat what was presented.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.requests import HTTPConnection
from fastapi.responses import StreamingResponse
from starlette.types import Message

from core import data_access
from core.data_access import DataAccessDenied, Principal, refuse_upgrade, require_data_access, ws_data_access
from routes.data_v1 import KEYED_RESPONSES, LIVE_FILTER_KEYS, KeyedRefusal, live_filters_query, parse_live_filters
from services.live_search import LiveFilters, LiveIndex, live_index

logger = logging.getLogger(__name__)

router = APIRouter(tags=["keyed data"])

# Both read at every use, so a test can lower them.
POLL_INTERVAL_S = 1.0
SSE_KEEPALIVE_S = 15.0

REASON_INVALID_FILTERS = "invalid_filters"
REASON_BAD_MESSAGE = "bad_message"

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_KEEPALIVE = b": keepalive\n\n"

# The filters whose value is text in the query; the rest are numbers.
_TEXT_FILTERS = frozenset({"bbox", "hex", "callsign"})
_FILTER_NAMES = ", ".join(sorted(LIVE_FILTER_KEYS))
_FILTER_VALUES = (
    "bbox is text or a list of 4 numbers [south, west, north, east], hex text or a list of text, "
    "callsign text and every other filter a number; null is no such filter"
)
_BAD_MESSAGE = 'A message must be a JSON object of type "filters", sent as text.'


# ── Frames ───────────────────────────────────────────────────────────────────


def _aircraft(index: LiveIndex, filters: LiveFilters) -> dict[str, Any]:
    return {"now": index.now, "aircraft": index.search(filters)}


def _error(reason: str, message: str) -> dict[str, str]:
    return {"type": "error", "reason": reason, "message": message}


async def _send(websocket: WebSocket, frame: Mapping[str, Any]) -> None:
    await websocket.send_text(orjson.dumps(frame).decode())


def _event(name: str, data: Any) -> bytes:
    # orjson writes no newline, so the data is one line.
    return b"event: " + name.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


async def _recheck(conn: HTTPConnection) -> DataAccessDenied | None:
    """The gate's refusal of the caller now, or None while it still passes or cannot be read."""
    try:
        await data_access.check_data_access(conn)
    except DataAccessDenied as denied:
        return denied
    except Exception:
        logger.warning("Could not re-check a keyed stream's access", exc_info=True)
    return None


# ── Filters a socket's client sends ──────────────────────────────────────────


class _Refused(Exception):
    """A client message the socket answers with an error frame, staying open."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.frame = _error(reason, message)


def _invalid_filters(rule: str) -> _Refused:
    return _Refused(REASON_INVALID_FILTERS, f"{rule}. The filters are {_FILTER_NAMES}.")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _filter_value(name: str, value: Any) -> str | float:
    """A filter's value from a message, as the query would carry it."""
    if isinstance(value, str) or (_is_number(value) and name not in _TEXT_FILTERS):
        return value
    if name == "bbox" and isinstance(value, list) and len(value) == 4 and all(_is_number(v) for v in value):
        return ",".join(str(v) for v in value)
    if name == "hex" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return ",".join(value)
    raise _invalid_filters(_FILTER_VALUES)


def _filters_from(message: Message) -> LiveFilters:
    """The filters a client message asks for, or _Refused saying why it cannot be taken.

    The refusal names the rule broken, never what was sent.
    """
    text = message.get("text")
    try:
        body = orjson.loads(text) if isinstance(text, str) else None
    except orjson.JSONDecodeError:
        body = None
    if not isinstance(body, dict) or body.get("type") != "filters":
        raise _Refused(REASON_BAD_MESSAGE, _BAD_MESSAGE)
    values: dict[str, str | float] = {}
    for name, value in body.items():
        if name == "type":
            continue
        if name not in LIVE_FILTER_KEYS:
            raise _invalid_filters("A filters message takes its type and the filters alone")
        if value is not None:
            values[name] = _filter_value(name, value)
    try:
        return parse_live_filters(values)
    except HTTPException as refused:
        raise _invalid_filters(str(refused.detail)) from None


# ── The websocket ────────────────────────────────────────────────────────────


async def _serve_socket(websocket: WebSocket, filters: LiveFilters) -> None:
    """Frames until the client goes, a send fails (WebSocketDisconnect) or a re-check refuses the caller.

    This task alone sends. Client messages are read one receive at a time,
    awaited beside the next deadline, and the receive pending on the way out is
    cancelled and awaited, so nothing outlives the socket. A message never
    delays the deadlines: they are checked after every wake.
    """
    loop = asyncio.get_running_loop()
    index = live_index()
    await _send(websocket, {"type": "aircraft", **_aircraft(index, filters)})
    sent_now = index.now
    next_poll = loop.time() + POLL_INTERVAL_S
    next_check = loop.time() + data_access.RECHECK_INTERVAL_S
    receiving = asyncio.create_task(websocket.receive())
    try:
        while True:
            await asyncio.wait({receiving}, timeout=max(0.0, min(next_poll, next_check) - loop.time()))
            if receiving.done():
                message = receiving.result()
                if message["type"] == "websocket.disconnect":
                    return
                receiving = asyncio.create_task(websocket.receive())
                try:
                    filters = _filters_from(message)
                except _Refused as refused:
                    await _send(websocket, refused.frame)
                else:
                    index = live_index()
                    await _send(websocket, {"type": "aircraft", **_aircraft(index, filters)})
                    sent_now = index.now
            now = loop.time()
            if now >= next_check:
                next_check = now + data_access.RECHECK_INTERVAL_S
                if (denied := await _recheck(websocket)) is not None:
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=denied.reason)
                    return
            if now >= next_poll:
                next_poll = now + POLL_INTERVAL_S
                index = live_index()
                if index.now != sent_now:
                    await _send(websocket, {"type": "aircraft", **_aircraft(index, filters)})
                    sent_now = index.now
    finally:
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)


@router.websocket("/ws/v1/aircraft")
async def aircraft_socket(websocket: WebSocket, principal: Principal = Depends(ws_data_access)) -> None:
    """The real fleet's live aircraft over a websocket, narrowed by the live filters.

    The gate admits the socket before this runs (ws_data_access): a key in
    `?apikey=` or a key header, a session from the console's own pages, or
    internal access. The live filters in the query apply from the first frame;
    `apikey` and any other parameter are ignored.

    Server to client, JSON text:

    - `{"type": "aircraft", "now", "aircraft"}`, as /api/v1/aircraft answers:
      at once, on every new flush, and at once after a filters message is taken.
    - `{"type": "error", "reason", "message"}`: `invalid_filters` for a filters
      message refused, whose previous filters stay; `bad_message` for anything
      else (not JSON text, not an object, another type, binary). The socket
      stays open.

    Client to server: `{"type": "filters", <filter>: <value>, ...}` replaces the
    filters wholly. A value is a number or text, bbox also a list of 4 numbers
    [south, west, north, east] and hex also a list of text; null or absent is no
    such filter.

    Refused before accept, as an HTTP response with the status, headers and
    body a REST call gets, and no socket opens (data_access.refuse_upgrade):

    - 401, 403 or 429 `rate_limited` with the gate's reason
      (`missing_credentials`, `invalid_key`, `not_entitled`, `lapsed`,
      `grant_ended`, `forbidden_origin`).
    - 422 naming the rule a filter in the query breaks, as /api/v1/aircraft
      does; no slot is taken.
    - 429 `stream_limit`: the caller's streams are all open.

    Once open, it is closed 1008 with the gate's reason (`invalid_key`,
    `not_entitled`, `lapsed`, `grant_ended`, `missing_credentials`) when a
    re-check refuses the caller.
    """
    query = {name: value for name, value in websocket.query_params.items() if name in LIVE_FILTER_KEYS}
    async with AsyncExitStack() as held:
        try:
            filters = parse_live_filters(query)
            await held.enter_async_context(data_access.stream_slot(principal))
        except HTTPException as refused:
            # Before accept, so the client gets the 422 or the 429 a REST call would.
            refuse_upgrade(refused)
        try:
            await websocket.accept()
            await _serve_socket(websocket, filters)
        except WebSocketDisconnect:
            # The client went away, or a send to it failed.
            pass


# ── The event stream ─────────────────────────────────────────────────────────


async def _response_scope() -> AsyncIterator[AsyncExitStack]:
    """An exit stack left once the response has ended, however it ends.

    A dependency with yield in the request's scope, so FastAPI holds it on the
    request's own exit stack, which encloses the route and the sending of its
    response alike. What a route enters here is left when the stream ends, the
    client goes (before the first byte or after), an exception or a
    cancellation ends the request, or a response is built and never sent. A
    `finally` in the body's async generator would not do: it runs only once the
    generator has started, and one left waiting at a `yield` is closed only
    when the garbage collector gets to it.
    """
    async with AsyncExitStack() as stack:
        yield stack


async def _events(request: Request, filters: LiveFilters) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    sent_now: float | None = None
    next_poll = loop.time()
    next_check = next_poll + data_access.RECHECK_INTERVAL_S
    quiet_until = next_poll + SSE_KEEPALIVE_S
    while True:
        await asyncio.sleep(max(0.0, min(next_poll, next_check, quiet_until) - loop.time()))
        now = loop.time()
        if now >= next_check:
            next_check = now + data_access.RECHECK_INTERVAL_S
            if (denied := await _recheck(request)) is not None:
                yield _event("error", denied.detail)
                return
        if now >= next_poll:
            next_poll = now + POLL_INTERVAL_S
            index = live_index()
            if index.now != sent_now:
                sent_now = index.now
                yield _event("aircraft", _aircraft(index, filters))
                quiet_until = loop.time() + SSE_KEEPALIVE_S
        if loop.time() >= quiet_until:
            yield _KEEPALIVE
            quiet_until = loop.time() + SSE_KEEPALIVE_S


@router.get(
    "/api/v1/stream",
    response_class=StreamingResponse,
    responses={
        **KEYED_RESPONSES,
        429: {
            "model": KeyedRefusal,
            "description": "`rate_limited`: over the tier's requests a minute (see the Retry-After and X-RateLimit-* "
            "headers); or `stream_limit`: more streams open than the tier allows (see the Retry-After header).",
        },
        200: {
            "content": {"text/event-stream": {}},
            "description": "Server-sent events: `aircraft` and `error` events, and `keepalive` comments.",
        },
    },
)
async def aircraft_stream(
    request: Request,
    principal: Principal = Depends(require_data_access),
    filters: LiveFilters = Depends(live_filters_query),
    response_scope: AsyncExitStack = Depends(_response_scope, scope="request"),
) -> StreamingResponse:
    """The real fleet's live aircraft as server-sent events, narrowed by the filters; none given is the whole feed.

    - `event: aircraft`, data `{"now", "aircraft"}` as /api/v1/aircraft
      answers: at once, then on every new flush of the feed (about once a
      second).
    - `event: error`, data `{"reason", "message"}`: access is checked again
      every 30 s, and once the key is revoked (`invalid_key`) or its owner no
      longer entitled (`not_entitled`, `lapsed`, `grant_ended`) this is the
      last event and the stream ends.
    - `: keepalive`, a comment, after 15 s without an event.

    An open stream holds one of the tier's concurrent streams: one past them is
    a 429 with the reason `stream_limit`.
    """
    # Entered before the response starts, so a 429 `stream_limit` goes out as the gate's JSON refusal.
    await response_scope.enter_async_context(data_access.stream_slot(principal))
    return StreamingResponse(_events(request, filters), media_type="text/event-stream", headers=_SSE_HEADERS)
