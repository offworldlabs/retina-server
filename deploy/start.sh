#!/bin/bash
# bash (not POSIX sh) is used for `wait -n` in the supervisor loop below, which
# lets us block on BOTH nginx and uvicorn at once and react to whichever exits
# first. bash 5.2 ships in the image; everything else here stays POSIX-simple.
set -e

# Tunable via env vars (docker-compose or .env)
# NOTE: workers MUST stay at 1 — the app uses in-memory shared state and a TCP
# server bound to a single port.  Concurrency is handled by FRAME_WORKERS
# threads inside the single process.
FRAME_WORKERS="${FRAME_WORKERS:-6}"
FRAME_QUEUE_SIZE="${FRAME_QUEUE_SIZE:-10000}"

export FRAME_WORKERS
export FRAME_QUEUE_SIZE

# Ensure the image-fresh constants.py is always imported instead of the
# potentially-stale copy inside the /app/backend/config named volume.
# We store the pristine copy at /app/deploy/config-image/config/constants.py
# (outside the volume) and prepend its parent to PYTHONPATH so Python finds
# it first via namespace-package merging, regardless of whether the cp below
# succeeds.  This means the app always runs with the correct constants even
# on servers where the volume is still root-owned.
export PYTHONPATH="/app/deploy/config-image${PYTHONPATH:+:$PYTHONPATH}"

# Also attempt an in-place refresh of the volume copy so that the next
# restart (or any tool that reads the file directly) also sees the new
# version.  Non-fatal: the PYTHONPATH override above guarantees correctness
# even when this fails.
if [ -f /app/deploy/config-image/config/constants.py ]; then
    cp /app/deploy/config-image/config/constants.py /app/backend/config/constants.py 2>/dev/null \
        || echo "[start.sh] Info: could not refresh volume constants.py (volume may be root-owned); PYTHONPATH override is active"
fi

# ── Environment guard ───────────────────────────────────────────────────────
# docker-compose.yml is a shared base carrying no environment identity; the
# per-environment values live in docker-compose.{prod,staging}.yml. If the
# overlay is missing (usually: no COMPOSE_FILE in the host's ./.env) we would
# otherwise boot a stack with no hostnames and an empty CSP, which looks fine
# until someone loads the site. Fail here instead, with the fix in the message.
: "${RETINA_ENV:?not set — the compose overlay is missing. On the host: cp deploy/env.<prod|staging>.example .env}"
: "${HOST_MAIN:?not set — the compose overlay is missing (see deploy/env.*.example)}"

# core/users.py refuses to import without JWT_SECRET outside dev and test, and
# the migration step below does not import it. Checked here as well, so a box
# that cannot serve refuses before it moves the schema rather than after.
case "$(printf '%s' "$RETINA_ENV" | tr '[:upper:]' '[:lower:]')" in
  dev|test) ;;
  *) : "${JWT_SECRET:?not set, and the app will not start without it. Restore backend/.env on the host, or generate one as backend/.env.example shows}" ;;
esac

# ── Alerting guard ──────────────────────────────────────────────────────────
# ALERT_WEBHOOK_URL and ALERT_WEBHOOK_AUTH carry a channel id and a credential,
# so unlike the settings beside ALERT_ENVIRONMENT in the compose overlays they
# live only in the host's backend/.env. That makes them the one part of alerting
# a rebuild can silently drop, and a production box that boots unalerting reads
# as healthy while nothing is watching it. Refuse the boot instead: the fix is
# one file on the host, and docs/runbook.md holds the values.
#
# Production only. On staging and test the same absence costs visibility into a
# box no user depends on, which is not worth trading for an outage.
#
# Tested on a trimmed copy rather than with the `${VAR:?}` form used above,
# because services/alerting.py strips before deciding a value is present: a
# key left holding only a stray space passes a bare emptiness test while
# leaving the box unalerting, which is the failure this guard exists to catch.
alert_url=$(printf '%s' "${ALERT_WEBHOOK_URL:-}" | tr -d '[:space:]')
alert_auth=$(printf '%s' "${ALERT_WEBHOOK_AUTH:-}" | tr -d '[:space:]')

