#!/bin/bash
# ── Staging Smoke Tests ──────────────────────────────────────────────────────
# Run against the staging server to verify deployment health before
# promoting to production.
#
# Usage: bash deploy/staging-smoke-test.sh
# Exit code: 0 = all checks passed, 1 = failure
set -euo pipefail

BASE_URL="https://staging-towers.retina.fm"
API_URL="https://staging-api.retina.fm"
DASH_URL="https://staging-dash.retina.fm"
# The admin bundle's own vhost. Both it and DASH_URL serve dashboard/dist; the
# hostname is what selects the admin route table (dashboard/src/utils/surface.ts),
# so this is the only name here that renders the admin console.
ADMIN_URL="https://staging-admin.retina.fm"
# The public data explorer (data-explorer/, static). Like the admin vhost, its
# record is recent and unmonitored, so its probes are guarded rather than
# assumed. See DNS_NOT_DEPLOY_BLOCKING.
DATA_URL="https://staging-data.retina.fm"
# Both vhosts are rooted at frontend/dist and so serve the tower finder too.
# testmap is the public demo (prod parks the name as testmap-retired).
MAP_URL="https://staging-map.retina.fm"
TESTMAP_URL="https://testmap.retina.fm"
# TOWER_CONTRACT_QUERY / TOWER_CONTRACT_ECHO: what a backend must echo back.
# shellcheck source=deploy/tower-contract.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tower-contract.sh"
CURL="curl -s --connect-timeout 10 --max-time 30"
# PASS/FAIL/WARN and smoke_summary, shared with the production suite in ci.yml.
# shellcheck source=deploy/smoke-tally.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/smoke-tally.sh"

