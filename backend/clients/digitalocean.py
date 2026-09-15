"""DigitalOcean API, read-only, for the admin Infrastructure page.

Uptime check state and droplet metrics only. Every function raises on an
API error and NotConfigured when no token is set; the caller decides what
a missing item costs the page.
"""

import os

import httpx

_BASE = "https://api.digitalocean.com/v2"
_TIMEOUT_S = 10.0
TOKEN_ENV = "DIGITALOCEAN_READ_TOKEN"
TAG_ENV = "DIGITALOCEAN_DROPLET_TAG"
DEFAULT_TAG = "retina"


# One pooled client for the process: a cold snapshot is up to two dozen calls,
# and a fresh TLS handshake apiece does not fit the dashboard's 10 s abort. No
# token is needed to construct it; the bearer goes on each request instead.
_client = httpx.AsyncClient(base_url=_BASE, timeout=_TIMEOUT_S)


class NotConfigured(RuntimeError):
    """No token: the page is off, not broken."""


def token() -> str:
    # Read per call: main.py calls load_dotenv() after the route imports, so an
    # import-time read sees an empty environment on a bare `uvicorn main:app`.
    return os.getenv(TOKEN_ENV, "").strip()


def droplet_tag() -> str:
    return os.getenv(TAG_ENV, "").strip() or DEFAULT_TAG


async def _get_json(path: str, params: dict | None = None) -> dict:
    tok = token()
    if not tok:
        raise NotConfigured(f"{TOKEN_ENV} is not set")
    response = await _client.get(path, params=params, headers={"Authorization": f"Bearer {tok}"})
    response.raise_for_status()
    return response.json()


async def aclose() -> None:
    # main.py's lifespan closes the pool on shutdown.
    await _client.aclose()


async def list_checks() -> list[dict]:
    return (await _get_json("/uptime/checks", {"per_page": 200})).get("checks", [])


async def check_state(check_id: str) -> dict:
    return (await _get_json(f"/uptime/checks/{check_id}/state")).get("state", {})


async def list_droplets(tag: str) -> list[dict]:
    return (await _get_json("/droplets", {"tag_name": tag, "per_page": 200})).get("droplets", [])


async def droplet_metric(name: str, host_id: int, start: int, end: int) -> list[dict]:
    """One Prometheus-style result list: [{"metric": {labels}, "values": [[ts, "v"], ...]}]."""
    params = {"host_id": host_id, "start": start, "end": end}
    return (await _get_json(f"/monitoring/metrics/droplet/{name}", params)).get("data", {}).get("result", [])
