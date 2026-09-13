"""PUT /v1/nodes/contact, where a node reports whom to contact about it.

The router carries no prefix: `routes/nodes.py` supplies it at mount.

Distinct from `services/node_contact.py`, which validates a payload, and from
`services/node_contact_store.py`, which persists it.

None of this is configuration. It is not versioned, it never reaches the solver,
and it is stored per node rather than per configuration version, so no detection
frame refers to it.
"""

from fastapi import APIRouter, Depends, Request, Security
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core.users import get_async_session
from routes.node_responses import INVALID_CONTACT, NODE_BODY_LIMITS, SERVER_ERROR, TOO_LARGE, UNAUTHORIZED
from routes.node_schemas import ContactResponse, ErrorBody
from services.node_auth import bearer_node, node_bearer_scheme
from services.node_contact import ContactInvalid, contact_json_schema, validate_contact
from services.node_contact_store import upsert_contact

# Tag and security scheme as in routes/node_config.py, and for the same reasons.
router = APIRouter(tags=["contact"], dependencies=[Security(node_bearer_scheme)])

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


_DESCRIPTION = """\
Whom to contact about this node. Sent when the details change locally, and not otherwise:
nothing on the server asks for them, and no response marks them stale.

The document is replaced wholesale, so a field omitted or sent as null is cleared. Every
field is optional; a node with nothing to report need never call this.

What is stored is unverified and is used to reach the owner about their own node. It is
not an account, and it grants nothing.
"""


@router.put(
    "/contact",
    response_model=ContactResponse,
    responses={400: INVALID_CONTACT, 401: UNAUTHORIZED, 413: TOO_LARGE, "5XX": SERVER_ERROR},
    summary="Report the owner's contact details.",
    description=_DESCRIPTION,
    response_description="When the stored details last changed.",
    operation_id="putContact",
    openapi_extra={
        "x-cadence": "on local change only",
        "x-max-body-bytes": NODE_BODY_LIMITS["/v1/nodes/contact"],
        # The body is read inside the handler rather than declared, so FastAPI has
        # nothing to describe it with and the published operation would otherwise
        # take no body at all.
        "requestBody": {
            "required": True,
            "description": "The full contact document. Fields omitted are cleared.",
            "content": {"application/json": {"schema": contact_json_schema()}},
        },
    },
)
async def put_contact(
    request: Request,
    node_id: str = Depends(bearer_node),
    session: AsyncSession = Depends(get_async_session),
) -> ContactResponse | JSONResponse:
    """Store the contact document and return when it last changed.

    The body is read here rather than declared as a parameter, and the bearer is
    resolved before it is touched, for the reason routes/node_config.py gives:
    a declared body is validated ahead of the handler, which would put a
    body-shaped refusal in front of identity resolution.
    """
    try:
        payload = await request.json()
    except ValueError:
        # A body that is not JSON at all fails the same way one that is JSON but
        # not a contact document does, and the remedy is the same either way.
        return _error(400, "invalid_contact", "contact")

    try:
        contact = validate_contact(payload)
    except ContactInvalid as exc:
        return _error(400, "invalid_contact", exc.field)

    updated_at = await upsert_contact(session, node_id, contact)
    await session.commit()
    return ContactResponse(updated_at=updated_at)
