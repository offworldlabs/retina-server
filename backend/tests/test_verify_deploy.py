"""deploy/verify-deploy.py's verdicts, against a fake environment.

Each case is a way a deploy has been misjudged here: a degraded health that was
only the baseline, an empty map read before it had time to fill, an asset the SPA
fallback answered with a 200, a change searched for in the wrong chunk. The
checks must fail on the real faults and pass the baseline, so both directions are
asserted.
"""

from __future__ import annotations

import email.utils
import http.client
import importlib.util
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "verify-deploy.py"
_spec = importlib.util.spec_from_file_location("verify_deploy", _SCRIPT)
vd = importlib.util.module_from_spec(_spec)
sys.modules["verify_deploy"] = vd
_spec.loader.exec_module(vd)

API = "api.example.test"
APP = "app.example.test"
NOW = 1_800_000_000.0
MARKER = ("x-retina-origin", "retina-server")
KEY = "rk-test-9f3c2a7e5b1d4c60"
HEALTH = f"{API}/api/health"
FEED = f"{APP}/api/radar/data/aircraft-live.json"
ALL = f"{APP}/api/radar/data/aircraft.json"
NODES = f"{API}/api/radar/nodes"
DASHBOARD = f"{API}/api/test/dashboard"
REAL = ("ndeaaa", "ndebbb", "ndeccc")

INDEX_HTML = (
    '<html><head><script src="/theme-boot.js"></script>'
    '<link rel="stylesheet" href="https://unpkg.example.test/leaflet.css" />'
    '<script type="module" crossorigin src="/assets/index-abc.js"></script>'
    '<link rel="stylesheet" crossorigin href="/assets/index-xyz.css"></head></html>'
)
INDEX_JS = 'const a=()=>import("./Lazy-def.js");__vite__mapDeps(["assets/Other-ghi.js","assets/Lazy-def.css"]);'


def _resp(body, *, status=200, ctype="application/json", marked=True, date=NOW) -> vd.Response:
    headers = {"content-type": ctype, "date": email.utils.formatdate(date, usegmt=True)}
    if marked:
        headers[MARKER[0]] = MARKER[1]
    raw = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
    return vd.Response(status, headers, raw)


def _feed(aircraft=2, age=1, by="ndeaaa", also=()):
    """A feed of `aircraft` aircraft, each seen by node `by` and contributed to by `also`."""
    seen = [{"hex": f"a{i}", "node_ref": by, "contributing_node_refs": list(also)} for i in range(aircraft)]
    return _resp({"now": NOW - age, "aircraft": seen})


def _health(fleet=False, polling=False, status="ok"):
    return _resp({"status": status, "synthetic_fleet": fleet, "polled_radar_polling": polling})


def _nodes(real=REAL, synthetic=("synth-1",), gone=("ndegone",)):
    listed = {}
    for refs, status, is_synthetic in (
        (real, "active", False),
        (synthetic, "active", True),
        (gone, "disconnected", False),
    ):
        for ref in refs:
            listed[f"k-{ref}"] = {"node_ref": ref, "status": status, "is_synthetic": is_synthetic}
    return _resp({"nodes": listed})


def _dashboard(env="test", stale=(), subsystems=None):
    return {
        "environment": env,
        "subsystem_health": subsystems or {"tcp_server": "ok"},
        "task_health": {"stale_tasks": list(stale)},
    }


def _site(**overrides) -> dict[str, object]:
    """URL -> response (or a list of responses, served in turn) for a healthy box without the fleet."""
    site = {
        f"https://{HEALTH}": _health(),
        f"https://{FEED}": _feed(),
        f"https://{ALL}": _feed(3, by="synth-1"),
        f"https://{NODES}": _nodes(),
        f"https://{DASHBOARD}": _resp(_dashboard()),
        f"https://{APP}/": _resp(INDEX_HTML, ctype="text/html", marked=False),
        f"https://{APP}/assets/index-abc.js": _resp(INDEX_JS, ctype="text/javascript", marked=False),
        f"https://{APP}/assets/Lazy-def.js": _resp('export const t="Shipped today";', ctype="text/javascript"),
        f"https://{APP}/assets/Other-ghi.js": _resp("export const u=1;", ctype="text/javascript"),
        f"https://{APP}/assets/index-xyz.css": _resp(".retina-banner{color:teal}", ctype="text/css", marked=False),
        f"https://{APP}/assets/Lazy-def.css": _resp(".map-surface{inset:0}", ctype="text/css", marked=False),
        f"https://{APP}/theme-boot.js": _resp('document.documentElement.dataset.theme="dark"', ctype="text/javascript"),
    }
    site.update({(k if k.startswith("https://") else f"https://{k}"): v for k, v in overrides.items()})
    return site


