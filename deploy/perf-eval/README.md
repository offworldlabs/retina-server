# RETINA node performance evaluation (independent denominator)

Operator notes for the units installed by `deploy/perf-eval/install.sh`. The full
method, interpretation and limitations are in
`docs/node-performance-independent.md` of the retina-server repository.

- `retina-truth-recorder.service` polls adsb.retina.fm (ADSBHub) around every
  real receiver and writes raw hourly files under `RETINA_PERF_ROOT/truth/`.
- `retina-perf-eval.timer` runs `run_eval.sh` at 01:20, 04:20, ... UTC, which
  scores every closed three-hour window not yet processed, reconciles the two
  before it, and renders `RETINA_PERF_ROOT/out/index.html` plus JSON.
- `retina-perf-dashboard.service` serves `RETINA_PERF_ROOT/out` on loopback.

Everything under `RETINA_PERF_ROOT` is private derived data (it is built from
true receiver positions); never publish or commit it.
