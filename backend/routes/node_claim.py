"""`/v1/nodes/claim`, where a node offers the address that owns it.

The router carries no prefix: `routes/nodes.py` supplies it at mount.

Distinct from `services/node_claim.py`, which validates a payload, from
`services/node_claim_store.py`, which persists it and derives the state, from
`services/claim_resend.py`, which sends the mail and holds the resend's rules,
and from `services/claim_links.py`, which mints the link and queues its mail.

Nothing here is an authorisation decision. Offering an address grants nothing
and a node with an unverified one runs exactly as a node with none does; what
grants is the click, which lands on `/api/auth` rather than in the node API,
because the person clicking is not the node.
"""

import time

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.users import get_async_session
from routes.node_responses import (
    ALREADY_CLAIMED,
    INVALID_CLAIM,
    MAIL_UNAVAILABLE,
    MAIL_UNAVAILABLE_RESEND,
    MAIL_UNAVAILABLE_RETRY_AFTER_S,
    NODE_BODY_LIMITS,
    RATE_LIMITED,
    SERVER_ERROR,
    TOO_LARGE,
    UNAUTHORIZED_CLAIM,
)
from routes.node_schemas import ClaimResponse, ErrorBody
from services import claim_resend
from services.claim_resend import Result
from services.node_auth import bearer_node, node_bearer_scheme
from services.node_claim import ClaimInvalid, claim_json_schema, validate_claim
from services.node_claim_store import ClaimStatus, claim_status

# Tag and security scheme as in routes/node_contact.py, and for the same reasons.
router = APIRouter(tags=["claim"], dependencies=[Security(node_bearer_scheme)])

# `Error.detail`'s bound, from the contract and from ErrorBody in routes/node_schemas.py.
_MAX_DETAIL = 512


def _error(status_code: int, error: str, detail: str | None = None) -> JSONResponse:
    """The contract's `Error` shape. `detail` is dropped when absent, not nulled.

    Truncated for the reason routes/node_config.py gives: an over-long detail
    would fail ErrorBody's own validation and turn a 400 into a 500, and a node
    retries a 5XX where it does not retry a 400.
    """
    if detail is not None:
        detail = detail[:_MAX_DETAIL]
    return JSONResponse(ErrorBody(error=error, detail=detail).model_dump(), status_code=status_code)


def _mail_unavailable() -> JSONResponse:
    """The 503 a claim gets from a server that cannot send mail, with the wait
    routes/node_responses.py publishes beside it."""
    response = _error(503, "mail_unavailable")
    response.headers["Retry-After"] = str(MAIL_UNAVAILABLE_RETRY_AFTER_S)
    return response


def _state(status: ClaimStatus, status_code: int = 200) -> JSONResponse:
    body = ClaimResponse(state=status.state, email=status.email, undeliverable=status.undeliverable)
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)


_PUT_DESCRIPTION = """\
Offer the address that owns this node. The server mails it a link, and clicking that
link binds the node to the account behind the address, creating one if there is not
already one.

Sending an address the node already holds is accepted and changes nothing, so this may
be resent on every configuration sync. It does not mail anything again: asking for the
mail again is `POST /v1/nodes/claim/resend`, because a write that mailed every time it
ran would mail on every sync.

The response says where the claim stands, and is the same shape `GET` returns. Nothing
about the node's operation depends on any of it: an address that is never verified
grants nothing, and a node with no address at all runs unowned indefinitely and can be
claimed whenever its owner gets round to it.

`pending` means the mail is on its way: it leaves just after the answer rather than
before it. If it cannot be delivered after all, the claim goes back to `unclaimed` with
the address still on file, and `undeliverable` set when the address itself is what the
mail could not reach. Either way the `GET` and the heartbeat say so.

A server that cannot send mail at all answers `503` with `mail_unavailable` instead of
`pending`, and records nothing: the claim stands as it did before the call. Say on the
setup page that the server cannot send mail, rather than asking anyone to watch an inbox,
and offer the address again after `Retry-After` or at the next configuration sync.

Poll the `GET` every few seconds while a setup page is open and somebody is waiting.
Afterwards stop: `HeartbeatResponse` carries the same three fields once a minute, which
is how a release performed months later reaches a node that stopped polling long ago.
"""

_GET_DESCRIPTION = """\
Where this node's claim stands. Cheap, and safe to poll every few seconds while a setup
page is open; `HeartbeatResponse` carries the same three fields for the rest of the
node's life, so there is no reason to keep polling once the page closes.
"""

_RESEND_DESCRIPTION = """\
Send the link again, to the address already on file. No body.

Separate from `PUT` because a write that mailed every time it ran would mail on every
configuration sync. This is the explicit ask, and it is what a person who did not get
the first mail presses.

Refused on a node that already has an owner, and answered without sending on an address
that bounced hard: a second copy to an address that does not exist earns nothing but a
second bounce, and bounces are how a sending domain loses its reputation.

A server that cannot send mail at all answers `503` with `mail_unavailable`, and changes
nothing: a link already sent, if one is still live, keeps working. Ask again after
`Retry-After`.
"""


