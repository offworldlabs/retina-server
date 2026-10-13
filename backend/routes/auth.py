"""Authentication routes: sign-in by emailed link, session, node ownership.

Sign-in is a link mailed to the address (services/mail.py); there is no
password and no third-party provider. A redeemed link opens a session held on
the server (core/sessions.py), whose token is the auth_token cookie, and a
signed-in account can list its sessions and end them here.
"""

import logging
import os
import re
import threading
from dataclasses import replace
from ipaddress import IPv6Address, ip_address, ip_network
from time import monotonic
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, EmailStr

from core import sessions, state
from core.auth import INTENT_SIGNIN, consume_magic_link, create_magic_link, get_user_nodes, peek_magic_link
from core.node_ids import POLLED_BLAH2, system_of
from core.users import (
    ACCESS_LOGOUT_PATH,
    ANONYMOUS_USER,
    MagicLinkRefused,
    User,
    UserSession,
    async_session_maker,
    get_current_user,
    get_or_create_magic_link_user,
    has_access_session,
    record_visit,
    user_to_dict,
)
from routes.sim_ingest import synthetic_fleet_enabled
from services import blah2_poller, mail, polled_registration, publication
from services.node_claim_store import claim_addresses
from services.node_claiming import (
    ClaimOutcome,
    complete_claim,
    decline_claim,
    preview_claim,
    release_node,
)
from services.node_config import carrier_hz, position_status
from services.node_refs import owner_identity, public_name
from services.polled_radars import owner_views, remove_polled_radar
from services.tasks.aircraft_flush import owner_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_source(request: Request) -> str:
    """Use the transport peer after the ASGI server's trusted-proxy handling.

    Never read forwarding headers here: deployed nginx appends its validated
    client address and uvicorn trusts only the configured proxy peer. IPv6
    addresses in one /64 share a quota, including privacy-address rotation.
    This is an admission limit rather than an identity; mobile clients may roam.
    """
    host = request.client.host if request.client else None
    if not host:
        return "unknown"
    try:
        address = ip_address(host)
    except ValueError:
        return host
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ip_network(f"{address}/64", strict=False))
    return str(address)


def _client_network(request: Request) -> str | None:
    """The network the caller is in, as a session records it: never the address itself."""
    return sessions.coarse_ip(request.client.host if request.client else None)


async def _open_session(user, request: Request, **extra) -> JSONResponse:
    """Answer with the signed-in user, and a new session's token in the auth_token cookie.

    Opening a session is itself a visit, and the answer carries it. Recorded
    once the session exists, so a sign-in that fails leaves no visit behind.
    Raises InactiveAccount, having written nothing, for an account deactivated
    since the caller last checked it.
    """
    token, session = await sessions.open_session(
        user, user_agent=request.headers.get("user-agent"), ip=_client_network(request)
    )
    await record_visit(user)
    response = JSONResponse({"user": _session_user(user_to_dict(user)), **extra})
    sessions.set_session_cookie(response, token, session)
    return response


async def _exchange_legacy_cookie(
    request: Request, response: Response, resolved: sessions.Resolved
) -> sessions.Resolved | None:
    """Swap a JWT cookie nobody has exchanged yet for a session, and set the new cookie on `response`.

    Returns what the cookie resolves to from now on, or None when the exchange
    opened nothing: another request exchanged or signed out the same cookie
    first, or the account was deactivated or signed out everywhere since.
    """
    exchanged = await sessions.exchange_legacy(
        resolved, user_agent=request.headers.get("user-agent"), ip=_client_network(request)
    )
    if exchanged is None:
        return None
    token, session = exchanged
    sessions.set_session_cookie(response, token, session)
    return replace(resolved, session=session)


# ── Magic links ───────────────────────────────────────────────────────────────

# Sign-in requests admitted per source per window. nginx already limits the
# credential endpoints to 5r/m, but only on the three vhosts carrying a
# `location /api/auth/`; the other six have a blanket `location /api/` at 30r/s
# and a direct caller has neither. Unbounded, that is a way to mail arbitrary
# strangers thirty times a second from our own domain, so the limit that
# matters lives here rather than only at the edge.
_MAGIC_LINK_WINDOW_S = 60.0
_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE = 5
# Distinct sources tracked at once: without a bound, a flood from many addresses
# is a way to grow this dict until the process dies. Full, no further source is
# admitted, so sign-in stops for the length of one window — which is the better
# of the two failures.
_MAX_MAGIC_LINK_SOURCES = 4096

