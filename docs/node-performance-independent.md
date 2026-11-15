# Node detection performance against an independent denominator

How RETINA measures what fraction of the aircraft that were actually there each
radar node detected, every three hours, with the uncertainty and the data
quality beside every figure. It replaces the reading of PR #727's node-opportunity
tables on environments whose ADS-B archive holds only the nodes' own tags, where
those tables measure tag persistence, not detection (see "What was wrong").

Code: `backend/services/performance_truth.py` (the scorer),
`backend/services/performance_truth_job.py` (windows, state, aggregates),
`backend/services/performance_truth_html.py` (the dashboard),
`backend/scripts/perf_eval.py` (the command), `deploy/perf-eval/` (the recorder,
the units, the installer). Tests: `backend/tests/test_performance_truth.py`.

## What was wrong, and what changed

| Problem in the earlier analysis | What this pipeline does |
| --- | --- |
| The denominator was the server's ADS-B archive, which on test holds only the nodes' own tags: an aircraft entered it only after a detection had been matched to it. The fraction measured how long a tag persisted (an isolated chance tag scored exactly 1/3). | The denominator is ADSBHub traffic polled by a recorder that never sees a detection (`deploy/perf-eval/truth_recorder.py`), or, where the server's own ADSBHub poller runs, the archive's `source="aggregator"` rows. Aircraft the radar never tagged are in it. |
| One spurious tag credited up to three 10 s buckets through the ±1 neighbour rule. | The raw ±1 figure is kept for comparison only. The headline (`validated`) credits a bucket only when a detection in that bucket fits the aircraft's independently predicted bistatic delay and Doppler. |
| Chance associations (WNJU, Crystal Palace) were indistinguishable from detections. | Every tag is checked against the independent geometry (`refuted` when it does not fit), and the chance rate of the validation itself is measured with a time-shifted control (`background`), reported and subtracted (`net`). |
| The learned field of view, trained on the radar's own successes, pruned hard regions from the denominator, and compared a limit learned in ground range with slant range. | Three gates side by side: `geometric` (delay window + horizon, the headline), `declared` (+ antenna wedge), `empirical` (+ learned field of view, from a copy saved before the window, compared in ground range). |
| Binomial intervals on correlated 10 s buckets. | Percentile bootstrap over encounters (one aircraft's run of consecutive opportunities); counts of opportunities, encounters and aircraft shown with every rate. |
| "Node sent a frame in the bucket" doubled as uptime. | Node activity is a frame within 60 s of the bucket plus the server's recorded offline events; truth availability (was the region polled?) is tracked separately, and missing ADS-B is never read as an absence of aircraft. |

## Definitions

All times UTC. A **window** is three hours, aligned on the clock; a **bucket** is
10 s (`BUCKET_MS`), the stack's unit.

**Truth.** A fix is `(hex, capture time, lat, lon, alt_baro, gs, track)`. The
recorder polls `adsb.retina.fm/v2/point/{lat}/{lon}/{radius}` over each cluster
of real receivers every 10 s and writes every answer; the capture time is the
answer's `now` minus the position's `seen_pos`. A fix repeated by later polls is
written once. The server's `source="aggregator"` rows are read the same way
(`--truth-source aggregator|both`), their `seen_ms` being the capture time.

**Trajectory.** For one aircraft, a position at instant t is interpolated
linearly between the two fixes bracketing t when they are at most 30 s apart
(`TRUTH_MAX_GAP_MS`), or dead-reckoned at the reported speed and track up to 6 s
past the first or last fix (`TRUTH_EDGE_MS`). Otherwise the aircraft has no
known position at t. The velocity is the interpolated ENU velocity from the
fixes' speed, track and vertical rate.

**Geometry.** For the config version active at t (the latest `created_at` not
after t, else the oldest), with the true receiver and transmitter positions from
the registry: Rt, Rr, L (baseline), delay = (Rt + Rr − L)/c, Doppler =
−d(Rt + Rr)/dt / λ (λ = c/fc), the receiver's ENU `up` of the target (horizon,
earth curvature included through ECEF), bearing, great-circle ground range and
bistatic angle.

