"""Authentication routes: sign-in by emailed link, session, node ownership.

Sign-in is a link mailed to the address (services/mail.py); there is no
password and no third-party provider. JWT issuance and cookie management are
fully delegated to fastapi-users' JWTStrategy + CookieTransport.
"""

import logging
import os
import threading
from ipaddress import IPv6Address, ip_address, ip_network
from time import monotonic

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

from core import state
from core.auth import (
    ClaimOutcome,
    complete_claim,
    consume_magic_link,
    create_claim_code,
    create_magic_link,
    decline_claim,
    get_user_nodes,
    list_claim_codes,
    preview_claim,
    release_node,
    revoke_claim_code,
)
from core.users import (
    ACCESS_LOGOUT_PATH,
    ANONYMOUS_USER,
    JWT_LIFETIME_SECONDS,
    MagicLinkRefused,
    get_current_user,
    get_jwt_strategy,
    get_or_create_magic_link_user,
    has_access_session,
    user_to_dict,
)
from routes.sim_ingest import synthetic_fleet_enabled
from services import mail, publication
from services.node_claim_store import claim_addresses
from services.node_config import position_status
from services.node_refs import owner_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _fix_scheme(url: str) -> str:
    if os.getenv("FORCE_HTTPS", "true").lower() == "true":
        return url.replace("http://", "https://", 1)
    return url


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


