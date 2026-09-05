# Uncertainty disc around multinode solves

Date: 2026-09-05. Branch `feat/solve-uncertainty-disc`.

## Goal

Draw a soft disc around every multinode-solved aircraft on the live map whose
radius is an honest, calibrated estimate of where the aircraft actually is.
The disc must grow while the icon dead-reckons between solves, and the detail
panel must quote the same number.

## What the backend already knows

- `retina_geolocator.multinode_solver.solve_multinode` returns `cov_en_km2`
  and `pos_sigma_km`: the Gauss-Newton `s²·(JᵀJ)⁻¹` covariance of the LM fit.
  This is a **measurement-noise-propagation lower bound**. It cannot see
  inter-node time skew, association contamination, or pinned-altitude error.
- `services/track_filter.py` composes that into the Kalman measurement noise
  `R = (4·σ_formal)² + 1200²` and exports `kf_pos_sigma_m`, the filter's
  post-update marginal. In practice `kf_pos_sigma_m` sits at ~1 km regardless
  of solve quality (the 1200 m floor dominates), so it carries almost no
  relative information and over-states the error of n≥3 solves by ~3×.
- Neither number reaches the map feed today.

## Calibration (2026-09-05, live data)

`/api/test/mlat-history?all=1` on staging (`testmap.retina.fm`, 249 records)
and the test droplet (`test-map.retina.fm`, 695 records), outcomes
`published` + `known_truth_match`, 944 solves with `gt_error_km` (raw solve
vs. dead-reckoned ADS-B truth) and a formal `pos_sigma_km`.

| n_nodes | k | raw error median | p90 | formal σ median | p90 | error ÷ formal σ median | p90 |
|---|---|---|---|---|---|---|---|
| 2 | 646 | 0.55 km | 1.56 km | 0.10 km | 0.51 km | 5.2 | 18 |
| 3 | 58 | 0.21 km | 0.44 km | 0.056 km | 0.17 km | 3.3 | 14 |
| ≥4 | 240 | 0.17 km | 0.36 km | 0.058 km | 0.12 km | 2.8 | 7.5 |

Formal σ is informative but under-scaled: quartiles of formal σ (27 m → 300 m)
map monotonically to raw-error medians of 0.22 km → 0.82 km. It also has a
degenerate tail: p99 is 3.8×10⁶ km, max 3×10⁸ km (near-parallel baselines),
so it must be capped before use.

### Model

Per-axis position sigma at solve time, in metres:

```
σ_solve = sqrt( (A · min(σ_formal, CAP))² + B(n_nodes)² ) · (DARK_GAIN if dark lane else 1)
```

Radial error is then modelled as Rayleigh with that σ, so the radius that
contains the aircraft with probability p is `k_p · σ`:
`k_50 = 1.177` (CEP), `k_68 = 1.510`, `k_95 = 2.448`. The map draws the 95%
radius.

Fit (B chosen per node-count bucket so that exactly 95% of raw errors fall
inside `2.448·σ`, `CAP = 3000 m`):

| A | B(n=2) | B(n=3) | B(n≥4) | R95 median n=2 / n=3 / n≥4 | R95 p90 n=2 |
|---|---|---|---|---|---|
| 0 | 726 m | 213 m | 197 m | 1.78 / 0.52 / 0.48 km | 1.78 km |
| **1** | **630 m** | **201 m** | **173 m** | **1.56 / 0.51 / 0.45 km** | **2.03 km** |
| 2 | 523 m | 186 m | 136 m | 1.38 / 0.55 / 0.44 km | 2.92 km |
| 3 | 467 m | 175 m | 86 m | 1.38 / 0.62 / 0.48 km | 4.10 km |

Chosen: **A = 1, B = 650 / 210 / 180 m, CAP = 3000 m** (rounded up from the
fit; see caveats). At those settings 50%-coverage is ~0.66 rather than 0.50 —
the empirical distribution is heavier-tailed than Rayleigh, so a single σ
calibrated at 95% is conservative in the middle. That is the right side to err
on for a disc whose caption says "95%".

