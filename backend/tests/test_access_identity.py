"""Tests for verifying Cloudflare Access assertions.

Uses a real RSA keypair and real signed tokens rather than mocking the
verification, because the failures that matter here are the ones where a token
verifies and should not have. A mocked verifier would pass all of them.
"""

import json
import logging
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core.access_identity import (
    JWKS_GRACE_SECONDS,
    JWKS_REFETCH_MIN_INTERVAL_SECONDS,
    JWKS_TTL_SECONDS,
    AccessIdentity,
)

TEAM = "offworldlab.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "e5ff9de8d1ca5fbc62b38d102d92a1fc7d910d5f89ef388caf63c83e828493b3"
OTHER_AUD = "0" * 64
EMAIL = "someone@offworldlab.com"


@pytest.fixture(scope="module")
def keys():
    """One keypair for the suite. Generating RSA is slow enough to matter."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _jwk(public_key, kid):
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    data.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return data


class FakeJWKS:
    """A real httpx client over a fake transport, counting fetches.

    Real client so the production code's own request path is exercised; the
    counter is what lets the caching and single-refetch behaviour be asserted.
    """

    def __init__(self, jwks, fail=False):
        self.jwks = jwks
        self.fail = fail
        self.calls = 0

    def client(self):
        def handler(request):
            self.calls += 1
            if self.fail:
                raise httpx.ConnectError("unreachable")
            return httpx.Response(200, json=self.jwks)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def token(keys, *, aud=AUD, iss=ISSUER, email=EMAIL, kid="kid-1", exp_delta=300, key=None, **extra):
    private, _ = keys
    claims = {
        "aud": aud,
        "iss": iss,
        "email": email,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()) - 10,
        **extra,
    }
    return jwt.encode(claims, key or private, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def verifier(keys):
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())
    v.fake = fake
    return v


async def test_a_valid_assertion_yields_the_email(verifier, keys):
    assert await verifier.identity(token(keys)) == EMAIL


async def test_the_key_set_is_cached(verifier, keys):
    for _ in range(5):
        await verifier.identity(token(keys))
    assert verifier.fake.calls == 1


# ── the refusals that matter ─────────────────────────────────────


async def test_a_token_for_another_application_is_refused(verifier, keys):
    """The one most easily left out, because such a token is perfectly signed.

    The team already runs Access on seventeen node hostnames, so without the
    audience check anyone holding a valid session for those is admitted here.
    """
    assert await verifier.identity(token(keys, aud=OTHER_AUD)) is None


async def test_a_token_from_another_team_is_refused(verifier, keys):
    other = "https://someone-else.cloudflareaccess.com"
    assert await verifier.identity(token(keys, iss=other)) is None


async def test_an_expired_assertion_is_refused(verifier, keys):
    assert await verifier.identity(token(keys, exp_delta=-600)) is None


async def test_a_token_signed_by_someone_else_is_refused(verifier, keys):
    """Right shape, right claims, wrong key. The signature is the whole point."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert await verifier.identity(token(keys, key=impostor)) is None


async def test_an_unsigned_token_is_refused(verifier):
    """alg=none walks past a verifier that trusts the header's own claim."""
    forged = jwt.encode(
        {"aud": AUD, "iss": ISSUER, "email": EMAIL, "exp": int(time.time()) + 300},
        key="",
        algorithm="none",
    )
    assert await verifier.identity(forged) is None


async def test_the_email_is_normalised(verifier, keys):
    """Cloudflare returns the claim as the identity provider supplied it, and
    the id derived from it must be the same person's every time. Everything else
    that handles an address here lowercases it first
    (get_or_create_magic_link_user), so this has to as well or the two never
    match."""
    assert await verifier.identity(token(keys, email="  Someone@OffworldLab.COM ")) == EMAIL


async def test_a_token_naming_nobody_is_refused(verifier, keys):
    """Verifies, but authenticates no one. Something truthy would let a caller
    believe it had identified a person."""
    assert await verifier.identity(token(keys, email="")) is None


