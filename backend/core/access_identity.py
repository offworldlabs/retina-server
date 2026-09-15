"""Verifying that a request really was authenticated by Cloudflare Access.

Cloudflare puts a signed assertion in `Cf-Access-Jwt-Assertion` on every request
it lets through. This turns that into an email address, or into nothing.

## Why verify at all, when Access sits in front

In normal operation nothing unauthenticated reaches the admin hostnames. This is
for the case where that stops being true. An Access application deleted, renamed
or misconfigured leaves the hostname open and the backend happily serving it, and
nothing else in the stack would notice.

It also does work no edge check can: the same app answers on every vhost, so an
assertion is the only thing that distinguishes an administrator from any caller
who found `api.retina.fm`.

## What is checked, and why each one matters

    signature   against the team's published keys. Without it the header is a
                claim anyone can type.
    audience    the tag of *this environment's* Access application. Without it a
                token minted for any other application in the team is accepted,
                and the team runs Access on seventeen node hostnames.
    issuer      the team domain, so a valid token from another Cloudflare team
                is not enough.
    expiry      with a little leeway for clock drift.

Missing any one of those turns verification into decoration. The audience is the
one most easily left out, because a token that fails it still has a perfectly
good signature.
"""

import asyncio
import logging

import httpx
import jwt
from jwt import PyJWKSet

log = logging.getLogger(__name__)

#: Cloudflare rotates signing keys, so a cached set goes stale. An unrecognised
#: key id refetches immediately regardless, so this is a ceiling on staleness
#: rather than the mechanism that handles rotation.
JWKS_TTL_SECONDS = 3600

#: Rejecting a freshly issued token because a clock is three seconds behind
#: would be a confusing way to fail.
CLOCK_LEEWAY_SECONDS = 30

#: Floor on refetching after an unrecognised key id. `kid` is read from the
#: unverified header, so anyone can name one without signing anything, and
#: without this floor each such request costs an outbound fetch to Cloudflare
#: made while holding the lock every admin request waits on. The cost is that a
#: genuine rotation is picked up up to this late, which is short beside both the
#: hour-long TTL and Cloudflare's real rotation cadence.
JWKS_REFETCH_MIN_INTERVAL_SECONDS = 60

#: How long a held key set keeps being used once refreshing it starts failing.
#:
#: Cloudflare Access at the edge and the endpoint publishing these keys are
#: different systems, so the outage worth designing for is the one where Access
#: admits the team normally and only the refresh fails. Refusing then would take
#: the admin console down on our own account, during someone else's partial
#: outage, using keys we are still holding and which would almost certainly
#: verify every live assertion.
#:
#: Bounded rather than indefinite: long past any plausible outage, short beside
#: Cloudflare's rotation cadence of weeks, so a genuinely retired key can never
#: be honoured for anything approaching a rotation cycle.
JWKS_GRACE_SECONDS = 6 * 3600

#: One refresh attempt per interval once one has failed, so a failing endpoint
#: is not retried by every request in turn while holding the lock.
JWKS_RETRY_INTERVAL_SECONDS = 30

#: One outage line per interval. The grace window is hours long and the request
#: rate is not ours to set, so a line each would bury what it reports in the
#: logs somebody is reading during the outage. Once a minute is ~360 lines
#: across the whole window: unmissable, and not a flood.
JWKS_WARN_INTERVAL_SECONDS = 60

#: Everything a fetch can fail with. httpx covers the network and the status;
#: PyJWKSetError arrives as PyJWTError, and a body that is not JSON as
#: ValueError. Named once so the fallback path and the caller agree on it.
_FETCH_ERRORS = (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError)


class _KeysUnavailable(Exception):
    """There is no key set fit to judge a token against.

    Raised rather than returned so it cannot be confused with "this token's key
    id is not among the published keys", which is a different fault, with a
    different cause and a different fix. Whoever raises this has already said
    why, through `_warn_outage`, so `identity` refuses without adding a line.
    """


def _short(value):
    """Enough of an identifier to match against, not enough to fill a line."""
    value = str(value or "")
    return value if len(value) <= 12 else value[:12] + "..."


