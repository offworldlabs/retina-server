#!/usr/bin/env python3
"""Does this push touch anything the droplets serve?

CI's `changes` job runs this on every push to main and gates the whole deploy
chain on the answer: staging's deploy, its smoke tests and its Playwright suite,
then production's deploy, its smoke tests and its E2E. A merge that only reworks
comments would otherwise redeploy two environments and run both browser suites
for no change in behaviour.

The gate is one-directional. It may only ever drop work it has positively shown
to be unnecessary, because the two mistakes are not symmetrical: a needless
deploy costs CI minutes and says so in the log, while a needless skip ships
nothing and says nothing. So every branch that cannot establish inertness
deploys, including every error path, and the verdict is `code=true` even when
this script fails outright.

Two things are inert:

* **Markdown**, except under data-explorer/ (see SERVED_VERBATIM).
* **Python whose syntax tree has not moved.** Comments and formatting are absent
  from the AST, so a reworded comment and a `ruff format` pass both compare
  equal, while a renamed local does not. Docstrings ARE nodes and so count as
  code, which is right: FastAPI publishes route docstrings into the generated
  node contract.

Nothing else. TypeScript is excluded because no comment check for it is
trustworthy, and shell, YAML, nginx conf and Dockerfiles are excluded because
they are full of heredocs, where a `#`-leading line is program text and no
line-wise filter can tell it from a comment.

Run locally with:  python3 deploy/deploy-scope.py <before-sha> <after-sha>
"""

from __future__ import annotations

import ast
import subprocess
import sys
from typing import NamedTuple

# A gitlink's mode. Submodule pointers arrive as an extensionless path with no
# blob to read, and libs/ is where the solver lives.
GITLINK = "160000"

# Copied into the served tree byte for byte, so nothing under either is inert.
# The Dockerfile COPYs data-explorer/ into the image and nginx serves it with
# `root /app/data-explorer`, which makes the markdown under it (including
# vendor/NOTICE.md, a third-party licence) a served file. Vite copies each app's
# public/ directory into the dist/ that nginx serves, so a file put there is
# served under its own name too. Matched on the path segment rather than on the
# app names, which move: the surfaces have been renamed and merged twice.
SERVED_VERBATIM = "data-explorer/"
PUBLIC_SEGMENT = "public/"

# Only a regular file's blob is program text. A symlink's blob is its target path,
# and `foo.py` parses happily as an attribute access, so a retargeted symlink could
# otherwise be cleared as Python that had not moved.
REGULAR_FILE = {"100644", "100755"}

STATUS_MEANING = {
    "A": "added",
    "C": "copied",
    "D": "deleted",
    "R": "renamed",
    "T": "changed type",
    "U": "unmerged",
    "X": "reported as unknown by git",
}


class Change(NamedTuple):
    src_mode: str
    dst_mode: str
    src_sha: str
    dst_sha: str
    status: str
    path: str


def _git(*args: str) -> bytes:
    # git's own diagnosis travels with the exception, or the only reason a run gives
    # for deploying is "returned non-zero exit status 128".
    done = subprocess.run(["git", *args], capture_output=True, check=False)
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _explain(message: str) -> None:
    # stderr, because stdout carries the verdict and nothing else: the calling step
    # reads it into a shell variable.
    print(message, file=sys.stderr)


def _is_commit(ref: str) -> bool:
    return (
        subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, check=False).returncode == 0
    )


def _changes(before: str, after: str) -> list[Change]:
    """The raw diff, which carries what --name-only drops: both file modes, so a
    chmod is visible, and the status letter, so an add is not mistaken for an edit.

    --no-renames, so a rename reports both the path that went and the path that
    arrived. Rename detection names only the destination, and `git mv app.py app.md`
    would then read as a markdown-only push while having deleted a module.

    -z, so a path is delimited rather than quoted, and --no-abbrev, so the object
    names can be read back without a second resolution step.
    """
    raw = _git("diff", "--raw", "--no-renames", "--no-abbrev", "-z", before, after)
    fields = raw.split(b"\0")
    changes = []
    # Records are a metadata field then a path field, repeating. The trailing NUL
    # leaves a final empty element, which zip drops with the metadata it has no
    # path for.
    for meta, path in zip(fields[::2], fields[1::2]):
        src_mode, dst_mode, src_sha, dst_sha, status = meta.decode().lstrip(":").split()
        # surrogateescape: a path git can store but the filesystem encoding cannot
        # decode must still be printable in a reason, and must not raise here.
        changes.append(Change(src_mode, dst_mode, src_sha, dst_sha, status, path.decode(errors="surrogateescape")))
    return changes