**Opportunity** (node n, aircraft h, bucket b with centre t_b): all of

1. h has a known position at t_b and `alt_baro > 0`;
2. delay(t_b) < window (max_range_km / c) and up(t_b) > 0 — the `geometric` gate;
3. for the `declared` gate, also inside the declared wedge when one is declared;
   for the `empirical` gate, also `ground range ≤ learned limit` at the bearing,
   from the newest copy of `empirical_<node>.json` saved before the window;
4. the node was active: an archived frame within 60 s of t_b
   (`ACTIVE_WINDOW_MS`) and no recorded offline span (events log:
   "went offline (no heartbeat for N s)", opened N s before the event, closed at
   the next frame) covering t_b;
5. truth was available: a successful poll of a region covering n within 20 s of
   the bucket (`TRUTH_POLL_SLACK_MS`).

**Numerators**, each exact counts over the same opportunities:

- `raw_pm1` — the node's own tag of h on a detection in b−1, b or b+1 (PR #727's rule);
- `raw` — the tag in b itself;
- `validated` — a detection in b whose (delay, Doppler) lies within
  1.5 µs × 5 Hz (`VALIDATE_DELAY_US`, `VALIDATE_DOPPLER_HZ`, an ellipse) of h's
  predicted values, with no second aircraft inside that ellipse; the tag is not
  required;
- `validated_tagged` — validated, and the tag names h;
- `matched_any` — validated or ambiguous;
- `ambiguous` — a detection fits h and another aircraft equally;
- `refuted` — tagged in b, but no detection so tagged fits h's geometry.

The validation tolerances come from measured residuals: on the Atlanta nodes'
tags the delay residual against the node's own fix has a MAD of 0.26 µs and the
Doppler residual against the fix's speed and track a MAD of 0.87 Hz, the node's
own association gate being 6.671 µs and 5 Hz.

**Signal-to-noise.** Each bucket row also carries the SNR the node reported for
the looks that validate it: `snr_db`, the best of the unambiguously matched
detections in the bucket; `snr_med_db`, their median; and `n_matched`, how many
there were (a node frames twice a second, so up to twenty looks a bucket). All
three are empty for a bucket without a validated detection. The control rows
keep them too: there they describe the detections a chance match lands on. The
per-cell summaries add `snr_n` and `snr_sum_db` (additive, so windows merge by
summing; their ratio is the mean best-look SNR in dB, the dashboard's SNR
column), and each window's diagnostics hold 1 dB histograms of the SNR of all
detections, of those fitting exactly one truth aircraft, and of the tagged
ones. SNR exists only where something was detected: a mean SNR by range or
class is the mean of what cleared the node's threshold, so it flattens towards
that threshold where detection is marginal. Read it beside the detection rate,
never instead of it.

**Background.** Every trajectory is read 600 s and 1800 s later than asked
(`BACKGROUND_SHIFTS_MS`) and scored against the detections of now as its own set
of control opportunities under the same gates. `background` =
matches / control opportunities, averaged over the shifts. `net` = validated −
background. Nothing is subtracted from `raw`.

**Encounter.** Consecutive opportunities of one aircraft with gaps of at most
six buckets (`ENCOUNTER_GAP_BUCKETS`). Detection latency is from the first
opportunity to the first validated bucket. Every interval is a 95 % percentile
bootstrap over encounters (`BOOTSTRAP_DRAWS` = 400); `net`'s interval resamples
the real and the control encounters independently. A span shows a rate only with
at least 30 opportunities and 5 encounters (`MIN_OPPORTUNITIES`, `MIN_ENCOUNTERS`).

**Status** per node and window: `no_truth` (no poll covered the node),
`node_inactive` (no frames), `insufficient`, `ok`; flags `truth_gaps`
(< 90 % of buckets polled), `node_partial` (< 90 % active),
`background_dominated` (background ≥ half the validated rate), `tags_refuted`
(> 30 % of tagged buckets refuted), `ambiguous_geometry`, `tags_not_in_truth`.

