"""Aircraft JSON flush + WebSocket broadcast — runs at ~1 Hz."""

import asyncio
import concurrent.futures
import logging
import os
import time

import orjson

from config.constants import AIRCRAFT_FLUSH_INTERVAL_S
from core import state
from services import node_refs
from services.frame_processor import build_combined_aircraft_json
from services.publication import public_aircraft_payload

_TAR1090_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "tar1090_data",
)

_aircraft_flush_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="aircraft-flush",
)


def published_bytes(payload: dict) -> bytes:
    """Serialise an already-filtered payload under its published identities.

    Substitution is the last step on every publication path.  Every filter it
    follows (the private-node redaction, the real-only filter, the per-owner
    one) matches `node_id` against a set of `node_id`, so a payload that
    reached them already carrying refs would match nothing and the feed would
    come back empty.
    """
    return orjson.dumps(node_refs.substitute_identities(payload), option=orjson.OPT_SERIALIZE_NUMPY)


def filter_payload_to_nodes(aircraft_data: dict, node_ids: set[str]) -> dict:
    """Build a slim WS payload containing only aircraft/arcs for `node_ids`.

    An aircraft is included if it was detected by one of these nodes directly,
    or if it's a multinode solution any of whose contributing nodes is ours.

    Still keyed by `node_id`; callers publish it through `published_bytes`.
    """
    matched_aircraft = [
        ac
        for ac in aircraft_data.get("aircraft", [])
        if ac.get("node_id") in node_ids
        or (ac.get("multinode") and any(nid in node_ids for nid in ac.get("contributing_node_ids", [])))
    ]
    matched_arcs = [arc for arc in aircraft_data.get("detection_arcs", []) if arc.get("node_id") in node_ids]
    return {
        "now": aircraft_data.get("now", 0),
        "messages": len(matched_aircraft),
        "aircraft": matched_aircraft,
        "detection_arcs": matched_arcs,
        # Debug/simulation surfaces stay off the filtered feeds, same as
        # ground truth: detecting_nodes reveals the synthetic fleet's view.
        "detecting_nodes": {},
        "ground_truth": {},
        "ground_truth_meta": {},
        "anomaly_hexes": [],
    }


def _real_only_dict(aircraft_data: dict) -> dict:
    """Build a slim WS payload filtered to non-synthetic nodes only."""
    with state.connected_nodes_lock:
        real_node_ids = {nid for nid, info in state.connected_nodes.items() if not info.get("is_synthetic", True)}
    return filter_payload_to_nodes(aircraft_data, real_node_ids)


# Per-client send budget for one broadcast.  The sends are fanned out with
# asyncio.gather, so this is the bound on the WHOLE broadcast rather than on
# each client in turn: before the fan-out, N wedged clients cost N x 5 s of
# serialised wall time inside the flush task, and every store GC that used to
# hang off the feed build stopped with it.
WS_SEND_TIMEOUT_S = 5.0


async def _fan_out_sends(pairs: list) -> set:
    """Send each (websocket, payload) pair concurrently; return the failures.

    Returns the set of clients whose send raised or timed out — the caller
    treats them exactly as the old per-client ``except`` branch did (drop from
    the registry, then close).  Timeouts are counted separately: a send that
    raises is a client that has already gone away, a send that times out is one
    that was still holding the broadcast open.
    """
    if not pairs:
        return set()
    results = await asyncio.gather(
        *(asyncio.wait_for(ws.send_text(payload), timeout=WS_SEND_TIMEOUT_S) for ws, payload in pairs),
        return_exceptions=True,
    )
    stale = set()
    for (ws, _payload), result in zip(pairs, results):
        if isinstance(result, BaseException):
            if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
                state.bump_counter("ws_send_timeouts")
            stale.add(ws)
    return stale


async def _close_all(clients) -> None:
    """Close dropped clients concurrently, ignoring every failure.

    Concurrent for the same reason the sends are: a client wedged badly enough
    to miss its send is exactly the one whose close can hang, and a serial
    close loop would just move the stall one line down.
    """
    if not clients:
        return
    await asyncio.gather(
        *(asyncio.wait_for(ws.close(), timeout=WS_SEND_TIMEOUT_S) for ws in clients),
        return_exceptions=True,
    )


