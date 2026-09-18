"""What `/openapi.json` publishes, and what it keeps back.

The public document is the node contract plus the unauthenticated reads. The
operation list is pinned: a route appearing in the public reference is a
publication decision, so it should arrive as a change to this file.
"""

import base64
import hashlib
import json
import re
from unittest.mock import patch

import pytest

from main import app
from routes.openapi_documents import node_contract, public_document
from routes.reference import CSP, DOCUMENTS, SCALAR_INTEGRITY, SCALAR_URL
from tests.nginx_helpers import VALUES, locations, render

PUBLIC_OPERATIONS = {
    ("get", "/api/custody/chain/{node_ref}"),
    ("get", "/api/custody/status"),
    ("get", "/api/custody/verify/{node_ref}"),
    ("get", "/api/data/archive"),
    ("get", "/api/data/archive/{key}"),
    ("get", "/api/health"),
    ("get", "/api/radar/accuracy"),
    ("get", "/api/radar/analytics"),
    ("get", "/api/radar/analytics/{node_ref}"),
    ("get", "/api/radar/anomalies"),
    ("get", "/api/radar/association/overlaps"),
    ("get", "/api/radar/association/status"),
    ("get", "/api/radar/data/aircraft-live.json"),
    ("get", "/api/radar/data/aircraft.json"),
    ("get", "/api/radar/data/receiver.json"),
    ("get", "/api/radar/nodes"),
    ("get", "/api/radar/status"),
    ("get", "/api/radar/stream"),
    ("get", "/api/stats/summary"),
    ("get", "/api/v1/ground-truth/aircraft"),
    ("get", "/api/v1/ground-truth/real"),
    ("get", "/api/v1/solver/aircraft"),
    ("post", "/v1/nodes/detection"),
    ("post", "/v1/nodes/heartbeat"),
    ("post", "/v1/nodes/register"),
    ("put", "/v1/nodes/config"),
    ("put", "/v1/nodes/contact"),
}

_REF = "#/components/schemas/"


@pytest.fixture(scope="module")
def document():
    return public_document(app.openapi(), app.description)


def _refs(node, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if ref.startswith(_REF):
            found.add(ref[len(_REF) :])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _refs(item, found)


def test_it_publishes_exactly_these_operations(document):
    published = {(method, path) for path, operations in document["paths"].items() for method in operations}
    assert published == PUBLIC_OPERATIONS


def test_no_account_admin_or_test_route_is_listed(document):
    internal = ("/api/admin/", "/api/auth/", "/api/test/", "/api/simulation/", "/api/sim/")
    assert not [path for path in document["paths"] if path.startswith(internal)]


def test_every_component_is_reached_by_a_published_operation(document):
    """A model only an internal route uses would describe that route by its shape."""
    schemas = document["components"]["schemas"]
    reached: set[str] = set()
    _refs(document["paths"], reached)
    while True:
        before = set(reached)
        for name in before:
            _refs(schemas.get(name, {}), reached)
        if reached == before:
            break
    assert set(schemas) == reached


def test_the_node_operations_are_the_contracts_own(document):
    contract = node_contract(app.openapi(), app.description)
    for path, operations in contract["paths"].items():
        assert document["paths"][path] == operations
    for name, schema in contract["components"]["schemas"].items():
        assert document["components"]["schemas"][name] == schema
    assert document["components"]["securitySchemes"] == contract["components"]["securitySchemes"]


def test_the_description_keeps_the_node_apis_sections_under_its_own_lead(document):
    description = document["info"]["description"]
    assert description.startswith("The RETINA server's public HTTP API")
    assert "## Error taxonomy" in description
    assert "internal to the map" not in description


def test_the_served_document_is_the_public_one(client, document):
    served = client.get("/openapi.json")
    assert served.status_code == 200
    assert served.json() == document


def test_the_whole_schema_is_for_an_administrator(client):
    with patch("core.users.AUTH_BYPASS", False):
        assert client.get("/api/admin/openapi.json").status_code == 401
    whole = client.get("/api/admin/openapi.json").json()
    assert "/api/admin/users" in whole["paths"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/docs/oauth2-redirect"])
def test_fastapis_own_pages_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_every_published_tag_is_declared_and_grouped_once(document):
    used = {tag for operations in document["paths"].values() for op in operations.values() for tag in op["tags"]}
    declared = [tag["name"] for tag in document["tags"]]
    grouped = [tag for group in document["x-tagGroups"] for tag in group["tags"]]
    assert set(declared) == used
    assert sorted(grouped) == sorted(declared)


def _inline_script(html: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    return scripts[0]


def test_the_page_loads_the_pinned_scalar_under_its_own_policy(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == CSP
    assert re.search(r"@scalar/api-reference@\d+\.\d+\.\d+/", SCALAR_URL)
    assert f'src="{SCALAR_URL}" integrity="{SCALAR_INTEGRITY}" crossorigin="anonymous"' in page.text


def test_the_policy_admits_the_inline_script_by_its_hash(client):
    script = _inline_script(client.get("/").text)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in CSP
    assert "'unsafe-inline'" not in CSP.split("script-src", 1)[1].split(";", 1)[0]


def test_the_page_renders_each_document_with_scalars_uploads_off(client):
    script = _inline_script(client.get("/").text)
    configs = json.loads(script[script.index("[") : script.rindex("]") + 1])
    assert [config["url"] for config in configs] == [document["url"] for document in DOCUMENTS]
    assert configs[0]["url"] == "/openapi.json"
    for config in configs:
        assert config["agent"] == {"disabled": True}
        assert config["mcp"] == {"disabled": True}
        assert config["withDefaultFonts"] is False


def _api_location(rendered: str, header: str) -> str | None:
    api = rendered[rendered.index(f"server_name {VALUES['HOST_API']};") :]
    api = api[: api.index("\nserver {")] if "\nserver {" in api else api
    return next((body for head, body in locations(api) if head.strip() == header), None)


def test_the_api_vhost_serves_tower_finders_schema_from_its_own_origin():
    body = _api_location(render(), "location = /openapi/tower-finder.json")
    assert body is not None
    assert "proxy_pass http://$tfs_upstream:8000" in body
    assert "rewrite ^ /openapi.json break;" in body


def test_without_tower_finder_the_location_is_not_rendered():
    rendered = render(VALUES | {"TOWER_FINDER_ENABLED": "false"})
    assert _api_location(rendered, "location = /openapi/tower-finder.json") is None