async def test_a_token_with_no_audience_claim_is_refused(verifier, keys):
    private, _ = keys
    claims = {"iss": ISSUER, "email": EMAIL, "exp": int(time.time()) + 300}
    naked = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "kid-1"})
    assert await verifier.identity(naked) is None


async def test_rubbish_is_refused(verifier):
    for value in ("", None, "not-a-token", "a.b.c"):
        assert await verifier.identity(value) is None


# ── configuration ────────────────────────────────────────────────


async def test_no_config_means_no_identity(keys):
    """Fails closed. Before CF_ACCESS_AUD is set there is nothing to check
    against, and admitting anyone meanwhile would be the worst default."""
    v = AccessIdentity(team_domain="", audience="")
    assert v.is_configured() is False
    assert await v.identity(token(keys)) is None


async def test_a_partial_config_is_treated_as_absent(keys):
    v = AccessIdentity(team_domain=TEAM, audience="")
    assert v.is_configured() is False
    assert await v.identity(token(keys)) is None


# ── key rotation and outages ─────────────────────────────────────


async def test_an_unknown_key_id_triggers_one_refetch(keys):
    """A key id we have not seen is either a rotation or a forgery, and one
    refetch tells them apart.

    The refetch has a floor on it (see test_forged_kids_do_not_fetch_once_per_
    request), so the clock is wound back to put this rotation outside that
    window. Winding it back rather than sleeping keeps the test instant, and the
    floor is short next to both the hour-long TTL and Cloudflare's real rotation
    cadence, so a genuine rotation is never delayed by more than the window.
    """
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "old-kid")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys, kid="old-kid")) == EMAIL
    assert fake.calls == 1

    fake.jwks = {"keys": [_jwk(public, "new-kid")]}
    v._fetched_at -= JWKS_REFETCH_MIN_INTERVAL_SECONDS + 1
    assert await v.identity(token(keys, kid="new-kid")) == EMAIL
    assert fake.calls == 2


async def test_a_rotation_inside_the_floor_waits_for_it(keys):
    """The cost of the floor, stated rather than discovered.

    A new key published seconds ago is not picked up until the window passes.
    That is bounded and much shorter than the TTL that would otherwise govern.
    """
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "old-kid")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys, kid="old-kid")) == EMAIL
    fake.jwks = {"keys": [_jwk(public, "new-kid")]}
    assert await v.identity(token(keys, kid="new-kid")) is None
    assert fake.calls == 1

    v._fetched_at -= JWKS_REFETCH_MIN_INTERVAL_SECONDS + 1
    assert await v.identity(token(keys, kid="new-kid")) == EMAIL


async def test_a_kid_that_never_appears_is_refused_and_does_not_loop(keys):
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "real-kid")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys, kid="invented")) is None
    assert fake.calls <= 2, "must not refetch endlessly for a forged kid"


async def test_one_unknown_kid_costs_one_fetch_not_two(keys):
    """A cold cache fetches, misses, and must not immediately fetch the same set
    again: the second attempt cannot succeed where the first just failed."""
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "real-kid")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys, kid="invented")) is None
    assert fake.calls == 1, "fetched the key set twice for one lookup"


async def test_forged_kids_do_not_fetch_once_per_request(keys):
    """The reason there is a floor on refetching at all.

    `kid` is read from the unverified header, so anyone can name one without
    signing anything. One outbound fetch per request would let an unauthenticated
    caller drive a request to Cloudflare per request of their own, each holding
    the lock that every admin request waits on.
    """
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "real-kid")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    for i in range(20):
        assert await v.identity(token(keys, kid=f"forged-{i}")) is None

    assert fake.calls <= 2, f"{fake.calls} fetches for 20 forged assertions"


async def test_cloudflare_being_unreachable_refuses_rather_than_admits(keys):
    """With nothing in hand there is nothing to fall back on."""
    fake = FakeJWKS({}, fail=True)
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())
    assert await v.identity(token(keys)) is None