Larger A buys little sharpness (the median R95 barely moves) and pays for it
with a fat tail (n=2 p90 doubles by A=3), so the formal term is kept at unit
gain only to let a genuinely ill-conditioned solve show a bigger disc.

### Caveats

- The sample is almost entirely **known-lane** solves: the ADS-B fix seeded
  the initial guess and pinned the altitude, and the `truth_match` label
  right-censors the distribution at 2 km. Dark-lane solves (14 with ground
  truth, median 0.37 km) had a similar median but no tail information.
  `DARK_GAIN = 1.5` is a prior, not a measurement, and is env-tunable.
- `gt_error_km` is the **raw** solve error. The map draws the KF-smoothed
  position, which on the small `published` sample was not better (median
  0.44 km smoothed vs 0.30 km raw, k=14). The disc is centred on the drawn
  position but calibrated on raw error; re-check once `sigma_m` is in the
  history (below).
- Staging n=2 (real hardware, B95 = 718 m) and test n=2 (synthetic fleet,
  B95 = 738 m) agree, so one set of floors serves both environments.

### Growth while dead-reckoning

The feed dead-reckons a solve forward up to 30 s with the KF's learned
velocity, then holds; the frontend continues its own DR up to 60 s. The disc
grows accordingly:

```
σ(t) = sqrt( σ_solve² + (σ_v · min(t, 60))² )
```

`t` is the age of the solve at draw time, `σ_v` the velocity sigma:
`track_filter.learned_velocity(key)[2]` when the KF has state for the key
(clamped to [5, 150] m/s), else `SOLVE_SIGMA_VEL_DEFAULT_MS = 25`. A fresh
dark solve seeded from the CV fit carries the KF's 150 m/s prior, so its disc
grows fast between solves — that is honest (solved velocity was measured at
127 m/s median vector error on 2026-08-09).

## Wire contract

Two new optional fields on every `multinode_solve` feed entry
(`services/aircraft_feed.py::multinode_to_aircraft`), both rounded to 1 dp:

| field | meaning |
|---|---|
| `pos_sigma_m` | `σ_solve` above, per-axis, metres, at the solve epoch. Absent when `n_nodes` is missing. |
| `pos_sigma_vel_ms` | `σ_v`, m/s, for growth with age. |

`seen` (already present) is the solve age at flush time; the frontend adds the
time since the message arrived. The solve history record
(`_record_solve_history`) gains `sigma_m` (same `σ_solve`) so the calibration
above can be re-run from `/api/test/mlat-history` alone:
`fraction(gt_error_km·1000 ≤ 2.448·sigma_m)` should stay near 0.95.

Env keys (all read at import, documented in `backend/.env.example`):
`SOLVE_SIGMA_FORMAL_GAIN` (1.0), `SOLVE_SIGMA_FORMAL_CAP_M` (3000),
`SOLVE_SIGMA_FLOOR_N2_M` (650), `SOLVE_SIGMA_FLOOR_N3_M` (210),
`SOLVE_SIGMA_FLOOR_N4_M` (180), `SOLVE_SIGMA_DARK_GAIN` (1.5),
`SOLVE_SIGMA_VEL_DEFAULT_MS` (25). `σ_solve` is clamped to [50, 5000] m.

## Frontend

- `map/uncertainty.ts`: pure `solveUncertaintyRadiusM(ac, ageS)` returning
  the 95% radius (`2.448·σ(t)`), capped at 10 000 m, or 0 when the entry has
  no `pos_sigma_m`; plus the constants. Unit-tested.
- `SolveUncertaintyLayer`: imperative `L.circle` per visible
  `multinode_solve` entry on one `L.canvas` renderer in the passive pane,
  updated at the 2 Hz display tick, centred on the smoothed icon position,
  hidden whenever `hideDrIcon` hides the icon. Fill in the lane colour
  (`getAircraftColor`) at low opacity, hairline stroke, non-interactive.
- Toggle `showUncertainty` (persisted `tf.layer.uncertainty`, default on,
  hash letter `u`, Toolbar button).
- Detail panel, Multi-node section: `Accuracy (95%)` → `±<now> m` with
  `(±<at solve> m)` when they differ.
