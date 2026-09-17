"""OAuth2 authentication routes — Google & GitHub SSO.

The OAuth flow is implemented here (custom routes keep the URL paths stable
so the frontend needs no changes). JWT issuance and cookie management are
fully delegated to fastapi-users' JWTStrategy + CookieTransport.
"""

import logging
import math
import os
import secrets
import threading
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address, ip_network
from time import monotonic
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr

from core import state
from core.auth import (
    consume_invite_for_email,
    consume_magic_link,
    create_claim_code,
    create_magic_link,
    get_user_nodes,
    list_claim_codes,
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
    get_or_create_oauth_user,
    has_access_session,
    user_to_dict,
)
from services import mail, publication
from services.node_config import position_status
from services.node_refs import owner_identity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")

# State must be bound to the browser AND consumed once: signing a redirect
# alone lets another browser complete a login it never started. The deployment
# already requires one worker (deploy/start.sh), so short-lived challenges can
# live here. A process restart invalidates pending logins; the user starts again.
_OAUTH_STATE_TTL_S = 600
_MAX_OAUTH_STATES = 4096
# Main/API nginx vhosts already limit credentials to 5 requests/minute per IP.
# Other vhosts and direct backend access need this pending-work quota too; 64
# exceeds that credential allowance and leaves room in the shared global cap.
_MAX_OAUTH_STATES_PER_SOURCE = 64


@dataclass(frozen=True, slots=True)
class _OAuthState:
    redirect: str
    provider: str
    callback: str
    browser_token: str
    expires_at: float
    source: str


_oauth_states: dict[str, _OAuthState] = {}
_oauth_state_lock = threading.Lock()


def _fix_scheme(url: str) -> str:
    if os.getenv("FORCE_HTTPS", "true").lower() == "true":
        return url.replace("http://", "https://", 1)
    return url


def _safe_redirect(state_param: str) -> str:
    """Accept local paths without relying on response URL quoting for safety."""
    if (
        state_param.startswith("/")
        and not state_param.startswith("//")
        and "\\" not in state_param
        and not any(ord(char) < 32 or ord(char) == 127 for char in state_param)
    ):
        return state_param
    return "/"


