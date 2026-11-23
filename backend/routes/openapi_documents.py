"""The OpenAPI documents this server publishes, cut from the application's schema.

`node_contract` is the v1 node API's wire contract, which
scripts/generate_openapi.py commits to `contracts/nodes-v1.openapi.yaml`.
`data_contract` is the keyed data API's, committed to
`contracts/data-v1.openapi.yaml`: every route behind the data-access gate, found
from the dependency rather than from a path list, so a route gated later joins
it when the file is regenerated. `public_document` is the node contract plus the
unauthenticated reads and the keyed reads, and is what `/openapi.json` serves;
the application's whole schema, with its admin, account and test routes, goes
only to an administrator (routes/reference.py).

Everything in a document comes from the application: the descriptions and the
`x-` annotations from the route decorators, the schemas from the Pydantic
models, the security scheme from the dependency. The constants the contract
reaches for by name (`NODE_API_VERSION`, `NODE_API_TAGS`, `NODE_API_SERVERS`)
live in routes/nodes.py beside the router they describe, and the data
contract's (`DATA_API_VERSION`, `DATA_API_TAGS`, `DATA_API_SERVERS`) in
routes/data_v1.py. It also reaches for the
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

from collections.abc import Iterable
from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts

from core.data_access import require_data_access
from routes.data_v1 import DATA_API_SERVERS, DATA_API_TAGS, DATA_API_VERSION
from routes.nodes import NODE_API_SERVERS, NODE_API_TAGS, NODE_API_VERSION, is_node_path
from services.node_claim import claim_json_schema
from services.node_config import config_json_schema
from services.node_contact import contact_json_schema

TITLE = "RETINA node ingest"
PUBLIC_TITLE = "RETINA server API"
DATA_TITLE = "RETINA keyed data API"

# The ways to present a key, as the data-access gate reads them.
DATA_SECURITY_SCHEMES: dict[str, Any] = {
    "ApiKeyHeader": {
        "type": "apiKey",
        "in": "header",
        "name": "x-apikey",
        "description": "The API key, in the `x-apikey` header.",
    },
    "ApiKeyBearer": {
        "type": "http",
        "scheme": "bearer",
        "description": "The API key, as `Authorization: Bearer <key>`.",
    },
}
_DATA_SECURITY = [{"ApiKeyHeader": []}, {"ApiKeyBearer": []}]

DATA_DESCRIPTION = """\
Live aircraft, aircraft lookup and track history from the RETINA network's real
nodes, for outside clients. The synthetic fleet is never served, to any caller.

## Authentication

Send the API key as `x-apikey: <key>` or as `Authorization: Bearer <key>`.
Websockets take it as `?apikey=<key>`. A signed-in account that is entitled to
the data API can also use its session cookie, which is how the dashboard calls
it.

A refused request answers with `{"detail": {"reason", "message"}}`:

- `401`: `missing_credentials` (no key was sent) or `invalid_key`.
- `403`: `not_entitled`, `lapsed` or `grant_ended`.
- `429`: `rate_limited`, with `Retry-After`, `X-RateLimit-Limit`,
  `X-RateLimit-Remaining` and `X-RateLimit-Reset` headers; or `stream_limit`,
  when more streams are open than the key's tier allows.

## Limits

Limits are per key and set by its tier: requests per minute, and concurrent
streams (`/api/v1/stream` and `/ws/v1/aircraft` each hold one while open).

## Live filters

`/api/v1/aircraft`, `/api/v1/stream` and `/ws/v1/aircraft` take the same
filters, all optional and combined with AND:

- `bbox=south,west,north,east`, in degrees. A west greater than the east
  crosses the antimeridian.
- `lat`, `lon` and `radius_nm` together: aircraft within that great-circle
  distance, in nautical miles.
- `alt_min`, `alt_max` (feet) and `gs_min`, `gs_max` (knots).
- `hex`: comma-separated ICAO addresses, up to 1,000.
- `callsign`: a prefix, case-insensitive.

A refused parameter is a `422` whose `detail` is a message string, or FastAPI's
validation list for a value of the wrong type.

## Aircraft rows

Rows follow readsb's `aircraft.json`: `hex`, `flight`, `lat`, `lon`, `alt_baro`
(feet), `gs` (knots), `track` (degrees) and `seen` (seconds), plus
`position_source`, `multinode`, `n_nodes`, `pos_sigma_m`, `node_refs` and
`is_anomalous`. A value that is not known is omitted.

## Lookup

`/api/v1/lookup` takes exactly one of `hex` or `callsign` and returns the
matching aircraft with a `trail` of up to 60 points.

## History

`/api/v1/history` returns archived positions for a window: `start` and `end`
(epoch milliseconds or ISO-8601; `end` is exclusive), narrowed by `hex` and/or
`bbox`, `limit` rows a page (default 1,000, at most 10,000) and `cursor`, the
previous page's `next_cursor` with the same window and filters. The body is
`{"aircraft": [...], "next_cursor": string | null}`.

The window may span at most the caller's tier `max_history_span_s`; for
administrators and internal services, which have no tier, that is 604,800
seconds. A longer span is a `422` naming the cap. A `503` means an archived day
could not be loaded from cold storage; retry it.

## Websocket

`wss://api.retina.fm/ws/v1/aircraft?apikey=<key>`, with the live filters in the
query. Websockets are not part of OpenAPI, so the protocol is described here.

Server to client, JSON text:

- `{"type": "aircraft", "now": <epoch seconds>, "aircraft": [rows]}`, at once,
  on each new flush of the feed (about once a second) and right after a filters
  message is taken.