if [ "$RETINA_ENV" = "production" ]; then
  if [ -z "$alert_url" ]; then
    echo "ALERT_WEBHOOK_URL is not set — production must not boot unalerting. Restore backend/.env on the host; see docs/runbook.md" >&2
    exit 1
  fi
  if [ -z "$alert_auth" ]; then
    echo "ALERT_WEBHOOK_AUTH is not set — ClickUp answers 401 to every alert without it. Restore backend/.env on the host; see docs/runbook.md" >&2
    exit 1
  fi
elif [ -z "$alert_url" ]; then
  echo "WARNING: ALERT_WEBHOOK_URL is not set — this box will alert nobody (docs/runbook.md)" >&2
elif [ -z "$alert_auth" ]; then
  # Only worth saying once the destination exists: without a URL nothing is
  # posted at all, and the line above is the one to act on.
  echo "WARNING: ALERT_WEBHOOK_AUTH is not set — ClickUp will answer 401 to every alert (docs/runbook.md)" >&2
fi

# Read-only, and it powers the admin console's Infrastructure page and nothing
# else, so its absence degrades one page rather than the monitoring.
if [ -z "${DIGITALOCEAN_READ_TOKEN:-}" ]; then
  echo "WARNING: DIGITALOCEAN_READ_TOKEN is not set — the admin Infrastructure page will be empty" >&2
fi

# ── Render the nginx config for this environment ────────────────────────────
# One template + one set of snippets serve every deployed environment; only the
# hostnames and the CSP's connect-src differ. Previously this was a `cp` that
# swapped in a separate, hand-maintained nginx-staging.conf — which is how
# staging ended up with no Content-Security-Policy and no /api/auth/ rate limit.
#
# The renderer substitutes an explicit allowlist of variables and leaves nginx's
# own ($host, $remote_addr, …) untouched; it exits non-zero on a missing or
# unknown variable. `set -e` turns that into a failed boot, which the deploy's
# health gate reports — far better than silently serving a half-rendered site.

# Fail with the actual cause rather than a generic nginx config error. `nginx -t`
# below does catch this, but names only the directive. Mirrors how
# setup-server.sh refuses to run without the origin certificate.
#
# Deliberately fail-closed: a missing CA must never degrade to serving TLS
# without verification, because that is indistinguishable from the boundary
# working.
if [ "${TLS_ENABLED:-true}" != "false" ] && [ ! -f /etc/ssl/cloudflare/origin-pull-ca.pem ]; then
    echo "[start.sh] ✗ /etc/ssl/cloudflare/origin-pull-ca.pem is missing." >&2
    echo "[start.sh]   nginx is configured for Authenticated Origin Pulls and" >&2
    echo "[start.sh]   cannot start without Cloudflare's origin-pull CA. Fetch it:" >&2
    echo "[start.sh]     curl -fsS -o /etc/ssl/cloudflare/origin-pull-ca.pem \\" >&2
    echo "[start.sh]       https://developers.cloudflare.com/ssl/static/authenticated_origin_pull_ca.pem" >&2
    exit 1
fi

python3 /app/deploy/render-nginx-config.py \
    /app/deploy/nginx/nginx.conf.template \
    /etc/nginx/sites-available/default

# Validate before the supervisor starts, for the same reason.
nginx -t
echo "[start.sh] Rendered nginx config for RETINA_ENV=${RETINA_ENV} (${HOST_MAIN})"