def _fleet_site(**overrides) -> dict[str, object]:
    """The fleet's box, healthy: the flag on and the synthetic fleet connected and seeing aircraft."""
    return _site(**{HEALTH: _health(fleet=True), **overrides})


class _Clock:
    def __init__(self):
        self.t = NOW
        self.sleeps = 0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds


class _Asked(list):
    """The URLs asked for, in order, and the headers each was last asked with."""

    def __init__(self):
        super().__init__()
        self.headers: dict[str, dict | None] = {}


def _serve(site, url):
    answer = site[url]
    if isinstance(answer, list):
        answer = answer.pop(0) if len(answer) > 1 else answer[0]
    if isinstance(answer, Exception):
        raise answer
    return answer


def _verify(site, expect=(), *, absent=(), wait_s=60, retina_env="test", fleet=False, polling=False, key=""):
    clock = _Clock()
    asked = _Asked()

    def get(url, headers=None):
        parts = urllib.parse.urlsplit(url)
        path = f"{parts.scheme}://{parts.netloc}{parts.path}"
        asked.append(path)
        asked.headers[path] = headers
        return _serve(site, path)

    env = vd.Environment(API, APP, retina_env, fleet, polling)
    verifier = vd.Verifier(
        env, get=get, clock=clock, sleep=clock.sleep, log=lambda _: None, wait_s=wait_s, marker=MARKER, key=key
    )
    report = verifier.run(list(expect), list(absent))
    return report, clock, asked


def _rows(report, level):
    return {name: detail for lvl, name, detail in report.rows if lvl == level}


def test_a_healthy_environment_passes():
    report, clock, asked = _verify(_site())
    assert _rows(report, "FAIL") == {}
    assert set(_rows(report, "PASS")) == {"health", "aircraft feed", "map", "box", "origin", "bundle"}
    assert clock.sleeps == 0
    # The unfiltered feed is the fleet's box's business alone.
    assert f"https://{ALL}" not in asked


def test_degraded_health_is_the_baseline_and_only_warns():
    report, _, _ = _verify(_site(**{HEALTH: _health(status="degraded")}))
    assert "health" in _rows(report, "WARN")
    assert _rows(report, "FAIL") == {}


def test_a_cloudflare_1010_refusal_is_named():
    refused = _resp("error code: 1010", status=403, ctype="text/plain", marked=False)
    report, _, _ = _verify(_site(**{HEALTH: refused}))
    assert "1010" in _rows(report, "FAIL")["health"]
    assert "User-Agent" in _rows(report, "FAIL")["health"]


def test_an_answer_without_the_origin_marker_fails():
    other = _resp({"now": NOW, "aircraft": []}, marked=False)
    report, _, _ = _verify(_site(**{FEED: other}))
    assert f"https://{FEED}" in _rows(report, "FAIL")["origin"]


def test_a_feed_the_flush_stopped_writing_fails():
    report, _, _ = _verify(_site(**{FEED: _feed(1, age=300)}))
    assert "stopped" in _rows(report, "FAIL")["aircraft feed"]


def test_an_empty_map_is_polled_until_it_fills():
    report, clock, _ = _verify(_site(**{FEED: [_feed(0), _feed(0), _feed(4)]}), wait_s=600)
    assert "4 aircraft on the public map" in _rows(report, "PASS")["map"]
    assert clock.sleeps == 2


def test_the_map_is_judged_on_the_feed_it_draws_not_the_unfiltered_one():
    report, _, asked = _verify(_site(**{FEED: _feed(0), ALL: _feed(9)}), wait_s=60)
    assert "no aircraft on the public map" in _rows(report, "WARN")["map"]
    assert f"https://{FEED}" in asked


# Real-node boxes: an empty public map with real nodes connected warns, and the
# control beside it passes.


def test_an_empty_public_map_over_real_nodes_only_warns():
    report, _, _ = _verify(_site(**{FEED: _feed(0)}), wait_s=60)
    assert "quiet sky" in _rows(report, "WARN")["map"]
    assert _rows(report, "FAIL") == {}


def test_the_same_box_with_aircraft_on_the_public_map_passes():
    report, _, _ = _verify(_site(**{FEED: _feed(5)}), wait_s=60)
    assert _rows(report, "PASS")["map"] == "5 aircraft on the public map; 3 real node(s) connected"