_magic_link_requests: dict[str, list[float]] = {}
_magic_link_lock = threading.Lock()


def _magic_link_quota_available(source: str) -> bool:
    """Whether this source may ask for another link, recording it if so."""
    with _magic_link_lock:
        now = monotonic()
        for key, times in list(_magic_link_requests.items()):
            fresh = [t for t in times if t > now - _MAGIC_LINK_WINDOW_S]
            if fresh:
                _magic_link_requests[key] = fresh
            else:
                del _magic_link_requests[key]
        recent = _magic_link_requests.get(source)
        if recent is None:
            if len(_magic_link_requests) >= _MAX_MAGIC_LINK_SOURCES:
                logger.warning("Magic-link source table is full; refusing new sources")
                return False
            recent = _magic_link_requests.setdefault(source, [])
        if len(recent) >= _MAX_MAGIC_LINK_REQUESTS_PER_SOURCE:
            return False
        recent.append(now)
        return True


#: Where the mailed link lands: the console's own route (AuthLinkPage), at the
#: root of HOST_APP.
_SIGN_IN_PATH = "/auth/link/"

#: A console page for the link to open once redeemed. Anyone may ask for a link
#: to anyone's address, so this is a stranger's text in a genuine mail from us:
#: plain path characters only, and never `//`, which a browser reads as a host.
_NEXT_PATH = re.compile(r"/(?!/)[A-Za-z0-9/_.-]{0,200}")

#: The one answer for a sign-in link that is unknown, expired or spent, from the
#: preview and the redemption alike. The console shows it verbatim.
_DEAD_LINK = "That sign-in link is no longer valid"


def _session_user(user_dict: dict) -> dict:
    """The user as the console holds it, from /me or straight from a sign-in.

    The sign-in pages adopt the user they are handed without asking /me again,
    so everything the console reads off its user has to be here for both.
    `synthetic_fleet` is the rule that mounts the fleet's ingest routes, so the
    physics layer is offered exactly where there is a fleet for it to draw.
    `polled_radar_registration` offers the radar registration page only where
    its routes answer.
    """
    return {
        **user_dict,
        "synthetic_fleet": synthetic_fleet_enabled(os.environ),
        "polled_radar_registration": polled_registration.enabled(),
    }


class MagicLinkRequest(BaseModel):
    email: EmailStr
    #: The page the visitor asked for before being sent to sign in.
    next: str | None = None


class MagicLinkConsume(BaseModel):
    token: str


def _next_query(path: str | None) -> str:
    """The mailed link's `?next=`, or nothing for a value that is not a plain
    console path. Dropped rather than refused: a bad destination costs the
    visitor the page they wanted, not the sign-in."""
    if path is None or not _NEXT_PATH.fullmatch(path):
        return ""
    return "?" + urlencode({"next": path}, safe="/")


def _sign_in_body(link: str) -> str:
    return (
        "A request has been made to sign in to RETINA with this email address.\n\n"
        "Click this link to log in:\n"
        f"{link}\n\n"
        "The link works once and expires in 15 minutes.\n\n"
        "If this wasn't you, you may safely ignore this email."
    )


@router.post("/magic-link", status_code=202)
async def request_magic_link(body: MagicLinkRequest, request: Request):
    """Mail a sign-in link, if the address is one we should mail.

    Answers 202 identically whether or not anything was sent, and hands the
    send to a background thread so the response time does not vary either. The
    alternative is an oracle for which addresses own receivers.
    """
    # Both about this deployment, not about the address, so they are safe to
    # say. Checked before a token is minted, so a refusal leaves no row behind.
    if not mail.is_configured():
        raise HTTPException(status_code=503, detail="Sign-in by email is unavailable")
    if mail.link_to(_SIGN_IN_PATH) is None:
        logger.error("HOST_APP is not set; refusing to mail a link built from the request host")
        raise HTTPException(status_code=503, detail="Sign-in by email is unavailable")

    if _magic_link_quota_available(_client_source(request)):
        token = await create_magic_link(body.email)
        if token:
            link = mail.link_to(_SIGN_IN_PATH + token + _next_query(body.next))
            mail.send_in_background(body.email, "Sign in to RETINA", _sign_in_body(link))

    return {"status": "accepted"}


