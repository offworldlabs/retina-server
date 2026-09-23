#!/usr/bin/env python3
"""Ask a deployed environment whether its deploy worked.

    just verify-deploy <prod|staging|test> [--expect-string S ...] [--expect-absent S ...] [--wait MINUTES]

Green CI does not cover the compose, env and front-end seams, so this asks the
environment itself. It reads only, and only through the public hostnames, which
come from the environment's compose overlay. Its verdict rests on public reads;
/api/test/dashboard answers only an administrator or the radar key, so the
checks that need it (RETINA_ENV, task health) run only when the key is in
VERIFY_DEPLOY_RADAR_KEY, and are reported as skipped otherwise. The key is sent
in one header, to that one route, and never printed.

* A request from a script needs a browser User-Agent, or Cloudflare answers
  403 with error 1010. A curl on the droplet answers 400, because nginx demands
  Cloudflare's client certificate (authenticated origin pulls).
* `x-retina-origin` on every /api/ answer proves this repo's nginx answered,
  rather than whatever service the hostname currently points at. The
  synthetic-fleet and radar-polling flags in /api/health, read back against the
  overlay, tell prod, staging and test apart; RETINA_ENV, when keyed, agrees.
* `/api/health` reads `degraded` for 5-11 minutes roughly hourly, and after
  every restart, with aircraft on the map throughout. So it warns and never
  fails; judge a suspected regression against the alert channel's history.
* After a restart the server answers 5xx for a while, publishes an empty feed,
  and takes minutes to refill the map while nodes reconnect and tracks form. So
  health, the public map's feed, the public node list and, on the fleet's box,
  the unfiltered feed are polled together for up to --wait minutes, and only
  what is still wrong at the end fails; a failed read on the last poll alone,
  after a good one, is a blip. A 4xx other than a rate limit, or a certificate
  the interpreter cannot check, will not clear with waiting and ends the wait.
* The public map on every deployed app host draws aircraft-live.json, which
  carries real nodes only, so its aircraft are counted there rather than in the
  map's toolbar, which a scripted browser can leave at 0; nodes are counted in
  the public node list, which leaves out private nodes and polled radars in
  probation. Real nodes can solve nothing for a while (quiet hours, or frames
  that solve nothing), so an empty public map with real nodes connected warns,
  at the cost of the whole --wait. No real node connected, or a feed that has
  stopped, fails: every frame a vetted node sends rewrites the feed, aircraft or
  none. Only the synthetic fleet guarantees aircraft, so on its box the
  unfiltered aircraft.json must carry aircraft a synthetic node saw, and a fleet
  not connected fails.
* A status code on the page proves nothing about the front end. Every script
  and stylesheet the page names from its own host, and every chunk and
  stylesheet those reach, has to come back as what it is; ones on other hosts
  are listed, not read. --expect-string proves a change shipped by
  finding a string it adds, and --expect-absent by the absence of one it
  removes; run both before the deploy as well, or a string that was always
  there proves nothing. The page itself is searched too. A file the page never
  names (one under public/ that only a link reaches) is not read.

No public endpoint reports the deployed commit (ClickUp 123zgec4n48), so a
front-end change is attributed with --expect-string. The connected real nodes
on the public list are listed but not judged, having nothing to compare with;
set the list beside one from a run before the deploy. Straight after a restart
the list is empty until the first analytics refresh, and can then still hold
nodes that have not reconnected (they are swept offline within about three
minutes), so repeat a run made that early before comparing. Looking at the map
in a browser stays a human check.

Exit status: 0 when every check passed (warnings allowed), 1 when one failed,
2 on a usage error.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import functools
import http.client
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

OVERLAYS = {
    "prod": "docker-compose.prod.yml",
    "staging": "docker-compose.staging.yml",
    "test": "docker-compose.test.yml",
}
ALIASES = {"production": "prod"}

# Any current desktop browser's string passes Cloudflare's browser integrity check.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# What the public map draws (dashboard/src/pages/map/hooks.ts), and the unfiltered feed.
MAP_FEED = "/api/radar/data/aircraft-live.json"
FLEET_FEED = "/api/radar/data/aircraft.json"
# /api/health's flags that say which box answered, and the overlay variable each
# follows. Both are on at exactly "1" (routes/sim_ingest.py, services/blah2_poller.py).
IDENTITY = (("synthetic_fleet", "SYNTHETIC_FLEET_ENABLED"), ("polled_radar_polling", "POLLED_RADAR_POLLING_ENABLED"))
# The aircraft flush rewrites the feed about once a second.
FEED_STALE_S = 60
POLL_S = 20
DEFAULT_WAIT_MIN = 10
# Where the radar key comes from, when the keyed checks are wanted.
KEY_VARIABLE = "VERIFY_DEPLOY_RADAR_KEY"
# Timeouts and rate limits: 4xx answers that waiting can clear.
TRANSIENT_4XX = {408, 425, 429}
# A bound on the asset walk; the console has about fifty.
MAX_ASSETS = 400
# The content types deploy/page-asset.sh accepts as a script, pinned together by
# backend/tests/test_verify_deploy.py.
JAVASCRIPT = re.compile(r"javascript|ecmascript")
STYLESHEET = re.compile(r"text/css")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Unreachable(Exception):
    def __init__(self, message: str, *, final: bool = False):
        super().__init__(message)
        # A certificate this interpreter cannot check. A name that does not resolve is
        # not final: macOS reports a dropped network the same way.
        self.final = final


class Final(str):
    """Why a reading failed, when waiting will not change it (a 4xx)."""


@functools.lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    # python.org's interpreter ships without a CA bundle; the backend venv has certifi.
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


class _Stay(urllib.request.HTTPRedirectHandler):
    """Answers a redirect with the 3xx itself, so a credential never follows one to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def _open(request: urllib.request.Request, *, follow: bool):
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())] + ([] if follow else [_Stay()])
    return urllib.request.build_opener(*handlers).open(request, timeout=30)


