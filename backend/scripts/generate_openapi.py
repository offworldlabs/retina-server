"""Write the API contracts, generated from the routes themselves.

Two files: the v1 node API's (`contracts/nodes-v1.openapi.yaml`) and the keyed
data API's (`contracts/data-v1.openapi.yaml`). The data one is scoped by the
data-access gate: every route that depends on `require_data_access`, so a route
gated later joins it when the files are regenerated.

The node contract used to be a hand-written `nodes_api_v1.yml` kept outside any git
repo. A hand-maintained document beside a generated one is a second artefact
that can only ever be wrong, and reconciling the two is permanent work, so the
generated schema is the contract now and this is what produces it (86cb2d059).
CI regenerates and fails on a difference, which is what makes that safe: the
committed file cannot be hand-edited to match a change, because the next run
notices (86cb4y0u2).

Each is scoped to its API rather than the whole application. The node API and
the keyed data API are the surfaces here with consumers holding a pinned
version — the node client and the conformance harness are independent
implementations of the first, and external clients pin to the second — and a
document carrying seventy-odd internal map and dashboard routes would bury the
few that matter under diffs from work that cannot affect them.

The documents are built by `node_contract` and `data_contract` in
routes/openapi_documents.py; this file renders them and holds the gate that CI
and the pre-commit hook run.
`just contract` runs it, and `just contract --check` compares; by hand,
RETINA_ENV=dev is what lets the app import without production's secrets:

    cd backend && RETINA_ENV=dev .venv/bin/python -m scripts.generate_openapi
"""

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from main import app
from routes.openapi_documents import data_contract, node_contract

# Repo-relative, so the files sit with the tree rather than under backend/: they are
# read by the node client and the conformance harness, neither of which is Python.
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contracts" / "nodes-v1.openapi.yaml"
DATA_CONTRACT_PATH = CONTRACT_PATH.with_name("data-v1.openapi.yaml")


def contract() -> dict[str, Any]:
    return node_contract(app.openapi(), app.description)


def data_contract_document() -> dict[str, Any]:
    return data_contract(app.openapi(), app.routes)


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
    """Dumper for the contracts, with multi-line strings as literal blocks.

    Subclass of SafeDumper to avoid mutating the global yaml.SafeDumper class
    when registering the literal block representer.
    """


_NodeContractDumper.add_representer(str, _literal_block)


def render(document: dict[str, Any]) -> str:
    # sort_keys=False keeps `openapi`, `info`, `paths` in a reading order and
    # leaves each operation's responses in the order the route declares them.
    # Reproducibility does not depend on it: the ordering comes from the source,
    # and the schema closure is sorted.
    return yaml.dump(document, Dumper=_NodeContractDumper, sort_keys=False, allow_unicode=True, width=100)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if a committed contract is not what the routes generate, and change nothing",
    )
    args = parser.parse_args(argv)

    documents = {CONTRACT_PATH: render(contract()), DATA_CONTRACT_PATH: render(data_contract_document())}
    if not args.check:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        for path, rendered in documents.items():
            path.write_text(rendered)
            print(f"wrote {path}")
        return 0

    stale = [path for path, rendered in documents.items() if (path.read_text() if path.exists() else "") != rendered]
    for path in documents:
        if path not in stale:
            print(f"{path.name} is current")
    if not stale:
        return 0
    for path in stale:
        print(
            f"{path} is not what the routes generate.\n"
            "The contract is generated, not authored: regenerate it in the same commit as the change "
            "that moved it.\n\n"
            f"    just contract && git add :/contracts/{path.name}\n",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