# A feed that errors, times out or is not a feed fails; the healthy control is
# test_a_healthy_environment_passes.


def test_a_feed_that_errors_fails():
    broken = _resp("bad gateway", status=502, ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{FEED: broken}), wait_s=60)
    assert "502" in _rows(report, "FAIL")["aircraft feed"]


def test_a_feed_that_times_out_fails():
    report, _, _ = _verify(_site(**{FEED: vd.Unreachable(f"https://{FEED}: timed out")}), wait_s=60)
    assert "timed out" in _rows(report, "FAIL")["aircraft feed"]


@pytest.mark.parametrize(
    "body",
    [{"now": NOW, "planes": []}, {"now": "soon", "aircraft": []}, {"now": NOW, "aircraft": {}}],
    ids=["no aircraft list", "now not a number", "aircraft not a list"],
)
def test_a_feed_of_the_wrong_shape_fails(body):
    report, _, _ = _verify(_site(**{FEED: _resp(body)}), wait_s=60)
    assert "is not an aircraft feed" in _rows(report, "FAIL")["aircraft feed"]


def test_a_node_list_of_the_wrong_shape_fails():
    report, _, _ = _verify(_site(**{NODES: _resp({"nodes": []})}), wait_s=60)
    assert "carries no node list" in _rows(report, "FAIL")["map"]


# The fleet's box: the fleet must be connected and must see aircraft.


def test_the_fleet_s_box_passes_with_the_fleet_seeing_aircraft():
    report, _, _ = _verify(_fleet_site(), fleet=True)
    assert _rows(report, "PASS")["fleet"] == "3 aircraft seen by the 1 connected synthetic node(s)"
    assert _rows(report, "FAIL") == {}


def test_the_fleet_connected_with_no_aircraft_after_the_window_fails():
    # The same box as the test above, with the fleet's aircraft taken away.
    report, clock, _ = _verify(_fleet_site(**{ALL: _feed(0)}), wait_s=60, fleet=True)
    assert "was seen by the 1 connected synthetic" in _rows(report, "FAIL")["fleet"]
    assert "post-restart window" in _rows(report, "FAIL")["fleet"]
    assert clock.t - NOW >= 60


def test_on_the_fleet_s_box_real_nodes_aircraft_do_not_stand_in_for_the_fleet():
    report, clock, _ = _verify(_fleet_site(**{ALL: _feed(9, by="ndeaaa")}), wait_s=60, fleet=True)
    assert "was seen by the 1 connected synthetic" in _rows(report, "FAIL")["fleet"]
    assert clock.sleeps > 0


def test_an_aircraft_the_fleet_contributed_to_counts_for_it():
    report, _, _ = _verify(_fleet_site(**{ALL: _feed(2, by="ndeaaa", also=("synth-1",))}), fleet=True)
    assert "2 aircraft seen by" in _rows(report, "PASS")["fleet"]


def test_on_the_fleet_s_box_a_fleet_not_connected_fails():
    report, clock, _ = _verify(_fleet_site(**{NODES: _nodes(synthetic=())}), wait_s=60, fleet=True)
    assert "synthetic fleet is not connected" in _rows(report, "FAIL")["fleet"]
    assert clock.sleeps > 0


def test_an_unfiltered_feed_of_the_wrong_shape_fails_the_fleet():
    report, _, _ = _verify(_fleet_site(**{ALL: _resp({"now": NOW})}), wait_s=60, fleet=True)
    assert "is not an aircraft feed" in _rows(report, "FAIL")["fleet"]


def test_the_fleet_s_box_with_no_real_node_says_the_public_map_is_empty():
    report, _, _ = _verify(_fleet_site(**{NODES: _nodes(real=()), FEED: _feed(0)}), fleet=True)
    assert "shows real nodes only" in _rows(report, "INFO")["map"]
    assert _rows(report, "FAIL") == {}


def test_a_stale_feed_fails_even_under_a_quiet_sky():
    # Every frame rewrites the feed, aircraft or none, so a quiet sky leaves it fresh.
    unpublished = _resp({"now": 0, "aircraft": []})
    report, _, _ = _verify(_site(**{FEED: unpublished}), wait_s=60)
    assert "stopped" in _rows(report, "FAIL")["aircraft feed"]


def test_no_node_connected_fails_even_on_a_real_node_environment():
    report, _, _ = _verify(_site(**{FEED: _feed(0), NODES: _nodes(real=(), synthetic=())}), wait_s=60)
    assert "no real node on the public list is connected" in _rows(report, "FAIL")["map"]