def fetch(url: str, headers: dict[str, str] | None = None) -> Response:
    """GET `url`. Extra headers carry a credential, so a request with them is not redirected."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})})
    for attempt in (1, 2):
        try:
            try:
                with _open(request, follow=not headers) as resp:
                    return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
            except urllib.error.HTTPError as err:
                return Response(err.code, {k.lower(): v for k, v in err.headers.items()}, err.read())
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            reason = getattr(err, "reason", err)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise Unreachable(
                    f"{url}: certificate verification failed. This interpreter has no CA bundle; "
                    "run it through `just verify-deploy`, whose venv carries certifi.",
                    final=True,
                ) from err
            if attempt == 2:
                raise Unreachable(f"{url}: {reason!r}") from err
            time.sleep(5)
    raise AssertionError("unreachable")


def _overlay_setting(env: str, key: str) -> str | None:
    text = (REPO / OVERLAYS[env]).read_text()
    found = re.search(rf"^\s*-\s*{key}=(\S+)\s*$", text, re.MULTILINE)
    return found.group(1) if found else None


@dataclass(frozen=True)
class Environment:
    api: str
    app: str
    retina_env: str
    synthetic_fleet: bool
    polled_radar_polling: bool = False


def environment(env: str) -> Environment:
    """What `env`'s overlay deploys: its hostnames, and what the server should say it is."""
    settings = {key: _overlay_setting(env, key) for key in ("HOST_API", "HOST_APP", "RETINA_ENV")}
    missing = sorted(key for key, value in settings.items() if not value)
    if missing:
        raise SystemExit(f"{OVERLAYS[env]} sets no {', '.join(missing)}")
    flags = {flag: _overlay_setting(env, variable) == "1" for flag, variable in IDENTITY}
    return Environment(settings["HOST_API"], settings["HOST_APP"], settings["RETINA_ENV"], **flags)