**Legacy figure.** For comparison each window also carries PR #727's
node-opportunity rate over the same hours: `source="node"` rows attributed to
the node, in the window and above the horizon, tags within ±1 bucket.

## Independence of the truth

The recorder reads the node registry (positions) and adsb.retina.fm. It reads
no detection, no tag, no server state, and runs as its own process. Whether an
aircraft is in its files depends only on ADSBHub having heard it inside the
polled disc. The aggregator rows of the server's archive are attributed by
position alone (`services/tasks/adsb_snapshot.py`), so they are independent too;
the `source="node"` rows are not (they are the tags) and are never used as truth.

Known limits of the truth: ADSBHub coverage is incomplete (no ADS-B, no truth:
non-cooperative targets are never in the denominator, so this is a probability
of detecting cooperative traffic); positions arrive in 6 s batches 2–8 s old,
hence the interpolation; a region's polls are shared across the 2 requests/s
budget of the host with the synthetic fleet, and a 429 shows as a truth gap.

## Operation

Installed by `sudo deploy/perf-eval/install.sh` (copies scripts to
`/opt/retina-perf/bin`, units to systemd, `env.example` to
`/etc/retina-perf/env` on first install; edit that file for the host, in
particular `RETINA_PERF_BACKEND`, the checkout the evaluation runs from). Units:

- `retina-truth-recorder.service` — the recorder, always on, writes
  `RETINA_PERF_ROOT/truth/adsbhub/YYYY-MM-DD/HH.jsonl.gz` (one file per UTC hour
  of receipt), `truth/regions/*.json` and `truth/STATUS.json`;