def test_synthetic_nodes_do_not_stand_in_for_real_ones_off_the_fleet_s_box():
    report, _, _ = _verify(_site(**{NODES: _nodes(real=())}), wait_s=60)
    assert "no real node on the public list is connected" in _rows(report, "FAIL")["map"]


def test_the_real_nodes_are_listed_for_comparison():
    report, _, _ = _verify(_site())
    assert _rows(report, "INFO")["real nodes"] == "3 connected: ndeaaa ndebbb ndeccc"


def test_an_empty_node_list_is_read_again():
    report, clock, _ = _verify(_site(**{NODES: [_resp({"nodes": {}}), _nodes(real=("ndeaaa",))]}))
    assert _rows(report, "INFO")["real nodes"].startswith("1 connected")
    assert clock.sleeps == 1


# Which box answered, from /api/health's flags.


def test_a_hostname_reaching_a_box_with_the_other_fleet_setting_fails():
    report, _, _ = _verify(_site(**{HEALTH: _health(fleet=True)}))
    assert "synthetic_fleet=True" in _rows(report, "FAIL")["box"]


def test_prod_answering_for_staging_fails():
    report, _, _ = _verify(_site(**{HEALTH: _health(polling=True)}), retina_env="test", polling=False)
    assert "polled_radar_polling=True where the overlay leaves off" in _rows(report, "FAIL")["box"]
    report, _, _ = _verify(_site(**{HEALTH: _health(polling=True)}), retina_env="production", polling=True)
    assert "polled_radar_polling=True" in _rows(report, "PASS")["box"]


def test_a_health_that_does_not_say_which_box_warns():
    report, _, _ = _verify(_site(**{HEALTH: _resp({"status": "ok"})}))
    assert "unproven" in _rows(report, "WARN")["box"]


# The keyed checks.


def test_without_the_key_the_keyed_checks_are_skipped_and_not_asked_for():
    report, _, asked = _verify(_site())
    assert vd.KEY_VARIABLE in _rows(report, "SKIP")["keyed checks"]
    assert f"https://{DASHBOARD}" not in asked
    assert "environment" not in _rows(report, "PASS")


def test_the_key_goes_to_the_dashboard_alone_and_in_a_header():
    report, _, asked = _verify(_site(), key=KEY)
    assert asked.headers[f"https://{DASHBOARD}"] == {"X-API-Key": KEY}
    assert all(not headers for url, headers in asked.headers.items() if url != f"https://{DASHBOARD}")
    assert not any(KEY in url for url in asked)
    assert "RETINA_ENV=test" in _rows(report, "PASS")["environment"]
    assert "keyed checks" not in _rows(report, "SKIP")


def test_with_the_key_a_hostname_reaching_the_wrong_box_fails():
    report, _, _ = _verify(_site(), retina_env="production", key=KEY)
    assert "RETINA_ENV='test'" in _rows(report, "FAIL")["environment"]


def test_with_the_key_stale_tasks_and_waiting_subsystems_warn():
    dash = _dashboard(stale=["solver"], subsystems={"tcp_server": "ok", "mqtt": "starting"})
    report, _, _ = _verify(_site(**{DASHBOARD: _resp(dash)}), key=KEY)
    assert "solver" in _rows(report, "WARN")["tasks"]
    assert _rows(report, "WARN")["subsystems"] == "mqtt=starting"
    assert _rows(report, "FAIL") == {}


def test_a_key_the_environment_refuses_fails_the_keyed_checks():
    refused = _resp({"detail": "Not authenticated"}, status=401, marked=False)
    report, _, _ = _verify(_site(**{DASHBOARD: refused}), key=KEY)
    assert "401 or 403 means the key is not this environment's" in _rows(report, "FAIL")["keyed checks"]


@pytest.mark.parametrize(
    ("key", "echo"),
    [
        (KEY, "x" * 190 + KEY + "y" * 40),  # straddles the 200-character cut
        ("rk'quoted\\key", "key rk'quoted\\key is not valid here"),  # repr would escape it
    ],
    ids=["cut", "escaped"],
)
def test_a_key_echoed_back_is_redacted_before_the_body_is_quoted(key, echo):
    refused = _resp(echo, status=403, ctype="text/plain", marked=False)
    report, _, _ = _verify(_site(**{DASHBOARD: refused}), key=key)
    shown = report.render()
    assert "keyed checks" in _rows(report, "FAIL")
    assert "[radar key" in shown
    assert not any(key[i : i + 6] in shown for i in range(len(key) - 5))


