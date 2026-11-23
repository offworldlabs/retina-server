"""Fixed-window rate limits for the node API, and for probing a polled radar.

Four limiters, because four things need bounding and they are not the same
thing. The authenticated paths are limited per (node_id, endpoint) and refuse
with a 429: the caller already holds a token, so telling it that it is going too
fast leaks nothing it does not know. Registration has no token, so it is limited
on the node_id in the request body and refuses with the shared 403 rather than a
429. Claiming is authenticated but is bounded on the address as well as the
node, because what it spends is somebody else's mailbox rather than this
server's time. Probing a polled radar is limited per account, because each probe
is a request this server sends to a host the caller chose.

The claim limiter counts in the database, in core/rate_counters.py, and the
other three count in process memory. An allowance of mail to somebody else's
mailbox bounds nothing if a restart refills it or a second worker doubles it.

Detections are held to 8 a second, sized against the 2 Hz contract ceiling
rather than the roughly 1.1 Hz measured cadence of D45. A node cannot exceed one
frame per CPI, and sizing this against today's measurement would start refusing
frames the day blah2 gets faster. Heartbeat and config get 30 a minute each,
both being needed precisely when a node is in trouble.

Fixed windows rather than sliding: a node can send double its rate across a
window boundary, which is harmless at these sizes and a great deal easier to
reason about. Only admitted requests are counted, so a caller hammering a closed
window cannot push its reset further out.

The refusal is returned as data rather than a response. The route layer owns
FastAPI; this module owns a clock, a dict for each limiter counting in memory,
and the claim limiter's queries.
"""

import hashlib
import logging
import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import case, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from core.rate_counters import RateCounter
from services.node_refusals import RATE_LIMITED_BODY, REFUSAL_BODY, refusal_retry_after

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

# The token limiter's keys come from (node_id, endpoint), and node_id is resolved
# from a bearer token, so the keyspace is bounded by rows in the nodes table. This
# bound raises the alarm rather than evicting, so it sits well above the fleet:
# fifty nodes across three limited endpoints need 150 counters, and this leaves
# room for several times that before anything is said.
MAX_TRACKED_KEYS = 512

# The registration limiter's keys are a node_id an unauthenticated caller supplies in
# the request body, so this bound is enforced rather than merely reported: a full map
# refuses identities it has never seen. Its size is therefore a security parameter and
# not a fleet estimate. Fifty nodes need 100 counters, two per node, and everything
# above that is what an attacker has to get admitted before the map is full and every
# node it does not already hold is locked out until the counters expire. At one dict
# entry each, 25,000 identities cost single-digit megabytes, which the droplet can
# spare far more easily than it can spare the endpoint.
MAX_REGISTRATION_KEYS = 50_000

OVERFLOW_LOG_INTERVAL_S = 60.0
RECLAIM_INTERVAL_S = 1.0

# endpoint -> (requests admitted, window in seconds)
ENDPOINT_LIMITS: dict[str, tuple[int, int]] = {
    "detection": (8, 1),
    "heartbeat": (30, 60),
    "config": (30, 60),
}


@dataclass(frozen=True)
class Refusal:
    """What the route should send back. Rendering it is the route's business."""

    status_code: int
    body: dict[str, str]
    retry_after_s: int


