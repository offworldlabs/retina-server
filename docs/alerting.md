# Alerting & monitoring

How RETINA detects problems and notifies operators. The goal is pre-launch
coverage with **no infrastructure we have to run ourselves**: alerting is
in-process, plus outside-in probes and notifications from the platforms
already in use (layer 3).

## How it works

Three layers, in order of what they catch:

1. **Health monitor (in-process).** `services/tasks/health_monitor.py` runs
   every `HEALTH_MONITOR_INTERVAL_S` (default 30s). It evaluates the shared
   checks in `services/health.py` and fires a webhook alert per issue. This is
   independent of who calls `/api/health` — the server alerts on its own
   schedule. Catches: degraded-but-running conditions (stale tasks, queue
   saturation, disk/memory pressure, solver accuracy, node dropout, etc.).

2. **Webhook delivery.** `services/alerting.py` POSTs to `ALERT_WEBHOOK_URL`.
   `ALERT_WEBHOOK_FORMAT` selects the body shape: `raw` (default) sends a
   plain JSON payload (`alert_type`, `message`, `timestamp`, `environment`,
   `host`, `meta`), for a Slack/Discord/PagerDuty incoming webhook;
   `clickup_chat` sends `{"type": "message", "content": "<markdown>"}`, for
   ClickUp's chat message endpoint, with the rendered content carrying the
   bold `alert_type`, the message, an `environment: <value>` line, a
   `host: <value>` line, then one `key: value` line per `meta` entry. Both
   shapes carry `environment` (from `ALERT_ENVIRONMENT`, or the literal
   `unknown` when unset or empty) and `host` (from `socket.gethostname()`, set
   by the `hostname:` each droplet overlay gives its `server` service, or
   `unknown` if that call fails or returns empty), because each droplet's
   `ALERT_WEBHOOK_URL` points at its own channel: channel routing is
   configuration, and a misrouted URL would otherwise put an alert in the
   wrong channel with nothing in the payload to reveal that.
   `ALERT_ENVIRONMENT` is deliberately its own setting rather than `RETINA_ENV`:
   that variable selects which backend guards apply, and staging and test both
   hold it at `test` for the build-out's auth-guard workaround (ClickUp
   86cb1emcx), so a field sourced from it could not tell those two apart, and
   would move whenever a guard decision did. The ClickUp branch exists because ClickUp has no inbound
   webhook of its own (its webhooks are outbound only), so reaching a
   ClickUp chat channel needs a shaped body and an `Authorization` header
   rather than a plain POST URL. `ALERT_WEBHOOK_AUTH`, when set, is sent
   verbatim as that header (no `Bearer` prefix: ClickUp personal tokens
   carry none). ClickUp documents the chat endpoint as experimental, so the
   server logs its alert destination (scheme and host only) at startup, as a
   trail back to the dependency if the endpoint ever breaks. That line is
   logged at INFO, which no deployed stack currently emits (ClickUp
   123zgec374r), so on a droplet it is not there to read. Alerts are
   deduplicated per `alert_type` with a `ALERT_COOLDOWN_S` cooldown, so an
   ongoing problem re-notifies at most once per window. The default is 300s;
   see below for what each deployed environment actually runs. A
   `resolved:<type>` alert is sent once when a condition clears.

   Delivery is retried up to three times, with jittered exponential backoff
   between attempts. The retry exists because ClickUp's chat API returns
   intermittent 500s (roughly one delivery in three, measured on production,
   with no 429s), which the health monitor survives, its conditions being
   still true at the next cycle, but `mender_unreachable` and
   `registration_held` do not, since both fire once at the moment they
   matter.

   What may be retried follows the same three-way split this repo hands its
   own nodes as `x-retry` (see `routes/node_responses.py`):

   | Outcome | Treatment |
   |---|---|
   | 2xx | Delivered |
   | 3xx | Terminal. Redirects are not followed, since 301/302/303 turn the POST into a GET and would deliver nothing, so a redirecting URL means the alert went nowhere |
   | 5xx, transport error | Retry with backoff: the request never reached a handler that made a decision |
   | 429, 408 | Retry, honouring `Retry-After` over the backoff when it asks for longer (delta-seconds only, capped at `_MAX_RETRY_AFTER_S`) |
   | any other 4xx | Terminal. The sink has decided about this token (401) or this body (400), so resending multiplies one failure into three |

   `Retry-After` is honoured on any retriable answer, not only the 4xx pair:
   a 503 in a maintenance window carries it as readily as a 429 does.

   Only one delivery per `alert_type` runs at a time. The cooldown
   reservation alone cannot guarantee that, because a retry sequence can
   outlast a short `ALERT_COOLDOWN_S`, so an in-flight marker does it
   directly: the next health cycle will not open a second delivery of an
   alert that is still being retried.

   What happens to the reservation afterwards depends on why the delivery
   failed. A terminal 4xx or 3xx keeps it: nothing will change until an
   operator acts, and re-reporting every cycle only floods the channel. An
   exhausted retriable failure replaces it with an explicit deadline
   `_FAILURE_REOPEN_S` out, since the sink may be back well before the full
   window is out, but a dropped alert must not buy a full `ALERT_COOLDOWN_S`
   of silence either. That constant is sized so a failing sink is never
   asked to carry more than it did before retries existed: three attempts
   per window against one per `HEALTH_MONITOR_INTERVAL_S` before, so the
   window has to cover a whole sequence of attempts.

   A delivery that fails every attempt, and any terminal answer, is logged at
   `error`; the individual retriable failures are logged at `warning`. All
   of them keep the `Alert webhook returned <code>` wording, which is what
   the droplet logs are grepped for when counting delivery failures.

