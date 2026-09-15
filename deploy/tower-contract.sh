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

# Cloudflare Access service-token credentials, for the admin vhosts that sit
# behind an Access application. Without them the edge answers a browserless
# request with a 302 to its login page, which arrives here as `got HTTP 302`
# and reads like a routing fault rather than a missing credential.
#
# Empty unless the environment supplies both, so an ungated hostname and a
# developer running this by hand behave exactly as before. The token only buys
# passage through the edge: it carries no email claim, so the origin treats it
# as nobody and it cannot reach an admin route.
TOWER_CONTRACT_CF_HEADERS=()
if [ -n "${CF_ACCESS_CLIENT_ID:-}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
    TOWER_CONTRACT_CF_HEADERS=(
        -H "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}"
        -H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}"
    )
fi

# Every call site expands it as ${TOWER_CONTRACT_CF_HEADERS[@]+"${...[@]}"}:
# expanding an empty array is an unbound-variable error under `set -u` before
# bash 4.4, and staging-smoke-test.sh sources this with `set -euo pipefail`.
# The `+` guard expands to nothing at all when the array is empty.

# A gateway status means nginx HERE matched the location and forwarded, and the
# thing on the other side did not answer. That is not this repo's deploy to fail
# on: the production smoke rolls production back, and tower-finder-service being
# down is not grounds for reverting a healthy retina-server release. Its own CI
# smoke-tests these routes on every deploy, and `tower-service-contract` probes
# the service directly on every PR here.
#
# A broken forward does NOT look like this. With no `location /api/towers` the
# request falls through `location /` to the app, whose copy of the tower stack
# went with the monolith, so it answers 404 — which stays fatal, and is the
# regression these probes exist to catch.
#
# The one gateway status that can still be ours is a 502 where nginx matched but
# could not reach the service at all: the retina-edge network or the container
# alias gone. The message below names it so a human looks, and it shows up on
# every vhost at once rather than one, which is the tell.
_is_upstream_failure() {
    case "$1" in 502 | 503 | 504) return 0 ;; *) return 1 ;; esac
}

_explain_upstream_failure() {
    local label="$1" url="$2" code="$3"
    echo "${label}: ${url} was forwarded by our nginx and answered ${code}. The route is"
    echo "reachable, so this is tower-finder-service or the hop to it, not this deploy."
    echo "If every vhost shows this, check the retina-edge network and the service's"
    echo "container alias before blaming the service."
}

