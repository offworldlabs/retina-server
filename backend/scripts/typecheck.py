"""Type-check the backend with pyright, and hold the packages that are clean.

Every package pyproject's [tool.pyright] includes is checked and its error count
printed, which is the burn-down. Only the packages in CLEAN fail the run: a
package joins CLEAN once it reads 0, after which an error in it fails the job.

    cd backend && python -m scripts.typecheck
"""

import collections
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
# Packages that type-check cleanly and must stay that way.
CLEAN = ("analysis", "config", "migrations", "main.py")


def _relative(path: str) -> Path:
    resolved = Path(path).resolve()
    return resolved.relative_to(BACKEND) if resolved.is_relative_to(BACKEND) else resolved


def package(path: str) -> str:
    """The top-level package or module a diagnostic's file belongs to, or the
    whole path of a file outside the backend."""
    relative = _relative(path)
    return str(relative) if relative.is_absolute() else relative.parts[0]


def tally(report: dict, packages: list[str]) -> tuple[dict[str, int], list[dict]]:
    """Errors per package, and the errors in a CLEAN package. An error outside
    the packages included gets a row of its own, so the total is pyright's."""
    errors = [d for d in report["generalDiagnostics"] if d["severity"] == "error"]
    counts = collections.Counter(package(d["file"]) for d in errors)
    rows = {p: counts.pop(p, 0) for p in packages} | dict(sorted(counts.items()))
    return rows, [d for d in errors if package(d["file"]) in CLEAN]


def _table(counts: dict[str, int]) -> str:
    rows = [f"| {'held' if p in CLEAN else 'counted'} | `{p}` | {n} |" for p, n in counts.items()]
    return "\n".join(["| | Package | Errors |", "| --- | --- | --- |", *rows, f"| | total | {sum(counts.values())} |"])


def _report(stdout: str) -> dict:
    """pyright's JSON, past anything its launcher printed first (a Node download, say)."""
    start = next(i for i, line in enumerate(stdout.splitlines()) if line.startswith("{"))
    return json.loads("\n".join(stdout.splitlines()[start:]))


def main() -> int:
    packages = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["pyright"]["include"]
    missing = [p for p in packages if not (BACKEND / p).exists()]
    if missing:
        print(f"[tool.pyright] include names what is not there: {', '.join(missing)}", file=sys.stderr)
        return 2
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--outputjson"], cwd=BACKEND, capture_output=True, text=True
    )
    try:
        report = _report(result.stdout)
    except (StopIteration, json.JSONDecodeError):
        report = None
    # pyright exits 1 for type errors alone; anything else, or nothing analysed, is a failed run.
    if report is None or result.returncode not in (0, 1) or not report["summary"]["filesAnalyzed"]:
        print(result.stdout + result.stderr, file=sys.stderr)
        return 2
    counts, held = tally(report, packages)
    print(_table(counts))
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as out:
            out.write(f"### Backend type check\n\n{_table(counts)}\n")
    for d in held:
        # pyright leaves out the range of a diagnostic at the very start of a file.
        line = d.get("range", {}).get("start", {}).get("line", 0) + 1
        print(f"{_relative(d['file'])}:{line}: {d['message']} [{d.get('rule', '')}]", file=sys.stderr)
    if held:
        print(f"{len(held)} type errors in packages held clean: {', '.join(CLEAN)}", file=sys.stderr)
    return 1 if held else 0


if __name__ == "__main__":
    raise SystemExit(main())
