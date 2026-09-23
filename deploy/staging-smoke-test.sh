#!/bin/bash
# ── Staging Smoke Tests ──────────────────────────────────────────────────────
# Run against the staging server to verify deployment health before
# promoting to production.
#
# Usage: bash deploy/staging-smoke-test.sh
# Exit code: 0 = all checks passed, 1 = failure
set -euo pipefail

# Served by tower-finder-service, NOT by this repo. A Cloudflare Origin Rule
# in the http_request_origin phase routes this hostname to origin port 8443
# (the tower-finder-edge container); retina-server's nginx listens on 443 and
# never sees the request. Only the tower contract may be asserted against it.
# retina-server's own API goes to API_URL.
BASE_URL="https://staging-towers.retina.fm"
API_URL="https://staging-api.retina.fm"
# The admin bundle's own vhost. It and APP_URL serve the same dashboard build; the hostname is what selects the admin route table
# (dashboard/src/utils/surface.ts), so this is the only name here that renders
# the admin console.
ADMIN_URL="https://staging-admin.retina.fm"
# The public surface: the console at /, opening on its map. `staging-map`,
# `staging-dash`, `staging-data` and the public `testmap` are Cloudflare
# redirects onto it and reach no origin, so only their redirects are probed.
APP_URL="https://staging-app.retina.fm"
# TOWER_CONTRACT_QUERY / TOWER_CONTRACT_ECHO: what a backend must echo back.
# shellcheck source=deploy/tower-contract.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tower-contract.sh"
CURL="curl -s --connect-timeout 10 --max-time 30"
# A query string no probe has sent before, for probes whose answer the edge may
# already hold. The query string is part of the cache key, so a fresh one is a
# guaranteed miss, and nginx matches its locations on the path alone.
BUST="smoke=$(date +%s)$RANDOM"
# PASS/FAIL/WARN and smoke_summary, shared with the production suite in ci.yml.
# shellcheck source=deploy/smoke-tally.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-tally.sh"
# assert_origin_marker: proves THIS repo's nginx answered, not merely that the
# hostname did. Shared with the production suite for the same reason as above.
# shellcheck source=deploy/origin-marker.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/origin-marker.sh"

# assert_page_asset: proves a mounted bundle resolves the assets it names, which
# no status check can. Shared with CI's production smoke tests.
# shellcheck source=deploy/page-asset.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/page-asset.sh"

# assert_legacy_redirect: proves a retired hostname still reaches the surface it
# was retired into. Shared with the production suite for the same reason.
# shellcheck source=deploy/legacy-redirects.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/legacy-redirects.sh"