# assert_tower_contract <endpoint-url>
# Endpoint, not host: the api vhost publishes this as /towers, everyone else as
# /api/towers. 0: contract honoured. 2: forwarded, upstream did not answer.
# 1: everything else. Prints why on stdout.
assert_tower_contract() {
    local endpoint="$1" body code resp attempt
    # Two attempts: the endpoint depends on third-party APIs, and a blip there
    # must not read as a routing fault and block a release.
    for attempt in 1 2; do
        resp=$(curl -s ${TOWER_CONTRACT_CF_HEADERS[@]+"${TOWER_CONTRACT_CF_HEADERS[@]}"} \
            --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "${endpoint}?${TOWER_CONTRACT_QUERY}" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "unreachable after 2 attempts: ${endpoint}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        if _is_upstream_failure "$code"; then
            _explain_upstream_failure "towers" "$endpoint" "$code"
            return 2
        fi
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
# Shared body for the two shape checks. 0: shape honoured. 2: forwarded, upstream
# did not answer. 1: everything else. Prints why on stdout.
_assert_json_keys() {
    local label="$1" url="$2" body code resp attempt key
    shift 2
    # Two attempts, same reasoning as assert_tower_contract: elevation fans out
    # to a third party, and a blip there must not read as a routing fault.
    for attempt in 1 2; do
        resp=$(curl -s ${TOWER_CONTRACT_CF_HEADERS[@]+"${TOWER_CONTRACT_CF_HEADERS[@]}"} \
            --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "$url" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "${label}: unreachable after 2 attempts: ${url}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        if _is_upstream_failure "$code"; then
            _explain_upstream_failure "$label" "$url" "$code"
            return 2
        fi
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
# and the service turns a refusal from it into a 503. Its own comment beside that
# status says why, and names this check: "503 and 404 rather than one 502: a
# caller, and the post-deploy smoke, must be able to tell 'the dependency is down'
# from 'this route is broken'". The other gateway statuses are tolerated alongside
# it through _is_upstream_failure: the edge in front of the service emits its own
# 502, and a fan-out to a third party is the likeliest thing here to time out into
# a 504. None of the three is ours.
#
# On 2026-08-27 open-meteo's daily quota ran out and these checks rolled production
# back over it, on a deploy that was fine (ClickUp 86cbaxrhp). A third party's rate
# limiter must not be able to do that, so both statuses warn and pass. The 2026-08-27
# fix tolerated 502 alone, which the service had already stopped sending, so the same
# throttle took production's deploy out again on 2026-09-14 (ClickUp 123zgec2qqa).
#
# Tolerating it costs no routing coverage, though no longer for the reason it once
# did: /api/towers and /api/config now warn on a gateway status too, for the reason
# given beside _is_upstream_failure. What holds the coverage up is that a broken
# forward is a 404 and not a 5xx, on all three routes alike, and 404 stays fatal
# everywhere. Cloudflare replaces the body on 5xx anyway, so the service's own
# {"detail": ...} never arrives here and the status is all there is to judge by.
#
# 404 stays fatal, and is the regression this probe exists to catch: with no
# `location /api/elevation` the request falls through `location /` to the app,
# whose copy of the route went with the monolith's tower stack. The service now
# has a 404 of its own, for a point the provider holds no data on, so that status
# is ambiguous in principle; it is not in practice, because the coordinate this
# probes is a city the provider has data for. A coordinate chosen anywhere else
# would have to tell the two apart by the body.
assert_elevation_contract() {
    local url="${1}?${TOWER_CONTRACT_ELEVATION_QUERY}" body code resp attempt
    # Two attempts, as the siblings above: a blip must not read as a routing fault.
    for attempt in 1 2; do
        resp=$(curl -s ${TOWER_CONTRACT_CF_HEADERS[@]+"${TOWER_CONTRACT_CF_HEADERS[@]}"} \
            --connect-timeout 10 --max-time "$TOWER_CONTRACT_MAX_TIME" \
            -w '\n%{http_code}' "$url" 2>/dev/null) || {
            [ "$attempt" = 1 ] && { sleep 5; continue; }
            echo "elevation: unreachable after 2 attempts: ${url}"
            return 1
        }
        code=$(printf '%s' "$resp" | tail -n1)
        body=$(printf '%s' "$resp" | sed '$d')
        [ "$code" = "200" ] && break
        [ "$attempt" = 1 ] && { sleep 5; continue; }
        if _is_upstream_failure "$code"; then
            echo "elevation: ${url} reached the service, which answered ${code} because its own"
            echo "upstream elevation provider refused it. Not a fault in this deploy; the"
            echo "routing this checks is proven by the 404 case, which stays fatal."
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
    # Both outcomes refuse the traffic, but for different reasons, and the
    # message has to say which: an instance that is not answering has not
    # "silently dropped the parameter", and sending someone to look for that is
    # a wasted hour.
    if REASON=$(assert_tower_contract "$TARGET"); then
        TARGET_RC=0
    else
        TARGET_RC=$?
    fi
    if [ "$TARGET_RC" = 0 ]; then
        echo "OK"
    elif [ "$TARGET_RC" = 2 ]; then
        echo "FAILED"
        printf '%s\n' "$REASON"
        echo "::error::tower-finder-service is not answering, so a vhost cannot be pointed at it yet. This is the instance being unreachable, not the contract being wrong."
        RC=1
    else
        echo "FAILED"
        printf '%s\n' "$REASON"
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
        # 2 is tolerated for elevation only. For elevation it is a third party's
        # rate limiter, which has no bearing on whether a vhost may be pointed
        # here. For config it is the service itself not answering, which is
        # exactly what this gate exists to refuse: the production smoke warns on
        # that because a rollback is the wrong response, but this gate is asked
        # whether the instance is ready to receive traffic, and it is not.
        if REASON=$("assert_${check}_contract" "${BASE}/api/${check}"); then
            CHECK_RC=0
        else
            CHECK_RC=$?
        fi
        if [ "$CHECK_RC" = 0 ]; then
            echo "OK"
        elif [ "$CHECK_RC" = 2 ] && [ "$check" = elevation ]; then
            echo "DEGRADED"
            printf '%s\n' "$REASON"
        else
            echo "FAILED"
            printf '%s\n' "$REASON"
            echo "::error::tower-finder-service does not yet answer /api/${check} the way retina's callers read it. snippets/towers-proxy.conf forwards that route on every vhost, so pointing one at this instance would break it."
            RC=1
        fi
    done
    exit "$RC"
fi
