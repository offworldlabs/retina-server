"""The shared address grammar.

Every case here is one services/node_contact.py refused before the grammar moved
out of it, so a behaviour change shows up as a failure rather than as a contact
document that starts accepting something it did not.
"""

import pytest

from services.email_address import MAX_ADDRESS_LENGTH, is_address


@pytest.mark.parametrize(
    "value",
    [
        "ada@example.com",
        "ada.lovelace+nodes@example.co.uk",
        "a@b.co",
        "ADA@EXAMPLE.COM",
    ],
)
def test_addresses_are_accepted(value):
    assert is_address(value)


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("ada example.com", "no at sign"),
        ("@example.com", "no local part"),
        ("ada@", "no domain"),
        ("ada@@example.com", "two at signs"),
        ("ada@example@com", "at sign in the domain"),
        ("ada@localhost", "single-label domain"),
        ("ada@example.", "empty trailing label"),
        ("ada@.com", "empty leading label"),
        ("ada @example.com", "inner space"),
        (" ada@example.com", "leading space"),
        ("ada@example.com ", "trailing space"),
        ("ada\t@example.com", "tab"),
        ("ada\n@example.com", "newline"),
        ("", "empty"),
    ],
)
def test_non_addresses_are_refused(value, why):
    assert not is_address(value), why


def test_the_bound_matches_the_column_the_two_tables_share():
    assert MAX_ADDRESS_LENGTH == 255