check() {
    local name="$1" url="$2" expected="$3"
    printf "  %-40s " "$name"
    BODY=$($CURL "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    # -F: every caller passes a literal, and an unescaped `.` in one would
    # otherwise match a character it was never meant to. -e: a needle starting
    # with a dash is read as an option otherwise, and grep then exits non-zero
    # having matched nothing, which is indistinguishable here from a body that
    # genuinely lacks it.
    if echo "$BODY" | grep -qF -e "$expected"; then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL (expected '$expected')"
        echo "    Response: $(echo "$BODY" | head -c 200)"
        FAIL=$((FAIL+1))
    fi
}

check_status() {
    local name="$1" url="$2" expected_code="$3"
    printf "  %-40s " "$name"
    CODE=$($CURL -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if [ "$CODE" = "$expected_code" ]; then
        echo "OK ($CODE)"
        PASS=$((PASS+1))
    else
        echo "FAIL (got $CODE, expected $expected_code)"
        FAIL=$((FAIL+1))
    fi
}


# The seam's assertion lives in tower-contract.sh so the gate and this suite
# cannot drift; this only adapts it to the PASS/FAIL tally.
check_contract() {
    local name="$1" endpoint="$2" reason rc
    printf "  %-40s " "$name"
    if reason=$(assert_tower_contract "$endpoint"); then
        rc=0
    else
        rc=$?
    fi
    if [ "$rc" = 0 ]; then
        echo "OK"
        PASS=$((PASS+1))
    elif [ "$rc" = 2 ]; then
        # Forwarded, and tower-finder-service did not answer. Warned for the same
        # reason the production suite warns: a red staging smoke skips the
        # production deploy, so the service being down would hold this repo's
        # releases behind an outage it cannot fix. The routing this covers is
        # proven by the 404 case, which stays fatal.
        echo "WARN"
        printf '    %s\n' "$reason"
        WARN=$((WARN+1))
    else
        echo "FAIL"
        printf '    %s\n' "$reason"
        FAIL=$((FAIL+1))
    fi
}

# Vhosts that render, and so are covered by the parity check, but have no DNS
# record on this environment. Their absence is the expected state, so a probe is
# skipped and asserts in full the moment a record appears.
#
# Empty today, and a name belongs here only while its record is genuinely
# unplanned: an entry turns a deleted record into a silent skip rather than a
# failure, which is the opposite of what every probe below is for.
NO_DNS_EXPECTED=""

# Vhosts whose record exists and is expected to, but whose absence must not
# fail the run: this runs inside the `staging` job that deploy-production needs
# (ci.yml calls staging-deploy-verify.yml), so a hard failure here would let a
# Cloudflare wobble block every release. Reported as WARN and tallied
# separately, because a deleted record must still be visible: skipping it
# silently would retire the only check on a vhost nothing else monitors.
DNS_NOT_DEPLOY_BLOCKING="staging-admin.retina.fm"

# Decides what to do about $1 not resolving, prints it, and returns 0 when the
# caller should skip its probe. Membership is tested before the lookup, which is
# only interesting for a host that is exempt. Both lists are narrow on purpose:
# an unresolvable name on any other vhost falls through and fails the run.
handle_unresolvable() {
    local host="$1" label="$2"
    case " ${NO_DNS_EXPECTED} ${DNS_NOT_DEPLOY_BLOCKING} " in
        *" ${host} "*) ;;
        *) return 1 ;;
    esac
    getent hosts "$host" >/dev/null 2>&1 && return 1
    case " ${NO_DNS_EXPECTED} " in *" ${host} "*)
        printf "  %-40s SKIP (no DNS record on this environment)\n" "$label"
        return 0 ;;
    esac
    printf "  %-40s WARN (record missing; not blocking the deploy)\n" "$label"
    # Only Actions reads this prefix; anywhere else it is noise in the output.
    if [ -n "${GITHUB_ACTIONS:-}" ]; then
        echo "::warning::${host} no longer resolves, so its vhost went untested. The record is expected to exist; restore it."
    fi
    WARN=$((WARN+1))
    return 0
}