def _kid(token):
    """The key id a token claims, for the log only.

    Read without verifying anything, which is safe precisely because the caller
    has already decided to refuse: this only ever describes a rejection.
    """
    try:
        return _short(jwt.get_unverified_header(token).get("kid"))
    except Exception:
        return "unreadable"


def _claim(token, name):
    """One unverified claim, for explaining a rejection. Never decides anything."""
    try:
        value = jwt.decode(token, options={"verify_signature": False}).get(name)
    except Exception:
        return "unreadable"
    if isinstance(value, list):
        return [_short(v) for v in value]
    return _short(value)


class AccessIdentity:
    """Turns a Cloudflare Access assertion into a verified email address."""

    def __init__(self, team_domain=None, audience=None, ttl=JWKS_TTL_SECONDS, client=None):
        self.team_domain = (team_domain or "").strip()
        self.audience = (audience or "").strip()
        self.ttl = ttl
        self._client = client
        self._jwks = None
        self._fetched_at = 0.0
        # -inf so the first attempt is never mistaken for one inside a backoff
        # window, whatever epoch the loop's monotonic clock happens to use.
        self._failed_at = float("-inf")
        self._last_error = None
        self._warned_at = float("-inf")
        # Requests are served concurrently; without this two arriving together
        # could each fetch and install a key set the other had moved past.
        self._lock = asyncio.Lock()

    def is_configured(self):
        return bool(self.team_domain and self.audience)

    # ── signing keys ─────────────────────────────────────────────

    async def _fetch_jwks(self):
        if self._client is None:
            self._client = httpx.AsyncClient()
        url = f"https://{self.team_domain}/cdn-cgi/access/certs"
        response = await self._client.get(url, timeout=10)
        response.raise_for_status()
        return PyJWKSet.from_dict(response.json())

    async def _refresh(self):
        self._jwks = await self._fetch_jwks()
        self._fetched_at = asyncio.get_running_loop().time()
        self._last_error = None

    async def _try_refresh(self, now):
        """Refresh, remembering a failure rather than raising it.

        The error is kept so the caller can re-raise it where there is nothing to
        fall back on, which is what lets `identity` tell an outage apart from a
        forged key id in the log.
        """
        try:
            await self._refresh()
            return True
        except _FETCH_ERRORS as exc:
            self._failed_at = now
            self._last_error = exc
            return False

    def _warn_outage(self, now, message, *args):
        """One outage line per interval, however many requests arrive.

        Shared by both outage messages rather than one throttle each: they
        describe the same failing endpoint, and the transition between them
        happens once, so a line arriving up to an interval late costs nothing
        against a condition that is already hours old.
        """
        if now - self._warned_at < JWKS_WARN_INTERVAL_SECONDS:
            return
        self._warned_at = now
        log.warning(message, *args)

    async def _usable_keys(self, now):
        """The key set to judge this request against.

        Refreshed once it has aged past the TTL. While that refresh is failing
        the held set keeps being used, for a bounded grace: Access at the edge
        and the endpoint publishing these keys are different systems, so
        refusing here would take the console down over an outage in the other
        one, using keys we are holding that almost certainly still verify every
        live assertion.
        """
        if self._jwks is not None and now - self._fetched_at <= self.ttl:
            return self._jwks

        if now - self._failed_at >= JWKS_RETRY_INTERVAL_SECONDS and await self._try_refresh(now):
            return self._jwks

        if self._jwks is None:
            self._warn_outage(
                now,
                "Refusing every Access assertion: could not reach %s for signing keys and none are held: %s",
                self.team_domain,
                self._last_error,
            )
            raise _KeysUnavailable

        age = now - self._fetched_at
        if age > self.ttl + JWKS_GRACE_SECONDS:
            self._warn_outage(
                now,
                "Refusing every Access assertion: %s's signing keys are %.1f hours "
                "old, past the grace, and could not reach it to refresh them: %s",
                self.team_domain,
                age / 3600,
                self._last_error,
            )
            raise _KeysUnavailable

        self._warn_outage(
            now,
            "Verifying against signing keys fetched %.0f minutes ago: refreshing "
            "them from %s is failing. This holds until they are %.0f hours old, "
            "after which every assertion is refused.",
            age / 60,
            self.team_domain,
            (self.ttl + JWKS_GRACE_SECONDS) / 3600,
        )
        return self._jwks

    def _lookup(self, kid):
        try:
            return self._jwks[kid]
        except KeyError:
            return None

    async def _signing_key(self, token):
        """The key this token was signed with, refetching once if it is new.

        An unrecognised key id means either a rotation we have not seen or a
        forgery, and asking Cloudflare again tells them apart. Asking is floored
        rather than done per request, because the key id comes from the
        unverified header and the fetch happens while holding the lock every
        other request waits on.
        """
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            return None

        async with self._lock:
            now = asyncio.get_running_loop().time()
            keys = await self._usable_keys(now)

            try:
                return keys[kid]
            except KeyError:
                pass

            # Only worth asking again if what we hold is old enough that
            # Cloudflare could plausibly have published something since. Fresh
            # means the answer would be the set we just read.
            if now - self._fetched_at < JWKS_REFETCH_MIN_INTERVAL_SECONDS:
                return None
            if now - self._failed_at < JWKS_RETRY_INTERVAL_SECONDS:
                return None
            if not await self._try_refresh(now):
                return None
            return self._lookup(kid)

    # ── the answer ───────────────────────────────────────────────

    async def identity(self, token):
        """The verified email address, or None.

        None covers every way this can fail: no configuration, no token, a bad
        signature, the wrong audience, the wrong team, an expired assertion, or
        Cloudflare being unreachable. The caller cannot act differently on any
        of them, and distinguishing them in a return value would invite somebody
        to treat one as good enough.

        The *log* does distinguish them, because operationally they could not be
        more different: a wrong audience is a misconfiguration nobody spots from
        outside, and an unreachable Cloudflare is an outage.

        The token is never logged. It is a bearer credential for its session, and
        a log quoting it hands that session to anyone who can read logs.
        """
        if not token:
            return None

        if not self.is_configured():
            log.warning(
                "Refusing an Access assertion: CF_ACCESS_TEAM_DOMAIN or "
                "CF_ACCESS_AUD is unset, so nothing can be verified against. "
                "Every admin request will be refused until they are set."
            )
            return None

        try:
            key = await self._signing_key(token)
            if key is None:
                log.warning(
                    "Refusing an Access assertion: signed with key id %s, which is not one of %s's published keys.",
                    _kid(token),
                    self.team_domain,
                )
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=f"https://{self.team_domain}",
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "aud", "iss"]},
            )
        except _KeysUnavailable:
            # Reported where it was found, at a rate that does not follow the
            # rate requests arrive at. Adding a line here would put the flood
            # back, and naming the key id would blame the caller for an outage.
            return None
        except jwt.InvalidAudienceError:
            # What a token minted for one of the team's node applications looks
            # like. The fix is configuration, not anything the caller did.
            log.warning(
                "Refusing an Access assertion: it names audience %r, but this "
                "environment's application is %s. Either it was issued for a "
                "different application, or CF_ACCESS_AUD is stale.",
                _claim(token, "aud"),
                _short(self.audience),
            )
            return None
        # PyJWTError rather than InvalidTokenError: PyJWT's error surface is not
        # ours to bound, and anything it raises outside that subtree would be a
        # 500 on a request that should simply be refused.
        except jwt.PyJWTError as exc:
            log.warning("Refusing an Access assertion: %s: %s", type(exc).__name__, exc)
            return None
        except (ValueError, KeyError) as exc:
            log.warning("Refusing an Access assertion: malformed: %s: %s", type(exc).__name__, exc)
            return None

        # Cloudflare puts the address in `email`. A token that verifies but names
        # nobody is not an identity, and something truthy would let a caller
        # believe it had authenticated a person.
        #
        # Lowercased here, at the one place an identity is produced, because the
        # claim arrives as the identity provider spelled it and callers derive a
        # stable id from it. ADMIN_EMAILS and get_or_create_oauth_user normalise
        # the same way, so an Access identity and an OAuth one for one address
        # compare equal.
        email = (claims.get("email") or "").strip().lower()
        if not email:
            log.warning(
                "Refusing an Access assertion: it verifies, but carries no email claim, so it identifies nobody."
            )
            return None
        return email