def test_the_report_redacts_the_key_whatever_brings_it():
    # A second line of defence, for a detail that reaches the report other than through an answer's body.
    report = vd.Report(secret=KEY)
    report.add("FAIL", "keyed checks", f"https://{DASHBOARD}: {KEY!r} refused")
    assert KEY not in report.render()
    assert "[radar key]" in report.render()


def _opener(site, asked):
    """verify-deploy's _open, answering from `site` by path, as fetch() would see it."""
    by_path = {urllib.parse.urlsplit(url).path: answer for url, answer in site.items()}

    class Answer:
        def __init__(self, resp):
            self.status, self.headers, self._body = resp.status, resp.headers, resp.body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _open(request, *, follow):
        asked.append((request.full_url, dict(request.header_items()), follow))
        resp = _serve(by_path, urllib.parse.urlsplit(request.full_url).path)
        if resp.status != 200:
            raise urllib.error.HTTPError(request.full_url, resp.status, "", resp.headers, io.BytesIO(resp.body))
        return Answer(resp)

    return _open


def _main(monkeypatch, capsys, site, key=None):
    asked = []
    monkeypatch.setattr(vd, "_open", _opener(site, asked))
    if key is None:
        monkeypatch.delenv(vd.KEY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(vd.KEY_VARIABLE, key)
    status = vd.main(["test", "--wait", "0"])
    out = capsys.readouterr()
    return status, out.out, out.err, asked


@pytest.mark.parametrize(
    ("dashboard", "status"),
    [
        (_resp(_dashboard()), 0),
        # A server that echoed the key back in an error.
        (_resp(f"key {KEY} is not valid here", status=403, ctype="text/plain", marked=False), 1),
    ],
)
def test_the_key_never_appears_in_any_output(monkeypatch, capsys, dashboard, status):
    code, out, err, asked = _main(monkeypatch, capsys, _fleet_site(**{DASHBOARD: dashboard}), key=KEY)
    assert code == status
    assert KEY not in out + err
    sent = [(headers, follow) for url, headers, follow in asked if url.endswith("/api/test/dashboard")]
    assert sent and all(headers.get("X-api-key") == KEY and not follow for headers, follow in sent)
    assert all(follow and "X-api-key" not in headers for url, headers, follow in asked if "/api/test/" not in url)
    assert not any(KEY in url for url, _, _ in asked)


def test_a_key_no_header_can_carry_is_refused_without_being_shown(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exited:
        _main(monkeypatch, capsys, _fleet_site(), key="rkNEWLINEsecret\nrest")
    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert vd.KEY_VARIABLE in err
    assert "rkNEWLINEsecret" not in err


def test_a_request_with_the_key_is_not_redirected(monkeypatch):
    built = []
    monkeypatch.setattr(vd.urllib.request, "build_opener", lambda *handlers: built.append(handlers) or None)
    with pytest.raises(AttributeError):
        vd._open(vd.urllib.request.Request("https://api.example.test/"), follow=False)
    assert any(isinstance(h, vd._Stay) for h in built[-1])
    with pytest.raises(AttributeError):
        vd._open(vd.urllib.request.Request("https://api.example.test/"), follow=True)
    assert not any(isinstance(h, vd._Stay) for h in built[-1])
    assert vd._Stay().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example.test/") is None


def test_keyed_checks_skipped_are_said_so_and_not_counted_as_passed(monkeypatch, capsys):
    code, out, _, asked = _main(monkeypatch, capsys, _fleet_site())
    assert code == 0
    assert re.search(r"^  SKIP +keyed checks", out, re.MULTILINE)
    assert out.rstrip().splitlines()[-1].startswith("PASS with keyed checks skipped, not passed.")
    assert not any(url.endswith("/api/test/dashboard") for url, _, _ in asked)


def test_the_spa_fallback_answering_the_bundle_fails():
    fallback = _resp(INDEX_HTML, ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{f"{APP}/assets/index-abc.js": fallback}))
    assert "SPA" in _rows(report, "FAIL")["bundle"]


def test_a_module_script_whose_src_comes_first_is_found():
    page = INDEX_HTML.replace(
        'type="module" crossorigin src="/assets/index-abc.js"', 'src="/assets/index-abc.js" type="module"'
    )
    report, _, _ = _verify(_site(**{f"{APP}/": _resp(page, ctype="text/html", marked=False)}))
    assert "index-abc.js" in _rows(report, "PASS")["bundle"]


def test_the_bundle_is_named_by_the_module_script_and_other_origins_are_not_read():
    report, _, asked = _verify(_site())
    assert "index-abc.js" in _rows(report, "PASS")["bundle"]
    assert f"https://{APP}/theme-boot.js" in asked
    assert not any("unpkg.example.test" in url for url in asked)
    assert "https://unpkg.example.test/leaflet.css" in _rows(report, "INFO")["other hosts"]


def test_an_expected_string_is_found_in_a_lazy_chunk():
    report, _, _ = _verify(_site(), expect=["Shipped today"])
    assert "Lazy-def.js" in _rows(report, "PASS")["expect"]


def test_every_asset_the_page_reaches_is_checked_without_being_asked():
    report, _, asked = _verify(_site())
    assert {f"https://{APP}/assets/Lazy-def.js", f"https://{APP}/assets/index-xyz.css"} <= set(asked)
    assert "5 other asset(s)" in _rows(report, "PASS")["bundle"]


def test_a_missing_expected_string_fails_after_searching_every_chunk():
    report, _, asked = _verify(_site(), expect=["never shipped"])
    assert "7 file(s)" in _rows(report, "FAIL")["expect"]
    assert {f"https://{APP}/assets/Lazy-def.js", f"https://{APP}/assets/Other-ghi.js"} <= set(asked)


def test_an_unreachable_host_fails_rather_than_raising():
    report, _, _ = _verify(_site(**{HEALTH: vd.Unreachable("api: timed out")}))
    assert "timed out" in _rows(report, "FAIL")["health"]


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ("prod", ("api.retina.fm", "app.retina.fm", "production", False, True)),
        ("staging", ("staging-api.retina.fm", "staging-app.retina.fm", "test", False, False)),
        ("test", ("test-api.retina.fm", "test-app.retina.fm", "test", True, False)),
    ],
)
def test_each_environment_is_read_from_its_overlay(env, expected):
    assert vd.environment(env) == vd.Environment(*expected)


