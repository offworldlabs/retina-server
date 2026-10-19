# Testing retina-custody against retina-server before nodes carry it

**Status:** Design, awaiting review. Nothing here is implemented.

**Goal.** Run the windowed hash chain that `retina-custody` `main` now builds end
to end against a running `retina-server`: a stand-in node builds and signs
entries, the server verifies them, stores the result, and reports it. The real
receiver nodes are not touched. When this works on the test droplet, the same
server surface is what the nodes will post to.

**Non-goals.** No change to the generated `/v1/nodes` contract. No IQ capture
work. No archive of detection batches on the server. No TSA or OpenTimestamps
validation on the server. No `retina-custody-server` (see the last section for
what that would be).

Writing conventions follow the repo: file and symbol references, no line numbers.

## 1. Where things stand

Three repos are involved and they disagree about what custody is.

| Repo | Pinned to | What it implements |
| --- | --- | --- |
| `retina-custody` `main` (`294c0b0`) | n/a | Windowed hash chain. `HashChainBuilder.add_detection(frame: dict)`, `close_if_due()`, `close_window()` return `(HashChainEntry, batch: bytes)`. `HashChainVerifier.verify_entry(entry, batch, expected_prev_hash)`. Entry fields `window_start`, `window_end`, `detections_hash`. No per-frame signing: `packet_signer.py`, `PacketSigner`, `SignedPacket` and `SignatureVerifier` are gone. |
| `retina-server` `libs/retina-custody` | `46e3a4a` (the initial extraction, 11 commits behind) | Hourly chain over per-frame `payload_hash`es. The server imports `SignatureVerifier` in `core/state.py`, verifies per-frame `payload_hash` + `signature` in `services/frame_processor.py`, and expects `hour_utc` on chain entries in `routes/custody.py` and `services/tcp_handler.py`. |
| `retina-simulation` `c62f6e4` (also a submodule here) | depends on `retina-custody>=0.1.0` | `retina_simulation/node.py` (`_stream_tcp`, the multi-node runner) uses `PacketSigner.sign_frame` and `HashChainBuilder.add_detection(payload_hash)` / `close_hour()`. The fleet orchestrator that the deployed fleet container runs (`retina_simulation/orchestrator.py`, `NodeConnection`) does not use custody at all. |

Consequences:

- Bumping the submodule to `main` breaks the server at import time
  (`SignatureVerifier`) and breaks `retina-simulation`'s `node.py` CLI modes.
  The deployed fleet is unaffected because the orchestrator never signed.
- In staging and production nothing registers a key or sends a chain entry
  today. `/api/test/dashboard`'s `chain_of_custody` block reads `waiting`
  whenever any node is connected, and `/api/custody/status` is empty.
- The server's own `backend/tests/test_custody.py` is a copy of the library's
  unit tests for the old API. It duplicates what `retina-custody/tests` already
  covers and will fail wholesale on the bump.
- Chain entries live in `state.chain_entries` (in-memory, rolling cap
  `CHAIN_ENTRIES_MAX_PER_NODE`) and are carried across restarts by
  `services/state_snapshot.py`. There is no on-disk store, and no batch is kept.