- `{"type": "error", "reason": "invalid_filters" | "bad_message", "message"}`.

Client to server: `{"type": "filters", ...}` replaces the filters wholly, under
the same names; `bbox` may be a list of four numbers and `hex` a list, and null
means no filter. A refused message keeps the old filters and the socket.

A connection refused at connect gets an HTTP response to its upgrade request,
with the status, headers and body a REST call gets, and no socket opens:

- `401` (`missing_credentials`, `invalid_key`) or `403` (`not_entitled`,
  `lapsed`, `grant_ended`, or `forbidden_origin` for a connection without a
  key from a page that is not the console's);
- `422` naming the rule a filter in the query breaks;
- `429` `rate_limited` or `stream_limit`, with `Retry-After`.

Once open, the socket is closed with `1008` and the refusal reason when the
access check, repeated every 30 seconds, refuses because the key was revoked
or the entitlement lapsed or ended.

## Server-sent events

`/api/v1/stream` carries the same feed as `text/event-stream`:

- `event: aircraft`, `data: {"now", "aircraft"}`, at once and on each new flush.
- A `: keepalive` comment after 15 seconds without a write.
- `event: error`, `data: {"reason", "message"}`, after which the stream ends,
  when the 30-second access check refuses.

`stream_limit` is a `429` before the stream starts.
"""

# Outside the node API only reads are published: every write there is an
# internal ingest path keyed on X-API-Key.
_PUBLIC_READ_PREFIXES = ("/api/v1/", "/api/radar/", "/api/data/", "/api/stats/", "/api/custody/")
_PUBLIC_READ_PATHS = frozenset({"/api/health"})

# Stands in for the application description's lead paragraph, which speaks for
# the node contract alone; the sections after it apply here as written.
_PUBLIC_INTRO = """\
The RETINA server's public HTTP API, in three parts:

- **Node ingest** (`/v1/nodes`): the contract RETINA nodes are built against,
  versioned as a unit and committed as `contracts/nodes-v1.openapi.yaml`.
- **Public reads**: the unauthenticated endpoints the live map, the dashboard
  and the data archive are built on. They are not versioned and move with those
  surfaces.
- **Keyed data** (`/api/v1/aircraft`, `/api/v1/lookup`, `/api/v1/history`,
  `/api/v1/stream`): live aircraft, lookup and track history for outside
  clients. These need an API key, and their contract is committed as
  `contracts/data-v1.openapi.yaml`.

Account, administration and test routes are not listed.
"""

# The public reads' tags, which each router declares by name.
READ_TAGS = [
    {"name": "aircraft", "description": "Solver positions and ADS-B ground truth, in a shape kept for outside use."},
    {"name": "radar", "description": "The live map's feeds: aircraft, receivers, node status and the aircraft stream."},
    {"name": "analytics", "description": "Per-node analytics, association between nodes, accuracy and anomalies."},
    {
        "name": "archive",
        "description": "Archived detection files, by day and node, and published solves, by time window.",
    },
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
    "NodeClaimRequest": claim_json_schema,
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


def _gated(dependencies: Iterable[Any]) -> bool:
    return any(dep.call is require_data_access or _gated(dep.dependencies) for dep in dependencies)


def keyed_operations(routes: Iterable[Any]) -> set[tuple[str, str]]:
    """The (path, method) of every route that depends on the data-access gate.

    Found from the dependency, router-level ones included, so a route gated
    later joins the data contract when it is regenerated rather than needing a
    path added to a list. `routes` is the application's, whose included routers
    are walked by FastAPI for their full paths and dependencies.
    """
    return {
        (route.path, method.lower())
        for route in iter_route_contexts(list(routes))
        if isinstance(route.original_route, APIRoute) and _gated(route.dependant.dependencies)
        for method in route.methods
    }


def data_contract(schema: dict[str, Any], routes: Iterable[Any]) -> dict[str, Any]:
    """The keyed data API's contract: the gated operations, each taking a key."""
    keyed = keyed_operations(routes)
    paths, schemas = _hoisted_parts(schema)
    paths = {
        path: {
            method: {**operation, "security": _DATA_SECURITY}
            for method, operation in operations.items()
            if (path, method) in keyed
        }
        for path, operations in paths.items()
    }
    paths = {path: operations for path, operations in paths.items() if operations}
    return {
        "openapi": schema["openapi"],
        "info": {"title": DATA_TITLE, "version": DATA_API_VERSION, "description": DATA_DESCRIPTION},
        "servers": DATA_API_SERVERS,
        "tags": DATA_API_TAGS,
        "paths": paths,
        "components": _components(paths, schemas, DATA_SECURITY_SCHEMES),
    }


def is_public_read(path: str, method: str) -> bool:
    return method == "get" and (path in _PUBLIC_READ_PATHS or path.startswith(_PUBLIC_READ_PREFIXES))


def public_document(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """The node contract's operations plus the public and keyed reads, for `/openapi.json`.

    The node operations are the contract's own, so the live reference and the
    committed file cannot describe the node API differently. The keyed reads are
    published here as every `/api/v1/` read is; their security and the full
    protocol are in the data contract.
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
        "tags": NODE_API_TAGS + READ_TAGS + DATA_API_TAGS,
        "x-tagGroups": [
            {"name": "Node ingest", "tags": [tag["name"] for tag in NODE_API_TAGS]},
            {"name": "Public reads", "tags": [tag["name"] for tag in READ_TAGS]},
            {"name": "Keyed data", "tags": [tag["name"] for tag in DATA_API_TAGS]},
        ],
        "paths": public,
        "components": _components(public, schemas, schema.get("components", {}).get("securitySchemes", {})),
    }
