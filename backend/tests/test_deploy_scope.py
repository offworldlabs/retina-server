"""Verdicts of the deploy filter, asserted against real repositories.

`deploy/deploy-scope.py` decides whether a push to main reaches the droplets. A
verdict wrong in one direction burns a whole deploy chain on a comment; wrong in
the other it silently never ships a change, and nothing downstream says so. The
second is what this file exists to prevent, so every case that must deploy is
represented, not only the ones that must not.

The cases are built as two-commit repositories rather than as function arguments
because the inputs that matter are git states: a mode change, a submodule bump
and a rename are indistinguishable to a filter that reads path names alone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "deploy-scope.py"

DEPLOY = "code=true"
INERT = "code=false"

MODULE = '''"""Solve things."""


def solve(x):
    # Add one, because the caller counts from zero.
    total = x + 1
    return total
'''

MODULE_RECOMMENTED = '''"""Solve things."""


def solve(x):
    # The caller counts from zero, so shift by one.
    total = x + 1
    return total
'''

MODULE_REDOCUMENTED = '''"""Solve things, in the sense of arithmetic."""


def solve(x):
    # Add one, because the caller counts from zero.
    total = x + 1
    return total
'''

MODULE_RENAMED_LOCAL = '''"""Solve things."""


def solve(x):
    # Add one, because the caller counts from zero.
    subtotal = x + 1
    return subtotal
'''

MODULE_RECOMMENTED_AND_CHANGED = '''"""Solve things."""


def solve(x):
    # The caller counts from one, so shift by two.
    total = x + 2
    return total
'''

CALL = "value = solve(1)"

CALL_WRAPPED = """value = solve(
    1,
)
"""

CALL_BLANK_LINES = """value = solve(1)


"""

CALL_TRAILING_SPACE = "value = solve(1)  "

TYPESCRIPT = """// one
export const x = 1;
"""

TYPESCRIPT_RECOMMENTED = """// two
export const x = 1;
"""

REQUIREMENTS = "fastapi"

REQUIREMENTS_EXTENDED = """fastapi
vulture
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _commit(repo: Path, message: str = "wip", *, add: bool = True) -> str:
    """Commit the worktree, or with add=False the index as it stands, which is what
    a fabricated gitlink needs: `git add -A` would stage its deletion, the
    submodule having no directory on disk."""
    if add:
        _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for name, body in files.items():
        path = repo / name
        if body is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", ".")
    return tmp_path


def _history(repo: Path, first: dict[str, str | None], second: dict[str, str | None]) -> tuple[str, str]:
    _write(repo, first)
    before = _commit(repo, "first")
    _write(repo, second)
    return before, _commit(repo, "second")


def _verdict(repo: Path, before: str, after: str = "HEAD") -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), before, after],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    # Never non-zero, whatever it meets. A crash fails the job, and a failed
    # `changes` job skips `staging`, which skips the whole deploy chain, so an
    # exception in here would read as "nothing to deploy".
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1, f"the verdict must be alone on stdout, got {result.stdout!r}"
    return result.stdout.strip()


def test_markdown_alone_is_inert(repo: Path) -> None:
    before, _ = _history(repo, {"README.md": "one", "app.py": MODULE}, {"README.md": "two"})
    assert _verdict(repo, before) == INERT


def test_a_reworded_comment_is_inert(repo: Path) -> None:
    before, _ = _history(repo, {"app.py": MODULE}, {"app.py": MODULE_RECOMMENTED})
    assert _verdict(repo, before) == INERT


@pytest.mark.parametrize(
    "reformatted",
    [
        pytest.param(CALL_WRAPPED, id="wrapped-with-a-magic-trailing-comma"),
        pytest.param(CALL_BLANK_LINES, id="trailing-blank-lines"),
        pytest.param(CALL_TRAILING_SPACE, id="trailing-whitespace"),
    ],
)
def test_reformatting_is_inert(repo: Path, reformatted: str) -> None:
    """A ruff format pass moves no code, and the AST is what proves it."""
    before, _ = _history(repo, {"app.py": CALL}, {"app.py": reformatted})
    assert _verdict(repo, before) == INERT


def test_a_requoted_string_is_inert(repo: Path) -> None:
    """Quote style belongs to the formatter, not the program: the constant is equal."""
    before, _ = _history(repo, {"app.py": "name = 'retina'"}, {"app.py": 'name = "retina"'})
    assert _verdict(repo, before) == INERT


def test_a_dropped_noqa_is_inert(repo: Path) -> None:
    """Runtime-inert, and deliberately classified so: a suppression comment binds
    ruff, not the interpreter. The lint job runs on this push whatever the verdict
    here, so a suppression dropped too eagerly reddens the run and the deploy chain
    never starts."""
    before, _ = _history(repo, {"app.py": "import os  # noqa: F401"}, {"app.py": "import os"})
    assert _verdict(repo, before) == INERT


def test_an_edited_docstring_deploys(repo: Path) -> None:
    """Docstrings are nodes rather than comments, and rightly: FastAPI publishes
    route docstrings into the generated node contract, so they are program text."""
    before, _ = _history(repo, {"app.py": MODULE}, {"app.py": MODULE_REDOCUMENTED})
    assert _verdict(repo, before) == DEPLOY


def test_a_renamed_local_deploys(repo: Path) -> None:
    before, _ = _history(repo, {"app.py": MODULE}, {"app.py": MODULE_RENAMED_LOCAL})
    assert _verdict(repo, before) == DEPLOY