class _FixedWindowCounters:
    """Counts admitted requests per key per window, and never evicts a live one.

    Keys map to (window_end, count). Storing the end of the window rather than
    its index means an entry says for itself whether it is still live, which is
    what makes reclaiming the dead ones safe.

    The lock is cheap and the counters are the only bound on the registration
    path, so it is not worth reasoning about which caller might one day run off
    the event loop.
    """

    def __init__(self, clock: Clock, max_tracked: int, *, refuse_when_full: bool = False) -> None:
        self._clock = clock
        self._max_tracked = max_tracked
        self._refuse_when_full = refuse_when_full
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, object], tuple[float, int]] = {}
        self._next_overflow_log = 0.0
        self._next_reclaim = 0.0

    @property
    def tracked_counters(self) -> int:
        with self._lock:
            return len(self._counters)

    def reset(self) -> None:
        """Clear every counter and throttle timestamp. For test fixtures only."""
        with self._lock:
            self._counters.clear()
            self._next_overflow_log = 0.0
            self._next_reclaim = 0.0

    def admit(self, specs: Sequence[tuple[tuple[str, object], int, int]]) -> float | None:
        """Admit one request against every spec, or report the wait in seconds.

        Every spec must have room, and only then is every spec incremented: a
        request refused by the daily limit must not consume the hourly one.
        """
        now = self._clock()
        with self._lock:
            self._reclaim_if_crowded(now)
            if self._refuse_when_full and len(self._counters) >= self._max_tracked:
                # Refuse only a caller none of whose keys are tracked: that is the
                # identity an attacker can manufacture. A caller with even one live
                # key is already known to the map, so it is admitted, even if that
                # re-inserts a sibling key (an hourly counter the reclaim above just
                # swept, say) that a full map would otherwise have refused room for.
                # The bound therefore caps distinct identities rather than exact key
                # counts, and a known caller can carry the map a little past
                # max_tracked, up to about twice it in the worst case, rather than
                # costing an identity it already tracks.
                if all(key not in self._counters for key, _limit, _window_s in specs):
                    # A lower bound on the wait rather than a countdown to anything:
                    # room appears when a live counter expires, which no cheap sum can
                    # predict, and never before the next reclaim.
                    return RECLAIM_INTERVAL_S
            for key, limit, _window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end > now and count >= limit:
                    return window_end - now
            for key, _limit, window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end <= now:
                    window_end, count = float((math.floor(now / window_s) + 1) * window_s), 0
                self._counters[key] = (window_end, count + 1)
            return None

    def _reclaim_if_crowded(self, now: float) -> None:
        """Drop counters whose window has passed, and warn about what is left.

        A live counter is never evicted. Flushing a victim's counter is the same
        as clearing its limit, so an attacker able to force an eviction would
        have removed the control. The gate is inclusive (at or above the bound,
        not only above it): a `refuse_when_full` map caps itself at exactly
        `max_tracked` and never grows past it, so a strictly-above gate would
        never fire for it and its dead entries would never be reclaimed, making
        a full map's refusals permanent instead of self-healing as counters
        expire. The scan itself is throttled the same way as the warning below,
        so a full map pays for one scan a second rather than one per request.
        """
        if len(self._counters) < self._max_tracked:
            return
        if now < self._next_reclaim:
            return
        self._next_reclaim = now + RECLAIM_INTERVAL_S
        for key in [key for key, (window_end, _) in self._counters.items() if window_end <= now]:
            del self._counters[key]
        if len(self._counters) >= self._max_tracked and now >= self._next_overflow_log:
            self._next_overflow_log = now + OVERFLOW_LOG_INTERVAL_S
            if self._refuse_when_full:
                logger.warning(
                    "node rate limit map holds %d live counters, at its %d bound; "
                    "identities it has not already seen are being refused until counters expire",
                    len(self._counters),
                    self._max_tracked,
                )
            else:
                logger.warning(
                    "node rate limit map holds %d live counters, at or above the %d expected of "
                    "the fleet; nothing has been evicted",
                    len(self._counters),
                    self._max_tracked,
                )


class TokenRateLimiter:
    """Per (node_id, endpoint), for the three paths that carry a bearer token."""

    def __init__(self, clock: Clock = time.monotonic, max_tracked: int = MAX_TRACKED_KEYS) -> None:
        self._counters = _FixedWindowCounters(clock, max_tracked)

    @property
    def tracked_counters(self) -> int:
        return self._counters.tracked_counters

    def reset(self) -> None:
        """Clear every tracked counter. For test fixtures only."""
        self._counters.reset()

    def admit(self, node_id: str, endpoint: str) -> Refusal | None:
        """Spend one allowance for this node and endpoint, or refuse.

        None means the call is admitted and the allowance is spent; call this
        once per request, not as a predicate to poll.
        """
        if endpoint not in ENDPOINT_LIMITS:
            raise ValueError(f"no rate limit configured for endpoint {endpoint!r}")
        limit, window_s = ENDPOINT_LIMITS[endpoint]
        wait_s = self._counters.admit([((node_id, endpoint), limit, window_s)])
        if wait_s is None:
            return None
        return Refusal(429, dict(RATE_LIMITED_BODY), max(1, math.ceil(wait_s)))