def origin_marker() -> tuple[str, str]:
    """The header and value deploy/origin-marker.sh asserts, so the two cannot drift."""
    text = (REPO / "deploy" / "origin-marker.sh").read_text()
    header = re.search(r'^RETINA_ORIGIN_HEADER="([^"]+)"', text, re.MULTILINE)
    value = re.search(r'^RETINA_ORIGIN_VALUE="([^"]+)"', text, re.MULTILINE)
    if not (header and value):
        raise SystemExit("deploy/origin-marker.sh no longer defines RETINA_ORIGIN_HEADER and RETINA_ORIGIN_VALUE")
    return header.group(1), value.group(1)


@dataclass
class Report:
    rows: list[tuple[str, str, str]] = field(default_factory=list)
    # Never printed: every detail passes through here on its way in.
    secret: str = ""

    def add(self, level: str, name: str, detail: str) -> None:
        if self.secret:
            detail = detail.replace(self.secret, "[radar key]")
        self.rows.append((level, name, detail))

    def levels(self, level: str) -> list[str]:
        return [name for lvl, name, _ in self.rows if lvl == level]

    def render(self) -> str:
        lines = []
        for level, name, detail in self.rows:
            first, *rest = detail.splitlines() or [""]
            lines.append(f"  {level:<5} {name:<14} {first}")
            lines.extend(f"{'':22}{line}" for line in rest)
        return "\n".join(lines)


@dataclass
class Reading:
    """One poll of the endpoints that recover after a restart."""

    health: dict | str = ""  # the body, or why it could not be read
    feed: dict | str = ""  # MAP_FEED
    feed_age: float = 0.0
    nodes: dict | str = ""  # /api/radar/nodes
    fleet: dict | str | None = None  # FLEET_FEED, read on the fleet's box only
    at: float = 0.0  # when the poll began
    feed_at: float = 0.0  # when its feed was read

    def over(self, earlier: Reading) -> Reading:
        """This reading, with an endpoint it could not read taken from `earlier` if that one could."""
        merged = Reading(self.health, self.feed, self.feed_age, self.nodes, self.fleet, self.at, self.feed_at)
        if isinstance(self.health, str) and not isinstance(self.health, Final) and isinstance(earlier.health, dict):
            merged.health = earlier.health
        carry_feed = isinstance(earlier.feed, dict) and earlier.feed_age <= FEED_STALE_S
        if isinstance(self.feed, str) and not isinstance(self.feed, Final) and carry_feed:
            merged.feed, merged.feed_age = earlier.feed, earlier.feed_age + (self.at - earlier.feed_at)
        if isinstance(self.nodes, str) and not isinstance(self.nodes, Final) and isinstance(earlier.nodes, dict):
            merged.nodes = earlier.nodes
        if isinstance(self.fleet, str) and not isinstance(self.fleet, Final) and isinstance(earlier.fleet, dict):
            merged.fleet = earlier.fleet
        return merged

    @property
    def final(self) -> bool:
        return any(isinstance(x, Final) for x in (self.health, self.feed, self.nodes, self.fleet))

    @property
    def shown(self) -> int:
        """Aircraft on the public map."""
        return len(self.feed["aircraft"]) if isinstance(self.feed, dict) else 0

    def connected(self, synthetic: bool) -> list[str]:
        """The refs of the connected nodes on the public list, synthetic or real."""
        listed = self.nodes["nodes"] if isinstance(self.nodes, dict) else {}
        return sorted(
            n.get("node_ref") or key
            for key, n in listed.items()
            if bool(n.get("is_synthetic")) == synthetic and n.get("status") != "disconnected"
        )

    def seen_by(self, refs: list[str]) -> int:
        """How many aircraft in the unfiltered feed a node in `refs` saw."""
        if not isinstance(self.fleet, dict):
            return 0
        wanted = set(refs)
        return sum(
            1 for a in self.fleet["aircraft"] if wanted & {a.get("node_ref"), *(a.get("contributing_node_refs") or [])}
        )


