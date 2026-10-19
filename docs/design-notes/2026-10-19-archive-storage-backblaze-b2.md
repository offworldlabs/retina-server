# Archive storage: move object storage from Cloudflare R2 to Backblaze B2

Date: 2026-10-19. Branch `claude/determined-fermi-bpg9eh`. Status: design, awaiting review.

## Confirmation of the current state

Archive data does go to Cloudflare R2. One module, `backend/services/r2_client.py`,
wraps a boto3 S3 client pointed at `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`
and is the only path from this backend to object storage. Everything below rides
on it; nothing else in the repo talks to R2 (no Workers, no CLI, no compose
service).

| Caller | What it stores | Key prefix | Operations used |
|---|---|---|---|
| `services/tasks/archive_lifecycle.py` | Detection archive Parquet (`coverage_data/archive/`) and solver track Parquet (`coverage_data/tracks/`), uploaded 1 day after creation, local copy deleted at 3 days once the `.uploaded` sentinel exists | `archive/`, `tracks/` | `upload_file` |
| `services/tasks/users_backup.py` | Daily `users.db` snapshot, 30-day retention enforced by listing and bulk-deleting | `backups/users-db/` | `upload_file`, `list_keys`, `delete_keys` |
| `services/state_snapshot.py` | The 60 s state snapshot, replicated for restore after a container recreate; restore falls back to it when the local copy is missing or corrupt | `snapshots/state_snapshot.json` | `upload_file`, `download_bytes` |
| `scripts/backfill_archive_fuzz.py` | One-off operator script that rewrites archived Parquet in place | `archive/` | `list_keys`, `download_bytes`, `upload_bytes` |

Configuration: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET` (default `retina-data`), optional `R2_ENDPOINT_URL`, all read from
`backend/.env` at import time. Unset, every operation is a no-op and the
`users_backup` task logs a warning. `deploy/setup-server.sh` does not write
these; they are added to a live box by hand.

Observability: `r2_replication_failed` alert (runbook) fires when the state
snapshot upload fails. Task health rows `archive_lifecycle` and `users_backup`
cover the other two.

Tests: `tests/test_r2_client.py` uses moto, which intercepts boto3 in-process
regardless of endpoint. `test_archive_lifecycle.py`, `test_users_backup.py` and
`test_state_snapshot_r2.py` monkeypatch the client functions. None of them
depend on R2 specifically.

Docs that mention R2: `docs/architecture.md` (which names the bucket
`retina-server-archive`, disagreeing with the code default `retina-data`; the
live value is whatever `backend/.env` says), `docs/runbook.md`, `backend/.env.example`,
`deploy/setup-server.sh`, and docstrings in the four callers above.

## Why move

Backblaze B2 storage is priced lower than R2 per GB-month. Both offer an
S3-compatible API, and this backend's usage is write-heavy with almost no reads:
one upload per archive file, one snapshot upload per minute, one listing per day.
Egress is near zero (the snapshot restore path and the backfill script are the
only readers), so R2's free egress buys nothing here.

Indicative list prices, to be re-checked against both vendors' pricing pages
before the decision is final:

| | Cloudflare R2 | Backblaze B2 |
|---|---|---|
| Storage | $0.015 / GB-month | $0.006 / GB-month |
| Writes (PUT, LIST, DELETE) | $4.50 per million after 1 M free / month | Free |
| Reads (GET) | $0.36 per million after 10 M free / month | 2,500 free / day, then $0.004 per 10,000 |
| Egress | Free | Free up to 3× average stored volume / month, then $0.01 / GB |

Operation counts for this workload are small. The state snapshot is the largest:
about 43,000 PUTs a month, within R2's free tier and free on B2. The saving is
the storage line, so it scales with how much archive we hold. **Before deciding,
record the current R2 bucket size and monthly growth** (Cloudflare dashboard, or
`rclone size` with the R2 remote); that number turns the ratio above into a
dollar figure. The archive is retained indefinitely, so it only grows.

## What B2 changes in the client

B2's S3 endpoint is `https://s3.<region>.backblazeb2.com`, where `<region>` is
the bucket's region (for example `us-west-004`) and must also be passed as the
boto3 `region_name`. The current client derives the endpoint from an account id
and hardcodes `region_name="auto"` when an endpoint is set. Two things therefore
have to change in `r2_client.py`:

1. **Endpoint is configured directly**, not derived from an account id.
2. **Region is configurable.** Keep `auto` only as the default for an R2-shaped
   endpoint; for B2 the region is required.

Everything else the client uses (`put_object`, `upload_file`, `get_object`,
`list_objects_v2` paginator, `delete_object`, `delete_objects` with `Quiet`,
`NoSuchKey`) is in B2's supported S3 subset. Two B2 differences to keep in mind
and cover in the smoke test:

- B2 application keys may be bucket-restricted. Create the key scoped to the one
  bucket so a leaked key cannot list other buckets.