Decisions already taken (from review of this plan's questions):

1. The node stand-in is a standalone script in `retina-custody`, not a
   server-side shadow chainer and not the fleet orchestrator.
2. Transport is the existing TCP `CHAIN_ENTRY` message and the legacy
   `POST /api/custody/chain-entry` route, both updated to the windowed shape and
   given a `batch` field. The v1 node contract is untouched.
3. The server verifies the batch on receipt and keeps only the entry plus the
   verdict. The batch is discarded.
4. Per-frame signature verification is removed from the server. The parquet
   columns `payload_hash`, `signature`, `signing_mode`, `signature_valid` stay,
   nullable; `blah2_poller`'s `signing_mode = "polled"` keeps flowing.
   `retina-simulation`'s `node.py` is updated to the new API in the same effort.

## 2. Wire shape

One message type carries one closed window. Both transports take the same body.

```json
{
  "node_id": "cust-demo-01",
  "entry": {
    "node_id": "cust-demo-01",
    "window_start": "2026-10-19T14:00:00Z",
    "window_end": "2026-10-19T14:15:00Z",
    "prev_hash": "genesis",
    "detections_hash": "<sha256 hex>",
    "n_detections": 1793,
    "node_config_hash": "<sha256 hex>",
    "firmware_version": "1.0.0-synthetic",
    "timestamp_utc": "2026-10-19T14:15:00.412Z",
    "entry_hash": "<sha256 hex>",
    "signature": "<hex ECDSA>",
    "signing_mode": "software",
    "tsa_token": "<base64, optional>",
    "ots_proof": "<base64, optional>"
  },
  "batch": "[{\"delay\":[...],\"doppler\":[...],...},...]"
}
```

- `entry` is exactly `HashChainEntry.to_dict()`. The server parses it with
  `HashChainEntry.from_dict`, which ignores unknown keys and raises on missing
  required ones.
- `batch` is the canonical JSON array of the window's frames, as a JSON string.
  `canonicalize()` emits ASCII with no whitespace, so a string round trip
  through JSON is byte-exact after `.encode("utf-8")`. The server never
  re-serialises the frames; it hashes the string's bytes. `batch` is optional:
  without it the server still checks hash, signature and linkage and records
  `batch_verified: null`.
- TCP: `{"type": "CHAIN_ENTRY", "node_id": ..., "entry": {...}, "batch": "..."}`,
  acknowledged by `CHAIN_ENTRY_ACK` carrying `entry_hash`, `verified`, `reason`.
  Note `tcp_handler._MAX_BUF_SIZE` is 1 MB per line. A 15-minute window at 2
  frames per second with ADS-B tags can exceed that. The test measures the real
  batch size (section 6); if it is near the cap, the script uses HTTP, which is
  the path real nodes will use anyway. Raising the TCP cap is out of scope.
- HTTP: `POST /api/custody/chain-entry`, `X-API-Key: <RADAR_API_KEY>`, same
  body. Response `{status, node_id, entry_hash, verified, reason, batch_verified}`.
- `POST /api/custody/register` is unchanged. `fingerprint`, `serial_number` and
  `signing_mode` come from `SoftwareCryptoBackend`.

## 3. Server changes (`retina-server`)

### 3.1 Submodule and lock

- `git -C libs/retina-custody checkout 294c0b0` (current `main`), commit the
  pointer. `retina-custody`'s only dependency (`cryptography>=46.0`) is
  unchanged between the two commits, so `backend/uv.lock` should not need to
  move; confirm with `uv sync --locked` from `backend/` using the `uv` version
  the `Dockerfile` pins, and relock only if it refuses.
- `retina-simulation` declares `retina-custody>=0.1.0`; both commits are
  `0.1.0`, so its resolution is unaffected.

### 3.2 Key registry (`core/state.py`)

Replace `sig_verifier = SignatureVerifier()` with nothing: `node_identities`
already holds `public_key_pem` per node. Add one helper in `routes/custody.py`
(or a tiny `services/custody.py`):

```python
def public_key_for(node_id: str) -> str | None:
    ident = state.node_identities.get(node_id)
    return ident.public_key_pem if ident else None
```

Every `HashChainVerifier(lambda nid: state.sig_verifier.get_key(nid))` becomes
`HashChainVerifier(public_key_for)`. Remove `register_key` calls in
`routes/custody.py` and `tcp_handler._handle_register_key`.

### 3.3 Frame processor and ingest

- `services/frame_processor.py`: delete the "Deferred signature verification"
  block in `process_one_frame`. `services/parquet_writer.py` and
  `services/storage.py` read `_signing_mode` and `_signature_valid` with a
  fallback to the unprefixed keys, so they keep working with the stamps gone.
- `services/tcp_handler._enqueue_detection`: delete the
  `_needs_sig_verify` stamp.
- `services/parquet_writer.py`: no schema change. `signature_valid` is now
  always null except where a frame carries `signature_valid` itself (none do);
  `signing_mode` keeps `"polled"` from `blah2_poller` and whatever a frame
  says. Add a comment at the column block saying the per-frame signature path
  was removed on this date and the columns are kept for schema stability.
- `docs/solverflow.md` §2 step "2.1 deferred Ed25519 verify" is removed from the
  diagram and list (it was also mislabelled: the code was ECDSA P-256).

### 3.4 One ingest function for both transports

Add `services/custody.py` with the logic both handlers share, so TCP and HTTP
cannot drift:

```python
def ingest_chain_entry(node_id: str, entry_dict: dict, batch: str | None) -> dict:
    """Verify one window's entry against the node's key and the chain the
    server already holds for it, append the verdict-stamped entry, and return
    the acknowledgement body."""
```

Behaviour:

1. `entry = HashChainEntry.from_dict(entry_dict)`. A `TypeError` (missing
   field) is a 422 on HTTP and a `CHAIN_ENTRY_ACK` with
   `verified: false, reason: "malformed entry: ..."` on TCP. Nothing is stored
   for a malformed entry; today the handlers store anything, which is how
   `hour_utc` test fixtures with `data: "test"` got in.
2. `entry.node_id` must equal the `node_id` the message was sent under (TCP:
   the HELLO node; HTTP: the body's top-level `node_id`). Mismatch: stored
   under `entry.node_id` with `verified: false, reason: "node_id mismatch"`.
3. `expected_prev = state.chain_entries[node_id][-1]["entry_hash"]` if the node
   has a stored entry whose `_verified` is true, else `""` (no linkage check).
   This is what makes the stored list a chain rather than a log: a node that
   skips or replays a window fails `prev_hash mismatch` and the entry is stored
   with that reason. An unverified tail entry does not anchor the next one,
   otherwise one bad entry would poison the rest.
4. `verified, reason = HashChainVerifier(public_key_for).verify_entry(entry, batch_bytes, expected_prev)`
   where `batch_bytes = batch.encode("utf-8")` when supplied.
   `batch_verified` is `verified` when a batch was supplied and `None` when not.
   When a batch is supplied and fails, `verified` is false too (the verifier
   returns one verdict), which is the intended reading: an entry whose data does
   not match is not a verified entry.
5. Window continuity: if the previous stored entry exists and
   `entry.window_start != prev["window_end"]`, set `_gap: true`. Not a
   rejection; the library's README leaves continuity to the archive and a gap
   is legitimate after a suspend or clock step.
6. Store `entry.to_dict() | {"_verified", "_reason", "_batch_verified", "_gap", "_received_at"}`
   under `state.chain_entries[node_id]`, apply the rolling cap.
7. Return `{"status": "stored", "node_id", "entry_hash", "verified", "reason", "batch_verified"}`.

`tcp_handler._handle_chain_entry` and `routes/custody.custody_submit_chain_entry`
become thin wrappers. The HTTP request model loses `hour_utc` and
`payload_hash`; it becomes `node_id`, `entry: dict`, `batch: str | None` with a
`max_length` on `batch` (section 6 picks the number from the measured size;
start at 4 MiB). The deploy nginx templates set no `client_max_body_size`, so nginx's 1 MiB
default applies at the origin; raise it for `/api/custody/` in the template
once the batch size is measured.

### 3.5 Read routes

- `GET /api/custody/status`: `latest_hour` becomes `latest_window_end`; add
  `verified` (count of `_verified` entries) and `gaps`.
- `GET /api/custody/chain/{node_ref}`: same rename, add `gaps`.
- `GET /api/custody/verify/{node_ref}`: `verify_chain` over the stored entries
  without batches (there are none). Its verdict is now the same check the
  server did on receipt minus the batch, so the route is a consistency check of
  what is held, not new evidence. Say so in its docstring. Keep the
  `node_id → node_ref` issue rewriting as is.
- `routes/test.py` dashboard block: add `verified_entries_total` beside
  `chain_entries_total` so the test droplet's dashboard shows the loop closing.
- `services/state_snapshot.py`: no change. The stamped keys are new strings in
  the same dicts. Entries restored from a snapshot written before this change
  carry `hour_utc` and no `window_end`; `latest_window_end` reads `None` for
  them and `verify_chain` reports them malformed. Acceptable on the test
  droplet; staging and production hold no entries.

### 3.6 Tests

- Delete `backend/tests/test_custody.py`. It tests the library, and the library
  tests itself. Keep anything in it that exercised server code; on reading, it
  does not.
- Rewrite `backend/tests/test_custody_routes.py` and
  `backend/tests/test_tcp_custody_protocol.py` around real entries. A shared
  fixture in `backend/tests/custody_helpers.py` builds a chain the way the
  library's own tests do: `SoftwareCryptoBackend(tmp_path key)`,
  `HashChainBuilder(..., window_s=60, clock=FakeClock)`, frames in, clock
  forward, `close_if_due()`. Cases:
  - register then post a valid `(entry, batch)`: `verified`, `batch_verified`
    both true; status and chain routes show one verified entry.
  - second window posts with correct `prev_hash`: verified; a third with a
    stale `prev_hash` is stored unverified with `prev_hash mismatch`.
  - batch with one frame altered: stored, `verified: false`,
    `detections_hash mismatch`.
  - entry without batch: verified, `batch_verified` null.
  - no key registered: stored, `no public key for node`.
  - malformed entry (missing `window_end`): HTTP 422, TCP ack with reason,
    nothing stored.
  - window gap: `_gap` true, still verified.
  - rolling cap still applies.
  - frame processor: a frame carrying `payload_hash` and `signature` is
    processed with no `_signature_valid` key set (regression for 3.3).
- `backend/tests/test_blah2_poller.py`, `test_parquet_writer.py` and
  `test_publication.py` reference `payload_hash` or `signing_mode` as frame
  fields; those stay valid (the columns remain) and should pass unchanged.
  Confirm rather than assume.
- `just check` is the gate. The custody routes are not under `/v1/nodes`, so
  `just contract --check` should not move; if it does, something was put in the
  wrong router.

### 3.7 Documentation

- `docs/architecture.md`: the one-line "retina-custody: custody protocol" entry
  gets two sentences: windowed chain, server verifies and holds entries, no
  per-frame signatures.
- `ONBOARDING.md` table row likewise.
- `backend/.env.example`: no new keys. `RADAR_API_KEY` already gates the
  custody write routes.

## 4. The node stand-in (`retina-custody`)

`scripts/custody_server_test.py` in `retina-custody`. `pyproject.toml` already
exempts `scripts/*` from the `S` and `E` ruff families. It depends only on the
package and the standard library (`urllib.request`), so it runs from a bare
`pip install -e .`.

```
python scripts/custody_server_test.py \
  --server https://<test api origin> --api-key "$RADAR_API_KEY" \
  --node-id cust-demo-01 --window-s 60 --frames-per-s 2 --windows 5 \
  --state-dir ./custody-demo [--tsa] [--user-agent "Mozilla/5.0 ..."]
```

Behaviour, in order:

1. `SoftwareCryptoBackend(key_file=<state-dir>/key.json)`. Rerunning reuses the
   key, so the chain continues across runs; `--fresh` deletes the state dir.
2. `POST /api/custody/register` with PEM, fingerprint, serial, `software`.
3. `HashChainBuilder(node_id, crypto, node_config, chain_dir=<state-dir>/chain, window_s, timestamper=TimestampManager() if --tsa else None)`.
   `node_config` is a fixed dict (`rx_lat`, `rx_lon`, `fc_hz`, `fs_hz`) so
   `node_config_hash` is stable and visible.
4. Frame loop at `--frames-per-s`: deterministic synthetic frames shaped like
   the TCP `DETECTION` payload (`timestamp`, `delay`, `doppler`, `snr` arrays),
   seeded so two runs produce the same batches. Frames go only into the builder;
   the script does not stream detections to the server. The chain's claim is
   about the frames the node had, and this test is about the chain, not the
   solver.
5. Every second call `close_if_due()`. Each `(entry, batch)` returned, from
   either call, is posted to `POST /api/custody/chain-entry` with
   `batch.decode("ascii")`. Print the window, `n_detections`, batch size in
   bytes, and the server's `verified`, `batch_verified`, `reason`. A non-2xx or
   `verified: false` is printed and counted; the script keeps going so one bad
   window shows up as one bad line, not an abort.
6. After `--windows` closes, `close_window()` for the partial one, post it, then
   `GET /api/custody/status` and print the node's block. Exit non-zero if any
   window came back unverified.
7. `--tamper` flag: on the third window, flip one `snr` value in the batch after
   the entry was built, so the server's `detections_hash mismatch` path is
   exercised on purpose. The script expects that window to come back unverified
   and still exits zero.

Cloudflare in front of the test droplet refuses non-browser user agents with
`403 1010`; `--user-agent` covers that and defaults to a browser string.

`GET /api/custody/verify/{node_ref}` needs the node's public ref, which the
script does not know (refs come from the node table, and this node was never
registered through `/v1/nodes`). The script does not call it. `status` and the
per-window acks are the evidence.

Tests in `retina-custody/tests/test_server_test_script.py` run the script's
frame generator and request builder against a local `http.server` stub the
same way `tests/test_tsa_client.py` stubs the TSA, asserting the body shape of
section 2 and the exit code logic. No network.

## 5. `retina-simulation` `node.py`

Not in this session's repository scope; it is a separate change in that repo,
then a submodule bump here. The spec for it:

- `_stream_tcp` and the multi-node runner drop `PacketSigner`. Frames are sent
  unsigned, as `generator.generate_frame()` returns them, in the `DETECTION`
  message. `chain_builder.add_detection(frame)` replaces
  `add_detection(signed.payload_hash)`; its return value, and
  `close_if_due()` called from the existing 60-second check, are each sent as
  the section 2 `CHAIN_ENTRY` message with `batch`. `close_hour()` and the
  manual TSA stitching go; pass `timestamper=TimestampManager()` to the builder
  instead, behind the existing CLI flag that enables it (add one if none).
- Pass `chain_dir` explicitly (`coverage_data/chains/<node_id>` beside the
  existing `coverage_data/keys/<node_id>`), since the library's default resolves
  inside `site-packages` when installed.
- `IQCircularBuffer` stays as is.
- The orchestrator is unchanged. Adding custody to the deployed fleet is a
  later decision, not part of this test.

Until this lands, `retina-simulation`'s `node.py` CLI is broken against
`retina-custody` `main`. `just up` runs the orchestrator, not `node.py`, so
local development is unaffected.

## 6. Test plan and acceptance

Order matters: the server must accept the new shape before the script can
exercise it, and the submodule bump must not land before 3.2 and 3.3 or the
server fails to import.

1. **Library.** `retina-custody`: `pre-commit run --all-files && pytest`
   with the new script and its tests.
2. **Server, unit.** `just check` green with the bump and sections 3.2 to 3.6.
3. **Server, local.** `just up`; run the script against `http://localhost:8000`
   with `--window-s 30 --windows 4`, no API key if `RADAR_API_KEY` is unset
   locally. Expect four verified windows, one more partial on exit, and
   `/api/custody/status` showing `count: 5, verified: 5`. Run again with
   `--tamper`: `verified: 4`. Run a third time without `--fresh`: the first
   entry's `prev_hash` is the previous run's last `entry_hash`, and the server
   verifies it, since its stored tail is that entry.
4. **Server, test droplet.** `just verify-deploy test` before, `just deploy-test`,
   `just verify-deploy test` after. Run the script from a workstation against
   the test droplet's API origin with `--window-s 900 --frames-per-s 2
   --windows 2` (half an hour), recording the batch size per window. Check
   `/api/test/dashboard` from the droplet (`tst` in the runbook) for
   `verified_entries_total`. Restart the server container and confirm
   `/api/custody/status` still lists the node (snapshot round trip), then post
   one more window and confirm it verifies against the restored tail.
5. **Acceptance.**
   - Every window the script posts untampered comes back `verified: true,
     batch_verified: true` locally and on the test droplet.
   - The tampered window and a replayed window each come back `verified: false`
     with the verifier's reason, and are stored as such.
   - A measured 15-minute batch size is written into this document's section 2
     note and into the `max_length` chosen for the HTTP `batch` field.
   - No per-frame signature code remains in the server; `rg SignatureVerifier
     payload_hash backend --glob '!tests/**'` finds only the parquet column and
     its comment.
   - Staging and production behaviour is unchanged: no node registers a key, so
     `chain_of_custody` on their dashboards reads as before.

## 7. Tasks

Server (`retina-server`, this repo), one PR:

- [ ] 3.4 `services/custody.py` with `ingest_chain_entry` and `public_key_for`.
- [ ] 3.2 remove `SignatureVerifier`; 3.3 remove per-frame verify and stamp.
- [ ] 3.4 rewire `tcp_handler._handle_chain_entry` and `routes/custody.py`;
      new request model.
- [ ] 3.5 read routes and dashboard block.
- [ ] 3.1 submodule bump to `294c0b0`; `uv sync --locked` check.
- [ ] 3.6 tests; 3.7 docs; `just check`.

Library (`retina-custody`), one PR:

- [ ] 4 `scripts/custody_server_test.py` and its tests.
- [ ] README "Usage" gains a pointer to the script as the reference host loop.

Simulation (`retina-simulation`), one PR, then a bump here:

- [ ] 5 `node.py` on the new API.

Then the test-droplet run in 6.4, with the measured sizes written back here.

## 8. Later: `retina-custody-server`

Not designed here; recorded so the shape of this work does not block it.

The node chain commits to what a receiver observed. The server produces its
own data that nothing commits to: multinode solves published to
`state.multinode_tracks` and the track archive (`docs/solverflow.md` §6). A
`retina-custody-server` would run a `HashChainBuilder` inside the server with
`node_id` set to a server identity, `add_detection` fed with each published
solve record (or each archive append), a server key in `SoftwareCryptoBackend`,
and TSA and OpenTimestamps on, so an outside party can check that a published
track existed by a given time and was not edited later. Questions it has to
answer that this document does not:

- What the frame is: the publication record, the archive row, or the solver
  input set. The hash is only as meaningful as what it covers.
- Where entries and batches go, since here the batch is dropped and there the
  server is the archive; an append-only store with batches is the point.
- Whether the server chain references node chain entries (the `entry_hash`es
  of the windows whose frames fed a solve), which would tie the two together.
- Who holds the server's public key outside Offworld Labs, which is what makes
  a server-side chain worth anything to a third party.

The verifier, canonical form, entry shape and the ingest function from section
3.4 are all reusable for it; that is the reason to keep 3.4 free of anything
node-specific beyond the key lookup.
