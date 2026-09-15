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
    """Pinned against the entry coming back.

    nginx proxies /api/towers over retina-edge, and every environment runs the
    tower-finder-service stack now, so all three overlays must join the network
    identically. A server that dropped off it would 502 the route; an allowlist
    entry here — scoped or blanket — is what would stop CI noticing.
    """

    @pytest.mark.parametrize(
        "path",
        ["services.server.networks.retina-edge", "networks.retina-edge.external"],
    )
    @pytest.mark.parametrize("env", ["test", "staging"])
    def test_edge_network_divergence_is_not_allowed_anywhere(self, parity, path, env):
        assert not parity.allowed(path, env)


class TestCloudflareAccessEntries:
    """The two halves of the Access configuration, which want opposite treatment.

    CF_ACCESS_AUD is the audience tag of one environment's Access application, so
    it differs by nature exactly as HOST_ADMIN does and must be allowed to. Left
    off the allowlist, every environment after the first would fail the parity
    check and no deploy would pass.

    AUTH_ALLOW_ANONYMOUS_ADMIN must stay off it, and that does not stop mattering
    once the flag is removed everywhere: the entry is what makes CI refuse a
    change that reintroduces the anonymous admin to one environment on its own.
    """

    @pytest.mark.parametrize("env", ["test", "staging"])
    def test_the_access_audience_may_differ_per_environment(self, parity, env):
        assert parity.allowed("services.server.environment.CF_ACCESS_AUD", env)

    @pytest.mark.parametrize("env", ["test", "staging"])
    def test_the_anonymous_admin_flag_may_never_differ(self, parity, env):
        assert not parity.allowed("services.server.environment.AUTH_ALLOW_ANONYMOUS_ADMIN", env)


class TestScopeValidation:
    def test_unknown_environment_is_rejected(self, parity):
        with pytest.raises(SystemExit):
            parity._compile_allowed((("nosuchenv", r"^x$"),))

    def test_scoping_to_the_reference_is_rejected(self, parity):
        """check_compose skips the reference, so such an entry could never fire."""
        with pytest.raises(SystemExit):
            parity._compile_allowed(((parity.REFERENCE, r"^x$"),))