- `retina-perf-eval.timer` — `run_eval.sh` hourly at :35 UTC (after the
  detection archive's hourly flush at about :23): scores the open three-hour
  window provisionally on what has arrived, scores every complete window the
  ledger lacks, reconciles the newest complete one, re-scores any whose inputs
  (file sizes) changed, then renders the outputs. A window is complete 75
  minutes after it ends. A provisional window is marked as such in the ledger,
  `WINDOW.json`, the summaries and the dashboard (hollow points, "provisional"),
  counts only in the "latest" span, and is scored again, complete, when it
  closes. Nodes are scored in parallel, `RETINA_PERF_WORKERS` processes
  (default 3) in one container;
- `retina-perf-dashboard.service` — `python3 -m http.server` on 127.0.0.1:8765
  serving `RETINA_PERF_ROOT/out`.

Commands (as root on the droplet, `A` an SSH alias on your side):

    systemctl status retina-truth-recorder retina-perf-eval.timer retina-perf-dashboard
    journalctl -u retina-truth-recorder -n 50
    journalctl -u retina-perf-eval -n 200           # the last scheduled runs
    /opt/retina-perf/bin/run_eval.sh                # run now, same as the timer
    /opt/retina-perf/bin/run_eval.sh --window 2026-11-15T15:00     # one window, forced
    /opt/retina-perf/bin/run_eval.sh --backfill-from 2026-11-15    # move the start back
    /opt/retina-perf/bin/run_eval.sh --nodes ret1a2b3c4d,ret9f8e7d6c
    /opt/retina-perf/bin/run_eval.sh --force --max-windows 4
    /opt/retina-perf/bin/run_eval.sh --no-provisional --workers 1
    ls /var/lib/retina-perf/logs/                   # one log per run
    cat /var/lib/retina-perf/state/ledger.json      # every window, status, reason
    ssh -L 8765:127.0.0.1:8765 A                    # then open http://localhost:8765/

Everything under `RETINA_PERF_ROOT` is private: region centres are means of true
receiver positions, and the bucket tables hold aircraft positions. Do not publish
or commit it. The dashboard shows node ids, towers and rates, never a position.

### Layout under `RETINA_PERF_ROOT`

    truth/                      recorder output (see above)
    registry/users.db           registry copy, out of WAL mode (run_eval.sh refreshes it)
    registry/events.json        events log copy
    state/aircraft_db.npz       aircraft type database (services/aircraft_db.py)
    state/fov/<node>/<ms>-<sha>.json   dated copies of the learned field of view
    state/windows/<YYYY-MM-DDTHHMM>/<node>.buckets.parquet    one row per (aircraft, bucket), real and control
                                       <node>.encounters.parquet
                                       <node>.summary.json
                                       WINDOW.json             the window's record
    state/ledger.json           windows processed: status, reason, inputs digest
    out/index.html              the dashboard
    out/fov/<node>.json         P85 detection reach per tier and altitude band, every bin
    out/fov/<node>.bins.parquet the same, one row per node, tier, band and bin
    out/latest.json             fleet figures per span, method constants
    out/history.json            per node, one row per window
    out/nodes/<node>.json       per node: every span's aggregate and the history

### Troubleshooting

- A run exits 1: a node raised; `WINDOW.json` and the ledger hold the error, the
  window is `partial`, the dashboard says so, and the next run retries it.
- `no_truth` everywhere: the recorder is down or rate-limited (`truth/STATUS.json`
  `counters.rate_limited`, `journalctl -u retina-truth-recorder`).
- `node_inactive` for a node that is up: the detection mirror stopped delivering
  its frames, or the hourly archive flush is late (a run before the flush scores
  the window provisionally; it is scored complete once the flush has landed).
- A run takes a few minutes per window for the fleet with two workers; the
  container's memory cap (`RETINA_PERF_MEMORY`) bounds how many nodes can be
  scored at once, so lower `RETINA_PERF_WORKERS` before raising the cap. A busy
  node's full 3 h window takes about 850 MB per worker: three workers under the
  original 2.5 GB cap were OOM-killed once the open window filled up
  (2026-11-15), hence the shipped defaults of two workers and 3 GB.
- The evaluation imports the server code from `RETINA_PERF_BACKEND` inside the
  server image: after a deploy, point it at the checkout that holds these
  modules (`/opt/retina-server/backend` once merged).

## Reading the dashboard

The fleet table gives, per node and span (latest window, 24 h, 7 d, since
start), the validated detection probability with its interval, the raw rate, the
background and the counts. The latest span is the open window when it has been
scored provisionally, and says so; the longer spans hold complete windows only.
A span without enough data shows its status word. The
per-node sections show the trend of three-hour windows (validated, raw, background,
opportunities), the three coverage gates side by side, the figures by class,
type, ground range, slant range, delay, altitude, bearing, bistatic angle and
hour, the data-quality table and the window history with the traffic mix.

Questions it answers: is WXIA detecting a larger share of the aircraft that were
there today than yesterday (history rows, same denominator rule each day); does
WGTV still trail WXIA when scored independently (fleet table, same spans); are
small aircraft detected less often far out (by class and by range, with counts);
is an improvement real or a change of traffic, coverage or completeness (the
class mix, truth share and active share beside every window); are a new node's
tags geographically consistent or random (refuted share, background, and the
tags-in-truth diagnostics).

## Detection reach by aircraft tier and altitude (P85 maps)

The learned field of view (`retina_analytics.empirical_coverage`) is one polygon
per node: per 5° bearing bin, the 85th percentile of the ranges of the node's own
detections, of whatever flew past. `services/performance_truth_fov.py` draws the
same statistic from the independent evaluation instead, and several of them per
node: one per aircraft tier and pressure-altitude band, because a widebody at
cruise and a light aircraft in the pattern do not share a reach.

Per node, tier and band, and per 10° bearing bin from the receiver:

- `p85_det_km`: the 85th percentile of the receiver ground range of the
  validated detections in the bin (a detection whose delay and Doppler fit the
  aircraft's independently predicted geometry, tag or no tag), under the
  geometric gate, so nothing is removed from the denominator for coverage;
- `p95_det_km`: the 95th percentile, which the learned field of view extends its
  reach with (before its 1.25 margin);
- `p85_opp_km`: the same percentile over every opportunity the truth offered in
  the bin, detected or not. Where the detections reach it (`traffic_limited`,
  within 10 %), the traffic ran out before the radar did and the P85 is a lower
  bound on reach, not a measurement of it;
- `pd`: the bin's validated detection probability.

Tiers group the aircraft classes by radar cross-section: heavy (widebody,
military_transport), airliner (narrowbody, regional_jet), business (bizjet,
turboprop), light (light_GA, helicopter), plus "all"; unknown classes count in
"all" only. Altitude bands are 0–5, 5–10, 10–20 and 20–45 kft plus "all", so a
node has 25 maps. A bin is drawn once it holds 10 validated detections from 2
distinct aircraft, pooling its two neighbours first when it has fewer but at
least one of its own (the learned field of view pools the same way); a bin
still short, or with no detection of its own, is closed, not zero. A map is drawn at all only where its validated rate beats its own
chance-association background (3× and 2 points above the rate on the
time-shifted control trajectories): a node that associates by chance would
otherwise draw the traffic's extent as its reach, which is what WNJU and Crystal
Palace did under the old method.

The maps use every bucket table on hand (complete and provisional windows,
within the 30-day retention), since a geometry wants volume rather than
recency, and say how many windows they hold. They are receiver-relative
(bearing, range) and carry no position, so `out/fov/` can be published where
the learned state, which embeds the receiver's true position and raw aircraft
positions, cannot. Files: `out/fov/<node>.json` (every map and bin, with the
counts) and `out/fov/<node>.bins.parquet` (one row per node, tier, band and
bin). `perf_eval fov --perf-root … [--nodes …] [--all-maps]` prints them.

Reading them: a sector is a drawn bin, its radius the P85 reach; lighter sectors
were pooled; an amber outer arc marks a traffic-limited bin; the dashed outline
is where the traffic was; "chance level" is a map with detections at the
background rate. Compare maps on one node to see reach by tier and altitude
(for WXIA on the first evening: 30–40 km median P85 for every tier below 20 kft,
with the far side traffic-limited, and a tenth of the rate above 20 kft) and the
same map across nodes to compare receivers. The maps are not an association
gate: the evaluation keeps its geometric denominator. They could feed a
tier- and altitude-aware gate for the solver later; that is a product decision.

## Aircraft classes

`config/aircraft_classes.py` is kept for compatibility. Its anomalies: the
`military_transport` class mixes transports and fighters; King Air, TBM and
Cheyenne types are in `light_GA`, not `turboprop`; types the database does not
know are `unknown`.

## Limitations and open items

- No independent truth exists for the Atlanta nodes before the recorder started
  (2026-11-15 15:41 UTC); earlier windows show `no_truth`. A backfill would need
  the production archive's aggregator rows (production runs the server's ADSBHub
  poller; its ADS-B store is not reachable from the test droplet).
- Node liveness is read from archived frames (only frames with detections are
  archived) and the server's offline events; a persisted heartbeat log would be
  stricter. A node that is up but detects nothing for over 60 s reads as inactive
  for those buckets, which can only lower the denominator, never inflate the rate.
- The recorder's ground truth is cooperative traffic only, as all ADS-B truth is.
- The P85 maps inherit the traffic: a bin whose detections reach as far as the
  traffic did (most of WXIA's on the first evening) only bounds the reach from
  below, and the high bands and heavy tier fill slowly. They are drawn from
  validated detections, which include the chance matches; on a node whose rate
  is a few times its background the far tail of a map is partly chance.
- The probit/Tobit models of PR #727 are not fitted here; the empirical figures
  come first. Fitting them on the validated numerator and this denominator is the
  next step once a week of windows exists.
- Doppler validation uses the interpolated velocity; during turns the predicted
  Doppler can err by several Hz, which the 5 Hz tolerance does not always
  absorb; a turning aircraft can go unvalidated for a few buckets.
