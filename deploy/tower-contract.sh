#!/bin/bash
# What tower-finder-service must honour before any vhost proxies /api/towers to
# it. Sourced by deploy/staging-smoke-test.sh and run directly by CI's
# tower-service-contract job.
#
# FastAPI drops unknown query params without erroring, so a backend missing one
# answers 200 with the wrong ranking. Every parameter that changes the result
# therefore has to be asserted by its echo in the response, not by the status
# code. Add the next one here, not at the call sites.
#
# 1234.5 because parse_user_frequencies takes anything in 0 < v < 10000 and no
# broadcast tower transmits there, so the echo cannot be some tower's own value.
TOWER_CONTRACT_QUERY="lat=33.45&lon=-112.07&frequencies=1234.5"
TOWER_CONTRACT_ECHO='"user_frequencies_mhz":[1234.5]'

# Exact shape, because the check is a fixed-string match: the key name and the
# JSON rendering both matter. Recorded here so the porting work in
# tower-finder-service (ClickUp 86capx9mv scope item 2) has something to build
# against rather than inferring it from a failing pipeline.

assert_tower_contract() {
    local base="$1" body code resp
    resp=$(curl -s --connect-timeout 10 --max-time 45 -w '\n%{http_code}' \
        "${base}/api/towers?${TOWER_CONTRACT_QUERY}" 2>/dev/null) || {
        echo "unreachable: ${base}"
        return 1
    }
    code=$(printf '%s' "$resp" | tail -n1)
    body=$(printf '%s' "$resp" | sed '$d')

    if [ "$code" != "200" ]; then
        echo "got HTTP ${code} from ${base}/api/towers"
        return 1
    fi
    if ! printf '%s' "$body" | grep -qF "$TOWER_CONTRACT_ECHO"; then
        # Both causes look identical from here, so name them both.
        echo "${base} answered 200 but the response does not carry ${TOWER_CONTRACT_ECHO}."
        echo "Either \`frequencies\` is not implemented there yet (scope item 2 of ClickUp"
        echo "86capx9mv), or it is implemented under a different key or JSON shape. Check"
        echo "the response before assuming the former:"
        printf '    %s\n' "$(printf '%s' "$body" | head -c 300)"
        return 1
    fi
    return 0
}

# Run directly (not sourced) to gate a deploy on the contract.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -uo pipefail
    TARGET="${1:-https://tower-finder.retina.fm}"
    printf 'Asserting %s honours the parameters our vhosts will forward to it... ' "$TARGET"
    if assert_tower_contract "$TARGET"; then
        echo "OK"
        exit 0
    fi
    echo "::error::tower-finder-service is not ready to receive /api/towers. Routing a vhost to it now would silently drop the parameter for every caller, including the public demo on testmap.retina.fm."
    exit 1
fi