- `delete_objects` on B2 deletes the current version; with bucket versioning
  (B2's default "keep all versions") the old versions remain and are still
  billed. Create the bucket with lifecycle "keep only the last version" so the
  `users_backup` retention sweep actually frees space.

## Proposed configuration

Rename the env keys to be provider-neutral, since the module will no longer be
R2-specific, and keep reading the old names for one release so a live `.env` does
not break on deploy:

| New key | Old key read as fallback | Notes |
|---|---|---|
| `OBJECT_STORE_ENDPOINT_URL` | `R2_ENDPOINT_URL`, else derived from `R2_ACCOUNT_ID` | Required for B2 |
| `OBJECT_STORE_REGION` | none (defaults `auto`) | `us-west-004` or whatever the bucket reports |
| `OBJECT_STORE_ACCESS_KEY_ID` | `R2_ACCESS_KEY_ID` | B2 keyID |
| `OBJECT_STORE_SECRET_ACCESS_KEY` | `R2_SECRET_ACCESS_KEY` | B2 applicationKey |
| `OBJECT_STORE_BUCKET` | `R2_BUCKET` | default stays `retina-data` |

The module itself is renamed `services/object_store.py`; `services/r2_client.py`
becomes a one-line re-export shim for the same release and is removed after.
The alert name `r2_replication_failed` is kept unchanged so dashboards and the
runbook entry keep matching; its message text drops "R2".

If the rename is judged too much churn, the minimum viable change is adding
`R2_REGION` and reading `R2_ENDPOINT_URL` ahead of the account id. The rest of
this note still applies.

## Migration of existing data

The archive and backups are append-only and nothing reads them back except the
operator, so they can move lazily. The state snapshot is the one object whose
absence at restore time matters.

Order of work on each environment (test, then staging, then production):

1. Create the B2 bucket (private, versioning off or "keep last version only")
   and a bucket-scoped application key.
2. **Copy the whole R2 bucket to B2** with `rclone sync` from an operator
   machine, R2 remote to B2 remote. Both are S3 remotes to rclone; no code is
   involved. Run it once ahead of the cutover and once more immediately after
   step 4 to pick up what landed in between.
3. Set the new keys in `backend/.env` on the droplet (by hand, as today; this
   file is not managed by `setup-server.sh`). Leave the old `R2_*` keys in place
   until step 6 so a rollback is an edit, not a re-entry.
4. Deploy. Watch the first `archive_lifecycle` cycle (hourly), the first
   `users_backup` (daily, or trigger it), and the `r2_replication_failed` alert
   staying quiet for the state snapshot.
5. Run the final `rclone sync` so B2 holds everything R2 did.
6. After two weeks with no alert and the B2 bucket size matching expectation,
   remove the `R2_*` keys from `.env`, delete the R2 bucket, and delete the
   R2 API token.

Rollback at any point before step 6: restore the `R2_*` keys, remove the new
ones, redeploy. The lifecycle sentinels are local files, so anything uploaded to
B2 only will be re-uploaded to R2 only if its sentinel is removed; in practice
the final `rclone sync` reversed covers it.

## Verification

- `just check` passes with the moto tests unchanged; add one test asserting the
  region and endpoint are passed through to boto3 when configured.
- On the test droplet, with real B2 credentials: write a 1 KB object through
  `upload_bytes`, read it back with `download_bytes`, list it, bulk-delete it,
  and confirm B2's console shows no lingering version.
- `just verify-deploy <env>` before and after, as the CLAUDE.md rule requires.
  It cannot see the object store, so also run `tst /api/test/...` task health
  on the droplet and check the three task rows.
- Confirm snapshot restore: stop the container, move
  `state_snapshot.json` aside, start, and check the log line about restoring
  from the remote copy.

## Files touched

- `backend/services/r2_client.py` → `backend/services/object_store.py` (+ shim)
- `backend/services/tasks/archive_lifecycle.py`, `users_backup.py`,
  `backend/services/state_snapshot.py`, `backend/scripts/backfill_archive_fuzz.py`:
  import path and docstrings only
- `backend/services/tasks/periodic.py`: docstrings say "R2" and in one place
  "B2" already; make both say object storage
- `backend/tests/test_r2_client.py` → `test_object_store.py`, new env names
- `backend/.env.example`: new keys, B2 wording
- `backend/config/constants.py`: comments only
- `docs/architecture.md`, `docs/runbook.md`, `deploy/setup-server.sh`: wording;
  fix the `retina-server-archive` / `retina-data` bucket-name disagreement while
  there
- No dependency change: boto3 stays, `uv.lock` does not move. No change under
  `/v1/nodes`, so the OpenAPI contract does not move.

## Open questions

1. Current R2 bucket size and growth rate (needed for the dollar saving).
2. Which B2 region; pick the one closest to the production droplet.
3. Keep the provider-neutral rename, or take the minimum `R2_REGION` change?
4. Does anything outside this repo read the R2 bucket (an analysis notebook, the
   tower-finder service, a Cloudflare Worker)? Nothing in this repo does, but
   claude-shared should be checked before the bucket is deleted in step 6.
