"""The OpenAPI documents this server publishes, cut from the application's schema.

`node_contract` is the v1 node API's wire contract, which
scripts/generate_openapi.py commits to `contracts/nodes-v1.openapi.yaml`.
`public_document` is that contract plus the unauthenticated reads, and is what
`/openapi.json` serves; the application's whole schema, with its admin, account
and test routes, goes only to an administrator (routes/reference.py).

Everything in a document comes from the application: the descriptions and the
`x-` annotations from the route decorators, the schemas from the Pydantic
models, the security scheme from the dependency. The constants the contract
reaches for by name (`NODE_API_VERSION`, `NODE_API_TAGS`, `NODE_API_SERVERS`)
live in routes/nodes.py beside the router they describe. It also reaches for the
schema builders of the two wire objects the routes carry inline, to recognise
them in the document and hoist each into a component the operations reference;
see `_INJECTED_SCHEMAS` and `_hoisted`.

Numeric bounds publish as floats (`minimum: 1.0` rather than `1`) because
FastAPI validates its own output through `openapi.models`, whose
`Schema.minimum` and `Schema.maximum` are typed `float`. `$ref` names are drawn
from the whole application's model namespace, so a future model named
`ErrorBody` or `Agreements` added anywhere in the app would rename every `$ref`
in the contract and break a pinned consumer, which the CI gate would catch as a
confusing diff rather than a clean addition.
"""

from typing import Any

from routes.nodes import NODE_API_SERVERS, NODE_API_TAGS, NODE_API_VERSION, is_node_path
from services.node_config import config_json_schema
from services.node_contact import contact_json_schema

TITLE = "RETINA node ingest"
PUBLIC_TITLE = "RETINA server API"

# Outside the node API only reads are published: every write there is an
# internal ingest path keyed on X-API-Key.
_PUBLIC_READ_PREFIXES = ("/api/v1/", "/api/radar/", "/api/data/", "/api/stats/", "/api/custody/")
_PUBLIC_READ_PATHS = frozenset({"/api/health"})

# Stands in for the application description's lead paragraph, which speaks for
# the node contract alone; the sections after it apply here as written.
_PUBLIC_INTRO = """\
The RETINA server's public HTTP API, in two parts:

- **Node ingest** (`/v1/nodes`): the contract RETINA nodes are built against,
  versioned as a unit and committed as `contracts/nodes-v1.openapi.yaml`.
- **Public reads**: the unauthenticated endpoints the live map, the dashboard
  and the data archive are built on. They are not versioned and move with those
  surfaces.

Account, administration and test routes are not listed.
"""

# The public reads' tags, which each router declares by name.
READ_TAGS = [
    {"name": "aircraft", "description": "Solver positions and ADS-B ground truth, in a shape kept for outside use."},
    {"name": "radar", "description": "The live map's feeds: aircraft, receivers, node status and the aircraft stream."},
    {"name": "analytics", "description": "Per-node analytics, association between nodes, accuracy and anomalies."},
    {"name": "archive", "description": "Archived detection files, by day and node."},
    {"name": "custody", "description": "Each node's custody hash chain, and its verification."},
    {"name": "stats", "description": "Which illuminator towers nodes have selected."},
    {"name": "health", "description": "Liveness, and readiness with `strict=1`."},
]

_REF_PREFIX = "#/components/schemas/"

# The components this module names into existence rather than finding among the
# application's models. Each is built by a services leaf and carried inline by
# the routes that take it, because Pydantic resolves every `$ref` it emits
# against its own definitions and neither schema is one of its models, so the
# reference is made here: this is already the layer that shapes the document,
# and one component is what a generated client needs to produce one type for one
# wire object. A schema carried by a single operation is hoisted too, so the
# name its type is generated from is the document's rather than the generator's.
_INJECTED_SCHEMAS: dict[str, Any] = {
    "NodeConfig": config_json_schema,
    "NodeContact": contact_json_schema,
}


