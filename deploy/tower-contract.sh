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

# Run directly (not sourced) to gate a deploy on the contract.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -uo pipefail
    TARGET="${1:-https://tower-finder.retina.fm/api/towers}"
    printf 'Asserting %s honours what our vhosts will forward... ' "$TARGET"
    if assert_tower_contract "$TARGET"; then
        echo "OK"
        exit 0
    fi
    echo "::error::tower-finder-service is not ready to receive this traffic. Routing a vhost to it now would silently drop the parameter for every caller, including the public demo on testmap.retina.fm."
    exit 1
fi