3. **Outside-in probes (external).** DigitalOcean Uptime polls `/api/health` on `api`
   (this backend) and on `towers` (tower-finder-service's own edge) for prod and
   staging, every minute from four regions, and emails when every region has seen the
   origin down for two minutes. Together with the DigitalOcean resource alerts and
   Cloudflare's origin and certificate notifications, this is the layer that catches a
   crashed process, a dead host or a broken edge path, which in-process alerting cannot.
   What is configured, where to look and how to rebuild it:
   `claude-shared/docs/runbooks/uptime-monitoring.md`.

   `services/tasks/heartbeat.py` remains and is dormant: it pings `HEARTBEAT_URL` when
   that is set, and nothing sets it. An outside probe covers what a heartbeat would,
   except a dead alert loop behind a live HTTP server, which `health_monitor_task`'s
   per-cycle exception handling makes unlikely.

## Severity

Each issue carries a severity in the alert payload's `meta`:

- **critical** — output is down or about to be: `stale_task:*`,
  `frame_queue_saturated`, `disk_low`, `memory_high`, `node_dropout`,
  `no_active_tracks`.
- **warning** — degraded but serving: `solver_queue_drops`,
  `solver_queue_high`, `solver_latency_high`, `coverage_rebuild_backlog`,
  `anomaly_flood`, `solver_accuracy_degraded`, `high_miss_rate`,
  `frame_starvation:*`.

Route critical → a paging channel and warning → a quieter channel in your
webhook receiver (e.g. Slack workflow rules).

## Thresholds

Most are constants in `services/health.py`. Five are settings, because the
right value depends on the box: `NODE_DROPOUT_THRESHOLD` (default 0.8),
`HIGH_MISS_RATE_THRESHOLD` (default 0.98), `SOLVER_QUEUE_DROP_WINDOW_S`
(default 300, how recent a solver-queue drop must be to count),
`COVERAGE_BACKLOG_MAX_WAIT_S` (default 1200, the longest a grid rebuild may
wait behind the per-cycle budget; must clear the post-deploy warm-up, see the
runbook) and `FRAME_STARVATION_S` (default 900, how long a node that is heard
from may go without filing a frame). All are read per call and fall back to their default on a value that
does not parse, so a stray entry degrades one check rather than stopping the
server booting.

`high_miss_rate` needs care when reading it. The rate counts ADS-B aircraft
inside a node's theoretical beam wedge that the node's tracker did not
detect, and for passive bistatic radar that wedge is a much larger set than
what is physically detectable, so the rate has a high floor set by siting and
physics rather than by health. Production reports 72-94% when it is working.
The threshold sits above that band so the check is a tripwire for a network
that has genuinely gone blind, rather than a running commentary; it is a
stopgap, and replacing the measure with one that tracks a node against its
own history is tracked in ClickUp 86cb81gkn.

## Health endpoint

`GET /api/health`

- Default: always **200**. Body `{"status": "ok"}` or `{"status":
  "degraded"}`. Used as the Docker container **liveness** check — it must not
  flip to non-200 on transient degradation or the container would restart-loop.
- `?strict=1`: **readiness** probe — returns **503** when degraded. Not what the outside
  probes use: they assert liveness, because the strict form also trips on warnings that
  are still being calibrated (ClickUp 86cb5c8dq).

Details are intentionally **not** exposed on this unauthenticated endpoint —
they're in the logs and the webhook payloads.

## Setup checklist (no servers to run)

1. Pick a destination for `ALERT_WEBHOOK_URL`:
   - Slack/Discord/PagerDuty incoming webhook → set `ALERT_WEBHOOK_URL` to it
     and leave `ALERT_WEBHOOK_FORMAT` at its `raw` default.
   - ClickUp chat channel → create a personal token, set `ALERT_WEBHOOK_AUTH`
     to it, set `ALERT_WEBHOOK_FORMAT=clickup_chat`, and set
     `ALERT_WEBHOOK_URL` to
     `https://api.clickup.com/api/v3/workspaces/{workspace_id}/chat/channels/{channel_id}/messages`.
2. Outside-in probes, resource alerts and Cloudflare notifications are account-level
   configuration, not environment variables: see
   `claude-shared/docs/runbooks/uptime-monitoring.md`.

3. On a deployed stack the settings split two ways, and "Set by" below says
   which way each one goes. Everything that is safe to read in a public repo
   lives in `docker-compose.{prod,staging,test}.yml`, so the configuration in
   force can be read from the repository and survives a rebuild. The
   destination and its credential live only in the host's `backend/.env`, which
   compose loads via `env_file`; where both name a variable, the overlay's
   `environment` wins.

| Env var | Default | Set by | Purpose |
| --- | --- | --- | --- |
| `ALERT_WEBHOOK_URL` | _(unset → disabled)_ | host `backend/.env` | Where alerts are POSTed |
| `ALERT_COOLDOWN_S` | `300` | compose overlay | Per-alert-type re-notify cooldown |
| `ALERT_WEBHOOK_AUTH` | _(unset)_ | host `backend/.env` | Sent verbatim as the `Authorization` header when set |
| `ALERT_WEBHOOK_FORMAT` | `raw` | compose overlay | Payload shape: `raw` or `clickup_chat` |
| `ALERT_ENVIRONMENT` | _(unset → `unknown`)_ | compose overlay | Labels each alert's `environment` field |
| `HEARTBEAT_URL` | _(unset → disabled)_ | — | Dormant: set by no environment (see layer 3) |
| `HEARTBEAT_INTERVAL_S` | `60` | — | Heartbeat ping period |
| `HEALTH_MONITOR_INTERVAL_S` | `30` | — | Health evaluation period |
| `NODE_DROPOUT_THRESHOLD` | `0.8` | — | Active/peak node ratio below which dropout fires |
| `HIGH_MISS_RATE_THRESHOLD` | `0.98` | — | Fleet-average miss rate above which `high_miss_rate` fires |
| `FRAME_STARVATION_S` | `900` | — | Seconds a heard-from node may file no frame before `frame_starvation:<node_id>` fires |

Every deployed environment currently holds `ALERT_COOLDOWN_S` at `3600` rather
than the `300` default. Alerts post with a personal ClickUp token whose rate
limit is shared with interactive use of ClickUp, and several checks still fire
on conditions nobody would act on, so the cooldown is doing duty as a volume
control until those are calibrated (ClickUp 86cb5c8dq).

`deploy/start.sh` refuses to boot a `RETINA_ENV=production` stack that is
missing either `ALERT_WEBHOOK_URL` or `ALERT_WEBHOOK_AUTH`; staging and test
log a warning instead. See `docs/runbook.md` for restoring them on a fresh box.

## Deferred (needs real infrastructure)

Metrics history and dashboards (VictoriaMetrics + Grafana, Loki for logs, Sentry for
exceptions) are the third monitoring sub-project in
`claude-shared/docs/decisions/2026-09-15-uptime-monitoring.md` and are not yet ticketed.
The admin dashboard's Infrastructure page shows DigitalOcean's uptime and droplet metrics
meanwhile.
