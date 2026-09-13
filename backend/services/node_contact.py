"""The one validator for a node's owner contact details, and the source of the
schema the contract publishes for them.

A leaf on the same terms as services/node_config.py: it takes a dict and returns
a dict, knowing nothing of identity, HTTP or the database, and nothing beyond the
standard library may be imported here.

Contact is not configuration. It is stored per node rather than per version, and
none of it is verified: whoever holds a node's bearer token can set it.
"""

from typing import Any

# field -> the column width on node_contacts. The bounds and the published
# schema are both built from this, so a width changed in one place moves both.
_MAX_LENGTHS: dict[str, int] = {
    "first_name": 64,
    "last_name": 64,
    "email": 255,
    "phone": 32,
}

_FIELDS = frozenset(_MAX_LENGTHS)

# Deliberately shallow: the check is against a typo, and a stricter grammar
# refuses addresses that work. Nothing here says an address is real.
_PHONE_CHARACTERS = frozenset("0123456789 +-()")

_SCHEMA_DESCRIPTION = """\
Whom to contact about this node, as its owner gave them. Every field is optional
and nullable, and an absent field means the same as a null one: the document is
replaced wholesale, so a field left out is cleared.

None of it is verified, and none of it identifies anyone to the server: it is
carried so that a fault we can see and the owner cannot has somewhere to go."""


class ContactInvalid(Exception):
    def __init__(self, field: str, reason: str = "invalid") -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason


def contact_json_schema() -> dict[str, Any]:
    """The contact document's published JSON Schema, built from the table above.

    Fresh every call, nested dicts included: the comprehension below builds a new
    `{"anyOf": [...]}` literal per field per call, so no schema dict is ever
    shared between callers for a framework to mutate in place.
    """
    return {
        "type": "object",
        "title": "NodeContact",
        "description": _SCHEMA_DESCRIPTION,
        "properties": {
            field: {"anyOf": [{"type": "string", "maxLength": maximum}, {"type": "null"}]}
            for field, maximum in _MAX_LENGTHS.items()
        },
        # No key is required: a node with nothing to report sends {}.
        "required": [],
        # The validator names an unknown key back to the caller rather than
        # ignoring it, as the configuration validator does.
        "additionalProperties": False,
    }


def _string(field: str, value: Any) -> str | None:
    if not isinstance(value, str):
        raise ContactInvalid(field, "not a string")
    trimmed = value.strip()
    if not trimmed:
        # An owner clearing a field and an owner never filling it in are the same
        # state, and should not be two of them in the database.
        return None
    if len(trimmed) > _MAX_LENGTHS[field]:
        raise ContactInvalid(field, "too long")
    return trimmed


def _email(value: str) -> str:
    if any(character.isspace() for character in value):
        raise ContactInvalid("email", "not an address")
    local, separator, domain = value.partition("@")
    if not separator or not local or "@" in domain:
        raise ContactInvalid("email", "not an address")
    labels = domain.split(".")
    if len(labels) < 2 or not all(labels):
        raise ContactInvalid("email", "not an address")
    return value


def _phone(value: str) -> str:
    if not set(value) <= _PHONE_CHARACTERS or not any(character.isdigit() for character in value):
        raise ContactInvalid("phone", "not a number")
    # Returned as typed. A number reformatted wrongly is worse than the one the
    # owner gave, and no country can be inferred from what we hold.
    return value


def validate_contact(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the normalised contact document, or raise ContactInvalid naming one field."""
    # A JSON body is not necessarily an object, and set() over a non-iterable
    # raises TypeError, which would leave the route with no ContactInvalid to map
    # and answer a malformed body with a 500.
    if not isinstance(payload, dict):
        raise ContactInvalid("contact", "not an object")

    # Sorted so that a payload wrong in several places always names the same
    # field, and a node retrying unchanged always gets the same answer.
    unknown = sorted(set(payload) - _FIELDS)
    if unknown:
        raise ContactInvalid(unknown[0], "unknown field")

    out: dict[str, Any] = {}
    for field in _MAX_LENGTHS:
        value = payload.get(field)
        out[field] = None if value is None else _string(field, value)

    if out["email"] is not None:
        out["email"] = _email(out["email"])
    if out["phone"] is not None:
        out["phone"] = _phone(out["phone"])
    return out