def test_the_marker_is_the_one_the_smoke_suites_assert():
    assert vd.origin_marker() == MARKER


def test_an_unknown_environment_is_a_usage_error():
    with pytest.raises(SystemExit) as exited:
        vd.main(["nowhere"])
    assert exited.value.code == 2


def test_a_feed_not_yet_published_after_a_restart_is_waited_for():
    unpublished = _resp({"now": 0, "aircraft": []})
    report, clock, _ = _verify(_site(**{FEED: [unpublished, _feed(1)]}), wait_s=600)
    assert _rows(report, "FAIL") == {}
    assert clock.sleeps == 1


def test_a_feed_left_over_from_before_a_restart_is_waited_for():
    report, clock, _ = _verify(_site(**{FEED: [_feed(3, age=400), _feed(3)]}), wait_s=600)
    assert _rows(report, "FAIL") == {}
    assert clock.sleeps == 1


def test_a_server_answering_5xx_while_it_restarts_is_waited_for():
    warming = _resp("bad gateway", status=502, ctype="text/html", marked=False)
    report, clock, _ = _verify(_site(**{NODES: [warming, _nodes()]}), wait_s=600)
    assert _rows(report, "FAIL") == {}
    assert clock.sleeps == 1


def test_the_fleet_s_unfiltered_feed_is_waited_for():
    report, clock, _ = _verify(_fleet_site(**{ALL: [_feed(0), _feed(2, by="synth-1")]}), wait_s=600, fleet=True)
    assert _rows(report, "FAIL") == {}
    assert clock.sleeps == 1


def test_an_error_from_the_edge_is_not_blamed_on_the_origin_rule():
    refused = _resp("error code: 1010", status=403, ctype="text/plain", marked=False)
    report, _, _ = _verify(_site(**{HEALTH: refused}))
    assert "origin" not in _rows(report, "FAIL")


def test_a_lazy_chunk_answered_by_the_spa_fallback_is_not_searched():
    fallback = _resp(INDEX_HTML + "<title>Shipped today</title>", ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{f"{APP}/assets/Lazy-def.js": fallback}), expect=["Shipped today"])
    assert "expect" in _rows(report, "FAIL")


def test_a_connection_dropped_mid_response_is_unreachable_not_a_crash(monkeypatch):
    def drop(*args, **kwargs):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(vd, "_open", drop)
    monkeypatch.setattr(vd.time, "sleep", lambda s: None)
    with pytest.raises(vd.Unreachable):
        vd.fetch("https://api.example.test/api/health")