@router.get("/magic-link/{token}")
async def preview_magic_link(token: str):
    """Which address a sign-in link is for, without spending it.

    The landing page names the address and waits for a press before it
    redeems, so a scanner that renders the page and runs its script leaves the
    link alive. Reading it needs the same secret redeeming does, so it tells
    the holder nothing they could not learn by signing in.
    """
    link = await peek_magic_link(token, intent=INTENT_SIGNIN)
    if link is None:
        raise HTTPException(status_code=404, detail=_DEAD_LINK)
    return {"email": link.email}


@router.post("/magic-link/consume")
async def consume_magic_link_route(body: MagicLinkConsume, request: Request):
    """Redeem a link and open a session.

    A POST rather than the GET the mail links to: mail providers prefetch
    links, and a consuming GET would burn the token before the recipient ever
    clicked it. The mailed URL serves the page; the page calls this.
    """
    email = await consume_magic_link(body.token)
    if email is None:
        # One answer for unknown, expired and already-redeemed. Telling them
        # apart says which guesses were once real.
        raise HTTPException(status_code=400, detail=_DEAD_LINK)

    try:
        user = await get_or_create_magic_link_user(email)
        return await _open_session(user, request)
    except (MagicLinkRefused, sessions.InactiveAccount):
        # A deactivated account, refused before the session or, deactivated in
        # between, by it. Same answer as a token that never existed. A distinct
        # one would say which addresses are deactivated, and the link is spent
        # either way.
        raise HTTPException(status_code=400, detail=_DEAD_LINK) from None


# ── Claiming a node ───────────────────────────────────────────────────────────


class ClaimToken(BaseModel):
    token: str


@router.get("/claim/{token}")
async def preview_claim_link(token: str):
    """Which node a claim link is for, without spending it.

    The landing page names the node before either button is pressed, so that
    somebody mailed this by mistake declines something they can see. Reading it
    needs the same secret the click needs, so it tells the holder nothing they
    do not already have.
    """
    node_ref = await preview_claim(token)
    if node_ref is None:
        raise HTTPException(status_code=404, detail="That link is no longer valid")
    return {"node_ref": node_ref}


@router.post("/claim/consume")
async def consume_claim_link(body: ClaimToken, request: Request):
    """Redeem a claim link: bind the node, and sign the clicker in.

    A POST rather than the GET the mail links to. Mail providers and security
    scanners prefetch links, and a consuming GET would burn the token before the
    recipient ever clicked it. The mailed URL serves a page; the page calls this.
    """
    outcome, user, node_ref = await complete_claim(body.token)
    if outcome is ClaimOutcome.TAKEN:
        raise HTTPException(status_code=409, detail="That node already belongs to someone else")
    if outcome is not ClaimOutcome.BOUND:
        # Unknown, expired and already redeemed are one answer, so that trying
        # links cannot be used to learn which ones were ever real.
        raise HTTPException(status_code=400, detail="That link is no longer valid")

    try:
        return await _open_session(user, request, node_ref=node_ref)
    except sessions.InactiveAccount:
        # Deactivated after the node was bound, which is answered as an account
        # deactivated before is. Deactivation leaves an account's nodes with
        # it, as for an owner deactivated just after a claim.
        raise HTTPException(status_code=400, detail="That link is no longer valid") from None


@router.post("/claim/decline")
async def decline_claim_link(body: ClaimToken):
    """Refuse a claim, returning the node to unowned.

    This is what someone does when a stranger typed their address into a node.
    It needs no account and creates none: the whole point is that they want
    nothing to do with it.
    """
    if not await decline_claim(body.token):
        raise HTTPException(status_code=400, detail="That link is no longer valid")
    return {"ok": True}


@router.delete("/me/nodes/{node_id}/claim")
async def release_my_node(node_id: str, request: Request):
    """Hand a node back, so its next owner can claim it, or remove a polled radar.

    Guarded the same way every owner-scoped route here is, so a node somebody
    else owns answers exactly as one that does not exist. A polled radar is
    removed rather than handed back, since nothing can claim one afterwards
    (services/polled_radars.py, remove_polled_radar).
    """
    user = await _owned_node(request, node_id)
    if system_of(node_id) == POLLED_BLAH2:
        released = await _remove_polled_radar(node_id, user["id"])
    else:
        released = await release_node(node_id, user["id"])
    if not released:
        # Owned a moment ago and not now: a release that raced another release,
        # or an administrator reassigning it. Either way it is gone, which is
        # what was asked for.
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


