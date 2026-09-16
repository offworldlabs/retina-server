#!/bin/bash
# What "the simulated fleet is up" means, in nodes. The CI gate and the smoke
# suite both need the number and must not each keep their own copy, so it is
# read from the overlay that sets it rather than written down twice.
#
# A floor rather than the full count: the gate's job is to tell a fleet that is
# serving from one that is not, and holding out for all 50 would fail the run
# over a single node that took its time.
FLEET_HEALTHY_PCT=80

_fleet_compose() {
    echo "${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker-compose.staging.yml}"
}

# The node count the environment is configured for. Non-zero exit when the
# overlay no longer declares one, which must fail its caller: falling back to a
# default would be a gate that silently stopped checking the thing it is for.
fleet_configured_nodes() {
    local compose count
    compose="$(_fleet_compose "${1:-}")"
    [ -r "$compose" ] || return 1
    count=$(grep -oE '^[[:space:]]*-[[:space:]]*FLEET_NODES=[0-9]+' "$compose" | grep -oE '[0-9]+$' | head -1)
    [ -n "$count" ] || return 1
    echo "$count"
}

# How many must be answering for the fleet to count as up.
fleet_min_active() {
    local count
    count=$(fleet_configured_nodes "${1:-}") || return 1
    echo $(( count * FLEET_HEALTHY_PCT / 100 ))
}