# ── the grace period ─────────────────────────────────────────────
#
# Access at the edge and the endpoint publishing these keys are different
# systems. The outage to design for is the one where Access admits the team
# normally and only the refresh fails; refusing then is an outage of our own
# making, using keys we still hold.


async def _aged(v, seconds):
    """Wind the held set's age forward without waiting for it."""
    v._fetched_at -= seconds


async def test_a_stale_set_still_verifies_while_refreshing_fails(keys):
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys)) == EMAIL
    fake.fail = True
    await _aged(v, JWKS_TTL_SECONDS + 60)

    assert await v.identity(token(keys)) == EMAIL, "refused on keys it was holding"


async def test_a_failed_refresh_is_not_retried_by_every_request(keys):
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys)) == EMAIL
    fake.fail = True
    await _aged(v, JWKS_TTL_SECONDS + 60)

    before = fake.calls
    for _ in range(20):
        await v.identity(token(keys))
    assert fake.calls - before <= 1, (
        f"{fake.calls - before} attempts for 20 requests; a failing endpoint "
        "must not be retried by each one in turn while holding the lock"
    )


async def test_past_the_grace_the_stale_set_is_refused(keys):
    """The bound. Long past any plausible outage, short beside rotation."""
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    assert await v.identity(token(keys)) == EMAIL
    fake.fail = True
    await _aged(v, JWKS_TTL_SECONDS + JWKS_GRACE_SECONDS + 60)

    assert await v.identity(token(keys)) is None


async def test_a_cold_start_outage_is_not_logged_per_request(keys, caplog):
    """The other outage path, and the worse one: nothing was ever fetched, so
    nothing verifies. It needs the same throttle as the grace path, which it did
    not get when that one was added."""
    fake = FakeJWKS({}, fail=True)
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    caplog.set_level(logging.WARNING)
    for _ in range(50):
        assert await v.identity(token(keys)) is None

    assert caplog.records, "went quiet entirely"
    assert len(caplog.records) <= 2, f"{len(caplog.records)} warnings for 50 requests"


@pytest.mark.parametrize("aged_by", [0, JWKS_TTL_SECONDS + JWKS_GRACE_SECONDS + 60])
async def test_an_outage_is_never_reported_as_an_unknown_key_id(keys, caplog, aged_by):
    """Attribution, on both refusing paths.

    "not one of the published keys" names a forged or rotated key id, which is a
    different fault with a different fix. Saying it during an outage sends
    whoever is reading the log hunting an attacker while Cloudflare is down.
    """
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]}, fail=(aged_by == 0))
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())
    if aged_by:
        assert await v.identity(token(keys)) == EMAIL
        fake.fail = True
        await _aged(v, aged_by)

    caplog.set_level(logging.WARNING)
    assert await v.identity(token(keys)) is None

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "published keys" not in logged, f"blamed the key id for an outage: {logged}"


async def test_the_stale_warning_is_throttled(keys, caplog):
    """Loud once, not once per request.

    The window is hours long and the request rate is not ours to set: the header
    can be sent by anyone without signing anything, and nginx allows 30 a second
    per address. A line each would bury the very thing it reports, during an
    outage, in the logs somebody is reading to find out what broke.
    """
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    await v.identity(token(keys))
    fake.fail = True
    await _aged(v, JWKS_TTL_SECONDS + 60)

    caplog.set_level(logging.WARNING)
    for _ in range(50):
        assert await v.identity(token(keys)) == EMAIL

    assert caplog.records, "went quiet entirely"
    assert len(caplog.records) <= 2, f"{len(caplog.records)} warnings for 50 requests"


