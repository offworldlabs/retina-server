"""nginx takes the custody batch upload at its own body cap.

The default cap is 1 MiB, which a 15-minute batch outgrows. The number must
match the origin's CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES, and no other location
may raise the cap.
"""

import pytest

from config.constants import CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES
from tests.nginx_helpers import VALUES, locations, render

_HEADER = "location = /api/custody/chain-entry"


def _vhost(rendered: str, host: str) -> str:
    start = rendered.index(f"server_name {host};")
    end = rendered.find("\nserver {", start)
    return rendered[start : end if end != -1 else len(rendered)]


def test_the_cap_is_a_whole_number_of_kib():
    assert CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES % 1024 == 0


# The location that carries /api/custody/chain-entry today, per vhost.
_GENERIC = {"HOST_MAIN": "location /api/", "HOST_API": "location /"}


@pytest.mark.parametrize("host", ["HOST_MAIN", "HOST_API"])
def test_the_exact_location_carries_the_cap(host):
    locs = locations(_vhost(render(), VALUES[host]))
    exact = [body for header, body in locs if header.strip() == _HEADER]
    generic = [body for header, body in locs if header.strip() == _GENERIC[host]]
    assert len(exact) == 1
    assert len(generic) == 1
    cap = f"client_max_body_size {CUSTODY_CHAIN_ENTRY_MAX_BODY_BYTES // 1024}k;"
    assert cap in exact[0]
    # Same proxy and rate-limit zone as the location it replaces for this path.
    assert exact[0].replace(cap, "").split() == generic[0].split()


def test_no_other_location_gains_a_body_cap():
    rendered = render()
    capped = [header.strip() for header, body in locations(rendered) if "client_max_body_size" in body]
    assert capped.count(_HEADER) == 2
    assert set(capped) == {_HEADER}