def test_a_comment_beside_a_change_in_one_file_deploys(repo: Path) -> None:
    before, _ = _history(repo, {"app.py": MODULE}, {"app.py": MODULE_RECOMMENTED_AND_CHANGED})
    assert _verdict(repo, before) == DEPLOY


def test_a_comment_beside_a_typescript_change_deploys(repo: Path) -> None:
    """No comment check exists for TypeScript, so every .ts change is code."""
    before, _ = _history(
        repo,
        {"app.py": MODULE, "frontend/src/app.ts": TYPESCRIPT},
        {"app.py": MODULE_RECOMMENTED, "frontend/src/app.ts": TYPESCRIPT_RECOMMENTED},
    )
    assert _verdict(repo, before) == DEPLOY


def test_a_new_module_deploys(repo: Path) -> None:
    before, _ = _history(repo, {"app.py": MODULE}, {"extra.py": CALL})
    assert _verdict(repo, before) == DEPLOY


def test_a_deleted_module_deploys(repo: Path) -> None:
    before, _ = _history(repo, {"app.py": MODULE, "extra.py": CALL}, {"extra.py": None})
    assert _verdict(repo, before) == DEPLOY


def test_a_module_that_became_markdown_deploys(repo: Path) -> None:
    """Rename detection is off, so this arrives as an add and a delete. With it on,
    the surviving path would be the markdown one and a deleted module would read as
    a docs push."""
    before, _ = _history(repo, {"app.py": MODULE}, {"app.py": None, "app.md": MODULE})
    assert _verdict(repo, before) == DEPLOY


def test_a_mode_change_deploys(repo: Path) -> None:
    """Identical blobs on both sides, so the AST agrees and only the mode moved. An
    entrypoint losing its executable bit changes what the image can run."""
    _write(repo, {"app.py": MODULE})
    before = _commit(repo, "first")
    (repo / "app.py").chmod(0o755)
    _commit(repo, "second")
    # A filesystem that does not record the executable bit would leave an empty
    # diff here, which the filter deploys for an entirely different reason while
    # the assertion below still passed. Prove the mode moved before trusting it.
    raw = _git(repo, "diff", "--raw", "--no-renames", before, "HEAD")
    assert raw.startswith(":100644 100755 "), raw
    assert _verdict(repo, before) == DEPLOY


def test_a_retargeted_symlink_deploys(repo: Path) -> None:
    """A symlink's blob is its target path, not a program, and `module.py` parses
    happily as an attribute access. The two targets here are chosen to parse to the
    same tree, so a content check would clear the link: what must stop it is the
    file mode, and only a test whose blobs agree can show that it does."""
    (repo / "module.py").write_text(MODULE)
    (repo / "app.py").symlink_to("module.py")
    before = _commit(repo, "first")
    (repo / "app.py").unlink()
    (repo / "app.py").symlink_to("module.py ")
    _commit(repo, "second")
    assert _verdict(repo, before) == DEPLOY


@pytest.mark.parametrize("path", ["dashboard/public/help.md", "public/help.md"])
def test_markdown_under_a_public_directory_deploys(repo: Path, path: str) -> None:
    """Vite copies public/ into the dist/ that nginx serves, so a file there is
    served under its own name."""
    before, _ = _history(repo, {path: "one"}, {path: "two"})
    assert _verdict(repo, before) == DEPLOY


@pytest.mark.parametrize("broken_side", ["before", "after"])
def test_a_file_that_will_not_parse_deploys(repo: Path, broken_side: str) -> None:
    broken = "def f(:"
    before, after = _history(
        repo,
        {"app.py": broken if broken_side == "before" else MODULE},
        {"app.py": MODULE if broken_side == "before" else broken},
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), before, after],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == DEPLOY
    # Name the file, or a run that deploys for no visible reason is unexplainable.
    assert "app.py" in result.stderr


def test_a_submodule_bump_deploys(repo: Path) -> None:
    """A gitlink has no extension and no blob to parse, and libs/ is where the
    solver lives."""
    _write(repo, {"app.py": MODULE})
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},libs/retina-analytics")
    before = _commit(repo, "first", add=False)
    _git(repo, "update-index", "--cacheinfo", f"160000,{'2' * 40},libs/retina-analytics")
    _commit(repo, "second", add=False)
    assert _verdict(repo, before) == DEPLOY


def test_markdown_under_data_explorer_deploys(repo: Path) -> None:
    """nginx serves that directory verbatim, so its markdown is a served file."""
    before, _ = _history(
        repo,
        {"data-explorer/vendor/NOTICE.md": "one"},
        {"data-explorer/vendor/NOTICE.md": "two"},
    )
    assert _verdict(repo, before) == DEPLOY


def test_a_requirements_change_deploys(repo: Path) -> None:
    before, _ = _history(
        repo,
        {"backend/requirements.txt": REQUIREMENTS},
        {"backend/requirements.txt": REQUIREMENTS_EXTENDED},
    )
    assert _verdict(repo, before) == DEPLOY


@pytest.mark.parametrize("base", ["", "0" * 40], ids=["no-base", "unknown-base"])
def test_an_undiffable_base_deploys(repo: Path, base: str) -> None:
    """A force-push or a rewritten history arrives with a base this cannot read."""
    _history(repo, {"README.md": "one"}, {"README.md": "two"})
    assert _verdict(repo, base) == DEPLOY


def test_an_empty_diff_deploys(repo: Path) -> None:
    """Nothing to compare is not the same as nothing to do."""
    _write(repo, {"README.md": "one"})
    head = _commit(repo, "first")
    assert _verdict(repo, head, head) == DEPLOY
