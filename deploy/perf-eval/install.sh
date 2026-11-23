#!/bin/bash
# Install or update the node-performance evaluation units on a droplet.
#   sudo deploy/perf-eval/install.sh [--no-enable]
# Copies the scripts to /opt/retina-perf/bin, the units to /etc/systemd/system,
# writes /etc/retina-perf/env from env.example when there is none yet (edit it
# for the host), reloads systemd and, unless --no-enable, enables and starts
# the recorder, the dashboard and the three-hourly timer. Re-running updates the
# scripts and units in place and restarts the recorder; the env file is kept.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
enable=1
[ "${1:-}" = "--no-enable" ] && enable=0

install -d -m 755 /opt/retina-perf/bin /etc/retina-perf
install -m 755 "$here/truth_recorder.py" "$here/run_eval.sh" /opt/retina-perf/bin/
install -m 644 "$here/README.md" /opt/retina-perf/README.md
if [ ! -f /etc/retina-perf/env ]; then
  install -m 600 "$here/env.example" /etc/retina-perf/env
  echo "wrote /etc/retina-perf/env from env.example; check its paths for this host"
fi
# shellcheck disable=SC1091
. /etc/retina-perf/env
install -d -m 700 "${RETINA_PERF_ROOT:-/var/lib/retina-perf}"
for unit in retina-truth-recorder.service retina-perf-eval.service retina-perf-eval.timer retina-perf-dashboard.service; do
  install -m 644 "$here/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
if [ "$enable" = 1 ]; then
  systemctl enable --now retina-truth-recorder.service retina-perf-dashboard.service retina-perf-eval.timer
  systemctl restart retina-truth-recorder.service
  systemctl --no-pager --lines=0 status retina-truth-recorder.service retina-perf-eval.timer retina-perf-dashboard.service || true
fi
echo "installed; next evaluation runs:"; systemctl list-timers retina-perf-eval.timer --no-pager | head -3
