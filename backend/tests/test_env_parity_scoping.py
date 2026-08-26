"""Per-environment scoping in deploy/check-env-parity.py's allowlist.

That script gates the production deploy, and this part of it fails silently: an
entry that widens back to every environment leaves the check passing while it
has stopped looking. Nothing else asserts the difference.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "check-env-parity.py"


@pytest.fixture(scope="module")
def parity():
    spec = importlib.util.spec_from_file_location("check_env_parity", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestScoping:
    def test_unscoped_entry_applies_to_every_environment(self, parity, monkeypatch):
        monkeypatch.setattr(parity, "_ALLOWED", parity._compile_allowed((r"^services\.x$",)))
        assert parity.allowed("services.x", "staging")
        assert parity.allowed("services.x", "test")

    def test_scoped_entry_applies_only_to_its_own_environment(self, parity, monkeypatch):
        monkeypatch.setattr(parity, "_ALLOWED", parity._compile_allowed((("test", r"^services\.x$"),)))
        assert parity.allowed("services.x", "test")
        assert not parity.allowed("services.x", "staging")

    def test_non_matching_path_is_never_allowed(self, parity, monkeypatch):
        monkeypatch.setattr(parity, "_ALLOWED", parity._compile_allowed((("test", r"^services\.x$"),)))
        assert not parity.allowed("services.y", "test")


class TestEdgeNetworkEntry:
    """The reason the scoping exists, pinned against a widening edit.

    nginx proxies /api/towers over retina-edge. A staging server that dropped
    off that network would 502 the route, and a blanket entry here is what
    would stop CI noticing.
    """

    @pytest.mark.parametrize(
        "path",
        ["services.server.networks.retina-edge", "networks.retina-edge.external"],
    )
    def test_edge_network_divergence_is_allowed_for_test_only(self, parity, path):
        assert parity.allowed(path, "test")
        assert not parity.allowed(path, "staging")


class TestScopeValidation:
    def test_unknown_environment_is_rejected(self, parity):
        with pytest.raises(SystemExit):
            parity._compile_allowed((("nosuchenv", r"^x$"),))

    def test_scoping_to_the_reference_is_rejected(self, parity):
        """check_compose skips the reference, so such an entry could never fire."""
        with pytest.raises(SystemExit):
            parity._compile_allowed(((parity.REFERENCE, r"^x$"),))