check() {
    local name="$1" url="$2" expected="$3"
    printf "  %-40s " "$name"
    BODY=$($CURL "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    # -F: every caller passes a literal, and an unescaped `.` in one would
    # otherwise match a character it was never meant to.
    if echo "$BODY" | grep -qF "$expected"; then
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

check_json_field() {
    local name="$1" url="$2" field="$3" min_value="$4"
    printf "  %-40s " "$name"
    BODY=$($CURL "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    VALUE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)$field)" 2>/dev/null) || {
        echo "FAIL (can't parse field $field)"
        FAIL=$((FAIL+1))
        return
    }

    if [ "$VALUE" -ge "$min_value" ] 2>/dev/null; then
        echo "OK ($VALUE >= $min_value)"
        PASS=$((PASS+1))
    else
        echo "FAIL ($VALUE < $min_value)"
        FAIL=$((FAIL+1))
    fi
}

# The seam's assertion lives in tower-contract.sh so the gate and this suite
# cannot drift; this only adapts it to the PASS/FAIL tally.
check_contract() {
    local name="$1" endpoint="$2" reason
    printf "  %-40s " "$name"
    if reason=$(assert_tower_contract "$endpoint"); then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL"
        printf '    %s\n' "$reason"
        FAIL=$((FAIL+1))
    fi
}

# Vhosts that render, and so are covered by the parity check, but have no DNS
# record on this environment. Their absence is the expected state, so a probe is
# skipped and asserts in full the moment a record appears. Empty today: every
# rendered vhost resolves.
NO_DNS_EXPECTED=""

# Vhosts whose record exists and is expected to, but whose absence must not
# fail the run: staging-smoke-tests is a `needs:` of deploy-production
# (ci.yml), so a hard failure here would let a Cloudflare wobble block every
# release. Reported as WARN and tallied separately, because a deleted record
# must still be visible: skipping it silently would retire the only check on a
# vhost nothing else monitors.
DNS_NOT_DEPLOY_BLOCKING="staging-admin.retina.fm staging-data.retina.fm"

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

# The _if_dns wrappers: as check_status/check_header, but tolerating a name on
# one of the lists above rather than failing on it.
check_status_if_dns() {
    local name="$1" url="$2" expected_code="$3" host
    host="${url#https://}"; host="${host%%/*}"
    if handle_unresolvable "$host" "$name"; then return; fi
    check_status "$name" "$url" "$expected_code"
}

check_header_if_dns() {
    local name="$1" url="$2" header="$3" host
    host="${url#https://}"; host="${host%%/*}"
    if handle_unresolvable "$host" "$name"; then return; fi
    check_header "$name" "$url" "$header"
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

check_rate_limit() {
    local name="$1" url="$2" tries="$3"
    printf "  %-40s " "$name"
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
    codes=$(seq 1 "$tries" | xargs -P "$tries" -I{} $CURL -o /dev/null -w '%{http_code}\n' "$url" 2>/dev/null || true)

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
echo "  frontend: ${BASE_URL}"
echo "  api:      ${API_URL}"
echo "  dash:     ${DASH_URL}"
echo "═══════════════════════════════════════════════════"

echo ""
echo "── Health & API endpoints (staging.retina.fm) ──"
check_status "GET /api/health"              "${BASE_URL}/api/health"        "200"
check_status "GET /api/radar/nodes"         "${BASE_URL}/api/radar/nodes"   "200"
check_status "GET /api/radar/analytics"     "${BASE_URL}/api/radar/analytics" "200"
check_status "GET /api/test/dashboard"      "${BASE_URL}/api/test/dashboard" "200"
check_status "GET /api/test/mlat-verification" "${BASE_URL}/api/test/mlat-verification" "200"
# BASE_URL is HOST_MAIN, which proxies /api/config to tower-finder-service, so
# this asserts the SERVICE's ranking config is readable through the edge. There
# is no second copy to compare it against any more: the monolith's tower stack
# was deleted with the proxy dedup.
check_status "GET /api/config (service)"    "${BASE_URL}/api/config"        "200"

echo ""
echo "── Dedicated API subdomain (staging-api.retina.fm) ──"
check_status "staging-api /api/health"      "${API_URL}/api/health"         "200"
# Deliberately no /api/config check here: the api vhost has no /api/config
# location, so the request falls through `location /` to the app, which no
# longer implements the route (the monolith's tower stack went with the proxy
# dedup). A 404 there is by design; the route is asserted on the tower vhosts,
# where it is served.

echo ""
echo "── Dashboard subdomain (staging-dash.retina.fm) ──"
check_status "staging-dash GET /"           "${DASH_URL}/"                  "200"

echo ""
echo "── Data explorer subdomain (staging-data.retina.fm) ──"
# Served from /app/data-explorer, which the Dockerfile copies straight from the
# source tree — no build stage, so a missing COPY shows up here as a 404 rather
# than as a broken bundle.
check_status_if_dns "staging-data GET /"    "${DATA_URL}/"                  "200"

echo ""
echo "── Frontend assets ──"
check_status "GET / (frontend)"             "${BASE_URL}/"                  "200"
check        "HTML has app root"            "${BASE_URL}/"                  "id=\"root\""

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
check_header "CSP on dashboard vhost"       "${DASH_URL}/api/health" "content-security-policy"
# The data explorer vendors react, react-dom, lodash, classnames and
# @edsc/timeline under data-explorer/vendor/ precisely because this policy is
# `script-src 'self'`. If the header ever stops being served on this vhost the
# vendoring silently stops being load-bearing, and a CDN script would start
# working locally and in staging while remaining blocked nowhere — assert it.
check_header_if_dns "CSP on data explorer vhost" "${DATA_URL}/api/health" "content-security-policy"
check_header "CSP on frontend vhost"        "${BASE_URL}/api/health" "content-security-policy"
check_header "HSTS on api subdomain"        "${API_URL}/api/health"  "strict-transport-security"
# Two zones, two checks. The credential surface carries the tight limit that
# actually resists brute force; the session reads a page load spends on every
# visit carry a looser one. Testing only /api/auth/me would leave the
# credential limit — the one that matters — unasserted.
check_rate_limit "credential endpoints rate limited" "${BASE_URL}/api/auth/login/google" 10
check_rate_limit "session endpoints rate limited"    "${BASE_URL}/api/auth/me"            30

echo ""
echo "── tower-finder-service seam ──"
# EVERY vhost that routes to the service, not a sample: the defect this guards
# against is one vhost silently missing the proxy, which a sample cannot see.
# test_towers_vhost_coverage.py asserts this list matches the template.
for endpoint in "${BASE_URL}/api/towers" "${MAP_URL}/api/towers" \
                "${TESTMAP_URL}/api/towers" "${API_URL}/towers" \
                "${DASH_URL}/api/towers" "${ADMIN_URL}/api/towers" \
                "${DATA_URL}/api/towers"; do
    host="${endpoint#https://}"; host="${host%%/*}"
    if handle_unresolvable "$host" "$host"; then continue; fi
    check_contract "${endpoint#https://}" "$endpoint"
done
# The other half of the seam: a sibling /api/ path on the same vhost is still
# served by the app. /api/radar/nodes has no counterpart on the service, so a
# 200 here can only have come from the monolith — the proxy must take the three
# tower routes and nothing else.
check_status  "sibling /api/ path stays on the app" "${BASE_URL}/api/radar/nodes"            "200"

# The other two deduplicated routes, on a vhost that used to answer them from
# the monolith. Probed for the seam, not the payload: tower-contract.sh owns the
# only assertion about what the service must return, and /api/towers above is
# where it is made.
#
# `elevation_m` was the shared key of both implementations back when there were
# two; the monolith's copy is deleted, so a 200 here can only be the service —
# through the proxy on a vhost (dash) that had none before the dedup.
#
# Via the shared helper rather than `check`, so this environment tolerates the
# service's upstream provider being unavailable on the same terms production
# does. Staging answered 200 on 2026-08-27 only because its droplet holds a
# separate quota from production's; nothing here is immune to the same outage.
printf "  %-40s " "dash /api/elevation answers"
if REASON=$(assert_elevation_contract "${DASH_URL}/api/elevation"); then
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
check_status "dash /api/config answers"     "${DASH_URL}/api/config"                          "200"
# PUT is the half that genuinely changed hands: the monolith gated it on an
# admin session, the service gates it on a bearer token, and only the service's
# handler is left. An unauthenticated PUT must still be refused. 401 or 403 both
# pass: the point is that no path here is open, not which layer says no.
printf "  %-40s " "unauthenticated PUT /api/config denied"
PUT_CODE=$($CURL -o /dev/null -w "%{http_code}" -X PUT -H 'Content-Type: application/json' \
    -d '{}' "${DASH_URL}/api/config" 2>/dev/null) || PUT_CODE="000"
if [ "$PUT_CODE" = "401" ] || [ "$PUT_CODE" = "403" ]; then
    echo "OK ($PUT_CODE)"; PASS=$((PASS+1))
else
    echo "FAIL ($PUT_CODE — expected 401 or 403; an open config write is a takeover)"; FAIL=$((FAIL+1))
fi

echo ""
echo "── Detection archive (dash /data) ──"
# The Data Explorer reads this endpoint. It returns an empty list for the first
# hour after a deploy (ARCHIVE_FLUSH_INTERVAL_S), so assert the endpoint answers
# rather than that it has rows — the volume that makes those rows survive a
# rebuild is asserted by deploy/check-env-parity.sh instead.
check_status "GET /api/data/archive"        "${BASE_URL}/api/data/archive?limit=1" "200"

echo ""
echo "── Synthetic fleet data (wait for fleet to connect) ──"
# The fleet takes ~30-60s to fully connect; CI waits before calling this script
check_json_field "Active nodes > 0"         "${BASE_URL}/api/test/dashboard" "['nodes']['active']" "1"

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