def test_a_lazy_chunk_the_deploy_lacks_fails_the_bundle():
    missing = _resp("<html>404</html>", status=404, ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{f"{APP}/assets/Other-ghi.js": missing}))
    assert "the deploy does not carry it" in _rows(report, "FAIL")["bundle"]


def test_an_asset_answering_5xx_once_is_asked_again():
    flaky = [_resp("bad gateway", status=502, ctype="text/html", marked=False), _resp("x", ctype="text/javascript")]
    report, _, _ = _verify(_site(**{f"{APP}/assets/Other-ghi.js": flaky}))
    assert "bundle" in _rows(report, "PASS")


def test_a_string_only_a_stylesheet_carries_is_found():
    report, _, _ = _verify(_site(), expect=["retina-banner"])
    assert "index-xyz.css" in _rows(report, "PASS")["expect"]


def test_a_string_the_change_removes_must_be_gone():
    report, _, _ = _verify(_site(), absent=["Shipped today"])
    assert "still in Lazy-def.js" in _rows(report, "FAIL")["expect"]
    report, _, _ = _verify(_site(), absent=["retired wording"])
    assert "expect" in _rows(report, "PASS")


def test_a_4xx_ends_the_wait_at_once():
    gone = _resp("not found", status=404, ctype="text/html", marked=False)
    report, clock, _ = _verify(_site(**{NODES: gone}), wait_s=600)
    assert "404" in _rows(report, "FAIL")["map"]
    assert clock.sleeps == 0


def test_a_4xx_on_the_unfiltered_feed_ends_the_wait_at_once():
    gone = _resp("not found", status=404, ctype="text/html", marked=False)
    report, clock, _ = _verify(_fleet_site(**{ALL: gone}), wait_s=600, fleet=True)
    assert "404" in _rows(report, "FAIL")["fleet"]
    assert clock.sleeps == 0


def test_a_lazy_page_s_stylesheet_is_read():
    report, _, _ = _verify(_site(), expect=["map-surface"])
    assert "Lazy-def.css" in _rows(report, "PASS")["expect"]
    missing = _resp("<html>404</html>", status=404, ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{f"{APP}/assets/Lazy-def.css": missing}))
    assert "Lazy-def.css" in _rows(report, "FAIL")["bundle"]


def test_a_script_the_page_names_outside_assets_is_read():
    report, _, _ = _verify(_site(), expect=["dataset.theme"])
    assert "theme-boot.js" in _rows(report, "PASS")["expect"]


def test_one_failed_read_on_the_last_poll_after_good_ones_is_a_blip():
    blip = vd.Unreachable("api.example.test: timed out")
    health = [_health(), _health(), _health(), blip]
    report, _, _ = _verify(_site(**{FEED: _feed(0), HEALTH: health}), wait_s=60)
    assert "health" not in _rows(report, "FAIL")
    assert "quiet sky" in _rows(report, "WARN")["map"]


def test_a_blip_is_forgiven_only_when_the_poll_before_it_was_good():
    down = vd.Unreachable("api.example.test: timed out")
    health = [_health(), down, down, down]
    report, _, _ = _verify(_site(**{FEED: _feed(0), HEALTH: health}), wait_s=60)
    assert "timed out" in _rows(report, "FAIL")["health"]


def test_a_node_list_blip_on_the_last_poll_keeps_the_earlier_list():
    blip = vd.Unreachable("api.example.test: timed out")
    nodes = [_nodes(), _nodes(), _nodes(), blip]
    report, _, _ = _verify(_site(**{FEED: _feed(0), NODES: nodes}), wait_s=60)
    assert "quiet sky" in _rows(report, "WARN")["map"]
    assert _rows(report, "INFO")["real nodes"].startswith("3 connected")


def test_an_unfiltered_feed_blip_on_the_last_poll_keeps_the_earlier_feed():
    blip = vd.Unreachable("app.example.test: timed out")
    fleet = [_feed(0), _feed(0), _feed(0), blip]
    report, _, _ = _verify(_fleet_site(**{ALL: fleet}), wait_s=60, fleet=True)
    assert "was seen by the 1 connected synthetic" in _rows(report, "FAIL")["fleet"]


def test_a_rate_limit_is_waited_out():
    limited = _resp("too many requests", status=429, ctype="text/plain", marked=False)
    report, clock, _ = _verify(_site(**{NODES: [limited, _nodes()]}), wait_s=600)
    assert _rows(report, "FAIL") == {}
    assert clock.sleeps == 1


