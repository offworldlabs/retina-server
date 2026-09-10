"""The Access assertion as a third source of identity for the auth dependencies.

The verifier itself is tested in test_access_identity.py against real signed
tokens. These cover the wiring: that a verified email becomes an admin, that an
unverified one becomes a 401, and that an unconfigured verifier changes nothing.
"""

import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import State

from core.users import get_current_user, require_admin

EMAIL = "someone@offworldlab.com"


class StubVerifier:
    """Stands in for AccessIdentity, recording what it was asked to verify."""

    def __init__(self, email=None, configured=True):
        self.email = email
        self.configured = configured
        self.seen = []

    def is_configured(self):
        return self.configured

    async def identity(self, token):
        self.seen.append(token)
        # Same contract as the real verifier: no token is no identity, never an
        # error. A stub that answered regardless would hide a seam that admitted
        # callers who sent no assertion at all.
        return self.email if token else None


def _request(header=None):
    request = MagicMock()
    request.headers = {"Cf-Access-Jwt-Assertion": header} if header else {}
    request.cookies = {}
    request.state = State()
    return request


# ── a verified assertion is an administrator ─────────────────────


def test_a_verified_assertion_makes_require_admin_an_admin():
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        user = asyncio.run(require_admin(_request("a-token")))
    assert user["email"] == EMAIL
    assert user["is_superuser"] is True
    assert user["role"] == "admin"


def test_the_assertion_is_what_gets_verified():
    """Guards against reading the wrong header, which would silently never
    authenticate anyone."""
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        asyncio.run(require_admin(_request("the-assertion")))
    assert verifier.seen == ["the-assertion"]


def test_a_verified_assertion_also_satisfies_get_current_user():
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        user = asyncio.run(get_current_user(_request("a-token")))
    assert user["email"] == EMAIL


def test_the_identity_carries_a_stable_id_derived_from_the_email():
    """Admin actions are attributed in /api/admin/events, so the same person
    must be the same id across requests and restarts, without a database row."""
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        first = asyncio.run(require_admin(_request("t1")))
        second = asyncio.run(require_admin(_request("t2")))
    assert first["id"] == second["id"]
    assert first["id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, f"mailto:{EMAIL}"))
    assert first["id"] != "00000000-0000-0000-0000-000000000000"


def test_the_provider_says_where_the_identity_came_from():
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        user = asyncio.run(require_admin(_request("a-token")))
    assert user["provider"] == "cloudflare-access"


# ── refusals ─────────────────────────────────────────────────────


def test_an_assertion_that_does_not_verify_is_refused():
    verifier = StubVerifier(email=None)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_admin(_request("forged")))
    assert exc.value.status_code == 401


def test_the_plain_email_header_is_not_trusted():
    """Cloudflare also sets CF-Access-Authenticated-User-Email, which is an
    unsigned header anyone can type. Only the signed assertion is identity."""
    verifier = StubVerifier(email=EMAIL)
    request = _request()
    request.headers = {"Cf-Access-Authenticated-User-Email": "attacker@example.com"}
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_admin(request))
    assert exc.value.status_code == 401


def test_no_assertion_and_no_bypass_is_refused():
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", False), patch("core.users.access_identity", verifier):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(require_admin(_request()))
    assert exc.value.status_code == 401


# ── the bypass, and not consulting an unconfigured verifier ──────


def test_an_unconfigured_verifier_is_never_consulted():
    """Every environment without CF_ACCESS_AUD set, including the test suite.
    Reading the header there would refuse tokens nobody sent and log for it."""
    verifier = StubVerifier(email=EMAIL, configured=False)
    with patch("core.users.AUTH_BYPASS", True), patch("core.users.access_identity", verifier):
        user = asyncio.run(require_admin(_request("a-token")))
    assert verifier.seen == []
    assert user["id"] == "00000000-0000-0000-0000-000000000000"


def test_the_bypass_still_works_where_it_is_opted_into():
    """A laptop has no Access assertion, and docker-compose.local.yml keeps the
    flag for that reason."""
    verifier = StubVerifier(email=None, configured=False)
    with patch("core.users.AUTH_BYPASS", True), patch("core.users.access_identity", verifier):
        user = asyncio.run(require_admin(_request()))
    assert user["role"] == "admin"


def test_a_verified_assertion_beats_the_bypass():
    """Where both are available the real person is the better answer: the
    destructive admin endpoints are attributed, and 'Admin (no auth)' is not an
    attribution."""
    verifier = StubVerifier(email=EMAIL)
    with patch("core.users.AUTH_BYPASS", True), patch("core.users.access_identity", verifier):
        user = asyncio.run(require_admin(_request("a-token")))
    assert user["email"] == EMAIL