async def _set_auth_cookie(response: Response, user) -> None:
    """Write the fastapi-users JWT into the auth_token cookie."""
    strategy = get_jwt_strategy()
    token = await strategy.write_token(user)
    response.set_cookie(
        "auth_token",
        token,
        max_age=JWT_LIFETIME_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


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


#: Where the mailed link lands. HOST_APP serves the map bundle at `/` and mounts
#: the dashboard under `/dash/` (deploy/nginx/nginx.conf.template), and the
#: dashboard's router carries a matching basename, so a link to `/auth/link/...`
#: would render the map and never redeem the token. This prefix and
#: dashboard/src/utils/basePath.ts have to agree.
_SIGN_IN_PATH = "/dash/auth/link/"


def _session_user(user_dict: dict) -> dict:
    """The user as the console holds it, from /me or straight from a sign-in.

    The sign-in pages adopt the user they are handed without asking /me again,
    so everything the console reads off its user has to be here for both.
    `synthetic_fleet` is the rule that mounts the fleet's ingest routes, so the
    physics layer is offered exactly where there is a fleet for it to draw.
    """
    return {**user_dict, "synthetic_fleet": synthetic_fleet_enabled(os.environ)}


class MagicLinkRequest(BaseModel):
    email: EmailStr


class MagicLinkConsume(BaseModel):
    token: str


def _sign_in_host() -> str:
    """The host the mailed link points at, or "" when it cannot be known."""
    return os.getenv("HOST_APP", "").strip()


def _sign_in_link(request: Request, token: str) -> str:
    """Build the mailed URL from configuration, never from the request.

    `request.base_url` derives from the Host header. An attacker who could set
    it would ask for a link to somebody else's address and have the mail carry
    a URL pointing at their own server: the victim clicks, and the token is
    handed over. The deployed vhosts only match known hostnames, so nginx
    closes that today, but a credential must not rest on that staying true.

    HOST_APP is the consolidated public surface and is already set per
    environment in every compose overlay, so this needs no new setting. Unset,
    the caller refuses the request rather than falling back on the request's own
    host: a fallback there would quietly restore the very thing this exists to
    prevent, the first time some start path forgot the variable.
    """
    return _fix_scheme(f"http://{_sign_in_host()}") + _SIGN_IN_PATH + token


def _sign_in_body(link: str) -> str:
    return (
        "Someone asked to sign in to RETINA with this address.\n\n"
        f"{link}\n\n"
        "The link works once and expires in 15 minutes.\n\n"
        "If this wasn't you, nothing has happened and you can ignore this message."
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
    if not _sign_in_host():
        logger.error("HOST_APP is not set; refusing to mail a link built from the request host")
        raise HTTPException(status_code=503, detail="Sign-in by email is unavailable")

    if _magic_link_quota_available(_client_source(request)):
        token = await create_magic_link(body.email)
        if token:
            link = _sign_in_link(request, token)
            mail.send_in_background(body.email, "Sign in to RETINA", _sign_in_body(link))

    return {"status": "accepted"}


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
        raise HTTPException(status_code=400, detail="That sign-in link is no longer valid")

    try:
        user = await get_or_create_magic_link_user(email)
    except MagicLinkRefused:
        # Same answer as a token that never existed. A distinct one would say
        # which addresses are privileged, and the link is spent either way.
        raise HTTPException(status_code=400, detail="That sign-in link is no longer valid") from None

    response = JSONResponse({"user": _session_user(user_to_dict(user))})
    await _set_auth_cookie(response, user)
    return response


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
async def consume_claim_link(body: ClaimToken):
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

    response = JSONResponse({"user": _session_user(user_to_dict(user)), "node_ref": node_ref})
    await _set_auth_cookie(response, user)
    return response


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
    """Hand a node back, so its next owner can claim it.

    Guarded the same way every owner-scoped route here is, so a node somebody
    else owns answers exactly as one that does not exist.
    """
    user = await _owned_node(request, node_id)
    if not await release_node(node_id, user["id"]):
        # Owned a moment ago and not now: a release that raced another release,
        # or an administrator reassigning it. Either way it is gone, which is
        # what was asked for.
        raise HTTPException(status_code=404, detail="Node not found")
    return {"ok": True}


# ── Session endpoints ─────────────────────────────────────────────────────────


@router.get("/me")
async def me(request: Request):
    user_dict = await get_current_user(request)
    # Delegated rather than short-circuiting on AUTH_BYPASS, so this agrees with
    # what require_admin decided: a verified Access assertion outranks the
    # bypass, and answering "Admin (no auth)" while the admin routes attribute a
    # real person would make the console wrong about its own session.
    anonymous = user_dict["id"] == ANONYMOUS_USER["id"]
    return {**_session_user(user_dict), "auth_enabled": not anonymous}


@router.post("/logout")
async def logout(request: Request):
    """End the session, and say so when the app cannot end it alone.

    Deleting auth_token is the whole job for a cookie session. On the admin
    hostnames it is not: identity comes from an Access assertion Cloudflare
    re-injects from a cookie on its own domain, which this app can neither read
    nor delete, so the next request would be verified and admitted again. The
    caller is handed the edge's logout path to visit instead, because only a
    top-level navigation there clears it.

    Both credentials are cleared rather than whichever one authenticated this
    request: a person may hold an Access session and an auth_token at once.
    """
    body = {"ok": True}
    if await has_access_session(request):
        body["redirect"] = ACCESS_LOGOUT_PATH
    response = JSONResponse(body)
    response.delete_cookie("auth_token", path="/")
    return response


# ── Node ownership self-service ───────────────────────────────────────────────


class LocationPrivacyUpdate(BaseModel):
    private: bool


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
    """
    user = await get_current_user(request)
    node_ids = await get_user_nodes(user["id"])
    out = []
    with state.connected_nodes_lock:
        snapshot = {nid: dict(state.connected_nodes.get(nid, {})) for nid in node_ids}
    # One pair of queries for the whole list rather than a lookup per node, and
    # read here rather than from services.publication's 30 s cache: this is the
    # page an owner has just changed the setting on, and showing them a stale
    # answer for up to half a minute is how a working switch reads as broken.
    privacy = await publication.location_privacy_map(node_ids)
    # One query for the list rather than a lookup per node, matching the privacy
    # map above. The address is what the owner was mailed to claim the node
    # with, so it is theirs to see; it is not published anywhere else.
    claimed_with = await claim_addresses(node_ids)
    for nid in node_ids:
        info = snapshot.get(nid) or {}
        cfg = info.get("config", {}) or {}
        private, source = privacy.get(nid, (False, publication.SOURCE_DEFAULT))
        out.append(
            {
                "node_id": nid,
                "node_ref": owner_identity(nid),
                "name": cfg.get("name", nid),
                "status": info.get("status", "never_connected"),
                "last_heartbeat": info.get("last_heartbeat"),
                "is_synthetic": info.get("is_synthetic", False),
                "rx_lat": cfg.get("rx_lat"),
                "rx_lon": cfg.get("rx_lon"),
                "position_status": position_status(cfg),
                "frequency": cfg.get("FC", cfg.get("frequency")),
                "location_private": private,
                "location_privacy_source": source,
                "claimed_with": claimed_with.get(nid),
            }
        )
    return out


@router.put("/me/nodes/{node_id}/location-privacy")
async def set_my_node_location_privacy(node_id: str, body: LocationPrivacyUpdate, request: Request):
    """Set this node's location privacy, overriding whatever registration said.

    The answer is always `override` as the source: writing the row is what this
    route does, and it outranks the registration choice whichever way the two
    happen to agree.  An owner who wants the registration choice back sends the
    DELETE below rather than a PUT that matches it, so that a later reflash
    changing the registration choice still reaches them.
    """
    user = await _owned_node(request, node_id)
    await publication.set_location_privacy(node_id, body.private, set_by=user["id"])
    # After the commit, so the next refresh cannot read the pre-write state and
    # cache it for another _TTL_S.
    publication.invalidate()
    return {
        "node_id": node_id,
        "location_private": body.private,
        "location_privacy_source": publication.SOURCE_OVERRIDE,
    }


@router.delete("/me/nodes/{node_id}/location-privacy")
async def clear_my_node_location_privacy(node_id: str, request: Request):
    """Drop the override and return the node to its registration choice.

    Returns the effective state rather than an `{"ok": true}`: what the node
    falls back to is the whole point of the call and the caller has no way to
    work it out, since a node that never registered and one registered public
    are both published and only the source tells them apart.
    """
    await _owned_node(request, node_id)
    await publication.clear_location_privacy(node_id)
    publication.invalidate()
    state_after = await publication.location_privacy(node_id)
    return {
        "node_id": node_id,
        "location_private": state_after["location_private"],
        "location_privacy_source": state_after["location_privacy_source"],
    }


@router.get("/me/claim-codes")
async def my_claim_codes(request: Request):
    user = await get_current_user(request)
    codes = await list_claim_codes(user["id"])
    codes.sort(key=lambda c: c.get("created_at", 0), reverse=True)
    return codes


@router.post("/me/claim-codes")
async def create_my_claim_code(request: Request):
    user = await get_current_user(request)
    try:
        return await create_claim_code(user["id"])
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e


@router.delete("/me/claim-codes/{code}")
async def revoke_my_claim_code(code: str, request: Request):
    user = await get_current_user(request)
    if not await revoke_claim_code(code, user["id"]):
        raise HTTPException(404, "Code not found, already used, or not yours")
    return {"ok": True}
