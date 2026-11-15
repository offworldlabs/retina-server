#!/bin/bash
# Run one evaluation pass in a throwaway container of the server image.
#   run_eval.sh [extra perf_eval arguments]
# Reads /etc/retina-perf/env (or the environment), copies the live registry and
# the events log into RETINA_PERF_ROOT/registry, then runs
# `python -m scripts.perf_eval run` from RETINA_PERF_BACKEND against the server's
# coverage volume (read-only) and RETINA_PERF_ROOT (read-write). Extra arguments
# go to perf_eval: `--backfill-from 2026-11-15`, `--nodes ret5cb8c964`,
# `--window 2026-11-15T12:00`, `--force`. Exit status is the evaluation's.
set -euo pipefail
if [ -z "${RETINA_PERF_ROOT:-}" ] && [ -f /etc/retina-perf/env ]; then
  # shellcheck disable=SC1091
  . /etc/retina-perf/env
fi
: "${RETINA_PERF_ROOT:=/var/lib/retina-perf}"
: "${RETINA_REGISTRY_SOURCE:=/var/lib/docker/volumes/retina-server_backend-data/_data/users.db}"
: "${RETINA_EVENTS_JSON:=/var/lib/docker/volumes/retina-server_backend-data/_data/events.json}"
: "${RETINA_COVERAGE_DATA:=/var/lib/docker/volumes/retina-server_backend-coverage-data/_data}"
: "${RETINA_PERF_BACKEND:=/opt/retina-server/backend}"
: "${RETINA_PERF_IMAGE:=retina-server:latest}"
: "${RETINA_PERF_MEMORY:=2500m}"
: "${RETINA_PERF_TIMEOUT:=110m}"
: "${RETINA_PERF_WORKERS:=3}"

mkdir -p "$RETINA_PERF_ROOT"/{registry,state,out,logs,truth}
# One evaluation at a time: a manual run and the timer's must not race on the
# registry copy, the window files or the ledger. Wait up to 15 minutes.
exec 9>"$RETINA_PERF_ROOT/.eval.lock"
if ! flock -w 900 9; then
  echo "perf-eval: another evaluation holds $RETINA_PERF_ROOT/.eval.lock; giving up" >&2
  exit 75
fi
python3 -I - "$RETINA_REGISTRY_SOURCE" "$RETINA_PERF_ROOT/registry/users.db" <<'PY'
import os, sqlite3, sys
source, copy = sys.argv[1], sys.argv[2]
tmp = f"{copy}.{os.getpid()}.tmp"
src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
try:
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("pragma journal_mode=delete")
        dst.commit()
    finally:
        dst.close()
finally:
    src.close()
os.replace(tmp, copy)
PY
if [ -f "$RETINA_EVENTS_JSON" ]; then
  cp -f "$RETINA_EVENTS_JSON" "$RETINA_PERF_ROOT/registry/events.json.tmp"
  mv -f "$RETINA_PERF_ROOT/registry/events.json.tmp" "$RETINA_PERF_ROOT/registry/events.json"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
log="$RETINA_PERF_ROOT/logs/eval-$stamp.log"
echo "perf-eval: $stamp backend=$RETINA_PERF_BACKEND image=$RETINA_PERF_IMAGE args=$*" | tee "$log"
set +e
timeout "$RETINA_PERF_TIMEOUT" docker run --rm --user 0:0 --name "retina-perf-eval-$stamp" --memory "$RETINA_PERF_MEMORY" \
  -v "$RETINA_PERF_BACKEND":/app/backend:ro \
  -v "$RETINA_COVERAGE_DATA":/d:ro \
  -v "$RETINA_PERF_ROOT":/perf \
  -w /app/backend --entrypoint python "$RETINA_PERF_IMAGE" -B -m scripts.perf_eval run \
  --archive-root /d/archive --adsb-root /d/adsb --fov-dir /d --perf-root /perf --workers "$RETINA_PERF_WORKERS" "$@" 2>&1 | tee -a "$log"
rc=${PIPESTATUS[0]}
set -e
echo "perf-eval: exit $rc" | tee -a "$log"
# Keep the last 200 logs.
ls -1t "$RETINA_PERF_ROOT"/logs/eval-*.log 2>/dev/null | tail -n +201 | xargs -r rm -f
exit "$rc"
