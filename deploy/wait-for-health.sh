#!/bin/bash
# Wait for the server container to answer /api/health, and the single
# implementation of that wait. Run as `bash deploy/wait-for-health.sh` by the
# three deploys, `just deploy-test` and setup-server.sh once the container is
# up, and sourced by rollback.sh, which moves the tree (and with it this file)
# before it waits.
#
# No `-f` flags: the host's ./.env names the compose files, as in rollback.sh.

# The compose healthcheck's start_period, the longest a cold boot is given. A
# shorter wait fails a boot the healthcheck would still accept.
HEALTH_WAIT_SECONDS=90

# health_probe
#
# One attempt: 0 = the server answered /api/health inside its container.
# timeout= as the healthcheck's, so a server that accepts the request and never
# answers fails the attempt rather than holding it. </dev/null because exec
# otherwise reads the caller's stdin, which may be its script.
health_probe() {
    docker compose exec -T server python3 -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" \
        >/dev/null 2>&1 </dev/null
}

# wait_for_health
#
# 0 = the server answered. 1 = it did not within HEALTH_WAIT_SECONDS; the
# container's recent logs are printed.
#
# Polled once a second and timed against the clock rather than counted: each
# attempt is a `docker compose exec` and a Python start, about 0.5 s on the
# droplets.
wait_for_health() {
    local start=$SECONDS elapsed
    while :; do
        if health_probe; then
            echo "Server healthy after $((SECONDS - start))s"
            return 0
        fi
        elapsed=$((SECONDS - start))
        if [ "$elapsed" -ge "$HEALTH_WAIT_SECONDS" ]; then
            echo "Health check failed after ${elapsed}s"
            docker compose logs --tail=50 </dev/null
            return 1
        fi
        echo "Waiting for health check... ${elapsed}s"
        sleep 1
    done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    wait_for_health
fi