# ── Database migrations ─────────────────────────────────────────────────────
# create_all is guarded off outside tests (core/users.py), so this is the only
# thing that builds or updates the schema. `set -e` makes a failure here abort
# the boot, which is deliberate: a server started against a half-applied schema
# fails later, at the first request touching the missing column, and the deploy
# health gate reports this instead.
#
# The first run on an existing droplet finds tables but no alembic_version.
# Revision 0001 detects that and records itself without recreating them, so no
# `alembic stamp` is needed by hand.
#
# A rollback moves the source tree (and migrations/versions/ with it) back to an
# older image while leaving the database exactly where the newer image left it.
# Alembic does not stop at the newest revision it recognises: `upgrade head`
# exits non-zero with "Can't locate revision identified by '<rev>'" the moment
# the database is ahead of what this image ships. Treating that as a boot
# failure would turn every rollback into a crash-loop, which defeats the point
# of rolling back, so that specific failure is logged and swallowed; anything
# else (a broken revision, a locked or corrupt database, an unreadable file)
# still aborts the boot exactly as before.
#
# The swallow is not a judgement that the gap is harmless. This image cannot
# make that judgement: the revisions the database is ahead by are precisely the
# ones absent from its own migrations/versions/, so all it has is a revision id
# it cannot resolve. Any rule it enforced would therefore fire on every
# rollback, additive ones included, and refusing to boot would turn a benign
# rollback into an outage on the recovery path. The grading happens on the host
# instead, before the tree moves, where both trees are reachable:
# deploy/classify-migration-gap.py, called by deploy/rollback.sh. A destructive
# revision needs `alembic downgrade` run by hand; see docs/runbook.md.
#
# The grep below matches free text Alembic itself controls, not us, so a
# future Alembic release rewording that message would make this stop matching
# and every legitimate rollback would fall through to the `exit 1` below,
# silently, at the worst possible moment. backend/tests/test_migrations.py's
# test_rollback_ahead_sentinel_matches_alembics_wording reproduces this exact
# scenario against a real Alembic run and asserts the substring is still
# present, so a wording change fails loudly in CI instead. The substring
# itself lives once, in backend/tests/migration_helpers.py's
# ROLLBACK_AHEAD_SENTINEL; this grep is the other half.
#
# The `if !`-equivalent form below (testing the assignment itself) is required
# to keep `set -e` from aborting on the very failure this block exists to
# inspect: a failing command substitution used bare, `x=$(cmd)` outside a
# conditional, still triggers errexit. Once inside the if/elif, the failure
# branch must exit explicitly, since being the tested command is what silences
# the automatic abort.
echo "[start.sh] Applying database migrations..."
# migrate.py is `alembic upgrade head` plus a report of the revision reached.
if MIGRATION_OUTPUT=$(cd /app/backend && python3 /app/deploy/migrate.py 2>&1); then
    echo "[start.sh] Migrations applied"
    printf '%s\n' "${MIGRATION_OUTPUT}"
elif printf '%s\n' "${MIGRATION_OUTPUT}" | grep -q "Can't locate revision"; then
    echo "[start.sh] Database is ahead of this image's migrations (rollback); booting anyway."
    echo "[start.sh] This image cannot tell whether that is safe: it does not ship the"
    echo "[start.sh] revisions in question. deploy/rollback.sh grades them at rollback"
    echo "[start.sh] time and prints the verdict; see docs/runbook.md."
    printf '%s\n' "${MIGRATION_OUTPUT}"
else
    echo "[start.sh] Migration failed, refusing to boot:"
    printf '%s\n' "${MIGRATION_OUTPUT}"
    exit 1
fi

# ── uvicorn supervision: exit on a persistent fast crash-loop ────────────────
# The fast in-process restart below absorbs *transient* uvicorn crashes without
# recreating the whole container. But if uvicorn crash-loops persistently the
# in-process loop alone would spin forever while nginx keeps the container
# "up" — Docker would mark it `unhealthy` yet never restart it, because
# `restart: unless-stopped` acts on container EXIT, not health. So we count
# consecutive *fast* failures and, past a threshold, tear down nginx and exit
# non-zero so Docker recreates the container and clears the wedged backend.
#
# T (MIN_UPTIME_S): a uvicorn run that stayed up at least this long before
#   dying is treated as a transient crash — the counter resets. 60s is chosen
#   to sit safely BELOW the compose healthcheck `start_period` (90s in both
#   docker-compose.yml and docker-compose.staging.yml). A slow-but-fine cold
#   boot keeps uvicorn *running* (it doesn't exit), so its run length grows
#   past 60s and can never be scored as a fast failure — this logic never
#   fights the healthcheck grace window.
# N (MAX_FAST_FAILURES): give up after this many *consecutive* sub-T crashes.
#   5 x (crash + 2s backoff) means at least ~10s of real thrashing before we
#   hand off to Docker — long enough to ride out a genuine transient, few
#   enough to recover a broken backend promptly without a restart storm.
MIN_UPTIME_S=60
MAX_FAST_FAILURES=5
FAST_FAILURES=0

cd /app/backend

# nginx runs in the foreground mode it always has (`daemon off;`, non-forking)
# but backgrounded here so the supervisor loop below is the controlling
# foreground process and can decide to stop nginx and exit. pid already set in
# /etc/nginx/nginx.conf.
nginx -g "daemon off;" &
NGINX_PID=$!

