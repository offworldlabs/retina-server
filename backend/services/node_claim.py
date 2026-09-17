"""The one validator for the address a node is claimed with, and the source of
the schema the contract publishes for it.

A leaf on the same terms as services/node_contact.py: it takes a dict and
returns a value, knowing nothing of identity, HTTP or the database. Its one
import is the shared address grammar.

Distinct from the contact document in what it demands and what it does with
what it gets. `email` is required here where every contact field is optional,
because an absent address on a nomination is a malformed request rather than an
owner clearing a field: clearing is release, and release is not the node's to
perform. The address is also lower cased, where the contact document keeps what
was typed, because this one is a key into accounts and has to match what
sign-in stored.
"""

from typing import Any

from services.email_address import MAX_ADDRESS_LENGTH, is_address

_SCHEMA_DESCRIPTION = """\
The address to claim this node with. The server mails it a link; clicking that
link binds the node to the account behind the address, creating one if there is
not already one.

Required, and the only field. Sending an address the node already holds is
accepted and changes nothing, so this may be resent freely; it does not mail
anything again. Asking for the mail again is a separate call.

Surrounding whitespace is stripped and the address is lower cased before it is
judged, so the bound here describes the trimmed form rather than the bytes sent.

Nothing here verifies that the address exists. Until the link is clicked the
address grants nothing, and the node runs exactly as it would with no address
at all."""


class ClaimInvalid(Exception):
    def __init__(self, field: str, reason: str = "invalid") -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def claim_json_schema() -> dict[str, Any]:
    """The nomination body's published JSON Schema.

    Fresh every call, nested dicts included, for the reason contact_json_schema
    gives: no schema dict is ever shared between callers for a framework to
    mutate in place.
    """
    return {
        "type": "object",
        "title": "NodeClaimRequest",
        "description": _SCHEMA_DESCRIPTION,
        "properties": {
            "email": {
                "type": "string",
                "maxLength": MAX_ADDRESS_LENGTH,
                "description": "The owner's address, lower cased and trimmed by the server.",
            }
        },
        "required": ["email"],
        # The validator names an unknown key back to the caller rather than
        # ignoring it, as the other two validators do.
        "additionalProperties": False,
    }


def validate_claim(payload: dict[str, Any]) -> str:
    """Return the normalised address, or raise ClaimInvalid naming one field."""
    # A JSON body is not necessarily an object, and set() over a non-iterable
    # raises TypeError, which would leave the route with no ClaimInvalid to map
    # and answer a malformed body with a 500.
    if not isinstance(payload, dict):
        raise ClaimInvalid("claim", "not an object")

    # Sorted so that a payload wrong in several places always names the same
    # field, and a node retrying unchanged always gets the same answer.
    unknown = sorted(set(payload) - {"email"})
    if unknown:
        raise ClaimInvalid(unknown[0], "unknown field")

    value = payload.get("email")
    if not isinstance(value, str):
        # An absent address and a null one are the same fault: there is nothing
        # to mail either way, and no field here may be cleared by omission.
        raise ClaimInvalid("email", "not a string")

    normalised = value.strip().lower()
    if not normalised:
        raise ClaimInvalid("email", "not an address")
    if len(normalised) > MAX_ADDRESS_LENGTH:
        raise ClaimInvalid("email", "too long")
    if not is_address(normalised):
        raise ClaimInvalid("email", "not an address")
    return normalised
