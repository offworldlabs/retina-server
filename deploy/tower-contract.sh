#!/bin/bash
# What tower-finder-service must honour before any vhost proxies to it, and the
# single implementation of that assertion. Sourced by staging-smoke-test.sh and
# by CI's production smoke tests; run directly to gate a deploy.
#
# FastAPI drops unknown query params without erroring, so a backend missing one
# answers 200 with the wrong ranking. Every parameter that changes the result is
# therefore asserted by its echo in the response, never by the status code. Add
# the next one here; the call sites take whatever this file requires.
#
# 1234.5 because parse_user_frequencies takes anything in 0 < v < 10000 and no
# broadcast tower transmits there, so the echo cannot be some tower's own value.
TOWER_CONTRACT_QUERY="lat=33.45&lon=-112.07&frequencies=1234.5"
# Exact shape: the match is fixed-string, so key name and JSON rendering both
# count. Recorded for the porting work in tower-finder-service (ClickUp
# 86capx9mv scope item 2) rather than left to be inferred from a red pipeline.
TOWER_CONTRACT_ECHO='"user_frequencies_mhz":[1234.5]'
# One budget for every caller. A tower search fans out to the FCC and open-meteo
# and measures ~3s; this is the outage threshold, not the expected time.
TOWER_CONTRACT_MAX_TIME=45

