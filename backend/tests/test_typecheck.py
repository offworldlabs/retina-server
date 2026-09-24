"""scripts/typecheck.py fails only on the packages it holds clean, and counts the rest."""

import json
import tomllib
from types import SimpleNamespace

import pytest

import scripts.typecheck as typecheck
from scripts.typecheck import BACKEND, CLEAN, _report, tally


def _diagnostic(relative: str, severity: str = "error") -> dict:
    return {"file": str(BACKEND / relative), "severity": severity, "range": {"start": {"line": 0}}, "message": "m"}


def test_an_error_in_a_held_package_fails_and_one_elsewhere_is_counted():
    report = {
        "generalDiagnostics": [
            _diagnostic("config/constants.py"),
            _diagnostic("services/solver.py"),
            _diagnostic("services/solver.py"),
            _diagnostic("routes/radar.py", severity="warning"),
        ]
    }
    counts, held = tally(report, ["config", "services", "routes", "main.py"])
    assert counts == {"config": 1, "services": 2, "routes": 0, "main.py": 0}
    assert [d["file"] for d in held] == [str(BACKEND / "config/constants.py")]


def test_a_held_error_without_a_range_is_still_reported(capsys, monkeypatch):
    """pyright omits `range` for a diagnostic at 0:0."""
    diagnostic = _diagnostic("config/x.py")
    del diagnostic["range"]
    report = {"summary": {"filesAnalyzed": 1}, "generalDiagnostics": [diagnostic]}
    completed = SimpleNamespace(stdout=json.dumps(report), stderr="", returncode=1)
    monkeypatch.setattr(typecheck.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert typecheck.main() == 1
    assert "config/x.py:1:" in capsys.readouterr().err


def test_an_error_outside_the_packages_checked_is_still_counted():
    counts, held = tally({"generalDiagnostics": [{**_diagnostic("x"), "file": "/elsewhere/lib.py"}]}, ["main.py"])
    assert counts == {"main.py": 0, "/elsewhere/lib.py": 1}
    assert held == []


def _run(monkeypatch, returncode: int, report: dict | None):
    completed = SimpleNamespace(stdout=json.dumps(report) if report else "", stderr="", returncode=returncode)
    monkeypatch.setattr(typecheck.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return typecheck.main()


def test_a_run_it_cannot_trust_fails_on_its_own_account(monkeypatch):
    analysed = {"summary": {"filesAnalyzed": 1}, "generalDiagnostics": []}
    assert _run(monkeypatch, 0, analysed) == 0
    assert _run(monkeypatch, 2, analysed) == 2  # pyright itself failed
    assert _run(monkeypatch, 0, {"summary": {"filesAnalyzed": 0}, "generalDiagnostics": []}) == 2
    assert _run(monkeypatch, 0, None) == 2  # no JSON at all


def test_an_include_entry_that_is_not_there_fails_before_pyright_runs(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pyright]\ninclude = ["gone"]\n')
    monkeypatch.setattr(typecheck, "BACKEND", tmp_path)
    monkeypatch.setattr(typecheck.subprocess, "run", lambda *args, **kwargs: pytest.fail("pyright ran"))
    assert typecheck.main() == 2


def test_a_clean_report_holds_nothing():
    counts, held = tally({"generalDiagnostics": []}, ["main.py"])
    assert counts == {"main.py": 0}
    assert held == []


def test_every_held_package_is_one_pyright_checks():
    include = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["pyright"]["include"]
    assert set(CLEAN) <= set(include), f"held but not checked: {sorted(set(CLEAN) - set(include))}"
    # package() counts by top-level name, so a nested entry would be miscounted.
    assert all("/" not in p and (BACKEND / p).exists() for p in include), include


def test_pyrights_json_is_found_past_what_its_launcher_printed():
    assert _report('Downloading node...\n{\n  "summary": {"filesAnalyzed": 1}\n}\n') == {
        "summary": {"filesAnalyzed": 1}
    }