def _referenced(node: Any, found: set[str]) -> None:
    """Every schema reachable from `node`, transitively.

    Walked rather than taken wholesale: the application's components hold every
    model in the API, and a document carrying models none of its operations
    reach would be describing something else.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
            name = ref[len(_REF_PREFIX) :]
            if name not in found:
                found.add(name)
        for value in node.values():
            _referenced(value, found)
    elif isinstance(node, list):
        for item in node:
            _referenced(item, found)


def _closure(paths: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    found: set[str] = set()
    _referenced(paths, found)
    # A model reached only through another model, `Agreements` through
    # `RegisterRequest` for instance, appears on this second pass.
    seen: set[str] = set()
    while found - seen:
        for name in sorted(found - seen):
            seen.add(name)
            _referenced(schemas.get(name, {}), found)
    return {name: schemas[name] for name in sorted(found) if name in schemas}


def _hoisted(node: Any, injected: dict[str, dict[str, Any]]) -> Any:
    """`node` with every inline copy of an injected schema replaced by its `$ref`.

    Rebuilds rather than mutates: `app.openapi()` caches its result, and editing
    it in place would leave the application's own document holding a reference
    to a component only this one defines.

    Matched by equality on the whole schema, so a partial copy is left alone
    rather than silently referred to something it does not equal.
    tests/test_node_openapi.py holds the other end, that the operations really
    do end up referring to them.
    """
    if isinstance(node, dict):
        for name, inline in injected.items():
            if node == inline:
                return {"$ref": _REF_PREFIX + name}
        return {key: _hoisted(value, injected) for key, value in node.items()}
    if isinstance(node, list):
        return [_hoisted(item, injected) for item in node]
    return node


def _node_paths(paths: dict[str, Any]) -> dict[str, Any]:
    """The node API's operations, with FastAPI's automatic 422 dropped.

    That 422 describes a `RequestValidationError` rendered by the framework, and
    under this prefix none reaches the wire: the taxonomy handler in
    routes/nodes.py converts every one into a 400 in the contract's `Error`
    shape, which the routes declare explicitly. Publishing a status no request
    can produce is the sort of thing generating the contract is meant to stop, so
    it is removed here rather than described. tests/test_node_openapi.py holds
    both ends of that: no operation publishes 422, and a malformed body really
    does answer 400.
    """
    node = {}
    for path, operations in paths.items():
        if not is_node_path(path):
            continue
        node[path] = {
            method: {
                key: ({code: body for code, body in value.items() if code != "422"} if key == "responses" else value)
                for key, value in operation.items()
            }
            for method, operation in operations.items()
        }
    return node


def _hoisted_parts(schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The application's paths and component schemas, injected schemas hoisted."""
    injected = {name: build() for name, build in _INJECTED_SCHEMAS.items()}
    declared = schema["components"]["schemas"]
    clashes = sorted(set(injected) & set(declared))
    if clashes:
        # A Pydantic model of one of these names anywhere in the application
        # would be published under it instead, silently, and a pinned consumer
        # would see one type become another. Refused rather than clobbered.
        raise RuntimeError(f"{', '.join(clashes)} are already application models; the contract cannot inject them")
    return _hoisted(schema["paths"], injected), _hoisted(declared, injected) | injected


def _components(paths: dict[str, Any], schemas: dict[str, Any], security: dict[str, Any]) -> dict[str, Any]:
    """The schemas and security schemes `paths` reach, and nothing else."""
    components: dict[str, Any] = {"schemas": _closure(paths, schemas)}
    used = {name for operation in _operations(paths) for entry in operation.get("security", []) for name in entry}
    if used:
        components["securitySchemes"] = {name: security[name] for name in sorted(used) if name in security}
    return components


def _operations(paths: dict[str, Any]) -> list[dict[str, Any]]:
    return [operation for operations in paths.values() for operation in operations.values()]


def node_contract(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The v1 node API's contract, from the application's schema and description.

    The description is the application's own, so the error taxonomy and the `x-`
    vocabulary have one home rather than a copy here that can disagree with it.
    """
    paths, schemas = _hoisted_parts(schema)
    paths = _node_paths(paths)
    return {
        "openapi": schema["openapi"],
        "info": {"title": TITLE, "version": NODE_API_VERSION, "description": description},
        "servers": NODE_API_SERVERS,
        "tags": NODE_API_TAGS,
        "paths": paths,
        "components": _components(paths, schemas, schema.get("components", {}).get("securitySchemes", {})),
    }


def is_public_read(path: str, method: str) -> bool:
    return method == "get" and (path in _PUBLIC_READ_PATHS or path.startswith(_PUBLIC_READ_PREFIXES))


def public_document(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The node contract's operations plus the public reads, for `/openapi.json`.

    The node operations are the contract's own, so the live reference and the
    committed file cannot describe the node API differently.
    """
    paths, schemas = _hoisted_parts(schema)
    public = _node_paths(paths)
    for path, operations in paths.items():
        reads = {method: operation for method, operation in operations.items() if is_public_read(path, method)}
        if reads and not is_node_path(path):
            public[path] = reads
    sections = description.find("\n## ")
    return {
        "openapi": schema["openapi"],
        "info": {
            "title": PUBLIC_TITLE,
            "version": schema["info"]["version"],
            "description": _PUBLIC_INTRO + (description[sections:] if sections >= 0 else ""),
        },
        "tags": NODE_API_TAGS + READ_TAGS,
        "x-tagGroups": [
            {"name": "Node ingest", "tags": [tag["name"] for tag in NODE_API_TAGS]},
            {"name": "Public reads", "tags": [tag["name"] for tag in READ_TAGS]},
        ],
        "paths": public,
        "components": _components(public, schemas, schema.get("components", {}).get("securitySchemes", {})),
    }
