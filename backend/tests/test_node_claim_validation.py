"""The nomination body's validator.

The grammar itself is pinned in test_email_address.py. What is tested here is
what this leaf adds to it: the field being required rather than optional, the
normalisation, and the shapes that would otherwise reach the route as something
it has no ClaimInvalid to map.
"""

import pytest

from services.node_claim import ClaimInvalid, claim_json_schema, validate_claim


def test_an_address_comes_back_normalised():
    assert validate_claim({"email": "  Ada@Example.COM  "}) == "ada@example.com"


def test_an_address_already_normalised_is_unchanged():
    assert validate_claim({"email": "ada@example.com"}) == "ada@example.com"


def test_a_missing_address_is_refused():
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({})
    assert raised.value.field == "email"


def test_a_null_address_is_refused_rather_than_clearing_anything():
    """Every contact field may be cleared by omission; this one may not. Clearing
    is release, and release is not the node's to perform."""
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({"email": None})
    assert raised.value.field == "email"


def test_whitespace_alone_is_refused():
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({"email": "   "})
    assert (raised.value.field, raised.value.reason) == ("email", "not an address")


def test_a_non_string_address_is_refused():
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({"email": 42})
    assert raised.value.field == "email"


def test_an_over_long_address_is_refused_on_its_trimmed_form():
    local = "a" * 250
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({"email": f"  {local}@example.com  "})
    assert (raised.value.field, raised.value.reason) == ("email", "too long")


def test_an_address_at_the_bound_is_accepted():
    local = "a" * (255 - len("@example.com"))
    assert validate_claim({"email": f"{local}@example.com"}).endswith("@example.com")


def test_an_unknown_key_is_named_back():
    with pytest.raises(ClaimInvalid) as raised:
        validate_claim({"email": "ada@example.com", "code": "X"})
    assert raised.value.field == "code"


def test_several_unknown_keys_always_name_the_same_one():
    with pytest.raises(ClaimInvalid) as first:
        validate_claim({"zeta": 1, "alpha": 2})
    with pytest.raises(ClaimInvalid) as second:
        validate_claim({"alpha": 2, "zeta": 1})
    assert first.value.field == second.value.field == "alpha"


@pytest.mark.parametrize("payload", [["ada@example.com"], "ada@example.com", 7, None])
def test_a_body_that_is_not_an_object_is_refused_rather_than_raising_type_error(payload):
    """set() over a non-iterable raises TypeError, which the route has no
    ClaimInvalid to map, so a malformed body would answer 500 instead of 400."""
    with pytest.raises(ClaimInvalid):
        validate_claim(payload)


def test_the_published_schema_requires_the_one_field_it_describes():
    schema = claim_json_schema()
    assert schema["required"] == ["email"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["email"]["maxLength"] == 255


def test_the_published_schema_is_not_shared_between_callers():
    first, second = claim_json_schema(), claim_json_schema()
    first["properties"]["email"]["maxLength"] = 1
    assert second["properties"]["email"]["maxLength"] == 255