async def _remove_polled_radar(node_id: str, user_id: str) -> bool:
    async with async_session_maker() as session:
        async with session.begin():
            if not await remove_polled_radar(session, node_id, user_id=user_id):
                return False
    polled_registration.let_go(node_id)
    return True


# ── Session endpoints ─────────────────────────────────────────────────────────


@router.get("/me")
async def me(request: Request, response: Response):
    user_dict = await get_current_user(request)
    resolved = await sessions.resolve_request(request)
    if resolved is not None and resolved.session is None:
        # A JWT cookie from before sessions, which every page load brings here
        # first: swapped for a session, so that signing out can end it. The
        # answer is the same either way, so a swap that cannot write (a locked
        # database, a full disk) is dropped and the next page load tries again.
        # A cookie another request has just exchanged keeps working as an alias
        # of the session that one opened.
        try:
            await _exchange_legacy_cookie(request, response, resolved)
        except Exception:
            logger.warning("Could not exchange a legacy cookie for a session", exc_info=True)
    # Delegated rather than short-circuiting on AUTH_BYPASS, so this agrees with
    # what require_admin decided: a verified Access assertion outranks the
    # bypass, and answering "Admin (no auth)" while the admin routes attribute a
    # real person would make the console wrong about its own session.
    anonymous = user_dict["id"] == ANONYMOUS_USER["id"]
    return {**_session_user(user_dict), "auth_enabled": not anonymous}


@router.post("/logout")
async def logout(request: Request):
    """End the session, and say so when the app cannot end it alone.

    Ending the auth_token's session on the server, so that no copy of the
    cookie works either, and deleting the cookie is the whole job for a cookie
    session. On the admin hostnames it is not: identity comes from an Access
    assertion Cloudflare re-injects from a cookie on its own domain, which this
    app can neither read nor delete, so the next request would be verified and
    admitted again. The caller is handed the edge's logout path to visit
    instead, because only a top-level navigation there clears it.

    Both credentials are cleared rather than whichever one authenticated this
    request: a person may hold an Access session and an auth_token at once.
    """
    body: dict[str, bool | str] = {"ok": True}
    if await has_access_session(request):
        body["redirect"] = ACCESS_LOGOUT_PATH
    resolved = await sessions.resolve_request(request)
    if resolved is not None:
        await sessions.end(resolved)
    response = JSONResponse(body)
    sessions.clear_session_cookie(response)
    return response


async def _signed_in_session(request: Request, response: Response) -> tuple[User, UserSession]:
    """The account the auth_token cookie signs in, and the session it is. 401 if none.

    The cookie's alone: an Access identity or the anonymous admin holds no
    session here to list or end. A JWT cookie nobody has exchanged is exchanged
    first, so there is always a session to call current.
    """
    resolved = await sessions.resolve_request(request)
    if resolved is not None and resolved.session is None:
        exchanged = await _exchange_legacy_cookie(request, response, resolved)
        # None when another request exchanged or signed out the same cookie
        # first. Resolved again, as it may now alias the session that one opened.
        resolved = exchanged or await sessions.resolve(request.cookies.get(sessions.COOKIE_NAME))
    if resolved is None or resolved.session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await record_visit(resolved.user)
    return resolved.user, resolved.session


@router.get("/me/sessions")
async def my_sessions(request: Request, response: Response):
    """The caller's signed-in browsers, the most recently used first.

    Enough to tell one's own devices apart and end the ones that should not be
    signed in: a label for the browser and the network it was in, never the
    token or its hash. `id` is what a revocation names, and `current` marks the
    session asking.
    """
    user, current = await _signed_in_session(request, response)
    return [
        {
            "id": row.id,
            "created_at": row.created_at.timestamp(),
            "last_used_at": row.last_used_at.timestamp(),
            "expires_at": row.expires_at.timestamp(),
            "user_agent": row.user_agent,
            "ip": row.ip,
            "current": row.id == current.id,
        }
        for row in await sessions.list_sessions(user.id)
    ]


@router.delete("/me/sessions/{session_id}")
async def revoke_my_session(session_id: str, request: Request, response: Response):
    """Sign one of the caller's sessions out.

    Another account's session answers as one that does not exist. Ending the
    caller's own is signing out, so its cookie goes as well.
    """
    user, current = await _signed_in_session(request, response)
    if not await sessions.revoke_session(user.id, session_id):
        # Answered rather than raised, so that a cookie exchanged on the way in
        # still reaches the browser.
        response.status_code = 404
        return {"detail": "Session not found"}
    if session_id == current.id:
        sessions.clear_session_cookie(response)
    return {"ok": True}


