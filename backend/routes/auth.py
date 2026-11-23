"""Authentication routes: sign-in by emailed link, session, node ownership.

Sign-in is a link mailed to the address (services/mail.py); there is no
password and no third-party provider. A redeemed link opens a session held on
the server (core/sessions.py), whose token is the auth_token cookie, and a
signed-in account can list its sessions and end them here, and change the
address it signs in with (services/account_email.py).
"""

import logging
import os
import re
import threading
from dataclasses import replace
from time import monotonic, time
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Receive

from core import sessions
from core.auth import (
    INTENT_SIGNIN,
    MAGIC_LINK_EXPIRY_S,
    _hash_token,
    consume_magic_link,
    create_magic_link_in,
    invalidate_magic_link,
    peek_magic_link,
    sweep_magic_links,
)
from core.users import (
    ACCESS_LOGOUT_PATH,
    ANONYMOUS_USER,
    MagicLinkRefused,
    User,
    UserSession,
    async_session_maker,
    forwarding_address,
    get_async_session,
    get_current_user,
    get_or_create_magic_link_user,
    has_access_session,
    record_visit,
    user_to_dict,
)
from routes.sim_ingest import synthetic_fleet_enabled
from services import account_email, claim_resend, mail, mail_outbox, mail_templates, polled_registration
from services.claim_resend import Result
from services.client_source import client_source
from services.node_claiming import (
    ClaimOutcome,
    complete_claim,
    decline_claim,
    preview_claim,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


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


async def _mail_sign_in_link(receive: Receive, email: str, landing: str, next_path: str | None) -> None:
    """Mint a sign-in link for `email` and queue its mail, once the request is answered.

    The route's background task, so it runs inside the request's ASGI call:
    the server waits for it at shutdown, and a test client finds the mail
    queued when its call returns. Starlette starts it once the route's response
    is sent, but behind a BaseHTTPMiddleware (LimitUploadSize, main.py) "sent"
    means handed to the middleware, and the middleware passes the end of it on
    first only because of the order its streams happen to be scheduled in. So
    it waits for `receive` to say http.disconnect, which the ASGI spec has a
    server say once the response has been sent or the caller has gone. Either
    way, nobody is timing it any more.

    Nobody hears about a failure from here on but the log, and nothing may
    escape: past that middleware it vanishes without a word, and without it the
    server prints it whole. Only the type is logged, since what an exception
    says can quote the address, or the message and the link with it.

    A retired address signs its account in, with a link minted for and mailed
    to the address the account has now. The retired one is never mailed: it
    may be dead, or somebody else's, by now. Where it goes is read under the
    write lock the link is minted under, so a change of address confirmed
    meanwhile has either committed before the read, which then sees it, or
    waits for the link's commit and kills it with the rest of the old
    mailbox's links (services/account_email.py).
    """
    try:
        while (await receive())["type"] != "http.disconnect":
            pass
        # One clock reading for both expiries, so the outbox never holds the
        # mail for longer than its link lives.
        now = time()
        async with async_session_maker() as session:
            async with session.begin():
                # The write lock, before where the mail goes is read.
                await sweep_magic_links(session, email, now)
                email = await forwarding_address(session, email) or email
                token = await create_magic_link_in(session, email, now=now)
    except Exception as exc:
        logger.error("Could not issue a sign-in link (%s)", type(exc).__name__)
        return
    if token:
        await queue_sign_in_mail(email, token, landing + token + _next_query(next_path), now)


async def queue_sign_in_mail(email: str, token: str, link: str, now: float) -> bool:
    """Queue the mail carrying a minted sign-in link, and say whether it was. Never raises."""
    try:
        rendered = mail_templates.sign_in(link)
        message = mail.Message(to=email, subject=rendered.subject, text=rendered.text, html=rendered.html)
        async with async_session_maker() as session:
            await mail_outbox.enqueue(
                session, message, kind=mail_outbox.KIND_SIGN_IN, expires_at=now + MAGIC_LINK_EXPIRY_S, now=now
            )
            await session.commit()
    except Exception as exc:
        # No mail carries the link, so it is withdrawn rather than left live.
        logger.error("Could not queue a sign-in mail (%s); withdrawing its link", type(exc).__name__)
        try:
            await invalidate_magic_link(_hash_token(token))
        except Exception as error:
            # Nobody holds it, so it dies unused at its expiry.
            logger.error("Could not withdraw an unsent sign-in link (%s)", type(error).__name__)
        return False
    mail_outbox.wake()
    return True


def sign_in_landing() -> str:
    """Where a mailed sign-in link lands, or 503 when no mail can leave.

    About this deployment, not about the address, so it is safe to say.
    Checked before a token is minted, so a refusal leaves no row behind. The
    link is built on the one reading of HOST_APP taken here: without it the
    only host to hand is the request's, and the caller controls that.
    """
    reason = mail_outbox.unavailable()
    landing = mail.link_to(_SIGN_IN_PATH)
    if reason is not None or landing is None:
        logger.error("Sign-in by email is unavailable: %s", reason or "HOST_APP is not set")
        raise HTTPException(status_code=503, detail="Sign-in by email is unavailable")
    return landing


@router.post("/magic-link", status_code=202)
async def request_magic_link(body: MagicLinkRequest, request: Request, background: BackgroundTasks):
    """Mail a sign-in link, if the address is one we should mail.

    Answers 202 identically whether or not a link was minted and queued, and the
    caller's response time must not vary with that either. The alternative is
    an oracle for which addresses own receivers. So the response goes before
    anything looks at the address: minting the link, which writes nothing for
    an address at its cap, and queueing its mail run after it
    (_mail_sign_in_link). The outbox's worker (services/mail_outbox.py) calls
    the provider, so nothing waits on the provider, slow or down.
    """
    landing = sign_in_landing()

    if _magic_link_quota_available(client_source(request)):
        background.add_task(_mail_sign_in_link, request.receive, body.email, landing, body.next)

    return {"status": "accepted"}


@router.get("/magic-link/{token}")
async def preview_magic_link(token: str):
    """Which address a sign-in link is for, without spending it.

    The landing page names the address and waits for a press before it
    redeems, so a scanner that renders the page and runs its script leaves the
    link alive. Reading it needs the same secret redeeming does, so it tells
    the holder nothing they could not learn by signing in.

    A link for an address an account has retired reads as a dead one, as
    redeeming refuses it (core/auth.py, consume_magic_link): the page would
    otherwise offer a sign-in that fails at the press.
    """
    link = await peek_magic_link(token, intent=INTENT_SIGNIN)
    if link is not None:
        async with async_session_maker() as session:
            if await forwarding_address(session, link.email) is not None:
                link = None
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


#: What a claim mail asked for again by a person is answered with, from the
#: claim page here and from the dashboard (routes/owner.py). The console shows
#: them as they are.
_CANNOT_RENEW = "That link cannot be renewed"
_CLAIM_TAKEN = "That node already belongs to someone else"
_CLAIM_MAIL_LIMITED = "Too many claim mails have been asked for; try again later"
_CLAIM_MAIL_UNAVAILABLE = "Claiming by email is unavailable"


def _claim_mail_answer(response: Response, outcome: claim_resend.Outcome, *, not_found: str, owned: str) -> dict:
    """A person's answer to asking for a claim mail again: 202 when one was
    queued, and only then.

    Set on `response` rather than raised, so that a cookie exchanged on the
    way in still reaches the browser. Nothing to send is said as a claim that
    is not there, in the caller's own words.
    """
    if outcome.result is Result.SENT:
        response.status_code = 202
        return {"status": "sent"}
    if outcome.result is Result.OWNED:
        response.status_code = 409
        return {"detail": owned}
    if outcome.result is Result.MAIL_UNAVAILABLE:
        response.status_code = 503
        return {"detail": _CLAIM_MAIL_UNAVAILABLE}
    if outcome.refusal is not None:
        response.status_code = 429
        response.headers["Retry-After"] = str(outcome.refusal.retry_after_s)
        return {"detail": _CLAIM_MAIL_LIMITED}
    response.status_code = 404
    return {"detail": not_found}


@router.post("/claim/resend", status_code=202)
async def resend_claim_link(body: ClaimToken, response: Response, session: AsyncSession = Depends(get_async_session)):
    """Send a claim's mail again, for whoever holds a link one carried.

    No sign-in: the link is the proof, as it is for the click. The claim page
    offers this once the link has stopped working, and the mail goes to the
    address the node gave, under the node resend's rules and against its
    allowance (services/claim_resend.py). A link that is no longer its
    claim's newest gets one answer however it came to be, as a dead link does
    at the click.
    """
    outcome = await claim_resend.resend_by_link(session, body.token, time())
    return _claim_mail_answer(response, outcome, not_found=_CANNOT_RENEW, owned=_CLAIM_TAKEN)


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
    first, so there is always a session to call current, and 503 if that
    exchange cannot be written.
    """
    resolved = await sessions.resolve_request(request)
    if resolved is not None and resolved.session is None:
        try:
            exchanged = await _exchange_legacy_cookie(request, response, resolved)
        except Exception:
            # Logged and dropped as in me(), but these routes need the session
            # it would have opened. A 401 would read as signed out, where the
            # cookie still signs in and the retry exchanges it.
            logger.warning("Could not exchange a legacy cookie for a session", exc_info=True)
            raise HTTPException(
                status_code=503, detail="Sessions are temporarily unavailable", headers={"Retry-After": "5"}
            ) from None
        # None when another request exchanged or signed out the same cookie
        # first. Resolved again, as it may now alias the session that one opened.
        resolved = exchanged or await sessions.resolve_any(sessions.cookie_tokens(request))
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


# ── Changing the account's address ────────────────────────────────────────────


class AddressChangeRequest(BaseModel):
    # A string rather than EmailStr: an address the core refuses is a 400 with
    # its reason, as the console shows it, not a validation error.
    email: str


def _refused(response: Response, refused: account_email.Refused) -> dict:
    """The answer a refusal gets, set on `response` rather than raised, so a
    cookie exchanged on the way in still reaches the browser."""
    response.status_code = refused.status_code
    if refused.retry_after_s is not None:
        response.headers["Retry-After"] = str(refused.retry_after_s)
    return {"detail": refused.detail}


@router.post("/me/email", status_code=202)
async def request_address_change(body: AddressChangeRequest, request: Request, response: Response):
    """Mail a link to a new address that makes it the one the caller signs in with.

    The address changes when the link is opened, not now. A second request
    replaces the first and kills its link. services/account_email.py says what
    is refused, and with what. Another account's address is not: it is answered
    as a free one is, so this cannot be asked whose an address is, as sign-in
    cannot.
    """
    user, _current = await _signed_in_session(request, response)
    try:
        requested = await account_email.request_change(user.id, body.email, by_account=True)
    except account_email.Refused as refused:
        return _refused(response, refused)
    return {"status": "sent", "email": requested.email, "expires_in": requested.expires_in}


@router.get("/email-change/{token}")
async def preview_address_change(token: str):
    """Which address a change's link would make the account's, without spending it.

    The landing page names it and waits for a press, as the sign-in page does,
    so a scanner that renders the page leaves the link alive.
    """
    address = await account_email.preview(token)
    if address is None:
        raise HTTPException(status_code=404, detail=account_email.DEAD_LINK)
    return {"email": address}


@router.post("/email-change/consume")
async def confirm_address_change(body: MagicLinkConsume):
    """Open a change's link: the address becomes the account's, and the one it
    replaces is retired to the account and told.

    A POST, for the reason the sign-in consume is one. Opens no session: the
    link proves the new address and nothing else, and whoever opened it signs
    in with it as anybody does.
    """
    try:
        address = await account_email.confirm(body.token)
    except account_email.Refused as refused:
        raise HTTPException(status_code=refused.status_code, detail=refused.detail) from None
    return {"email": address}