class Verifier:
    def __init__(
        self,
        env: Environment,
        *,
        get: Callable[[str], Response] = fetch,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
        wait_s: float = DEFAULT_WAIT_MIN * 60,
        marker: tuple[str, str] | None = None,
        key: str = "",
    ):
        self.env = env
        self.key = key
        self.api = f"https://{env.api}"
        self.app = f"https://{env.app}"
        self.get = get
        self.clock = clock
        self.sleep = sleep
        self.log = log
        self.wait_s = wait_s
        self.marker = marker or origin_marker()
        self.report = Report(secret=key)
        self.waited = 0.0
        self.foreign: list[str] = []
        # Ordered sets: the settle loop reads the same URLs repeatedly, several at a
        # time, so writes to them hold the lock.
        self._unmarked: dict[str, None] = {}
        self._marked: dict[str, None] = {}
        self._marks = threading.Lock()

    def run(self, expect: list[str], absent: list[str] = ()) -> Report:
        reading = self._settle()
        self._judge(reading)
        self._keyed()
        self._origin()
        self._bundle(expect, list(absent))
        return self.report

    def _json(self, url: str, headers: dict[str, str] | None = None) -> tuple[Response, dict] | str:
        """The response and its JSON object, or why there is none."""
        try:
            resp = self.get(url, headers) if headers else self.get(url)
        except Unreachable as err:
            return Final(err) if err.final else str(err)
        if self.key:
            # Before anything quotes the body: a quote cut short or escaped would slip past Report.
            resp = Response(resp.status, resp.headers, resp.body.replace(self.key.encode(), b"[radar key]"))
        if resp.status != 200:
            why = f"{url} answered {resp.status}.{_explain(resp)}"
            return Final(why) if 400 <= resp.status < 500 and resp.status not in TRANSIENT_4XX else why
        # Only a 200 is judged for the marker: an error page from the edge never reached nginx.
        header, value = self.marker
        with self._marks:
            if value in resp.headers.get(header, ""):
                self._marked[urllib.parse.urlsplit(url).netloc] = None
                self._unmarked.pop(url, None)
            else:
                self._unmarked[url] = None
        try:
            body = json.loads(resp.body)
        except ValueError:
            return f"{url} answered 200 with no JSON: {resp.text[:200]!r}"
        return (resp, body) if isinstance(body, dict) else f"{url} answered JSON that is not an object"

    def _feed(self, url: str) -> tuple[Response, dict] | str:
        """An aircraft feed, or why it is not one."""
        got = self._json(url)
        if isinstance(got, str):
            return got
        if not isinstance(got[1].get("aircraft"), list) or not isinstance(got[1].get("now", 0), (int, float)):
            return f"{url} is not an aircraft feed: it lacks an aircraft list or a numeric now"
        return got

    def _read(self) -> Reading:
        reading = Reading(at=self.clock())

        def read_feed() -> tuple[tuple[Response, dict] | str, float]:
            return self._feed(f"{self.app}{MAP_FEED}"), self.clock()

        def read_nodes() -> tuple[Response, dict] | str:
            got = self._json(f"{self.api}/api/radar/nodes")
            if isinstance(got, tuple) and not isinstance(got[1].get("nodes"), dict):
                return f"{self.api}/api/radar/nodes carries no node list"
            return got

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            health = pool.submit(self._json, f"{self.api}/api/health")
            feed = pool.submit(read_feed)
            nodes = pool.submit(read_nodes)
            fleet = pool.submit(self._feed, f"{self.app}{FLEET_FEED}") if self.env.synthetic_fleet else None
            health, (got, reading.feed_at), nodes = health.result(), feed.result(), nodes.result()
            if fleet is not None:
                fleet = fleet.result()
                reading.fleet = fleet[1] if isinstance(fleet, tuple) else fleet
        reading.health = health[1] if isinstance(health, tuple) else health
        reading.nodes = nodes[1] if isinstance(nodes, tuple) else nodes
        if isinstance(got, str):
            reading.feed = got
        else:
            reading.feed = got[1]
            reading.feed_age = _served_at(got[0], self.clock) - float(got[1].get("now") or 0)
        return reading

    def _settle(self) -> Reading:
        start = self.clock()
        deadline = start + self.wait_s
        announced = False
        previous = Reading()
        while True:
            reading = self._read()
            if self._settled(reading) or reading.final or self.clock() >= deadline:
                self.waited = self.clock() - start
                return reading.over(previous)
            previous = reading
            if not announced:
                self.log(
                    f"  waiting for the server to settle (a restart takes minutes to refill the map); "
                    f"polling every {POLL_S}s for up to {self.wait_s / 60:.0f} min"
                )
                announced = True
            self.sleep(POLL_S)

    def _settled(self, reading: Reading) -> bool:
        """Whether every reading the verdict rests on is as a healthy box leaves it."""
        if not all(isinstance(x, dict) for x in (reading.health, reading.feed, reading.nodes)):
            return False
        if reading.feed_age > FEED_STALE_S:
            return False
        if self.env.synthetic_fleet and not reading.seen_by(reading.connected(synthetic=True)):
            return False
        if reading.connected(synthetic=False):
            return reading.shown > 0
        # With no real node the public map has nothing to show; only the fleet's box can do without one.
        return self.env.synthetic_fleet

    def _judge(self, reading: Reading) -> None:
        after = f" after {_span(self.waited)} of polling" if self.waited >= POLL_S else ""
        health = reading.health
        if isinstance(health, str):
            self.report.add("FAIL", "health", health)
        elif health.get("status") == "ok":
            self.report.add("PASS", "health", "ok")
        elif health.get("status") == "degraded":
            self.report.add(
                "WARN",
                "health",
                "degraded, which is the baseline: roughly hourly for 5-11 minutes, and after every\n"
                "restart. Compare with the environment's alert channel before calling it a regression.",
            )
        else:
            self.report.add("FAIL", "health", f"status is {health.get('status')!r}, neither ok nor degraded")

        feed = reading.feed
        if isinstance(feed, str):
            self.report.add("FAIL", "aircraft feed", feed)
        elif reading.feed_age > FEED_STALE_S:
            self.report.add(
                "FAIL",
                "aircraft feed",
                f"last written {reading.feed_age:.0f}s ago{after}: the aircraft flush has stopped, or no\n"
                "vetted node is sending (a polled radar in probation does not refresh it)",
            )
        else:
            self.report.add(
                "PASS",
                "aircraft feed",
                f"the public map's feed carries {reading.shown} aircraft, written {max(reading.feed_age, 0):.0f}s ago",
            )

        if isinstance(reading.nodes, str):
            self.report.add("FAIL", "map", reading.nodes)
        else:
            self._map(reading, after)
            self._fleet(reading, after)
        if isinstance(health, dict):
            self._box(health)

    def _map(self, reading: Reading, after: str) -> None:
        real = reading.connected(synthetic=False)
        if not real and not self.env.synthetic_fleet:
            self.report.add("FAIL", "map", f"no real node on the public list is connected{after}")
        elif not real:
            self.report.add("INFO", "map", "no real node is connected, and the public map shows real nodes only")
        elif isinstance(reading.feed, str):
            pass  # the aircraft feed row says why there is no count
        elif reading.shown:
            self.report.add(
                "PASS", "map", f"{reading.shown} aircraft on the public map; {len(real)} real node(s) connected"
            )
        else:
            self.report.add(
                "WARN",
                "map",
                f"no aircraft on the public map with {len(real)} real node(s) connected{after}: a quiet sky,\n"
                "or frames that solve nothing. Compare with the alert channel.",
            )
        self.report.add("INFO", "real nodes", f"{len(real)} connected: {' '.join(real) or 'none'}")

    def _fleet(self, reading: Reading, after: str) -> None:
        if not self.env.synthetic_fleet:
            return
        real, synthetic = reading.connected(synthetic=False), reading.connected(synthetic=True)
        if not synthetic:
            self.report.add(
                "FAIL", "fleet", f"the synthetic fleet is not connected{after}; {len(real)} real node(s) are"
            )
        elif isinstance(reading.fleet, str):
            self.report.add("FAIL", "fleet", reading.fleet)
        elif seen := reading.seen_by(synthetic):
            self.report.add(
                "PASS", "fleet", f"{seen} aircraft seen by the {len(synthetic)} connected synthetic node(s)"
            )
        else:
            window = " Past the post-restart window, that is a fault." if after else ""
            self.report.add(
                "FAIL",
                "fleet",
                f"no aircraft in {FLEET_FEED} was seen by the {len(synthetic)} connected synthetic\n"
                f"node(s){after}.{window}",
            )

    def _box(self, health: dict) -> None:
        """Which box answered, from the flags /api/health reports and the overlay sets."""
        agreed, wrong, missing = [], [], []
        for flag, variable in IDENTITY:
            want = getattr(self.env, flag)
            if flag not in health:
                missing.append(flag)
            elif bool(health[flag]) != want:
                wrong.append(f"{flag}={health[flag]} where the overlay {'sets' if want else 'leaves off'} {variable}")
            else:
                agreed.append(f"{flag}={bool(health[flag])}")
        if wrong:
            self.report.add(
                "FAIL",
                "box",
                f"the server says {'; '.join(wrong)}:\n"
                "the hostname reaches another box, or the droplet's backend/.env sets the flag itself",
            )
        elif missing:
            self.report.add(
                "WARN", "box", f"/api/health does not report {', '.join(missing)}, so which box answered is unproven"
            )
        else:
            self.report.add("PASS", "box", f"{' and '.join(agreed)}, as the overlay sets")

    def _keyed(self) -> None:
        """The checks /api/test/dashboard allows, run only with the radar key."""
        if not self.key:
            self.report.add(
                "SKIP",
                "keyed checks",
                f"RETINA_ENV and task health were not read: set {KEY_VARIABLE} to run them.\n"
                "The key stays on the droplet; the runbook's tst reads the same route there.",
            )
            return
        got = self._json(f"{self.api}/api/test/dashboard", {"X-API-Key": self.key})
        if isinstance(got, str):
            self.report.add("FAIL", "keyed checks", f"{got} A 401 or 403 means the key is not this environment's.")
            return
        dash = got[1]
        served_env = dash.get("environment")
        if served_env == self.env.retina_env:
            self.report.add("PASS", "environment", f"RETINA_ENV={served_env}, as the overlay sets")
        else:
            self.report.add(
                "FAIL",
                "environment",
                f"the server says RETINA_ENV={served_env!r} where the overlay sets {self.env.retina_env!r}: "
                "the hostname reaches another box",
            )
        stale = dash.get("task_health", {}).get("stale_tasks") or []
        if stale:
            self.report.add("WARN", "tasks", f"stale: {', '.join(map(str, stale))}")
        waiting = {k: v for k, v in dash.get("subsystem_health", {}).items() if v != "ok"}
        if waiting:
            self.report.add("WARN", "subsystems", ", ".join(f"{k}={v}" for k, v in sorted(waiting.items())))

    def _origin(self) -> None:
        header, value = self.marker
        if self._unmarked:
            self.report.add(
                "FAIL",
                "origin",
                f"no {header}: {value} on {', '.join(self._unmarked)}.\n"
                "This repo's nginx did not answer: an Origin Rule points the hostname elsewhere,\n"
                "or the vhost lost its security-headers include.",
            )
        elif self._marked:
            self.report.add("PASS", "origin", f"{header}: {value} from {', '.join(self._marked)}")

    def _bundle(self, expect: list[str], absent: list[str]) -> None:
        page_url = f"{self.app}/?cb={secrets.token_hex(6)}"
        try:
            page = self.get(page_url)
            if page.status >= 500:
                page = self.get(page_url)
        except Unreachable as err:
            self.report.add("FAIL", "bundle", str(err))
            return
        if page.status != 200 or "text/html" not in page.headers.get("content-type", ""):
            self.report.add(
                "FAIL",
                "bundle",
                f"{page_url} answered {page.status} {page.headers.get('content-type')}.{_explain(page)}",
            )
            return
        src = next(
            (
                ref.group(1)
                for attrs in re.findall(r"<script\b([^>]*)>", page.text, re.IGNORECASE)
                if re.search(r'\btype=["\']?module\b', attrs, re.IGNORECASE)
                and (ref := re.search(r'\bsrc=["\']([^"\']+)["\']', attrs, re.IGNORECASE))
            ),
            None,
        )
        if not src:
            self.report.add(
                "FAIL", "bundle", f"{self.app}/ names no module script, so it is not the console's index.html"
            )
            return
        index_url = urllib.parse.urljoin(page_url, src)
        sources, broken, unread = self._walk(page_url, page.text, index_url)
        searched = {**sources, f"{self.app}/index.html": page.text}
        if index_url in broken:
            self.report.add("FAIL", "bundle", f"{_name(index_url)} {broken[index_url]}")
            return
        if broken:
            listed = "; ".join(f"{_name(url)} {why}" for url, why in list(broken.items())[:5])
            self.report.add("FAIL", "bundle", f"{len(broken)} of the page's assets did not resolve: {listed}")
        else:
            self.report.add(
                "PASS", "bundle", f"{_name(index_url)} and the {len(sources) - 1} other asset(s) it needs resolve"
            )
        if unread:
            self.report.add("WARN", "bundle", f"stopped at {MAX_ASSETS} assets with {unread} more unread")
        if self.foreign:
            self.report.add("INFO", "other hosts", f"the page also names, not read: {' '.join(self.foreign)}")
        for needle in expect:
            hit = next((url for url, text in searched.items() if needle in text), None)
            if hit:
                self.report.add("PASS", "expect", f"{needle!r} is in {_name(hit)}")
            else:
                self.report.add("FAIL", "expect", f"{needle!r} is in none of the {len(searched)} file(s) read")
        for needle in absent:
            hit = next((url for url, text in searched.items() if needle in text), None)
            if hit:
                self.report.add("FAIL", "expect", f"{needle!r} is still in {_name(hit)}")
            elif unread:
                self.report.add("FAIL", "expect", f"{needle!r} may be in one of the {unread} asset(s) not read")
            else:
                self.report.add("PASS", "expect", f"{needle!r} is in none of the {len(searched)} file(s) read")

    def _walk(self, page_url: str, page: str, index_url: str) -> tuple[dict[str, str], dict[str, str], int]:
        """Every script and stylesheet the page names and every chunk and stylesheet those reach.

        Returns their sources, why the rest did not resolve, and how many were left
        unread at the MAX_ASSETS bound.
        """
        origin = urllib.parse.urlsplit(page_url).netloc
        named = [index_url]
        self.foreign = []
        for tag, attrs in re.findall(r"<(script|link)\b([^>]*)>", page, re.IGNORECASE):
            if tag.lower() == "link" and not re.search(r'\brel=["\']?stylesheet\b', attrs, re.IGNORECASE):
                continue
            ref = re.search(r'\b(?:src|href)=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
            if ref:
                url = urllib.parse.urljoin(page_url, ref.group(1))
                if urllib.parse.urlsplit(url).netloc == origin:
                    named.append(url)
                else:
                    self.foreign.append(url)
        sources: dict[str, str] = {}
        broken: dict[str, str] = {}
        seen = set(named)
        level = list(dict.fromkeys(named))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            while level:
                room = MAX_ASSETS - len(sources) - len(broken)
                batch, rest = level[:room], level[room:]
                following = []
                for url, (text, why) in zip(batch, pool.map(self._asset, batch), strict=True):
                    if text is None:
                        broken[url] = why
                        continue
                    sources[url] = text
                    if _kind(url) is JAVASCRIPT:
                        for ref in _chunk_refs(text):
                            ref_url = urllib.parse.urljoin(url, ref)
                            if ref_url not in seen:
                                seen.add(ref_url)
                                following.append(ref_url)
                if rest:
                    return sources, broken, len(rest) + len(following)
                level = following
        return sources, broken, 0

    def _asset(self, url: str) -> tuple[str, None] | tuple[None, str]:
        """An asset's text, or why it is not what the page expects. A 5xx is asked twice."""
        kind = _kind(url)
        for attempt in (1, 2):
            try:
                resp = self.get(url)
            except Unreachable as err:
                # fetch has already asked twice.
                return None, f"unreachable: {err}"
            ctype = resp.headers.get("content-type", "")
            if resp.status == 200 and kind.search(ctype):
                return resp.text, None
            why = f"answered {resp.status} {ctype}"
            if resp.status == 200:
                return None, f"{why}: the SPA fallback's index.html, so the page renders nothing"
            if resp.status == 404:
                return None, f"{why}: the deploy does not carry it"
            if resp.status < 500 or attempt == 2:
                return None, why
        raise AssertionError("unreachable")


def _kind(url: str) -> re.Pattern:
    return STYLESHEET if urllib.parse.urlsplit(url).path.endswith(".css") else JAVASCRIPT


def _span(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 120 else f"{seconds / 60:.0f} min"


def _name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


def _chunk_refs(text: str) -> list[str]:
    # Vite names lazy chunks relative to the importer ("./Page-hash.js") and lists
    # preload dependencies, a lazy page's stylesheet among them, from the root
    # ("assets/Page-hash.css").
    refs = []
    for ref in re.findall(r"""["'`]((?:\./|/?assets/)[\w.-]+\.(?:js|css))["'`]""", text):
        refs.append("/" + ref.lstrip("/") if ref.startswith(("assets/", "/assets/")) else ref)
    return list(dict.fromkeys(refs))


def _served_at(resp: Response, clock: Callable[[], float]) -> float:
    # The edge's Date header, so a skewed local clock cannot fake a stale feed.
    try:
        return email.utils.parsedate_to_datetime(resp.headers["date"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return clock()


def _explain(resp: Response) -> str:
    body = resp.text
    if resp.status == 403 and "1010" in body:
        return " That is Cloudflare's browser integrity check (error 1010) refusing the User-Agent."
    if resp.status == 400 and "SSL certificate" in body:
        return " nginx wants Cloudflare's client certificate: this request bypassed the edge."
    return f" {body[:200]!r}" if body else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("env", help="prod, staging or test")
    parser.add_argument(
        "--expect-string",
        action="append",
        default=[],
        metavar="S",
        help="a string the deployed front end must contain; repeatable",
    )
    parser.add_argument(
        "--expect-absent",
        action="append",
        default=[],
        metavar="S",
        help="a string the deployed front end must no longer contain; repeatable",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT_MIN,
        metavar="MINUTES",
        help=f"how long a restarting server may take to settle (default {DEFAULT_WAIT_MIN})",
    )
    args = parser.parse_args(argv)
    name = ALIASES.get(args.env, args.env)
    if name not in OVERLAYS:
        parser.error(f"unknown environment {args.env!r}; use one of {', '.join(OVERLAYS)}")

    env = environment(name)
    print(f"verify-deploy {name}: api {env.api}, app {env.app}")
    key = os.environ.get(KEY_VARIABLE, "").strip()
    if key and not re.fullmatch(r"[!-~]+", key):
        # Named, never shown: http.client would put the key itself in its error.
        parser.error(f"{KEY_VARIABLE} holds a character an HTTP header cannot carry")
    report = Verifier(env, wait_s=args.wait * 60, key=key).run(args.expect_string, args.expect_absent)
    report.add(
        "INFO", "commit", "not reported by any public endpoint; attribute a front-end change with --expect-string"
    )
    print(report.render())

    failed, warned, skipped = report.levels("FAIL"), report.levels("WARN"), report.levels("SKIP")
    if failed:
        print(f"FAIL: {', '.join(dict.fromkeys(failed))}")
        return 1
    notes = [f"{len(warned)} warning(s)"] if warned else []
    notes += [f"{', '.join(skipped)} skipped, not passed"] if skipped else []
    summary = f" with {'; '.join(notes)}" if notes else ""
    print(f"PASS{summary}. The in-browser look at the map is still yours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