def _oauth_source(request: Request) -> str:
    """Use the transport peer after the ASGI server's trusted-proxy handling.

    Never read forwarding headers here: deployed nginx appends its validated
    client address and uvicorn trusts only the configured proxy peer. IPv6
    addresses in one /64 share a quota, including privacy-address rotation.
    This is an admission limit, not callback binding; mobile clients may roam.
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


def _make_oauth_state(request: Request, redirect: str, provider: str, callback: str) -> tuple[str, str]:
    """Issue a challenge and a cookie secret that is never sent to the provider."""
    source = _oauth_source(request)
    with _oauth_state_lock:
        now = monotonic()
        expired = [token for token, pending in _oauth_states.items() if pending.expires_at <= now]
        for token in expired:
            del _oauth_states[token]
        source_expiries = [pending.expires_at for pending in _oauth_states.values() if pending.source == source]
        if len(source_expiries) >= _MAX_OAUTH_STATES_PER_SOURCE:
            raise HTTPException(
                status_code=429,
                detail="Too many pending logins from this network. Please try again later.",
                headers={"Retry-After": str(max(1, math.ceil(min(source_expiries) - now)))},
            )
        if len(_oauth_states) >= _MAX_OAUTH_STATES:
            raise HTTPException(status_code=503, detail="Too many pending logins. Please try again later.")
        state = secrets.token_urlsafe(32)
        browser_token = secrets.token_urlsafe(32)
        _oauth_states[state] = _OAuthState(
            redirect=_safe_redirect(redirect),
            provider=provider,
            callback=callback,
            browser_token=browser_token,
            expires_at=now + _OAUTH_STATE_TTL_S,
            source=source,
        )
    return state, browser_token


def _verify_oauth_state(request: Request, state: str, provider: str) -> str | None:
    """Consume a matching, unexpired challenge before contacting the provider."""
    callback = _fix_scheme(str(request.url_for(f"callback_{provider}")))
    browser_token = request.cookies.get(f"__Host-retina-oauth-{provider}", "")
    with _oauth_state_lock:
        pending = _oauth_states.get(state)
        if pending is None:
            return None
        if pending.expires_at <= monotonic():
            del _oauth_states[state]
            return None
        if (
            pending.provider != provider
            or pending.callback != callback
            or not secrets.compare_digest(pending.browser_token.encode(), browser_token.encode())
        ):
            return None
        del _oauth_states[state]
        return pending.redirect


def _oauth_login_response(url: str, provider: str, browser_token: str) -> RedirectResponse:
    response = RedirectResponse(url)
    # __Host- forbids Domain cookies, including injection from sibling hosts.
    # Each provider has its own cookie; starting that provider again replaces
    # its pending browser login. Like auth_token, OAuth cookies require HTTPS.
    # Callback responses leave this cookie to expire: a slow accepted callback
    # could otherwise delete a newer login's cookie. Consumed server challenges,
    # not cookie deletion, enforce single use.
    response.set_cookie(
        f"__Host-retina-oauth-{provider}",
        browser_token,
        max_age=_OAUTH_STATE_TTL_S,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


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


# ── Google OAuth ──────────────────────────────────────────────────────────────


@router.get("/login/google")
async def login_google(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_google")))
    state, browser_token = _make_oauth_state(request, redirect, "google", callback)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return _oauth_login_response(
        f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}", "google", browser_token
    )


@router.get("/callback/google", name="callback_google")
async def callback_google(request: Request, code: str = "", state: str = ""):
    redirect_url = _verify_oauth_state(request, state, "google")
    if redirect_url is None:
        # A stale callback must not erase the cookie for a newer login.
        return RedirectResponse("/login?error=invalid_state")
    callback = _fix_scheme(str(request.url_for("callback_google")))
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": callback,
                "grant_type": "authorization_code",
            },
        )
        if tok.status_code != 200:
            logger.error("Google token exchange failed: %s", tok.text)
            return RedirectResponse("/login?error=google_token_failed")
        tokens = tok.json()

        info = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        userinfo = info.json()

    email = userinfo.get("email")
    if not email:
        return RedirectResponse("/login?error=no_email")

    user = await get_or_create_oauth_user(
        email=email,
        name=userinfo.get("name", ""),
        avatar=userinfo.get("picture", ""),
        provider="google",
        consume_invite_fn=consume_invite_for_email,
    )
    response = RedirectResponse(redirect_url)
    await _set_auth_cookie(response, user)
    return response


# ── GitHub OAuth ──────────────────────────────────────────────────────────────


@router.get("/login/github")
async def login_github(request: Request, redirect: str = "/"):
    callback = _fix_scheme(str(request.url_for("callback_github")))
    state, browser_token = _make_oauth_state(request, redirect, "github", callback)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": callback,
        "scope": "read:user user:email",
        "state": state,
    }
    return _oauth_login_response(
        f"https://github.com/login/oauth/authorize?{urlencode(params)}", "github", browser_token
    )


@router.get("/callback/github", name="callback_github")
async def callback_github(request: Request, code: str = "", state: str = ""):
    redirect_url = _verify_oauth_state(request, state, "github")
    if redirect_url is None:
        return RedirectResponse("/login?error=invalid_state")
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if tok.status_code != 200:
            logger.error("GitHub token exchange failed: %s", tok.text)
            return RedirectResponse("/login?error=github_token_failed")
        tokens = tok.json()
        access_token = tokens.get("access_token", "")

        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        profile = user_resp.json()

        email = profile.get("email")
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if emails_resp.status_code == 200:
                for e in emails_resp.json():
                    if e.get("primary"):
                        email = e["email"]
                        break

    if not email:
        return RedirectResponse("/login?error=no_email")

    user = await get_or_create_oauth_user(
        email=email,
        name=profile.get("name") or profile.get("login", ""),
        avatar=profile.get("avatar_url", ""),
        provider="github",
        consume_invite_fn=consume_invite_for_email,
    )
    response = RedirectResponse(redirect_url)
    await _set_auth_cookie(response, user)
    return response


# ── Magic links ───────────────────────────────────────────────────────────────

# Sign-in requests admitted per source per window. nginx already limits the
# credential endpoints to 5r/m, but only on the three vhosts carrying a
# `location /api/auth/`; the other six have a blanket `location /api/` at 30r/s
# and a direct caller has neither. Unbounded, that is a way to mail arbitrary
# strangers thirty times a second from our own domain, so the limit that
# matters lives here rather than only at the edge.
_MAGIC_LINK_WINDOW_S = 60.0
_MAX_MAGIC_LINK_REQUESTS_PER_SOURCE = 5
# Distinct sources tracked at once, matching _MAX_OAUTH_STATES' reason for
# existing: without it a flood from many addresses is a way to grow this dict
# until the process dies. Full, no further source is admitted, so sign-in stops
# for the length of one window — which is the better of the two failures.
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

    if _magic_link_quota_available(_oauth_source(request)):
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

    response = JSONResponse({"user": user_to_dict(user)})
    await _set_auth_cookie(response, user)
    return response


# ── Session endpoints ─────────────────────────────────────────────────────────


@router.get("/me")
async def me(request: Request):
    user_dict = await get_current_user(request)
    # Delegated rather than short-circuiting on AUTH_BYPASS, so this agrees with
    # what require_admin decided: a verified Access assertion outranks the
    # bypass, and answering "Admin (no auth)" while the admin routes attribute a
    # real person would make the console wrong about its own session.
    anonymous = user_dict["id"] == ANONYMOUS_USER["id"]
    return {**user_dict, "auth_enabled": not anonymous}


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