@router.put(
    "/claim",
    response_model=ClaimResponse,
    responses={
        400: INVALID_CLAIM,
        401: UNAUTHORIZED_CLAIM,
        409: ALREADY_CLAIMED,
        413: TOO_LARGE,
        429: RATE_LIMITED,
        503: MAIL_UNAVAILABLE,
        "5XX": SERVER_ERROR,
    },
    summary="Offer the address that owns this node.",
    description=_PUT_DESCRIPTION,
    response_description="Where the claim stands after this call.",
    operation_id="putClaim",
    openapi_extra={
        "x-cadence": "on local change only",
        "x-max-body-bytes": NODE_BODY_LIMITS["/v1/nodes/claim"],
        # The body is read inside the handler rather than declared, so FastAPI has
        # nothing to describe it with and the published operation would otherwise
        # take no body at all.
        "requestBody": {
            "required": True,
            "description": "The address to claim this node with.",
            "content": {"application/json": {"schema": claim_json_schema()}},
        },
    },
)
async def put_claim(
    request: Request,
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
) -> ClaimResponse | JSONResponse:
    """Offer an address, and say where that leaves the claim.

    The body is read here rather than declared as a parameter, and the bearer is
    resolved before it is touched, for the reason routes/node_config.py gives: a
    declared body is validated ahead of the handler, which would put a
    body-shaped refusal in front of identity resolution.
    """
    try:
        payload = await request.json()
    except ValueError:
        # A body that is not JSON at all fails the same way one that is JSON but
        # not a nomination does, and the remedy is the same either way.
        return _error(400, "invalid_claim", "email")

    try:
        email = validate_claim(payload)
    except ClaimInvalid as exc:
        return _error(400, "invalid_claim", exc.field)

    now = time.time()
    status = await claim_status(session, node_id, now)

    if status.state == "owned":
        # Writing the address it already holds is accepted and changes nothing;
        # a different one is refused, and the refusal carries the address that
        # won so the node reconciles from the answer rather than from a second
        # call. A node can only be here by acting on state up to a beat old, and
        # answering rather than merely refusing is what makes that harmless.
        return _state(status, 200 if status.email == email else 409)

    if status.email == email:
        # Already said, and saying it again neither mails nor changes anything.
        # Keyed on the address on file rather than on a challenge still being
        # live: a challenge expires in fifteen minutes, so a guard that asked
        # whether one was outstanding would mail again on the next
        # configuration sync, and again on the one after that, for as long as
        # nobody clicked. Sending mail to an address that has already had some
        # is what the resend exists to ask for explicitly.
        return _state(status)

    return _answer(await claim_resend.send(session, node_id, email, now))


@router.get(
    "/claim",
    response_model=ClaimResponse,
    # No 429: the limiter guards what costs somebody else's mailbox, and this
    # reads three rows. A node is told to poll it every few seconds, so a limit
    # here would refuse the cadence the description asks for.
    responses={401: UNAUTHORIZED_CLAIM, "5XX": SERVER_ERROR},
    summary="Read where this node's claim stands.",
    description=_GET_DESCRIPTION,
    response_description="Where the claim stands.",
    operation_id="getClaim",
    openapi_extra={"x-cadence": "every few seconds while a setup page is open, and not otherwise"},
)
async def get_claim(
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
) -> ClaimResponse | JSONResponse:
    """Where the claim stands. Reads three rows and writes none."""
    return _state(await claim_status(session, node_id, time.time()))


@router.post(
    "/claim/resend",
    response_model=ClaimResponse,
    responses={
        401: UNAUTHORIZED_CLAIM,
        409: ALREADY_CLAIMED,
        413: TOO_LARGE,
        429: RATE_LIMITED,
        503: MAIL_UNAVAILABLE_RESEND,
        "5XX": SERVER_ERROR,
    },
    summary="Send the claim link again.",
    description=_RESEND_DESCRIPTION,
    response_description="Where the claim stands after this call.",
    operation_id="resendClaim",
    openapi_extra={
        "x-cadence": "when somebody asks for the mail again, and never automatically",
        "x-max-body-bytes": NODE_BODY_LIMITS["/v1/nodes/claim/resend"],
    },
)
async def resend_claim(
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
) -> ClaimResponse | JSONResponse:
    """Mint and send a fresh challenge for the address already on file.

    The rules are services/claim_resend.py's, which the claim page and the
    dashboard ask under as well, against the same allowance. A node with
    nothing on file is answered `unclaimed` with no address, which is how it
    knows to nominate one first.
    """
    return _answer(await claim_resend.resend(session, node_id, time.time()))


def _answer(outcome: claim_resend.Outcome) -> JSONResponse:
    """A node's answer to a nomination or a resend: where its claim stands, or
    why nothing was done."""
    if outcome.result is Result.MAIL_UNAVAILABLE:
        return _mail_unavailable()
    if outcome.refusal is not None:
        return JSONResponse(
            outcome.refusal.body,
            status_code=outcome.refusal.status_code,
            headers={"Retry-After": str(outcome.refusal.retry_after_s)},
        )
    if outcome.status is None:
        # The one outcome left that says nothing about the claim: the node's row
        # has gone. As routes/node_stream.py answers it, the token outlived the
        # row it names.
        raise HTTPException(status_code=401, detail="unauthorized")
    return _state(outcome.status, 409 if outcome.result is Result.OWNED else 200)
