"""What this server accepts as an email address, in one place.

Two endpoints judge addresses: the site contact a node reports, and the address
a node is claimed with. A node told its address is fine by one and refused by
the other has nothing to act on, so the grammar is here rather than in each.

Deliberately shallow. The check is against a typo, and a stricter grammar
refuses addresses that work: the local part admits almost anything once quoted,
and the domain half of a real address can be an internal name this server has
never heard of. Nothing here says an address is real, only that it has the shape
of one.

A leaf on the same terms as services/node_config.py: nothing beyond the standard
library may be imported.

Normalisation is the caller's. The site contact keeps an address as it was
typed, because it is shown to a person about to write to it; claiming lower
cases, because there the address is a key into accounts and has to match what
sign-in stored.
"""

# The column width shared by node_contacts.email and node_claims.email. A bound
# on the trimmed form rather than on the bytes sent.
MAX_ADDRESS_LENGTH = 255


def is_address(value: str) -> bool:
    """Whether `value` has the shape of an email address.

    Judged on the value as given: an address with surrounding whitespace is
    refused rather than trimmed, since trimming is part of normalising and this
    is only the grammar.
    """
    if any(character.isspace() for character in value):
        return False
    local, separator, domain = value.partition("@")
    if not separator or not local or "@" in domain:
        return False
    labels = domain.split(".")
    # A bare hostname is refused even though it resolves on some networks: an
    # address we cannot reach from the mail path is worse than one refused at
    # the point somebody can still correct it.
    return len(labels) >= 2 and all(labels)