# assert_tower_contract <endpoint-url>
# Endpoint, not host: the api vhost publishes this as /towers, everyone else as
# /api/towers. Prints why it failed on stdout; returns non-zero.
assert_tower_contract() {
    local endpoint="$1" body code resp attempt
    # Two attempts: the endpoint depends on third-party APIs, and a blip there
    # must not read as a routing fault and block a release.
    for attempt in 1 2; do
        resp=$(curl -s --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "${endpoint}?${TOWER_CONTRACT_QUERY}" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "unreachable after 2 attempts: ${endpoint}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        echo "got HTTP ${code} from ${endpoint}"
        return 1
    done

    if ! printf '%s' "$body" | grep -qF "$TOWER_CONTRACT_ECHO"; then
        # Both causes look identical from here, so name them both.
        echo "${endpoint} answered 200 without ${TOWER_CONTRACT_ECHO}. Either \`frequencies\`"
        echo "is not implemented there yet (ClickUp 86capx9mv scope item 2), or it is"
        echo "implemented under a different key or JSON shape. Read the response first:"
        printf '    %s\n' "$(printf '%s' "$body" | head -c 300)"
        return 1
    fi
    return 0
}

# ── The other two deduplicated routes ────────────────────────────────────────
# /api/towers was never the whole tower stack. Every vhost that includes
# snippets/towers-proxy.conf forwards /api/elevation and /api/config as well, so
# all three are part of what the service must honour before a vhost points at
# it. They get a shape assertion rather than a parameter echo: neither takes a
# ranking parameter, and what a caller can be broken by is the response losing a
# key it reads.
#
# The keys below were the ones BOTH implementations returned while retina still
# had its own. That copy is deleted, so they now pin the shape retina's callers
# were written against onto the only implementation left — which is what makes
# asserting them worth more, not less: nothing else in this repo can catch the
# service dropping a key those callers read.
TOWER_CONTRACT_ELEVATION_QUERY="lat=33.45&lon=-112.07"
TOWER_CONTRACT_ELEVATION_KEY='"elevation_m"'
# Top-level keys of the ranking config. retina's own copy (which shipped exactly
# these four) is gone with the monolith's tower stack; the service answers the
# same four, and these are what retina's callers read.
#
# A space-separated STRING rather than an array, and the embedded double quotes
# are data: each element is grepped with -F against the raw JSON, so `"ranking"`
# matches the key and not the word wherever else it appears. It stays a scalar
# because ci.yml base64s this variable whole to send it over SSH to the staging
# droplet, which an array cannot survive. Split at the use site instead.
# shellcheck disable=SC2089  # the quotes are the payload, not shell quoting.
TOWER_CONTRACT_CONFIG_KEYS='"ranking" "receiver" "broadcast_bands" "search"'

# _assert_json_keys <label> <url> <key>...
# Shared body for the two shape checks. Prints why it failed; returns non-zero.
_assert_json_keys() {
    local label="$1" url="$2" body code resp attempt key
    shift 2
    # Two attempts, same reasoning as assert_tower_contract: elevation fans out
    # to a third party, and a blip there must not read as a routing fault.
    for attempt in 1 2; do
        resp=$(curl -s --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "$url" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "${label}: unreachable after 2 attempts: ${url}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        echo "${label}: got HTTP ${code} from ${url}"
        return 1
    done

    for key in "$@"; do
        if ! printf '%s' "$body" | grep -qF "$key"; then
            echo "${label}: answered 200 without ${key}. Callers read that key; see"
            echo "deploy/tower-contract.sh. First 300 bytes of the response:"
            printf '    %s\n' "$(printf '%s' "$body" | head -c 300)"
            return 1
        fi
    done
    return 0
}

# assert_elevation_contract <endpoint-url>   e.g. https://host/api/elevation
# Not _assert_json_keys, because two different things make this non-200 and only
# one of them is ours. 0: shape honoured. 2: the request reached the service and
# its own upstream is unavailable. 1: everything else.
#
# Elevation is the only one of the three routes that fans out to a third party,
# and the service turns a refusal from it into a 502. On 2026-08-27 open-meteo's
# daily quota ran out and these checks rolled production back over it, on a deploy
# that was fine (ClickUp 86cbaxrhp). A third party's rate limiter must not be able
# to do that, so a 502 warns and passes.
#
# Tolerating it costs no routing coverage. A tower-finder-service that is genuinely
# down 502s /api/towers and /api/config as well, and both are asserted strictly on
# these same vhosts through the same nginx include. Cloudflare replaces the body on
# 5xx anyway, so the service's own {"detail": ...} never arrives here and the status
# is all there is to judge by.
#
# 404 stays fatal, and is the regression this probe exists to catch: with no
# `location /api/elevation` the request falls through `location /` to the app,
# whose copy of the route went with the monolith's tower stack.
assert_elevation_contract() {
    local url="${1}?${TOWER_CONTRACT_ELEVATION_QUERY}" body code resp attempt
    # Two attempts, as the siblings above: a blip must not read as a routing fault.
    for attempt in 1 2; do
        resp=$(curl -s --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "$url" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "elevation: unreachable after 2 attempts: ${url}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        if [ "$code" = "502" ]; then
            echo "elevation: ${url} reached the service, which answered 502 because its own"
            echo "upstream elevation provider refused it. Not a fault in this deploy; the"
            echo "routing this checks is proven by /api/towers and /api/config alongside."
            return 2
        fi
        echo "elevation: got HTTP ${code} from ${url}"
        return 1
    done

    if ! printf '%s' "$body" | grep -qF "$TOWER_CONTRACT_ELEVATION_KEY"; then
        echo "elevation: answered 200 without ${TOWER_CONTRACT_ELEVATION_KEY}. Callers read"
        echo "that key; see deploy/tower-contract.sh. First 300 bytes of the response:"
        printf '    %s\n' "$(printf '%s' "$body" | head -c 300)"
        return 1
    fi
    return 0
}

# assert_config_contract <endpoint-url>      e.g. https://host/api/config
# GET only. PUT is deliberately NOT asserted here: it is gated on
# `Authorization: Bearer $TOWER_FINDER_ADMIN_TOKEN`, and this file runs from CI
# and from deploy gates that hold no such token — sending a write from a smoke
# test would also mean writing the live ranking config to prove it is reachable.
# The staging smoke test asserts the weaker property that actually matters, that
# an unauthenticated PUT is refused.
assert_config_contract() {
    # shellcheck disable=SC2086,SC2090  # intentional word split: one grep -F per key.
    _assert_json_keys "config" "$1" $TOWER_CONTRACT_CONFIG_KEYS
}

# Run directly (not sourced) to gate a deploy on the contract. Takes the
# /api/towers endpoint; the sibling routes are derived from it, so a caller
# cannot check the search and forget the other two.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -uo pipefail
    TARGET="${1:-https://tower-finder.retina.fm/api/towers}"
    BASE="${TARGET%/api/towers}"
    RC=0
    printf 'Asserting %s honours what our vhosts will forward... ' "$TARGET"
    if assert_tower_contract "$TARGET"; then
        echo "OK"
    else
        echo "::error::tower-finder-service is not ready to receive this traffic. Routing a vhost to it now would silently drop the parameter for every caller, including the public demo on testmap.retina.fm."
        RC=1
    fi

    # Only meaningful when the target really is a service root. `${TARGET}`
    # unchanged means it was not a /api/towers URL (the api vhost publishes the
    # search as /towers), and the siblings cannot be derived from it.
    if [ "$BASE" = "$TARGET" ]; then
        echo "Skipping the /api/elevation and /api/config checks: ${TARGET} is not a /api/towers URL, so the sibling routes cannot be derived. Point this at the service's own /api/towers to cover them."
        exit "$RC"
    fi

    for check in elevation config; do
        printf 'Asserting %s/api/%s honours the shape our callers read... ' "$BASE" "$check"
        if REASON=$("assert_${check}_contract" "${BASE}/api/${check}"); then
            echo "OK"
        else
            echo "FAILED"
            printf '%s\n' "$REASON"
            echo "::error::tower-finder-service does not yet answer /api/${check} the way retina's callers read it. snippets/towers-proxy.conf forwards that route on every vhost, so pointing one at this instance would break it."
            RC=1
        fi
    done
    exit "$RC"
fi
