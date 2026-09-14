"""No deployed environment may hand out an anonymous administrator.

AUTH_ALLOW_ANONYMOUS_ADMIN makes require_admin return a superuser to any caller,
on every vhost, since the same app answers all of them. ClickUp 86cb1emcx records
what that cost: 1966 irreversible deletes through /api/admin/* in one week, by
nobody in particular.

deploy/check-env-parity.py already refuses to let the three deployed overlays
disagree about it, which catches reintroducing it to one of them. It cannot catch
reintroducing it to all three at once, and that is what this covers.
"""

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_FLAG = "AUTH_ALLOW_ANONYMOUS_ADMIN"

#: Everything a real user can reach.
DEPLOYED = ["docker-compose.prod.yml", "docker-compose.staging.yml", "docker-compose.test.yml"]


class _ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (`!override`, `!reset`) are not YAML the safe loader knows."""


def _ignore_tag(loader: yaml.Loader, suffix: str, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _ignore_tag)


def _environment(overlay: str) -> dict[str, str]:
    """Every environment key the overlay sets, across all its services.

    Parsed rather than matched. Compose accepts `- KEY=value` and `KEY: value`
    alike, and either may arrive through a YAML anchor, so a regex for one
    spelling reports a clean overlay for a flag set in another — the drift this
    file exists to catch.
    """
    doc = yaml.load((_REPO / overlay).read_text(), Loader=_ComposeLoader) or {}
    env: dict[str, str] = {}
    for service in (doc.get("services") or {}).values():
        declared = (service or {}).get("environment") or {}
        if isinstance(declared, dict):
            env.update({k: "" if v is None else str(v) for k, v in declared.items()})
            continue
        for entry in declared:
            key, sep, value = str(entry).partition("=")
            env[key] = value if sep else ""
    return env


def _sets_the_flag(overlay: str) -> bool:
    """Whether the overlay actually opens the bypass, not merely whether it names it.

    `_derive_auth_flags` in backend/core/users.py opens it on exactly `"1"`, so a
    presence check would call `=0` open and, on local, call it closed.
    """
    return _environment(overlay).get(_FLAG, "").strip() == "1"


@pytest.mark.parametrize("overlay", DEPLOYED)
def test_no_deployed_overlay_opts_into_the_anonymous_admin(overlay: str) -> None:
    assert not _sets_the_flag(overlay), (
        f"{overlay} sets {_FLAG}, which serves an anonymous superuser to anyone "
        f"who asks, on every vhost. If an environment genuinely needs it, that "
        f"is a decision to argue for rather than a line to restore."
    )


@pytest.mark.parametrize("overlay", DEPLOYED)
def test_no_deployed_overlay_leaves_the_verifier_unconfigured(overlay: str) -> None:
    """The other half of the same guarantee.

    With the bypass gone and no audience set, an environment refuses everybody
    including the people who are supposed to get in, and the admin console is
    simply unusable there. Failing closed is right, but it should not be reached
    by forgetting something.
    """
    assert _environment(overlay).get("CF_ACCESS_AUD", "").strip(), (
        f"{overlay} sets no CF_ACCESS_AUD, so no Access assertion can be verified "
        f"there and every admin request is refused. Create that environment's "
        f"Access application and pin its audience here."
    )


def test_local_may_keep_the_bypass() -> None:
    """Pinned deliberately, so nobody 'tidies' it away.

    A laptop has no Access assertion and no OAuth, the surfaces bind to
    localhost, and a developer needs the console. docker-compose.local.yml is
    outside check-env-parity.py's OVERLAYS for the related reason that it renders
    the template's plain-HTTP branch.
    """
    assert _sets_the_flag("docker-compose.local.yml"), (
        "docker-compose.local.yml no longer opts into the anonymous admin, so "
        "local development cannot reach the admin surfaces at all."
    )
