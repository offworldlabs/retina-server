"""Which client a request counts as, for the limits kept per caller.

One rule for every in-process limit keyed on the caller's address: the
magic-link quota (routes/auth.py) and the public archive reads
(services/archive_rate_limit.py). A limit keyed on anything a caller can
choose bounds nothing, so the address is the transport peer as the ASGI server
hands it over, never a header read here.
"""

from ipaddress import IPv6Address, ip_address, ip_network

from fastapi import Request


def client_source(request: Request) -> str:
    """Use the transport peer after the ASGI server's trusted-proxy handling.

    Never read forwarding headers here: deployed nginx appends its validated
    client address and uvicorn trusts only the configured proxy peer. IPv6
    addresses in one /64 share a quota, including privacy-address rotation.
    This is an admission limit rather than an identity; mobile clients may roam.

    Finer than sessions.coarse_ip on purpose, so keep the two apart: that one
    is a label kept on file, coarse so no address is stored, where a /24 or a
    /48 here would put unrelated callers in one quota.
    """
    host = request.client.host if request.client else None
    if not host:
        return "unknown"
    try:
        address = ip_address(host)
    except ValueError:
        return host
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ip_network(f"{address}/64", strict=False))
    return str(address)