def _why_python_deploys(change: Change) -> str | None:
    """None when the two revisions of a module parse to the same tree."""
    trees = []
    for sha in (change.src_sha, change.dst_sha):
        try:
            # Bytes rather than text, so a PEP 263 coding cookie is honoured by the
            # parser instead of being assumed away by a decode here.
            trees.append(ast.dump(ast.parse(_git("cat-file", "blob", sha))))
        except (SyntaxError, ValueError, RuntimeError) as exc:
            return f"{change.path}: cannot be read as Python at {sha[:9]} ({type(exc).__name__}: {exc})"
    # type_comments stays off, so a `# type: ignore` is invisible here. It binds
    # a type checker, not the interpreter, and the lint job runs on this push
    # whatever this says.
    return None if trees[0] == trees[1] else f"{change.path}: the syntax tree moved"


def _why_deploys(change: Change) -> str | None:
    """Why this change has to reach the droplets, or None if it does not."""
    if change.status != "M":
        return f"{change.path}: {STATUS_MEANING.get(change.status, change.status)}"
    # Before anything reads the blobs: a chmod leaves both sides byte-identical, so
    # every content check agrees while the mode has moved under them, and an
    # entrypoint losing its executable bit changes what the image can run.
    if change.src_mode != change.dst_mode:
        return f"{change.path}: mode {change.src_mode} -> {change.dst_mode}"
    if change.src_mode == GITLINK:
        return f"{change.path}: submodule pointer moved"
    if change.src_mode not in REGULAR_FILE:
        return f"{change.path}: mode {change.src_mode} is not a regular file"
    if change.path.startswith((SERVED_VERBATIM, PUBLIC_SEGMENT)) or f"/{PUBLIC_SEGMENT}" in change.path:
        return f"{change.path}: served verbatim"
    if change.path.endswith(".md"):
        return None
    if change.path.endswith(".py"):
        return _why_python_deploys(change)
    return f"{change.path}: nothing here can clear it"


def _decide(argv: list[str]) -> str:
    if len(argv) != 2:
        _explain(f"usage: {sys.argv[0]} <before-sha> <after-sha>")
        return "code=true"
    before, after = argv
    # A force-push or a rewritten history arrives with a base that is not in the
    # repository, and the push event carries all-zeroes for a branch's first push.
    if not before or not _is_commit(before):
        _explain(f"No diffable base ({before!r}), so deploying.")
        return "code=true"
    changes = _changes(before, after)
    if not changes:
        _explain(f"Empty diff against {before}, so deploying.")
        return "code=true"
    _explain(f"Changed in {before}..{after}:")
    reasons = []
    for change in changes:
        reason = _why_deploys(change)
        _explain(f"  {'deploy ' if reason else 'inert  '} {reason or change.path}")
        if reason:
            reasons.append(reason)
    if reasons:
        _explain(f"{len(reasons)} of {len(changes)} changed paths need deploying.")
        return "code=true"
    _explain(f"All {len(changes)} changed paths are inert.")
    return "code=false"


if __name__ == "__main__":
    # The verdict is printed here and only here, so stdout carries exactly one
    # line however this ends. Anything unhandled deploys rather than propagating:
    # an exception would fail the job, and a failed `changes` job skips `staging`,
    # which skips the whole deploy chain: a crash would read as "nothing to do".
    try:
        verdict = _decide(sys.argv[1:])
    except Exception as exc:  # noqa: BLE001
        _explain(f"Deploying: this filter failed with {type(exc).__name__}: {exc}")
        verdict = "code=true"
    print(verdict)