# Tracked so cleanup can stop children, and so a shutdown-driven uvicorn exit
# is never miscounted as a crash (see SHUTTING_DOWN below).
UVICORN_PID=""
SHUTTING_DOWN=0

# Stop both children on any exit path (normal shutdown, INT, or our own exit 1).
cleanup() {
  SHUTTING_DOWN=1
  if [ -n "$NGINX_PID" ]; then
    kill "$NGINX_PID" 2>/dev/null || true
  fi
  if [ -n "$UVICORN_PID" ]; then
    kill "$UVICORN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT TERM INT

while true; do
  # If nginx died on its own, don't keep the container up serving nothing —
  # exit so Docker recreates it (mirrors the old nginx-foreground behaviour).
  if ! kill -0 "$NGINX_PID" 2>/dev/null; then
    echo "[supervisor] nginx is no longer running; exiting so Docker recreates the container"
    exit 1
  fi

  echo "[supervisor] starting uvicorn on ${UVICORN_HOST:-127.0.0.1}:8000..."
  # Default 127.0.0.1: only nginx (same container) reaches the app; port 8000
  # is never published to the host. Staging sets UVICORN_HOST=0.0.0.0 so the
  # fleet container can POST ground-truth/ADS-B to the API over the compose
  # network (8000 stays unpublished, so it is still not exposed off-host).
  START=$(date +%s)
  # Backgrounded + `wait` so our TERM/INT trap can interrupt promptly on
  # container stop (a plain foreground command would defer the trap).
  uvicorn main:app --host "${UVICORN_HOST:-127.0.0.1}" --port 8000 --workers 1 --log-level warning &
  UVICORN_PID=$!
  EXIT_CODE=0
  # Block until EITHER child exits, not just uvicorn. Waiting only on uvicorn
  # let nginx die unnoticed under a healthy uvicorn: the container stayed up —
  # and `healthy`, since the compose healthcheck hits uvicorn on :8000 directly,
  # bypassing nginx — so `restart: unless-stopped` never fired while :80/:443
  # served nothing. `wait -n` returns on the first of the two to exit, and `$?`
  # is that process's status. (`|| EXIT_CODE=$?` keeps `set -e` off our back.)
  wait -n "$NGINX_PID" "$UVICORN_PID" || EXIT_CODE=$?
  END=$(date +%s)

  # If the container is stopping, the exit is expected (SIGTERM), not a crash.
  # Break without counting it so normal shutdown never trips the give-up path.
  if [ "$SHUTTING_DOWN" = "1" ]; then
    echo "[supervisor] shutting down; not restarting uvicorn"
    break
  fi

  # nginx is the one that died? Recreate the container rather than loop-restart
  # uvicorn behind a dead proxy (this is the case the old `wait $UVICORN_PID`
  # could never reach). EXIT_CODE here is nginx's status, which is why it is not
  # fed into the uvicorn fast-failure logic below.
  if ! kill -0 "$NGINX_PID" 2>/dev/null; then
    echo "[supervisor] nginx exited (code $EXIT_CODE); stopping uvicorn and exiting so Docker recreates the container"
    kill "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
    exit 1
  fi

  RAN=$((END - START))
  if [ "$RAN" -ge "$MIN_UPTIME_S" ]; then
    echo "[supervisor] uvicorn ran ${RAN}s before exiting (code $EXIT_CODE); transient crash, resetting fast-failure counter"
    FAST_FAILURES=0
  else
    FAST_FAILURES=$((FAST_FAILURES + 1))
    echo "[supervisor] uvicorn exited fast after ${RAN}s (code $EXIT_CODE); fast failure ${FAST_FAILURES}/${MAX_FAST_FAILURES}"
    if [ "$FAST_FAILURES" -ge "$MAX_FAST_FAILURES" ]; then
      echo "[supervisor] ${MAX_FAST_FAILURES} consecutive fast failures; stopping nginx and exiting so Docker recreates the container"
      kill "$NGINX_PID" 2>/dev/null || true
      wait "$NGINX_PID" 2>/dev/null || true
      exit 1
    fi
  fi

  echo "[supervisor] restarting uvicorn in 2s..."
  sleep 2
done
