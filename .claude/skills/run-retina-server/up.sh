#!/usr/bin/env bash
# Bring the laptop stack up with sign-in on, seed ownership, wait for the
# fleet and print a sign-in link. Run from the repo root. Safe to re-run: an
# unchanged stack is left running and the seed is idempotent.
set -euo pipefail

skill=.claude/skills/run-retina-server
compose=(docker compose -p retina-signed-in -f docker-compose.yml -f docker-compose.local.yml -f "$skill/signed-in.compose.yml")

# Docker Desktop's credential helper can block a build forever on the keychain.
# The base images are public, so build with a config that has no helper.
if grep -q '"credsStore": *"desktop"' "$HOME/.docker/config.json" 2>/dev/null; then
  context=$(docker context show)
  DOCKER_CONFIG=$(mktemp -d)
  export DOCKER_CONFIG
  trap 'rm -rf "$DOCKER_CONFIG"' EXIT
  printf '{"auths": {}, "currentContext": "%s"}\n' "$context" >"$DOCKER_CONFIG/config.json"
  for entry in contexts cli-plugins buildx; do
    if [ -e "$HOME/.docker/$entry" ]; then ln -s "$HOME/.docker/$entry" "$DOCKER_CONFIG/$entry"; fi
  done
fi

# --wait: without it, a rebuild that recreates only the server returns before
# its health check passes, and the seed races start.sh's migrations.
"${compose[@]}" up -d --build --wait

docker exec -i -w /app/backend retina-local-server python - seed <"$skill/seed.py"

# The server's own count. /api/radar/nodes omits private nodes, so it never
# reaches 25 once one is seeded, and the fleet's log keeps saying 25 while its
# connections are down.
echo "Waiting for the synthetic fleet (1-5 minutes from a cold start)..."
fleet_up() {
  curl -sf http://api.localhost:8080/api/test/dashboard |
    python3 -c 'import json, sys; sys.exit(json.load(sys.stdin)["nodes"]["active"] < 25)' 2>/dev/null
}
for _ in $(seq 120); do
  if fleet_up; then break; fi
  sleep 5
done
if fleet_up; then echo "Fleet up: 25 nodes."; else echo "Fleet not up after 10 minutes; see docker logs retina-local-fleet." >&2; fi

docker exec -i -w /app/backend retina-local-server python - link you@example.com <"$skill/seed.py"
