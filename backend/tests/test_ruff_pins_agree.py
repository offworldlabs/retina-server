"""The ruff in the venv and the ruff pre-commit runs are one release.

Each is pinned on its own, in backend/pyproject.toml and .pre-commit-config.yaml,
and Dependabot moves them from separate entries. Formatted by one and checked by
the other, a file can fail the commit hook it was just formatted for.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_the_venv_and_the_hook_pin_the_same_ruff():
    dev = tomllib.loads((ROOT / "backend" / "pyproject.toml").read_text())["dependency-groups"]["dev"]
    venv = [m.group(1) for d in dev if (m := re.fullmatch(r"ruff==(\S+)", d))]
    repos = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())["repos"]
    hook = [r["rev"].removeprefix("v") for r in repos if r["repo"].rstrip("/").endswith("/ruff-pre-commit")]
    assert len(venv) == 1 and len(hook) == 1, f"expected one ruff pin in each, found {venv} and {hook}"
    assert venv == hook, (
        f"backend/pyproject.toml pins ruff {venv[0]} and .pre-commit-config.yaml {hook[0]}. "
        "Move both in the same PR, relocking backend/uv.lock."
    )
