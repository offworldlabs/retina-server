"""Whether a browser's request comes from a page of the console itself.

A browser sends the session cookie with a WebSocket handshake whatever page
opened it: SameSite=Lax lets a sibling host on the same site through, and the
CORS middleware in main.py does not look at websockets. What the browser cannot
leave out is the page's Origin, so that is what routes/streaming.py judges the
owner feed's handshake by, before it reads the cookie.

The console is served from HOST_APP and HOST_ADMIN, as each environment's
compose overlay names them (deploy/nginx/nginx.conf.template). Not HOST_MAIN,
which every deployed environment routes to tower-finder-service, and not
HOST_API, whose only page is the API reference. Hosts are compared and ports
are not: the cookie being protected is the host's, on any port. RETINA_ENV=dev
adds the loopback names the dev server runs on (localhost, *.localhost,
127.0.0.1, ::1).
"""

import ipaddress
import os
from urllib.parse import urlsplit


def foreign_origin(origin: str | None) -> bool:
    """Whether a request's Origin header names a page that is not the console's.

    No Origin at all is not a browser's request, and has no cookie to send but
    its own, so it is not foreign: the cookie decides it as before. "null",
    which a sandboxed frame or a file sends, is.
    """
    if origin is None:
        return False
    try:
        parts = urlsplit(origin)
        host = parts.hostname
    except ValueError:
        return True
    if parts.scheme not in ("http", "https") or not host:
        return True
    console = {_hostname(os.getenv("HOST_APP", "")), _hostname(os.getenv("HOST_ADMIN", ""))} - {None}
    if host in console:
        return False
    return not (os.getenv("RETINA_ENV", "").lower() == "dev" and _loopback(host))


def _hostname(configured: str) -> str | None:
    """A configured host as an Origin's hostname reads: lower case, any port dropped."""
    try:
        return urlsplit(f"//{configured.strip()}").hostname or None
    except ValueError:
        return None


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
