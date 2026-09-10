"""What services/node_contact.py accepts, against what its schema publishes.

The schema is generated from the same table the validator enforces, and
test_the_published_schema_matches_the_bounds is what stops the two drifting:
the document a node is built against cannot state a bound the server does not
apply.
"""

import pytest

from services.node_contact import ContactInvalid, contact_json_schema, validate_contact

FULL = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+44 20 7946 0000",
}


def test_a_full_document_passes_through_unchanged():
    assert validate_contact(dict(FULL)) == FULL


def test_an_empty_document_is_four_nulls():
    assert validate_contact({}) == dict.fromkeys(FULL, None)


def test_an_absent_field_is_null_rather_than_missing():
    assert validate_contact({"email": "ada@example.com"}) == {
        "first_name": None,
        "last_name": None,
        "email": "ada@example.com",
        "phone": None,
    }


def test_surrounding_whitespace_is_stripped():
    assert validate_contact({"first_name": "  Ada  "})["first_name"] == "Ada"


def test_a_field_that_is_only_whitespace_normalises_to_null():
    assert validate_contact({"first_name": "   "})["first_name"] is None


def test_a_body_that_is_not_an_object_is_refused():
    with pytest.raises(ContactInvalid) as exc:
        validate_contact(["ada@example.com"])
    assert exc.value.field == "contact"


def test_an_unknown_field_is_named_back():
    with pytest.raises(ContactInvalid) as exc:
        validate_contact({"middle_name": "Byron"})
    assert (exc.value.field, exc.value.reason) == ("middle_name", "unknown field")


@pytest.mark.parametrize("value", [42, True, 3.5, ["Ada"], {"first": "Ada"}])
def test_a_non_string_is_refused(value):
    with pytest.raises(ContactInvalid) as exc:
        validate_contact({"first_name": value})
    assert (exc.value.field, exc.value.reason) == ("first_name", "not a string")


@pytest.mark.parametrize(
    ("field", "length"),
    [("first_name", 65), ("last_name", 65), ("email", 256), ("phone", 33)],
)
def test_a_field_longer_than_its_column_is_refused(field, length):
    value = ("a" * (length - 12) + "@example.com") if field == "email" else "a" * length
    with pytest.raises(ContactInvalid) as exc:
        validate_contact({field: value})
    assert (exc.value.field, exc.value.reason) == (field, "too long")


@pytest.mark.parametrize(
    "value",
    ["ada", "ada@", "@example.com", "ada@example", "ada@@example.com", "ada lovelace@example.com"],
)
def test_an_address_that_cannot_be_one_is_refused(value):
    with pytest.raises(ContactInvalid) as exc:
        validate_contact({"email": value})
    assert exc.value.field == "email"


@pytest.mark.parametrize("value", ["ada+node@example.co.uk", "a.b@sub.example.com"])
def test_a_shallow_check_does_not_refuse_a_working_address(value):
    assert validate_contact({"email": value})["email"] == value


@pytest.mark.parametrize("value", ["+44 20 7946 0000", "(020) 7946-0000", "07700900123"])
def test_a_number_is_kept_as_typed(value):
    assert validate_contact({"phone": value})["phone"] == value


@pytest.mark.parametrize("value", ["not a number", "+44-EXT", "()"])
def test_a_phone_that_is_not_a_number_is_refused(value):
    with pytest.raises(ContactInvalid) as exc:
        validate_contact({"phone": value})
    assert exc.value.field == "phone"


def test_the_published_schema_matches_the_bounds():
    schema = contact_json_schema()

    assert schema["title"] == "NodeContact"
    assert schema["additionalProperties"] is False
    assert schema.get("required", []) == []
    assert sorted(schema["properties"]) == sorted(FULL)
    for field, maximum in (("first_name", 64), ("last_name", 64), ("email", 255), ("phone", 32)):
        branches = schema["properties"][field]["anyOf"]
        string_branch = next(branch for branch in branches if branch["type"] == "string")
        assert string_branch["maxLength"] == maximum
        assert {"type": "null"} in branches


def test_the_schema_is_fresh_every_call():
    first = contact_json_schema()
    first["properties"]["email"]["anyOf"][0]["maxLength"] = 1

    assert contact_json_schema()["properties"]["email"]["anyOf"][0]["maxLength"] == 255
