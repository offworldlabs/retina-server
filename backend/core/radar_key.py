"""Who the radar-key write routes admit.

The detection, custody, stats, analytics and simulation push routes admit a
caller by its X-API-Key alone. A server with RADAR_API_KEY set compares the
header to it in constant time. A server without one admits any caller only on
a laptop, and anywhere else refuses to start, since those routes would
otherwise be open to everyone who can reach them.

A laptop is RETINA_ENV=dev (`just up`, a bare uvicorn) or the anonymous-admin
opt-in (core.users.AUTH_BYPASS, which the local compose sets and which a
configured Cloudflare Access turns off on every droplet).
"""

import hmac
import logging
import os

from core import users

_RETINA_ENV = os.getenv("RETINA_ENV", "").lower()


def _keyless_admission_allowed() -> bool:
    # AUTH_BYPASS read from core.users on each call, not copied, so a test that
    # turns it off turns it off here too.
    return _RETINA_ENV == "dev" or users.AUTH_BYPASS


def radar_key_admits(configured: str, presented: str) -> bool:
    """Whether a caller presenting `presented` passes a route keyed on `configured`."""
    if configured:
        return hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8"))
    return _keyless_admission_allowed()


if not os.environ.get("RADAR_API_KEY", ""):
    if not _keyless_admission_allowed():
        raise RuntimeError(
            f"RADAR_API_KEY is not set (RETINA_ENV={_RETINA_ENV!r}), so the server refuses to start: the "
            "detection, custody, stats, analytics and simulation push routes admit callers only by that key. "
            "On a server, set RADAR_API_KEY in backend/.env (the fleet reads the same line) and recreate the "
            "containers with `docker compose up -d`: `docker compose restart` keeps the environment they "
            "were created with. "
            "RETINA_ENV=dev and AUTH_ALLOW_ANONYMOUS_ADMIN=1 are for a laptop only: do not set either on a "
            "server, where RETINA_ENV=dev would open these routes to anyone and relax other guards, and the "
            "anonymous-admin bypass stays off wherever Cloudflare Access is configured."
        )
    logging.getLogger(__name__).warning(
        "RADAR_API_KEY is not set: the detection, custody, stats, analytics and simulation push routes "
        "accept any caller on this laptop."
    )