# A process wants exactly one of each limiter, since the counters are the bound and
# a second instance is a second allowance. The module-level singleton belongs here,
# next to the class, and the branch that first imports one adds it: an instance with
# no caller is dead code, and the dead-code gate is right to say so.
#
# routes/node_stream.py is the first caller: detection and heartbeat are the two
# token-authenticated paths that exist. PUT /nodes/config shares this instance
# rather than building its own, since the allowance is per (node_id, endpoint).
token_rate_limiter = TokenRateLimiter()


# (requests admitted, window in seconds), applied to the node and to the address
# independently. Somebody bringing a node up types an address, mistypes it,
# corrects it and asks for the mail again, so five in an hour covers the real
# case several times over.
#
# The address half is the one that matters. A node is cheap to register, so a
# per-node limit alone would bound nothing about how much mail one mailbox can
# be sent from our domain.
CLAIM_LIMITS: tuple[tuple[int, int], ...] = ((5, 3600), (20, 86400))


def _counter_key(scope: str, identity: str) -> str:
    """The rate_counters key for one identity counted under one scope.

    The NUL makes the boundary between the two unambiguous, so no node_id can be
    spelled to land on an address's counter, or the other way about.
    """
    return hashlib.sha256(f"{scope}\x00{identity}".encode()).hexdigest()


def _spend(key: str, window_s: int, now: float) -> ReturningInsert[tuple[int, float]]:
    """One upsert spending a request against one counter, answering with the
    count and the start of the window it was spent in.

    A window starts at the request that opens it rather than on a boundary of
    the clock, which with wall-clock time would reset every daily counter at
    midnight UTC at once. A row whose window has passed is restarted in place,
    so a key that went quiet is treated exactly as one never seen.

    SQLite evaluates every SET expression against the row as it stood, so
    `lapsed` reads the old expires_at in all three, including the one that
    assigns it.
    """
    stmt = sqlite_insert(RateCounter).values(
        key=key, window_s=window_s, window_start=now, count=1, expires_at=now + window_s
    )
    lapsed = RateCounter.expires_at <= now
    return stmt.on_conflict_do_update(
        index_elements=[RateCounter.key, RateCounter.window_s],
        set_={
            "window_start": case((lapsed, stmt.excluded.window_start), else_=RateCounter.window_start),
            "count": case((lapsed, 1), else_=RateCounter.count + 1),
            "expires_at": case((lapsed, stmt.excluded.expires_at), else_=RateCounter.expires_at),
        },
    ).returning(RateCounter.count, RateCounter.window_start)


class ClaimRateLimiter:
    """Per node and per address, for the one endpoint that makes the server mail
    somebody who has not asked to hear from us.

    Both limits are spent together or neither is: a nomination refused because
    the address is busy must not consume the node's own allowance, or one
    popular address would exhaust every node that tried it. All four counters
    are spent in one transaction, committed only if every one had room.

    The counters are rows in rate_counters rather than entries in a dict, so a
    restart does not refill them and every worker spends the same ones. The
    first upsert takes SQLite's write lock and holds it to the commit or the
    rollback, which makes the check and the spend of all four one step across
    processes, not only within one. That rests on the driver opening its
    transaction at the first write rather than the first read, as pysqlite's
    legacy transaction mode does and this server runs it: a transaction that
    had read first would have to upgrade its lock, and SQLite refuses that
    upgrade outright, rather than waiting out busy_timeout, when another
    connection is writing or has written since the read.

    Wall-clock time, unlike the limiters counting in memory: a monotonic clock
    means nothing to another process, or to this one after a restart.

    A 429 tells the caller its own node is going too fast, which it already knows.
    It does also say, faintly, that an address it named has been nominated
    elsewhere recently, since the two limits are indistinguishable in the answer.
    That is the price of bounding the mailbox rather than the node, and the
    alternative is not bounding it.
    """

    def __init__(self, clock: Clock = time.time) -> None:
        self._clock = clock

    async def admit(self, session: AsyncSession, node_id: str, email: str) -> Refusal | None:
        """Spend one nomination for this node and this address, or refuse.

        None means the call is admitted and both allowances are spent; call this
        once per request, not as a predicate to poll. `email` must be normalised,
        or one address written two ways gets two allowances.

        Runs a transaction of its own on `session`, which must have nothing
        pending: committed when admitted, rolled back when refused or when
        anything raises, so a refused nomination writes nothing.
        """
        now = self._clock()
        try:
            # The node's counters before the address's. The first one found full
            # decides the Retry-After, so when both are full the answer counts
            # down the node's own window, which the caller already knows about,
            # rather than the address's.
            for key in (_counter_key("claim", node_id), _counter_key("claim_address", email)):
                for limit, window_s in CLAIM_LIMITS:
                    count, window_start = (await session.execute(_spend(key, window_s, now))).one()
                    if count > limit:
                        await session.rollback()
                        return Refusal(429, dict(RATE_LIMITED_BODY), max(1, math.ceil(window_start + window_s - now)))
            # Every admission sweeps the rows whose window has passed, under the
            # lock it already holds, so the table grows with a day's nominations
            # and no further.
            await session.execute(delete(RateCounter).where(RateCounter.expires_at < now))
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        return None


