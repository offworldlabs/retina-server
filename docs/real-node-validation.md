# Real-node validation and blind MLAT replay

Real receivers need their own reference data and evaluation denominators. The
test fleet may replay the same aircraft identities as the physical receivers;
an aircraft identity does not make their measurements interchangeable.

## Reference sources and clocks

Set `ADSB_SERVICE_URL` to an HTTPS readsb-compatible service to enable regional
polling for connected real nodes. An unset URL disables this poller. The existing
external-source fallback and node-carried ADS-B continue to work. Index-aligned
`adsb_hex` labels are resolved against a fresh reference; an identity alone does
not become a position. The detection archive retains these labels.

The readsb adapter accepts second or millisecond envelope clocks and subtracts
`seen_pos` from that upstream clock. Fetching the same observation again cannot
refresh its age. MLAT and TIS-B positions are excluded from validation references,
and absent altitude or velocity does not become a zero-valued measurement.
External-source eligibility is retained through the fallback adapters.

Real node-carried positions follow the same eligibility rules, preserve an
explicit position timestamp, and support the node API's legacy `alt` field in
feet. Missing altitude or velocity remains missing. Older tags without their
own position clock are marked as assuming the frame's time. Capture retains
source type and clock/altitude provenance; replay reports source-type counts.
An SBS `type=other` position is a coarse external reference, not a verified
direct ADS-B or precision GNSS observation.

These feeds can include upstream latency and barometric altitude. They support
coarse validation but are marked ineligible for precision truth. See the source
contracts for [readsb](https://github.com/wiedehopf/readsb/blob/dev/README-json.md)
and [OpenSky](https://openskynetwork.github.io/opensky-api/rest.html).

`/api/test/solver-stats` includes `by_world` attempt/publish/scoring funnels for
real, simulated, mixed and unknown provenance. The known lane uses ADS-B position,
altitude and velocity as priors, so its publish rate is **assisted**, not a blind
MLAT result. Mixed-world claims are withheld. Anonymous real solves are not
scored against nearby synthetic trails.

Reference normalization is shared across a frame's tagged detections, held
tracks and candidate assignment. It must not scan the fleet cache separately
for every detection; this becomes a significant ingest cost with real traffic.
Source pollers prepare normalized records once when writing their caches.
Reputation evaluation similarly reuses one trust score per node per pass,
rather than scanning the residual history again for every neighbor.

## Private capture

`REAL_DATA_CAPTURE=1` enables bounded capture under
`backend/data/runtime/real-validation/`. It records allowlisted real radar
measurements, resolved receiver/transmitter geometry and reference observations.
The directory is mode 0700 and new files are mode 0600. This is private material:
do not move it into the public coverage archive or commit it to the repository.

Capture uses a bounded queue, asynchronous disk writes and a 512 MiB total
budget. `REAL_DATA_CAPTURE_MAX_MIB` can set a total budget of 1–4096 MiB,
including files from earlier runs. Reaching the budget stops capture.
Explicitly invalid signatures are excluded, and changed truth is saved even
on ticks without radar frames. The test dashboard exposes enabled
state, bytes, frames, errors, drops and queue depth. Observe these counters and
the main ingest queue when running offline experiments on the server host.

Copy or select a completed capture interval before comparing configurations.
Use the same frame interval for each configuration, retain the parameter/source
hashes in the report, and reserve a later interval for validation.

## Blind evaluation

From `backend/` with the project libraries installed:

```bash
python -m scripts.blind_replay /private/cohort.jsonl \
  --output /private/baseline.json --records-output /private/baseline-records.json

python -m scripts.blind_replay /private/cohort.jsonl \
  --sigma-delay 0.8 --history-size 40 --unknown-beam omni \
  --output /private/experiment.json --records-output /private/experiment-records.json
```

The second command is an experiment, not a recommended deployment default.
`--unknown-beam omni` removes a narrow inherited beam assumption only when the
capture contains no declared width. It does not assert that the real antenna
has uniform sensitivity in every direction.

The estimator API receives only radar frames and geometry. It strips ADS-B
positions and identities before tracking, disables ADS-B seeding and claiming,
uses radar-derived guesses, and fits free altitude from several fixed starts.
No truth object is passed into this stage. Truth indexing and detection-label
joins happen after the estimator returns.

The evaluator first joins exact captured measurements back to node labels.
Disagreeing labels are recorded as identity conflicts, including how many would
have passed the numerical gates. Otherwise it uses a unique measurement-space
match when exact multi-node labels are unavailable. It never chooses the
aircraft nearest the solved position. Stale, missing and ambiguous references
remain separate; failed solves remain in the eligible denominator.

Reports include convergence, acceptance, position/altitude error, node counts,
association pool size, and an aircraft-minute funnel for identities detected by
at least two nodes within one five-second bin. This is conditional on captured,
tagged detections; it is not airspace-wide recall. Repeated attempts are not
independent aircraft trials.

Optimizer convergence is distinct from a returned numerical result. The pinned
geolocator exposes termination status, evaluation count, altitude saturation,
Jacobian rank and local horizontal uncertainty. `--max-nfev` tests additional
optimization effort; `--max-horizontal-sigma` can withhold poorly constrained
fits without using truth. Local uncertainty does not resolve global branch
ambiguity or unknown calibration errors. A low residual alone is insufficient
evidence for publication, especially with two nearly redundant bistatic paths.

`--exclusive-tracks` tests a greedy, residual-ordered selection that prevents
one node track from being accepted in multiple hypotheses during the same
association round. `--altitude-model layers` tests level flight at fixed 3, 7
and 11 km layers; those layers are independent of the target's ADS-B altitude.
Neither experiment establishes a correct identity by itself. Report horizontal
and vertical error and accepted identity conflicts for each.

## Node residuals and observed detection area

```bash
python -m scripts.reference_report /private/training.jsonl \
  --output /private/node-reference-report.json
```

This reports delay/Doppler residual distributions, delivery lag, reference
availability, independent aircraft count, and agreement with declared or
inherited beam geometry. Observed cells are 15 degrees in azimuth, 5 km in range,
and 1 km in altitude. Only identity-tagged detections agreeing with fresh
references contribute to the cells; rejected samples remain in the residual
diagnostics. An empty cell means no qualifying observation was captured.

The report proposes bounded per-node bias corrections only after sufficient
samples from several aircraft. Test them on a **later** capture:

```bash
python -m scripts.blind_replay /private/later-validation.jsonl \
  --calibration /private/node-reference-report.json \
  --output /private/calibrated-validation.json
```

The replay refuses overlap between calibration and evaluation intervals. This
is a blind solve with frozen, previously learned calibration, and should be
reported separately from an uncalibrated baseline. Do not tune acceptance gates
on the final evaluation interval or deploy settings based on acceptance rate
without checking errors and identity conflicts.

## Sustained-load checks

Monitor queue depth and delivery age alongside solve rates. The frame drop
counter includes queue rejections on TCP, v1 and both HTTP ingestion routes;
for bulk HTTP it counts every discarded timestamped frame after saturation.
It does not measure losses upstream or during a server restart.

`SINGLE_NODE_GEO_INTERVAL_S` defaults to 10 seconds. A value such as 20 reduces
single-node nonlinear fit frequency under CPU pressure, while tracking,
reference refresh and MLAT retain their existing cadence. Check the queue over
a sustained mixed real/synthetic load before treating a deployment as stable.
