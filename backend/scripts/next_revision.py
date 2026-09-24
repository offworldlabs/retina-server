"""Print the next free Alembic revision number.

Revisions are numbered NNNN in chain order (tests/test_migrations.py). A number
is taken once an open pull request adds it, not when it merges, so the next
number is one past the highest held by this tree, origin/main or any open pull
request. That keeps two branches from taking the same id, provided each opens
its pull request before the other scaffolds; a branch without one holds a
number nobody else can see. It does not stop both chaining
from the same parent: whichever lands second rechains onto the other, and
renumbers if its number now sorts before main's head, which the one-head and
chain-order tests enforce once both are in one tree. The branch's own pull
request is left out, so regenerating its revision does not count its own number
against it: downgrade the dev database off it, delete the file, and run the
recipe again. Without git
and gh answering for origin/main and the pull requests this refuses rather than
number from part of them, and it refuses on a tree missing any of origin/main's
revisions, whose new revision would fork the chain.

    cd backend && python -m scripts.next_revision
"""

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = "backend/migrations/versions"
NUMBERED = re.compile(rf"^{re.escape(VERSIONS)}/(\d{{4}})_[^/]*\.py$")
PULL_LIMIT = 500  # more open pull requests than this and the listing is refused


class Refused(Exception):
    """A number chosen now could collide with another branch's or fork the chain."""


def highest(paths: Iterable[str]) -> int:
    """The highest revision number among repo-relative paths, 0 for none."""
    return max((int(m.group(1)) for m in map(NUMBERED.match, paths) if m), default=0)


def next_revision(tree: Iterable[str], main: Iterable[str], pulls: Iterable[str]) -> str:
    tree, main = set(filter(NUMBERED.match, tree)), set(filter(NUMBERED.match, main))
    if missing := sorted(main - tree):
        raise Refused(f"this tree lacks {', '.join(missing)} from origin/main: rebase onto main first")
    return f"{max(highest(tree), highest(main), highest(pulls)) + 1:04d}"


def _run(*command: str) -> str:
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout


def _on_this_tree() -> list[str]:
    return [f"{VERSIONS}/{path.name}" for path in (REPO_ROOT / VERSIONS).glob("*.py")]


def _on_main() -> list[str]:
    _run("git", "fetch", "--quiet", "origin", "main")
    return _run("git", "ls-tree", "--name-only", "origin/main", f"{VERSIONS}/").splitlines()


def _is_own(pull: dict, branch: str, matched: int | None) -> bool:
    """This branch's pull request: the one gh matches it to, which covers a fork,
    or the same-repository one named after it, in case gh matches none."""
    return pull["number"] == matched or (not pull["isCrossRepository"] and pull["headRefName"] == branch)


def _gh_match() -> int | None:
    try:
        return json.loads(_run("gh", "pr", "view", "--json", "number"))["number"]
    except subprocess.CalledProcessError:
        return None


def _in_open_pull_requests() -> list[str]:
    fields = "number,headRefName,isCrossRepository,changedFiles,files"
    pulls = json.loads(_run("gh", "pr", "list", "--state", "open", "--limit", str(PULL_LIMIT + 1), "--json", fields))
    if len(pulls) > PULL_LIMIT:
        raise Refused(f"more than {PULL_LIMIT} open pull requests")
    branch, matched = _run("git", "rev-parse", "--abbrev-ref", "HEAD").strip(), _gh_match()
    paths = []
    for pull in (pull for pull in pulls if not _is_own(pull, branch, matched)):
        # The listing carries at most 100 files per pull request.
        if pull["changedFiles"] > len(pull["files"]):
            files = _run(
                "gh",
                "api",
                "--paginate",
                f"repos/{{owner}}/{{repo}}/pulls/{pull['number']}/files",
                "--jq",
                ".[].filename",
            ).splitlines()
            # The files endpoint stops at 3000.
            if len(files) < pull["changedFiles"]:
                raise Refused(f"#{pull['number']} changes more files than GitHub will list")
            paths += files
        else:
            paths += [file["path"] for file in pull["files"]]
    return paths


def main() -> int:
    try:
        tree, main_, pulls = _on_this_tree(), _on_main(), _in_open_pull_requests()
        revision = next_revision(tree, main_, pulls)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError, KeyError, Refused) as exc:
        detail = getattr(exc, "stderr", "") or exc
        print(f"✗ not choosing a revision number: {detail}", file=sys.stderr)
        return 1
    print(
        f"highest: this tree {highest(tree):04d}, origin/main {highest(main_):04d}, open pull requests {highest(pulls):04d}",
        file=sys.stderr,
    )
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