check_header() {
    local name="$1" url="$2" header="$3"
    printf "  %-40s " "$name"
    HEADERS=$($CURL -o /dev/null -D - "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if echo "$HEADERS" | tr 'A-Z' 'a-z' | grep -q "^${header}:"; then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL (no ${header} header)"
        FAIL=$((FAIL+1))
    fi
}

# As check_header, but the header must also carry the given value.
check_header_value() {
    local name="$1" url="$2" header="$3" value="$4"
    printf "  %-40s " "$name"
    HEADERS=$($CURL -o /dev/null -D - "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if echo "$HEADERS" | tr 'A-Z' 'a-z' | grep "^${header}:" | grep -qF -e "$value"; then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL (${header} does not say ${value})"
        FAIL=$((FAIL+1))
    fi
}

# assert_page_asset in this suite's reporting. Shared with CI's production
# smoke tests so the two cannot drift, as with assert_origin_marker below.
check_page_asset() {
    local name="$1" page="$2" out
    printf "  %-40s " "$name"
    if out=$(assert_page_asset "$page"); then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL"
        printf '    %s\n' "$out"
        FAIL=$((FAIL+1))
    fi
}

# assert_legacy_redirect in this suite's reporting. WARN rather than FAIL: the
# rule is Cloudflare's, so no deploy can have broken it and no rollback can fix
# it, and a red staging smoke skips deploy-production.
check_legacy_redirect() {
    local host="$1" path="$2" target="$3" out
    printf "  %-40s " "$host"
    if out=$(assert_legacy_redirect "$host" "$path" "$target"); then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "WARN"
        printf '    %s\n' "$out"
        WARN=$((WARN+1))
        if [ -n "${GITHUB_ACTIONS:-}" ]; then
            echo "::warning::${host} no longer redirects to ${target}. It is a retired hostname with no vhost, so it is now refused at the origin. Restore the Cloudflare redirect rule."
        fi
    fi
}

# assert_origin_marker in this suite's reporting. The diagnosis it prints names
# the likely cause, so it is echoed rather than reduced to FAIL.
check_origin() {
    local name="$1" url="$2" out
    printf "  %-40s " "$name"
    if out=$(assert_origin_marker "$url"); then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL"
        printf '    %s\n' "$out"
        FAIL=$((FAIL+1))
    fi
}

check_rate_limit() {
    local name="$1" url="$2" tries="$3" method="${4:-GET}"
    printf "  %-40s " "$name"
    # The method has to be one the endpoint answers, or the burst measures
    # nothing: limit_req does fire ahead of the proxy, but a run of 405s would
    # keep passing after the route itself had gone. A POST goes out with no
    # body, so the ones that get through are refused by request validation
    # before the handler can act on them.
    # A burst of requests must start getting 429s. Anything else means the
    # location is missing (the state staging was in before the nginx template
    # was shared with production) or that limit_req is keyed on something that
    # does not vary per client — behind Cloudflare, an unset `real_ip_header`
    # buckets per CF edge rather than per user and the limit never fires.
    #
    # The requests go out CONCURRENTLY, and must: a rate limit is only
    # observable while requests arrive faster than the zone refills, and
    # sequential curls cannot manage that against a per-second zone. At ~150 ms
    # per round trip from CI a serial loop sends ~7 r/s into `session`'s 5 r/s
    # refill, so draining its 21-token bucket would take ~79 requests — and at
    # >=200 ms latency it never drains at all, which is exactly how this check
    # failed against a correctly configured staging (run 31436629459: 30 serial
    # requests, all 200; 30 concurrent against the same host, 22/8).
    #
    # `tries` MUST exceed the zone's burst: `burst=N nodelay` admits N+1
    # requests before rejecting one, so a run of exactly N reports a false
    # failure.
    # `|| true` is load-bearing: xargs exits 123 if ANY child fails, and this
    # file runs under `set -euo pipefail`, so a bare assignment would abort the
    # whole script at this line — no FAIL line, no summary, and every later
    # check silently skipped. One flaky connection inside a 30-way concurrent
    # burst is precisely what this check provokes (ephemeral-port and TLS
    # handshake pressure on the runner), so tolerate partial failure and judge
    # on the codes that did come back, as the old serial loop's `|| continue`
    # did.
    local codes summary
    codes=$(seq 1 "$tries" | xargs -P "$tries" -I{} $CURL -X "$method" -o /dev/null -w '%{http_code}\n' "$url" 2>/dev/null || true)

    if printf '%s\n' "$codes" | grep -q '^429$'; then
        echo "OK (429 after burst)"
        PASS=$((PASS+1))
    else
        # Report the whole distribution: "all 200" means the limit never fired,
        # "all 000" means nothing was reachable, and a short count means the
        # burst partly failed — three different diagnoses that a single
        # last-code sample cannot tell apart.
        summary="${codes:+$(printf '%s\n' "$codes" | sort | uniq -c | awk '{printf "%s×%s ", $1, $2}')}"
        echo "FAIL (no 429 in $tries concurrent; got ${summary:-no responses})"
        FAIL=$((FAIL+1))
    fi
}

echo "═══════════════════════════════════════════════════"
echo "  Staging Smoke Tests"
echo "  towers:   ${BASE_URL} (tower-finder-service)"
echo "  api:      ${API_URL}"
echo "  app:      ${APP_URL}"
echo "═══════════════════════════════════════════════════"

echo ""
echo "── Health & API endpoints (staging-api.retina.fm) ──"
# On API_URL rather than the towers hostname. These are retina-server's own
# routes, and the towers name stopped reaching retina-server on 2026-09-14 when
# the Origin Rule above was created — nine checks here failed against a service
# that was never meant to answer them, and a red staging smoke skips
# deploy-production, so every merge sat undeployed until this moved.
check_status "GET /api/health"              "${API_URL}/api/health"         "200"
check_status "GET /api/radar/nodes"         "${API_URL}/api/radar/nodes"    "200"
check_status "GET /api/radar/analytics"     "${API_URL}/api/radar/analytics" "200"
check_status "GET /api/test/dashboard"      "${API_URL}/api/test/dashboard" "200"
check_status "GET /api/test/mlat-verification" "${API_URL}/api/test/mlat-verification" "200"
# Deliberately no /api/config check on this vhost: the api vhost has no
# /api/config location, so the request falls through `location /` to the app,
# which no longer implements the route (the monolith's tower stack went with the
# proxy dedup). A 404 there is by design; the route is asserted on the tower
# vhosts, where it is served.

# Nothing else is asserted against BASE_URL here. Every remaining probe of that
# hostname would be a hard assertion on a service this repo neither builds nor
# deploys, and a red staging smoke skips deploy-production — so a tower-finder
# outage would block an unrelated retina-server release. The seam loop below
# still probes it once, because test_towers_vhost_coverage.py requires every
# routed vhost to appear there; that is the whole of the coupling, deliberately.
# /api/config is asserted on APP_URL, where the proxy doing it is ours.

echo ""
echo "── Public app surface (staging-app.retina.fm) ──"
# The console at the root. `/` forwards to /map inside the router, so nginx
# answers both with the same index.html.
check_status "app GET /"                    "${APP_URL}/"                   "200"
check        "HTML has app root"            "${APP_URL}/"                   "id=\"root\""
check_status "app GET /map"                 "${APP_URL}/map"                "200"
# A deep link the SPA owns and nginx does not: proves the try_files fallback
# reaches the bundle's index.html.
check_status "app deep link"                "${APP_URL}/nodes"              "200"
# The console's own page; an exact-match location there would bounce it.
check_status "app GET /data"                "${APP_URL}/data"               "200"
# The bundle resolves its own assets. A page can pass every status check above
# while rendering nothing, which is what these are here for. Two segments deep
# as well as one: a relative base resolves `./assets/...` against the document
# path, and only a deeper page tells a rooted base from a relative one.
check_page_asset    "app / loads its bundle"       "${APP_URL}/"
check_page_asset    "app deep link loads it too"   "${APP_URL}/nodes/ret-smoke"

echo ""
echo "── Retired hostnames reach the app surface ──"
# No vhost claims these; Cloudflare redirects them onto the pages above, and
# the origin would refuse them with a 421. `testmap` is the one people outside
# the project have, from when staging's map was the simulator demo; it lands on
# the real network now, like every other name here.
check_legacy_redirect "staging-map.retina.fm"  /               "${APP_URL}/map"
check_legacy_redirect "testmap.retina.fm"      /               "${APP_URL}/map"
check_legacy_redirect "staging-dash.retina.fm" /smoke-redirect "${APP_URL}/smoke-redirect"
check_legacy_redirect "staging-data.retina.fm" /               "${APP_URL}/data"

echo ""
echo "── Shared nginx config (must match production) ──"
# These used to exist only in production's hand-maintained nginx.conf, so a
# change that broke either of them reached prod untested. Both environments now
# render from deploy/nginx/nginx.conf.template — assert staging really serves
# them, so the shared config is exercised and not merely present in the repo.
#
# Deliberately probed on /api/ rather than on `/`: nginx drops inherited
# add_header directives in any location that declares its own, and the SPA's
# `location = /index.html` sets Cache-Control — so the HTML document itself
# carries none of these headers. That is long-standing production behaviour,
# preserved as-is by the template refactor and tracked separately; asserting it
# here on `/` would just fail.
check_header "CSP on app vhost"             "${APP_URL}/api/health" "content-security-policy"
check_header "HSTS on api subdomain"        "${API_URL}/api/health"  "strict-transport-security"

echo ""
echo "── Which origin answered ──"
# The headers above prove a policy is served; they do not prove WE served it.
# tower-finder-service returns the same CSP, HSTS and X-Frame-Options on
# /api/health, so every check in the block above passes identically against
# either origin. These assert the marker only this repo's nginx sets, so a
# hostname quietly repointed by a Cloudflare Origin Rule fails here instead of
# passing for weeks.
#
# Two vhosts are deliberately absent. BASE_URL is the other service, so it must
# never carry this marker. ADMIN_URL sits behind Cloudflare Access, which
# answers a browserless request with a 302 from the edge carrying no origin
# headers at all — adding it would report a missing include that is present.
# tower-contract.sh reaches that vhost with service-token headers; this does not.
check_origin        "api vhost is this origin"   "${API_URL}/api/health"
# /api/health, not a page path: nginx drops every inherited add_header in a
# location that declares one of its own, and spa.conf's page locations declare
# a Cache-Control. The marker reaches this vhost's API responses only.
check_origin        "app vhost is this origin"   "${APP_URL}/api/health"
# Edge caching follows what nginx says, and Cloudflare keeps a `public,
# immutable` response for the whole `expires` window, so that policy is safe
# only on a name that carries a content hash (Vite's /assets/). A file whose
# name survives a deploy must say `no-store` instead, or the edge serves last
# week's copy under the new index.html, which every other check here still
# reads as a healthy 200. `no-store` specifically: on `no-cache` the edge
# revalidates but rewrites the browser-facing header to its own 4 h TTL.
#
# Probed with BUST: the header under test is nginx's, and a copy the edge
# already holds answers with the headers it was stored with.
check_header_value "theme-boot.js is not cached"        "${APP_URL}/theme-boot.js?${BUST}" "cache-control" "no-store"
MAP_ASSET=$($CURL "${APP_URL}/" 2>/dev/null | grep -o '/assets/index-[^"]*\.js' | head -n1 || true)
if [ -n "$MAP_ASSET" ]; then
    check_header_value "hashed /assets/ file is immutable" "${APP_URL}${MAP_ASSET}?${BUST}" "cache-control" "immutable"
else
    printf "  %-40s FAIL (no /assets/index-*.js referenced by the page)\n" "hashed /assets/ file is immutable"
    FAIL=$((FAIL+1))
fi
# Two zones, two checks. The credential surface carries the tight limit that
# actually resists brute force; the session reads a page load spends on every
# visit carry a looser one. Testing only /api/auth/me would leave the
# credential limit — the one that matters — unasserted.
# On API_URL: the limit_req zones live in the auth and session
# snippets, which the api vhost includes and which no longer sit on any
# hostname the edge routes past us.
check_rate_limit "credential endpoints rate limited" "${API_URL}/api/auth/magic-link" 10 POST
check_rate_limit "session endpoints rate limited"    "${API_URL}/api/auth/me"            30

echo ""
echo "── tower-finder-service seam ──"
# EVERY vhost that routes to the service, not a sample: the defect this guards
# against is one vhost silently missing the proxy, which a sample cannot see.
# test_towers_vhost_coverage.py asserts this list matches the template.
for endpoint in "${BASE_URL}/api/towers" "${API_URL}/towers" \
                "${ADMIN_URL}/api/towers" "${APP_URL}/api/towers"; do
    host="${endpoint#https://}"; host="${host%%/*}"
    if handle_unresolvable "$host" "$host"; then continue; fi
    check_contract "${endpoint#https://}" "$endpoint"
done
# The other half of the seam: a sibling /api/ path on the same vhost is still
# served by the app. /api/radar/nodes has no counterpart on the service, so a
# 200 here can only have come from the monolith — the proxy must take the four
# tower routes and nothing else.
check_status  "sibling /api/ path stays on the app" "${APP_URL}/api/radar/nodes"             "200"

# The other two deduplicated routes, on a vhost that used to answer them from
# the monolith. Probed for the seam, not the payload: tower-contract.sh owns the
# only assertion about what the service must return, and /api/towers above is
# where it is made.
#
# `elevation_m` was the shared key of both implementations back when there were
# two; the monolith's copy is deleted, so a 200 here can only be the service.
#
# Via the shared helper rather than `check`, so this environment tolerates the
# service's upstream provider being unavailable on the same terms production
# does. Staging answered 200 on 2026-08-27 only because its droplet holds a
# separate quota from production's; nothing here is immune to the same outage.
printf "  %-40s " "app /api/elevation answers"
if REASON=$(assert_elevation_contract "${APP_URL}/api/elevation"); then
    EL_RC=0
else
    EL_RC=$?
fi
if [ "$EL_RC" = 0 ]; then
    echo "OK"; PASS=$((PASS+1))
elif [ "$EL_RC" = 2 ]; then
    echo "WARN"; printf '    %s\n' "$REASON"; WARN=$((WARN+1))
else
    echo "FAIL"; printf '    %s\n' "$REASON"; FAIL=$((FAIL+1))
fi
# Through the shared helper, not check_status, for the same reason as the two
# above: a gateway status here is the service not answering, and failing on it
# would skip deploy-production and hold a healthy release behind an outage this
# repo cannot fix.
printf "  %-40s " "app /api/config answers"
if REASON=$(assert_config_contract "${APP_URL}/api/config"); then
    CFG_RC=0
else
    CFG_RC=$?
fi
if [ "$CFG_RC" = 0 ]; then
    echo "OK"; PASS=$((PASS+1))
elif [ "$CFG_RC" = 2 ]; then
    echo "WARN"; printf '    %s\n' "$REASON"; WARN=$((WARN+1))
else
    echo "FAIL"; printf '    %s\n' "$REASON"; FAIL=$((FAIL+1))
fi
# PUT is the half that genuinely changed hands: the monolith gated it on an
# admin session, the service gates it on a bearer token, and only the service's
# handler is left. An unauthenticated PUT must still be refused. 401 or 403 both
# pass: the point is that no path here is open, not which layer says no.
printf "  %-40s " "unauthenticated PUT /api/config denied"
PUT_CODE=$($CURL -o /dev/null -w "%{http_code}" -X PUT -H 'Content-Type: application/json' \
    -d '{}' "${APP_URL}/api/config" 2>/dev/null) || PUT_CODE="000"
if [ "$PUT_CODE" = "401" ] || [ "$PUT_CODE" = "403" ]; then
    echo "OK ($PUT_CODE)"; PASS=$((PASS+1))
else
    echo "FAIL ($PUT_CODE — expected 401 or 403; an open config write is a takeover)"; FAIL=$((FAIL+1))
fi
# The fourth route the include forwards. Same vhost as the two above, for the
# same reason; the probe itself is in tower-contract.sh and never reaches a
# geocoder upstream, so unlike elevation there is no degraded state to tolerate.
printf "  %-40s " "app /api/geocode answers"
if REASON=$(assert_geocode_contract "${APP_URL}/api/geocode"); then
    echo "OK"; PASS=$((PASS+1))
else
    echo "FAIL"; printf '    %s
' "$REASON"; FAIL=$((FAIL+1))
fi

echo ""
echo "── Detection archive (app /data) ──"
# The Data Explorer reads this endpoint. It returns an empty list for the first
# hour after a deploy (ARCHIVE_FLUSH_INTERVAL_S), so assert the endpoint answers
# rather than that it has rows — the volume that makes those rows survive a
# rebuild is asserted by deploy/check-env-parity.sh instead.
check_status "GET /api/data/archive"        "${APP_URL}/api/data/archive?limit=1" "200"

echo ""
echo "── No simulator here ──"
# Staging runs no synthetic fleet: production does not, and staging is its
# rehearsal (docker-compose.staging.yml keeps the fleet behind an unenabled
# `sim` profile and leaves SYNTHETIC_FLEET_ENABLED unset). Two probes, because
# the flag and the mount are set in different places and could disagree:
# /api/health reports the flag (the console reads it off /api/auth/me), and the
# ingest route main.py gates on it must be absent — 404, not the 405 a mounted POST route
# answers a GET with. Only the test droplet may answer these the other way.
check "server reports no synthetic fleet" "${API_URL}/api/health?${BUST}" '"synthetic_fleet":false'
check_status "sim ingest route is not mounted" "${API_URL}/api/sim/adsb/push?${BUST}" "404"

echo ""
echo "═══════════════════════════════════════════════════"
smoke_summary
echo "═══════════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    echo "STAGING SMOKE TESTS FAILED"
    exit 1
fi
echo "ALL STAGING SMOKE TESTS PASSED"
exit 0