def test_the_page_is_asked_twice_on_a_5xx():
    page = [_resp("bad gateway", status=502, ctype="text/html", marked=False), _resp(INDEX_HTML, ctype="text/html")]
    report, _, _ = _verify(_site(**{f"{APP}/": page}))
    assert "bundle" in _rows(report, "PASS")


def test_a_walk_at_its_bound_still_reads_what_fits(monkeypatch):
    monkeypatch.setattr(vd, "MAX_ASSETS", 4)
    report, _, _ = _verify(_site(), expect=["Shipped today"])
    assert "Lazy-def.js" in _rows(report, "PASS")["expect"]
    assert "2 more unread" in _rows(report, "WARN")["bundle"]


def test_a_fault_waiting_cannot_clear_ends_the_wait_at_once():
    bad_cert = vd.Unreachable("api.example.test: certificate verification failed", final=True)
    report, clock, _ = _verify(_fleet_site(**{HEALTH: bad_cert, ALL: _feed(0)}), wait_s=600, fleet=True)
    assert "certificate" in _rows(report, "FAIL")["health"]
    assert clock.sleeps == 0
    # One poll was taken, so no row may claim the fleet stayed idle through a wait.
    assert "was seen by the 1 connected synthetic" in _rows(report, "FAIL")["fleet"]
    assert "of polling" not in report.render()
    assert "post-restart window" not in report.render()


def test_a_walk_cut_short_says_so_and_cannot_prove_an_absence(monkeypatch):
    monkeypatch.setattr(vd, "MAX_ASSETS", 3)
    report, _, _ = _verify(_site(), absent=["retired wording"])
    assert "unread" in _rows(report, "WARN")["bundle"]
    assert "not read" in _rows(report, "FAIL")["expect"]


def test_the_script_content_types_match_the_smoke_suite_s():
    shell = (Path(__file__).resolve().parents[2] / "deploy" / "page-asset.sh").read_text()
    accepted = set(re.findall(r"200:\*(\w+)\*", shell))
    assert accepted == set(vd.JAVASCRIPT.pattern.split("|"))


def test_the_page_itself_is_searched():
    report, _, _ = _verify(_site(), expect=['<script src="/theme-boot.js">'])
    assert "index.html" in _rows(report, "PASS")["expect"]
    report, _, _ = _verify(_site(), absent=['crossorigin href="/assets/index-xyz.css"'])
    assert "still in index.html" in _rows(report, "FAIL")["expect"]


def test_a_short_wait_is_counted_in_seconds():
    report, _, _ = _verify(_fleet_site(**{ALL: _feed(0)}), wait_s=30, fleet=True)
    assert "after 40s of polling" in _rows(report, "FAIL")["fleet"]


def test_a_carried_feed_ages_from_when_it_was_read():
    earlier = vd.Reading(feed={"aircraft": []}, feed_age=1.0, at=100.0, feed_at=165.0)
    later = vd.Reading(feed="timed out", at=185.0, feed_at=190.0)
    assert later.over(earlier).feed_age == 21.0


def test_a_stylesheet_whose_href_comes_first_is_read():
    page = INDEX_HTML.replace("</head>", '<link href="/extra.css" rel="stylesheet"></head>')
    missing = _resp("<html>404</html>", status=404, ctype="text/html", marked=False)
    report, _, _ = _verify(_site(**{f"{APP}/": _resp(page, ctype="text/html"), f"{APP}/extra.css": missing}))
    assert "extra.css" in _rows(report, "FAIL")["bundle"]


def test_a_stale_earlier_feed_is_not_carried_over_a_failed_read():
    earlier = vd.Reading(feed={"aircraft": []}, feed_age=90.0, at=100.0, feed_at=101.0)
    later = vd.Reading(feed="timed out", at=121.0, feed_at=122.0)
    assert later.over(earlier).feed == "timed out"


def test_the_feed_is_timed_by_its_own_read_not_the_slowest_one():
    clock = _Clock()
    site = _site()

    def get(url, headers=None):
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/api/radar/nodes":
            # Once the feed's read is timed (the poll's start is the first call), a slow read's worth passes.
            deadline = time.monotonic() + 5
            while clock.calls < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            clock.t += 65
        return site[f"{parts.scheme}://{parts.netloc}{parts.path}"]

    env = vd.Environment(API, APP, "test", False)
    verifier = vd.Verifier(env, get=get, clock=clock, sleep=clock.sleep, log=lambda _: None, marker=MARKER)
    assert verifier._read().feed_at == NOW