@router.post("/me/sessions/revoke-others")
async def revoke_my_other_sessions(request: Request, response: Response):
    """Sign the caller out everywhere but here, answering how many sessions ended.

    Every JWT cookie the account still has out goes with them, including the
    one this session was exchanged from (core/sessions.py, revoke_other_sessions).
    """
    user, current = await _signed_in_session(request, response)
    return {"revoked": await sessions.revoke_other_sessions(user.id, current.id)}


# ── Node ownership self-service ───────────────────────────────────────────────


async def _owned_node(request: Request, node_id: str) -> dict:
    """The caller, having established they own `node_id`.  404 if they do not.

    404 rather than 403, deliberately, and the same wording as a node that does
    not exist: the two answers differ only in confirming the id is real, node
    ids are guessable, and an id that resolves is already a hint about where a
    receiver is.  The per-node analytics route makes the same trade for the same
    reason.
    """
    user = await get_current_user(request)
    if node_id not in await get_user_nodes(user["id"]):
        raise HTTPException(status_code=404, detail="Node not found")
    return user


@router.get("/me/nodes")
async def my_nodes(request: Request):
    """The caller's own nodes, under both identifiers.

    Authenticated and scoped to the owner, so the node_id is theirs to see and
    stays. The ref rides along because every public surface is keyed on it, and
    a consumer merging this list with one of those needs one key space rather
    than the same node twice under two. It is null for a node with no published
    handle, which is a node that appears on no public surface either.

    `polled` is null but for a polled radar, whose address it carries: the
    owner's to see, as claimed_with is.
    """
    user = await get_current_user(request)
    node_ids = await get_user_nodes(user["id"])
    out = []
    with state.connected_nodes_lock:
        snapshot = {nid: dict(state.connected_nodes.get(nid, {})) for nid in node_ids}
    private = await publication.registered_private(node_ids)
    # One query for the list rather than a lookup per node. The address is what
    # the owner was mailed to claim the node with, so it is theirs to see; it is
    # not published anywhere else.
    claimed_with = await claim_addresses(node_ids)
    async with async_session_maker() as session:
        polled = await owner_views(session, [nid for nid in node_ids if system_of(nid) == POLLED_BLAH2])
    for nid in node_ids:
        info = snapshot.get(nid) or {}
        cfg = info.get("config", {}) or {}
        ref = owner_identity(nid)
        radar = polled.get(nid)
        status = info.get("status", "never_connected")
        # The poller knows within a few polls, where the offline sweep waits out
        # its threshold. Here rather than in connected_nodes, since the sweep
        # logs a node going offline only when it is the one to mark it so.
        # Liveness never says online on its own: it is the last the poller
        # wrote, and stands still where nothing polls.
        if radar is not None and radar["liveness"] == blah2_poller.UNREACHABLE:
            status = "disconnected"
        out.append(
            {
                "node_id": nid,
                "node_ref": ref,
                # The console names a node by its ref, so an unnamed node, or
                # one whose name is a node id, is called by that instead.
                "name": public_name(cfg.get("name"), ref, node_ids),
                "status": status,
                "last_heartbeat": info.get("last_heartbeat"),
                # Here as well as on /api/radar/nodes, which leaves private nodes out.
                "config_hash": info.get("config_hash"),
                "is_synthetic": info.get("is_synthetic", False),
                "rx_lat": cfg.get("rx_lat"),
                "rx_lon": cfg.get("rx_lon"),
                "position_status": position_status(cfg),
                "frequency": carrier_hz(cfg),
                "location_private": nid in private,
                "claimed_with": claimed_with.get(nid),
                "polled": radar,
            }
        )
    return out


@router.get("/me/aircraft")
async def my_aircraft(request: Request):
    """The owner feed's current frame: aircraft and arcs from the caller's nodes.

    The snapshot /ws/aircraft/owner sends on connect, for a page that polls.
    Filtered from the unredacted frame, because the public feed drops a private
    node's single-node tracks, and a polled radar on probation counts as
    private, so its owner would find nothing of their own there.
    """
    user = await get_current_user(request)
    owned = set(await get_user_nodes(user["id"]))
    return Response(content=owner_bytes(state.latest_aircraft_json, owned), media_type="application/json")