# The one instance, per the note above routes/node_stream.py's limiter, though
# this one holds nothing but its clock: the counters are rows, so a second
# instance would spend the same allowance rather than add one.
claim_rate_limiter = ClaimRateLimiter()


# (requests admitted, window in seconds), from the ADR's table. The escalating
# cooldown it also specified is out of scope for this phase.
REGISTRATION_LIMITS: tuple[tuple[int, int], ...] = ((5, 3600), (20, 86400))


class RegistrationRateLimiter:
    """Per node_id, for the one endpoint with no token to key on.

    This is the only control bounding the identity-takeover path that
    re-registration accepts: a node Mender still accepts may register again and
    the incumbent token is revoked, so the rate at which that can be attempted is
    the whole of the defence.
    """

    def __init__(self, clock: Clock = time.monotonic, max_tracked: int = MAX_REGISTRATION_KEYS) -> None:
        self._counters = _FixedWindowCounters(clock, max_tracked, refuse_when_full=True)

    @property
    def tracked_counters(self) -> int:
        return self._counters.tracked_counters

    def reset(self) -> None:
        """Clear every tracked counter. For test fixtures only."""
        self._counters.reset()

    def admit(self, node_id: str) -> Refusal | None:
        """Spend one registration attempt for this node_id, or refuse.

        None means the attempt is admitted and the allowance is spent; call this
        once per request, not as a predicate to poll.

        The refusal is the same 403 an unknown device gets, with the same
        jittered Retry-After. A 429, or a Retry-After counting down this node's
        window, would confirm to an unauthenticated caller that the identity it
        named is one the server is tracking.
        """
        specs = [((node_id, i), limit, window_s) for i, (limit, window_s) in enumerate(REGISTRATION_LIMITS)]
        if self._counters.admit(specs) is None:
            return None
        return Refusal(403, dict(REFUSAL_BODY), refusal_retry_after())


# The one instance, per the note above: routes/node_register.py is the caller
# that made it worth having. Only registration is limited by node_id, so this is
# process-wide state a second uvicorn worker would double; phase 1 runs one.
registration_limiter = RegistrationRateLimiter()


# (requests admitted, window in seconds) per account. A submit probes again, so
# it spends one too.
POLLED_PROBE_LIMITS: tuple[tuple[int, int], ...] = ((10, 3600),)

# Keys are account ids, and an account costs no more than a mailbox, so the bound
# is enforced as registration's is.
MAX_POLLED_PROBE_KEYS = 10_000


class PolledProbeRateLimiter:
    """Per account, for the owner routes that probe a radar at an address the
    caller typed."""

    def __init__(self, clock: Clock = time.monotonic, max_tracked: int = MAX_POLLED_PROBE_KEYS) -> None:
        self._counters = _FixedWindowCounters(clock, max_tracked, refuse_when_full=True)

    def reset(self) -> None:
        """Clear every tracked counter. For test fixtures only."""
        self._counters.reset()

    def admit(self, user_id: str) -> Refusal | None:
        """Spend one probe for this account, or refuse with the wait in seconds."""
        specs = [
            ((user_id, ("polled_probe", i)), limit, window_s) for i, (limit, window_s) in enumerate(POLLED_PROBE_LIMITS)
        ]
        wait_s = self._counters.admit(specs)
        if wait_s is None:
            return None
        return Refusal(429, dict(RATE_LIMITED_BODY), max(1, math.ceil(wait_s)))


polled_probe_limiter = PolledProbeRateLimiter()