async def test_serving_a_stale_set_is_not_silent(keys, caplog):
    """An outage being survived should still be visible, or it is discovered
    only when the grace runs out and everybody is locked out at once."""
    _, public = keys
    fake = FakeJWKS({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())

    await v.identity(token(keys))
    fake.fail = True
    await _aged(v, JWKS_TTL_SECONDS + 60)

    caplog.set_level(logging.WARNING)
    assert await v.identity(token(keys)) == EMAIL
    assert caplog.records, "served a stale key set without saying so"


async def test_a_malformed_key_set_refuses_rather_than_raising(keys):
    """A key set that parses as JSON but carries no usable key.

    PyJWKSet.from_dict raises PyJWKSetError for it, which is a PyJWTError but
    not an InvalidTokenError, so it is caught where the fetch happens rather
    than by anything reading the token.
    """
    fake = FakeJWKS({"keys": []})
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())
    assert await v.identity(token(keys)) is None


# ── clock drift ──────────────────────────────────────────────────


async def test_small_clock_drift_is_tolerated(verifier, keys):
    assert await verifier.identity(token(keys, exp_delta=-5)) == EMAIL


async def test_large_drift_is_not_tolerated(verifier, keys):
    assert await verifier.identity(token(keys, exp_delta=-120)) is None


# ── explaining refusals ──────────────────────────────────────────
#
# Every refusal returns a bare None, which is right for the caller and useless
# for whoever has to work out why the console stopped admitting anyone. These
# assert the log makes up the difference, without handing out the credential it
# is describing.


async def test_every_refusal_says_why(verifier, keys, caplog):
    caplog.set_level(logging.WARNING)
    cases = {
        "wrong audience": token(keys, aud=OTHER_AUD),
        "wrong team": token(keys, iss="https://someone-else.cloudflareaccess.com"),
        "expired": token(keys, exp_delta=-600),
        "no email": token(keys, email=""),
        "unknown kid": token(keys, kid="never-published"),
    }
    for label, bad in cases.items():
        caplog.clear()
        assert await verifier.identity(bad) is None, label
        assert caplog.records, f"{label} was refused silently"


async def test_the_token_is_never_logged(verifier, keys, caplog):
    """A refused assertion is still a live bearer credential for its session.
    A log quoting it hands that session to anyone who can read logs, and these
    lines exist to be read by people debugging."""
    caplog.set_level(logging.DEBUG)
    for bad in (
        token(keys, aud=OTHER_AUD),
        token(keys, exp_delta=-600),
        token(keys, kid="never-published"),
    ):
        caplog.clear()
        await verifier.identity(bad)
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert bad not in logged
        # Not even a substantial slice. Signatures are the long tail.
        assert bad.split(".")[2][:24] not in logged


async def test_a_wrong_audience_names_both(verifier, keys, caplog):
    """The failure a misconfiguration actually produces, and the one invisible
    from outside: the hostname is up and refuses everybody."""
    caplog.set_level(logging.WARNING)
    await verifier.identity(token(keys, aud=OTHER_AUD))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert OTHER_AUD[:12] in logged, "does not say what the token claimed"
    assert AUD[:12] in logged, "does not say what this environment expects"


async def test_unreachable_cloudflare_is_distinguishable_from_a_bad_token(keys, caplog):
    """An outage and a forgery both return None. Confusing the two sends
    somebody hunting an attacker during a network problem."""
    caplog.set_level(logging.WARNING)
    fake = FakeJWKS({}, fail=True)
    v = AccessIdentity(team_domain=TEAM, audience=AUD, client=fake.client())
    assert await v.identity(token(keys)) is None
    logged = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "could not reach" in logged


async def test_missing_config_names_the_variables(keys, caplog):
    caplog.set_level(logging.WARNING)
    v = AccessIdentity(team_domain="", audience="")
    assert await v.identity(token(keys)) is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "CF_ACCESS_AUD" in logged


async def test_no_token_at_all_is_not_logged(verifier, caplog):
    """Unauthenticated requests are ordinary on the ungated vhosts. Logging each
    one would bury the refusals that mean something."""
    caplog.set_level(logging.WARNING)
    assert await verifier.identity(None) is None
    assert await verifier.identity("") is None
    assert not caplog.records


async def test_a_success_is_not_logged_as_a_refusal(verifier, keys, caplog):
    caplog.set_level(logging.WARNING)
    assert await verifier.identity(token(keys)) == EMAIL
    assert not caplog.records
