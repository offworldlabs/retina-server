"""What `/openapi.json` publishes, and what it keeps back.

The public document is the node contract plus the unauthenticated reads. The
operation list is pinned: a route appearing in the public reference is a
publication decision, so it should arrive as a change to this file.
"""

import base64
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from main import app
from routes.openapi_documents import node_contract, public_document
from routes.reference import CSP, DOCUMENTS, FAVICON, SCALAR_INTEGRITY, SCALAR_URL, SHELL_CSS, THEME_CSS
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
    ("get", "/v1/nodes/claim"),
    ("post", "/v1/nodes/claim/resend"),
    ("post", "/v1/nodes/detection"),
    ("post", "/v1/nodes/heartbeat"),
    ("post", "/v1/nodes/register"),
    ("put", "/v1/nodes/claim"),
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


def _configs(client) -> list[dict]:
    script = _inline_script(client.get("/").text)
    return json.loads(re.search(r"^  var documents = (.*);$", script, re.M).group(1))


def test_the_page_renders_each_document_with_scalars_uploads_off(client):
    configs = _configs(client)
    assert [config["url"] for config in configs] == [document["url"] for document in DOCUMENTS]
    assert configs[0]["url"] == "/openapi.json"
    for config in configs:
        assert config["agent"] == {"disabled": True}
        assert config["mcp"] == {"disabled": True}
        assert config["withDefaultFonts"] is False


def test_blah2_arms_document_is_the_one_committed_in_blah2_arm(client):
    config = next(c for c in _configs(client) if c["slug"] == "blah2-arm")
    assert config["url"] == "https://cdn.jsdelivr.net/gh/offworldlabs/blah2-arm@main/api/openapi.json"
    assert config["hideTestRequestButton"] is True


def test_the_policy_admits_blah2_arms_document_and_no_wider_source(client):
    connect = client.get("/").headers["content-security-policy"].split("connect-src ", 1)[1].split(";", 1)[0]
    assert connect.split() == ["'self'", "https://cdn.jsdelivr.net/gh/offworldlabs/blah2-arm@main/api/openapi.json"]


def test_the_palette_is_the_pages_before_it_is_scalars(client):
    """In the page's own stylesheet, and the theme script ahead of the bundle,
    so the first paint is already in the reader's mode, header included."""
    page = client.get("/").text
    style = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    assert THEME_CSS in style and SHELL_CSS in style
    assert not any("customCss" in config for config in _configs(client))
    assert page.index("<script>") < page.index(f'<script src="{SCALAR_URL}"')


def test_the_headers_switch_stands_in_for_scalars_toggle(client):
    page = client.get("/").text
    assert re.findall(r'data-theme="(\w+)"', page) == ["light", "system", "dark"]
    assert all(config["hideDarkModeToggle"] is True for config in _configs(client))


def test_the_header_links_to_this_environments_console(client, monkeypatch):
    monkeypatch.setenv("HOST_APP", "staging-app.retina.fm")
    monkeypatch.setenv("FORCE_HTTPS", "true")
    page = client.get("/").text
    assert '<a class="retina-mark" href="https://staging-app.retina.fm/"' in page
    assert '<a class="retina-open" href="https://staging-app.retina.fm/">Open app</a>' in page


def test_without_host_app_the_header_links_nowhere(client, monkeypatch):
    monkeypatch.delenv("HOST_APP", raising=False)
    page = client.get("/").text
    assert '<span class="retina-mark"' in page
    assert "retina-open" not in page.split("</style>", 1)[1]


def test_the_policy_admits_the_favicon(client):
    img_src = next(d for d in CSP.split("; ") if d.startswith("img-src ")).split()[1:]
    assert FAVICON.startswith("data:") and "data:" in img_src
    assert f'<link rel="icon" href="{FAVICON}">' in client.get("/").text


@pytest.fixture(scope="module")
def vendored_scalar() -> str:
    version = re.search(r"@scalar/api-reference@([\d.]+)/", SCALAR_URL).group(1)
    root = Path(__file__).resolve().parents[2]
    return (root / f"dashboard/public/vendor/scalar-api-reference-{version}/standalone.js").read_text()


def test_the_pinned_scalar_still_has_what_the_shell_names(vendored_scalar):
    """SHELL_CSS reaches into Scalar's markup, which a new version is free to
    rename without an error; the pin moving is what this catches."""
    classes = re.findall(r"(?<!\d)\.([a-z](?:[\w-]|\\/)*)", SHELL_CSS)
    assert classes
    missing = [
        name
        for name in {c.replace("\\/", "/") for c in classes}
        if not re.search(rf"[\s`'\"]{re.escape(name)}[\s`'\"]", vendored_scalar)
    ]
    unread = [name for name in re.findall(r"(--scalar-[\w-]+):", SHELL_CSS) if f"var({name}" not in vendored_scalar]
    options = [name for name in ("forceDarkModeState", "hideDarkModeToggle") if name not in vendored_scalar]
    assert (missing, unread, options) == ([], [], [])


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
