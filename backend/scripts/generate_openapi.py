"""Write the v1 node API's wire contract, generated from the routes themselves.

The contract used to be a hand-written `nodes_api_v1.yml` kept outside any git
repo. A hand-maintained document beside a generated one is a second artefact
that can only ever be wrong, and reconciling the two is permanent work, so the
generated schema is the contract now and this is what produces it (86cb2d059).
CI regenerates and fails on a difference, which is what makes that safe: the
committed file cannot be hand-edited to match a change, because the next run
notices (86cb4y0u2).

Scoped to `/v1/nodes` rather than the whole application. The node API is the
only surface here with a consumer holding a pinned version — the node client and
the conformance harness are independent implementations of this file — and a
document carrying seventy-odd internal map and dashboard routes would bury the
four that matter under diffs from work that cannot affect them.

Everything in the output comes from the application: the descriptions and the
`x-` annotations from the route decorators, the schemas from the Pydantic
models, the security scheme from the dependency, and `info.description` from the
application's own. The three constants this file reaches for by name
(`NODE_API_VERSION`, `NODE_API_TAGS`, `NODE_API_SERVERS`) live in routes/nodes.py
beside the router they describe. It also reaches for `config_json_schema`, to
recognise the configuration schema in the document and hoist it into a component
the two operations reference; see `_hoisted`.

Numeric bounds publish as floats (`minimum: 1.0` rather than `1`) because
FastAPI validates its own output through `openapi.models`, whose
`Schema.minimum` and `Schema.maximum` are typed `float`. `$ref` names are drawn
from the whole application's model namespace, so a future model named
`ErrorBody` or `Agreements` added anywhere in the app would rename every `$ref`
here and break a pinned consumer, which the CI gate would catch as a confusing
diff rather than a clean addition.

    cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi
    cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi --check
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from main import app
from routes.nodes import NODE_API_SERVERS, NODE_API_TAGS, NODE_API_VERSION, is_node_path
from services.node_config import config_json_schema

TITLE = "RETINA node ingest"

# Repo-relative, so the file sits with the tree rather than under backend/: it is
# read by the node client and the conformance harness, neither of which is Python.
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "nodes-v1.openapi.yaml"

_REF_PREFIX = "#/components/schemas/"

# The configuration schema is the one component this file names into existence
# rather than finding among the application's models. Both operations that take
# a configuration carry it inline, because Pydantic resolves every `$ref` it
# emits against its own definitions and this schema is not one of its models, so
# the reference is made here: this is already the layer that shapes the
# document, and one component is what a generated client needs to produce one
# type for one wire object.
_CONFIG_SCHEMA_NAME = "NodeConfig"


def _referenced(node: Any, found: set[str]) -> None:
    """Every schema reachable from `node`, transitively.

    Walked rather than taken wholesale: the application's components hold every
    model in the API, and a contract carrying the map's and the dashboard's would
    be a document about something else.
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


def _hoisted(node: Any, inline: dict[str, Any]) -> Any:
    """`node` with every inline copy of `inline` replaced by a `$ref` to it.

    Rebuilds rather than mutates: `app.openapi()` caches its result, and editing
    it in place would leave the application's own docs holding a reference to a
    component only this document defines.

    Matched by equality on the whole schema, so a partial copy is left alone
    rather than silently referred to something it does not equal.
    tests/test_node_openapi.py holds the other end, that both operations really
    do end up referring to it.
    """
    if isinstance(node, dict):
        if node == inline:
            return {"$ref": _REF_PREFIX + _CONFIG_SCHEMA_NAME}
        return {key: _hoisted(value, inline) for key, value in node.items()}
    if isinstance(node, list):
        return [_hoisted(item, inline) for item in node]
    return node


def _node_paths(paths: dict[str, Any]) -> dict[str, Any]:
    """The four operations, with FastAPI's automatic 422 dropped.

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


def contract() -> dict[str, Any]:
    schema = app.openapi()
    inline_config = config_json_schema()
    declared = schema["components"]["schemas"]
    if _CONFIG_SCHEMA_NAME in declared:
        # A Pydantic model of this name anywhere in the application would be
        # published under it instead, silently, and a pinned consumer would see
        # one type become another. Refused rather than clobbered.
        raise RuntimeError(f"{_CONFIG_SCHEMA_NAME} is already an application model; the contract cannot inject it")
    paths = _hoisted(_node_paths(schema["paths"]), inline_config)
    schemas = _hoisted(declared, inline_config) | {_CONFIG_SCHEMA_NAME: inline_config}
    components: dict[str, Any] = {"schemas": _closure(paths, schemas)}
    # Only the node routes declare one today, but filtering keeps that true
    # rather than assuming it.
    declared = schema.get("components", {}).get("securitySchemes", {})
    used = {name for operation in _operations(paths) for entry in operation.get("security", []) for name in entry}
    if used:
        components["securitySchemes"] = {name: declared[name] for name in sorted(used) if name in declared}
    return {
        "openapi": schema["openapi"],
        "info": {
            "title": TITLE,
            "version": NODE_API_VERSION,
            # The application's own, so the error taxonomy and the `x-` vocabulary
            # have one home rather than a copy here that can disagree with it.
            "description": app.description,
        },
        "servers": NODE_API_SERVERS,
        "tags": NODE_API_TAGS,
        "paths": paths,
        "components": components,
    }


def _operations(paths: dict[str, Any]) -> list[dict[str, Any]]:
    return [operation for operations in paths.values() for operation in operations.values()]


def _literal_block(dumper: yaml.Dumper, value: str):
    """Multi-line strings as `|` blocks.

    The descriptions are the bulk of this document and several are paragraphs.
    Quoted with `\\n` escapes they are one enormous line each, so a one-word
    change reads as a whole-description rewrite in review, which is the opposite
    of the reason the file is committed here.
    """
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


class _NodeContractDumper(yaml.SafeDumper):
    """Dumper for the node contract, with multi-line strings as literal blocks.

    Subclass of SafeDumper to avoid mutating the global yaml.SafeDumper class
    when registering the literal block representer.
    """


_NodeContractDumper.add_representer(str, _literal_block)


def render(document: dict[str, Any]) -> str:
    # sort_keys=False keeps `openapi`, `info`, `paths` in a reading order and
    # leaves each operation's responses in the order the route declares them.
    # Reproducibility does not depend on it: the ordering comes from the source,
    # and the schema closure above is sorted.
    return yaml.dump(document, Dumper=_NodeContractDumper, sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed contract is not what the routes generate, and change nothing",
    )
    args = parser.parse_args(argv)

    rendered = render(contract())
    if not args.check:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(rendered)
        print(f"wrote {CONTRACT_PATH}")
        return 0

    committed = CONTRACT_PATH.read_text() if CONTRACT_PATH.exists() else ""
    if committed == rendered:
        print(f"{CONTRACT_PATH.name} is current")
        return 0
    print(
        f"{CONTRACT_PATH} is not what the routes generate.\n"
        "The contract is generated, not authored: regenerate it in the same commit as the change "
        "that moved it.\n\n"
        "    cd backend && python -m scripts.generate_openapi\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