def _flush_once(aircraft_data: dict) -> tuple[dict, bytes]:
    """Build one frame's published payloads and store them on state.

    This is where the public and owner paths part company.  ``aircraft_data`` is
    the whole fleet as built; ``public_data`` is the same feed with nodes whose
    owners registered them ``private`` taken out of it (services/publication.py).
    Every unauthenticated surface reads the public one — the full and real-only
    websockets, /api/radar/aircraft, the SSE stream — and the per-owner feed
    reads the unredacted one, because an owner's own private node is precisely
    what that feed exists to show them.  Splitting here rather than inside
    build_combined_aircraft_json is what makes that possible: the filter has to
    be able to see an entry before deciding the caller may have it.

    ``latest_aircraft_json`` therefore keeps the frame unredacted and
    unsubstituted; the owner filter reads it from there.

    All of them are stored on state, because a websocket that connects between
    flushes is served its opening snapshot from there (routes/streaming.py) and
    must get the same payload the broadcast would have given it.
    """
    public_data = public_aircraft_payload(aircraft_data)
    # Built from the redacted dict while it still carries node_id: the
    # real-only filter matches ids against state.connected_nodes.
    real_bytes = published_bytes(_real_only_dict(public_data))
    public_out = node_refs.substitute_identities(public_data)

    state.latest_aircraft_json = aircraft_data
    state.latest_aircraft_json_public = public_out
    state.latest_aircraft_json_bytes = orjson.dumps(public_out, option=orjson.OPT_SERIALIZE_NUMPY)
    state.latest_real_aircraft_json_bytes = real_bytes
    return public_out, real_bytes


async def broadcast_aircraft(aircraft_data: dict):
    """Push updated aircraft data to all connected WebSocket clients.

    All three client sets are sent to with _fan_out_sends, so the broadcast
    costs one WS_SEND_TIMEOUT_S at worst no matter how many clients are wedged.
    """
    public_out, real_bytes = _flush_once(aircraft_data)

    if state.ws_live_clients:
        real_payload = real_bytes.decode()
        stale_live = await _fan_out_sends([(ws, real_payload) for ws in list(state.ws_live_clients)])
        state.ws_live_clients.difference_update(stale_live)
        await _close_all(stale_live)

    if state.ws_owner_clients:
        # The per-owner payload is built BEFORE the gather, not inside it: the
        # filtering is per-client CPU on the event loop either way, but doing
        # it here keeps the sends themselves concurrent.  A client whose filter
        # raises is stale without ever being sent to, which is what the old
        # try-block around both steps did.
        owner_pairs = []
        stale_owner = set()
        for ws, owned in list(state.ws_owner_clients.items()):
            try:
                owner_pairs.append((ws, published_bytes(filter_payload_to_nodes(aircraft_data, owned)).decode()))
            except Exception:
                stale_owner.add(ws)
        stale_owner |= await _fan_out_sends(owner_pairs)
        for ws in stale_owner:
            state.ws_owner_clients.pop(ws, None)
        await _close_all(stale_owner)

    if not state.ws_clients:
        return
    # The full feed (map + simulation fleet) is unauthenticated, so it is built
    # from the published payload like every other public surface — "full" here
    # means "not filtered to real nodes", not "not filtered at all".
    gt_full = public_out.get("ground_truth") or {}
    gt_slim = {hex_code: [positions[-1]] for hex_code, positions in gt_full.items() if positions}
    slim_data = {**public_out, "ground_truth": gt_slim}
    payload = orjson.dumps(slim_data, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    stale = await _fan_out_sends([(ws, payload) for ws in list(state.ws_clients)])
    state.ws_clients.difference_update(stale)
    await _close_all(stale)


async def aircraft_flush_task(default_pipeline):
    """Write aircraft.json to disk and broadcast via WS at ~1 Hz."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(AIRCRAFT_FLUSH_INTERVAL_S)
        if not state.aircraft_dirty:
            continue
        state.aircraft_dirty = False
        try:

            def _build_and_serialize():
                data = build_combined_aircraft_json(default_pipeline)
                # The on-disk copy is the tar1090 file layout, i.e. a document
                # meant to be handed to a viewer — so it gets the redacted
                # payload under published identities, not the one the owner feed
                # reads.  Nothing routes to it today; that is a reason to write
                # the safe version now rather than to leave a true one for
                # whoever points a webserver at this directory later.
                disk_bytes = published_bytes(public_aircraft_payload(data))
                aircraft_path = os.path.join(_TAR1090_DATA_DIR, "aircraft.json")
                # tmp + os.replace: an in-place truncating write let any
                # HTTP/tar1090 reader observe a half-written file.
                tmp_path = aircraft_path + ".tmp"
                with open(tmp_path, "wb") as f:
                    f.write(disk_bytes)
                os.replace(tmp_path, aircraft_path)
                return data

            aircraft_data = await loop.run_in_executor(
                _aircraft_flush_executor,
                _build_and_serialize,
            )
            await broadcast_aircraft(aircraft_data)
            state.task_last_success["aircraft_flush"] = time.time()
        except Exception:
            state.bump_task_error("aircraft_flush")
            logging.exception("Aircraft flush failed")
