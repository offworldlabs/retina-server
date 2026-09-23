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


class TestMailTransport:
    """Which environments may send sign-in links somewhere other than a mailbox.

    The test droplet writes them to its own log: it is deployed to from any
    branch, so real mail from it means mailing whoever a half-finished change
    happens to name. Staging must not have the same licence — it is the
    rehearsal for production, and a transport only production exercises is one
    nobody has tested. services/mail.py refuses the log transport in production
    outright, so this entry is the guard for the environment in between.
    """

    def test_the_test_droplet_may_diverge(self, parity):
        assert parity.allowed("services.server.environment.MAIL_TRANSPORT", "test")

    def test_staging_may_not(self, parity):
        assert not parity.allowed("services.server.environment.MAIL_TRANSPORT", "staging")

    def test_the_sender_identity_may_never_differ(self, parity):
        """One From address everywhere, so what a recipient sees is the same
        thing staging rehearsed."""
        for env in ("test", "staging"):
            assert not parity.allowed("services.server.environment.MAIL_FROM", env)


class TestAdsbClaimFallbackTrial:
    """Staging alone trials the adsb-service claim fallback, in shadow.

    The test droplet's fleet already relays the same traffic and shares the
    service's per-address budget, so it must not switch the fallback on, and it
    keeps the lane's binding default like production.
    """

    @pytest.mark.parametrize("key", ["ADSB_FALLBACK_ENABLED", "KNOWN_LANE_MODE"])
    def test_staging_may_diverge(self, parity, key):
        assert parity.allowed(f"services.server.environment.{key}", "staging")

    @pytest.mark.parametrize("key", ["ADSB_FALLBACK_ENABLED", "KNOWN_LANE_MODE"])
    def test_the_test_droplet_may_not(self, parity, key):
        assert not parity.allowed(f"services.server.environment.{key}", "test")

    def test_neighbouring_keys_stay_compared(self, parity):
        for key in ("ADSB_SEED_MODE", "KNOWN_LANE_MODE_X", "DARK_FOLLOW_MODE"):
            assert not parity.allowed(f"services.server.environment.{key}", "staging")
