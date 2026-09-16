"""The DigitalOcean read client: env handling and the thin wrappers over _get_json."""

import httpx
import pytest

from clients import digitalocean as do


def test_token_is_read_per_call(monkeypatch):
    monkeypatch.delenv(do.TOKEN_ENV, raising=False)
    assert do.token() == ""
    monkeypatch.setenv(do.TOKEN_ENV, "  abc  ")
    assert do.token() == "abc"


def test_droplet_tag_defaults(monkeypatch):
    monkeypatch.delenv(do.TAG_ENV, raising=False)
    assert do.droplet_tag() == "retina"
    monkeypatch.setenv(do.TAG_ENV, "fleet")
    assert do.droplet_tag() == "fleet"


async def test_get_json_refuses_without_token(monkeypatch):
    monkeypatch.delenv(do.TOKEN_ENV, raising=False)
    with pytest.raises(do.NotConfigured):
        await do._get_json("/uptime/checks")


async def test_wrappers_unwrap_the_envelope(monkeypatch):
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return {
            "/uptime/checks": {"checks": [{"id": "c1"}]},
            "/uptime/checks/c1/state": {"state": {"regions": {}}},
            "/droplets": {"droplets": [{"id": 7}]},
            "/monitoring/metrics/droplet/cpu": {"data": {"result": [{"metric": {}, "values": []}]}},
        }[path]

    monkeypatch.setattr(do, "_get_json", fake_get)
    assert await do.list_checks() == [{"id": "c1"}]
    assert await do.check_state("c1") == {"regions": {}}
    assert await do.list_droplets("retina") == [{"id": 7}]
    assert await do.droplet_metric("cpu", 7, 1, 2) == [{"metric": {}, "values": []}]
    assert calls[0] == ("/uptime/checks", {"per_page": 200})
    assert calls[1] == ("/uptime/checks/c1/state", None)
    assert calls[2] == ("/droplets", {"tag_name": "retina", "per_page": 200})
    assert calls[3] == ("/monitoring/metrics/droplet/cpu", {"host_id": 7, "start": 1, "end": 2})


async def test_aclose_closes_the_pool(monkeypatch):
    # monkeypatch restores the module's own client, which the lifespan closes for real.
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    monkeypatch.setattr(do, "_client", client)
    await do.aclose()
    assert do._client.is_closed


async def test_get_json_sends_the_bearer_per_request_and_raises_on_status(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "tok")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/boom"):
            return httpx.Response(500, json={"message": "server error"})
        return httpx.Response(200, json={"checks": [{"id": "c1"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://api.digitalocean.com/v2", transport=transport) as client:
        monkeypatch.setattr(do, "_client", client)
        assert await do._get_json("/uptime/checks", {"per_page": 200}) == {"checks": [{"id": "c1"}]}
        assert seen == ["Bearer tok"]
        with pytest.raises(httpx.HTTPStatusError):
            await do._get_json("/boom")
