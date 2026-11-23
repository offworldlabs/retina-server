# Mender snapshot

The contract between mender-auto-accept (node-infra) and retina-server. The
sender reads the whole Mender fleet and POSTs it here; retina-server keeps it in
a mirror table and answers registration from it. Operations are in the runbook,
under "Mender mirror".

## Endpoint

`POST /api/internal/mender/snapshot`, on any host that serves `/api/`. One
request per sweep, one target per environment.

```
Authorization: Bearer <MENDER_SYNC_TOKEN>
Content-Type: application/json
```

The route is outside `/v1/nodes`, so node tokens do not reach it. Other methods
on the path answer 405, as on any POST-only route.

## Body

```json
{
  "generated_at": "2026-10-16T14:02:11Z",
  "source": "mender-auto-accept@central",
  "devices": [
    {
      "mender_device_id": "6703f1…",
      "node_id": "ret1a2b3c4d",
      "auth_status": "accepted",
      "auth_set_id": "a1b2…",
      "auth_set_pubkey": "-----BEGIN PUBLIC KEY-----\n…",
      "device_type": "pi5-v3-arm64",
      "artifact_name": "owl-os-1.4.2",
      "inventory_updated_at": "2026-10-16T14:01:40Z"
    }
  ]
}
```

The body has exactly the keys `generated_at`, `source` and `devices`, and every
device exactly the eight keys shown. Use `null` where the table allows it.

| Field | Type | Null | Source in the Mender API |
| --- | --- | --- | --- |
| `generated_at` | ISO 8601 string with a timezone | no | when the devauth fetch started |
| `source` | string, 1 to 128 printable ASCII characters | no | the sender's identity |
| `devices` | list of objects, may be empty | no | the whole fleet |
| `mender_device_id` | string, up to 64 | no | devauth v2 `devices[].id` |
| `node_id` | string, up to 64 | yes | `identity_data.node_id`; null for a device without the key |
| `auth_status` | lowercase token | no | see below |
| `auth_set_id` | string, up to 64 | yes | the accepted `auth_sets[]` entry |
| `auth_set_pubkey` | string, up to 4096 | yes | the accepted `auth_sets[]` entry |
| `device_type` | string, up to 128 | yes | inventory v1 attribute `device_type` |
| `artifact_name` | string, up to 256 | yes | inventory v1 attribute `artifact_name` |
| `inventory_updated_at` | ISO 8601 string with a timezone | yes | inventory v1 `updated_ts` |

`auth_status` is a token matching `[a-z][a-z_]{0,31}` in full. The known ones:

- `accepted`: exactly one auth set is accepted.
- `ambiguous`: more than one auth set is accepted, or the device says accepted
  with none.
- `pending`, `rejected`, `preauthorized`, `noauth`: the device `status` from
  Mender.

Any other token is stored as sent. Only `accepted` lets a node register.
`accepted` should carry `auth_set_id` and `auth_set_pubkey`. A status other than
`accepted` may carry them too: when the device status disagrees with its one
accepted set, the sender passes both through. They are stored with their
fingerprint, and registration still refuses the node.

Send the fleet unfiltered, including devices whose `node_id` has no `ret` prefix
and devices with no `node_id`.

## What the server does with it

Two kinds of fault, handled differently.

A fault in the envelope refuses the whole push with 422 and leaves the mirror
untouched:

- the body is not JSON or not an object, or its keys are not exactly
  `generated_at`, `source` and `devices`;
- `generated_at` is not an ISO 8601 string with a timezone, like
  `2026-10-16T14:02:11Z` (a number, or a space instead of `T`, is refused, and so
  is a year no UTC time can hold), or is more than 300 s ahead of the server clock;
- `source` is not 1 to 128 printable ASCII characters;
- `devices` is not a list;
- a device is not an object, has keys other than exactly the eight, or has a field
  of the wrong JSON type (each is a string or null, as the table allows).

A fault in one device never gives 422. The other devices are still applied:

| Fault | Result |
| --- | --- |
| `mender_device_id` empty or over 64 characters | entry dropped, counted as rejected, logged |
| `mender_device_id` repeated in the push | the first entry is stored with an issue; the rest are dropped and counted as rejected |
| `node_id` is `""` | treated as null |
| `node_id` over 64 characters | stored null, with an issue |
| `auth_status` is not a lowercase token | stored as `invalid`, with an issue |
| `auth_set_id` or `auth_set_pubkey` is `""` or only whitespace | treated as null, whatever the status |
| `auth_set_id` over 64, or `auth_set_pubkey` over 4096 | that field stored null, with an issue |
| `accepted` without `auth_set_id` or `auth_set_pubkey` | stored, with an issue |
| `device_type` over 128, `artifact_name` over 256 | stored truncated, logged, no issue |
| `inventory_updated_at` not ISO 8601 with a timezone, or a year no UTC time can hold | stored null, logged, no issue |
| a non-null `node_id` shared by several stored entries | every one of them gets the issue `node_id shared by N devices` |

An issue is kept in the `snapshot_issue` column. A device with several gets them
joined with `; `, the collisions between devices first (repeated id, shared
`node_id`), then those of its own fields, cut to 128 characters. A device stored
with an issue stays present. Registration refuses that `node_id` alone, as
ambiguous, with no fleet-wide alert, and logs a WARNING `mender mirror refuses
<node_id> as ambiguous: ...` with the ids quoted. The Fleet page marks the row
`not usable` and shows the issue.

Once a push is applied, a device that was dropped or changed on the way in (cut,
nulled, key undecodable) gets one WARNING line naming its index, its id and what
was done. A device stored with an issue is logged only when the issue is new or
differs from the stored one, so an unchanged fleet does not repeat the same
lines every 30 s. The first 20 lines of a push are logged, then one line with the
number left. A push answered 4xx logs none of them.

## Semantics

- Full snapshot, whole-fleet upsert keyed on `mender_device_id`. A mirror row not
  in the body gets `present = false`. Nothing is deleted.
- A decommissioned device keeps its row with `present = false`. Its `node_id`
  becomes free: `node_id` is unique among present rows without an issue only.
- `generated_at` must be later than the stored one. Equal or older is answered
  409 and ignored, so a retried request or a second sender cannot roll the mirror
  back.
- `last_confirmed_at`, `accepted_at` and `auth_set_changed_at` take the
  snapshot's `generated_at`. `accepted_at` is set when the device becomes
  accepted and cleared when it stops; `auth_set_changed_at` moves when
  `auth_set_id` changes.
- `auth_set_fingerprint` is computed by the server from the public key. A key
  that does not decode is stored without one, logged, with no issue.
- The endpoint is idempotent. Retrying is safe, but a retry of a snapshot that
  already landed gets 409.
- Any 4xx leaves the mirror as it was.

## Staleness

Staleness is measured on `last_snapshot_at`, the server's own receive time, not
on `generated_at`. In `mirror` mode registration refuses with `mender_unreachable`
and the reason `mirror stale (<why>)` when the mirror cannot be trusted. `<why>` is one of:

- `no snapshot yet`;
- `last snapshot N s ago, limit M s`, when the last push is older than
  `MENDER_MIRROR_MAX_AGE_S` (default 300 s), so the sender should push every sweep,
  30 s apart;
- `last snapshot is N s in the future, the server clock went back`, when the last
  push is more than 30 s ahead of the server clock;
- the text of a malformed `MENDER_MIRROR_MAX_AGE_S`, for example
  `MENDER_MIRROR_MAX_AGE_S='five minutes' is not a number`. A bad value refuses
  registration instead of falling back to the default.

If the mirror table cannot be read, the reason is `mirror unreadable: <driver
message>` instead.

## Validation

Checked in this order, the first failure answers. In a deployed environment two
size limits answer 413 before the route is reached: nginx over 2 MiB, and the
app's global middleware on a `Content-Length` over 5 MB.

1. 404: `MENDER_SYNC_TOKEN` is unset, blank, or shorter than 32 characters once
   trimmed. A POST gets 404, with the body an unknown path gets, until the token
   is set. A short token also logs an ERROR line naming `MENDER_SYNC_TOKEN`.
2. 401: the bearer token is missing or wrong (constant-time comparison on bytes).
   The response carries `WWW-Authenticate: Bearer`.
3. 413: the body is over 2 MiB, by `Content-Length` or by the bytes actually read.
4. 422: a fault in the envelope (list above).
5. 409: `generated_at` is not later than the stored one.

The error body is JSON, but only the status code is part of the contract.

Logging of refused pushes: the first 401 is logged as a WARNING at once. After
that the route logs at most one WARNING per 60 s, carrying the number of refusals
held back since the last line and the last client address. The short-token ERROR
follows the same scheme. A push answered 200 logs one INFO line with the
accepted, rejected and absent counts.

## Responses

200 with

```json
{"accepted": 21, "rejected": 1, "absent": 1, "generated_at": "2026-10-16T14:02:11Z"}
```

- `accepted`: devices stored without an issue.
- `rejected`: devices dropped, or stored with an issue. `accepted + rejected` is
  the number of devices in the body.
- `absent`: mirror rows not in this body, which are `present = false` after the
  push.
- `generated_at`: the snapshot's own time as UTC with a `Z`, so an offset in the
  request comes back converted.

| Status | Meaning | Sender |
| --- | --- | --- |
| 200 | applied | log the counts; a non-zero `rejected` is worth a look |
| 401 | token missing or wrong | fix the target's token |
| 403 | Cloudflare error 1010, not the server | see Cloudflare below |
| 404 | server has no usable sync token | fix the server |
| 409 | a newer or equal snapshot is stored | log at INFO, treat as success |
| 413 | body over 2 MiB | the fleet outgrew the cap; the server's cap has to be raised |
| 422 | the envelope is bad | do not retry the same body |

## The sender

mender-auto-accept follows the contract this way:

- `source` is `mender-auto-accept@<short hostname>`.
- Timestamps are UTC whole seconds with a `Z`. `generated_at` is stamped when the
  devauth fetch starts.
- An empty `node_id` is sent as null.
- The snapshot is the whole fleet, devauth v2 plus inventory v1, paged at 500. If
  any page fails nothing is pushed, because a partial fleet would mark real
  devices absent. An empty fleet is pushed as it is.
- `RETINA_SYNC_TARGETS` is a comma-separated list of `url=token` pairs. Each url
  is the full endpoint, `https://<host>/api/internal/mender/snapshot`.
- Targets are pushed in turn, each with a 10 s timeout. A failing target does not
  stop the others. Redirects are not followed.
- A 409 is logged at INFO and treated as success. There is no retry queue: the
  next sweep is the retry.

## Cloudflare

The endpoint is reachable only through Cloudflare, which refuses some automated
clients before the request reaches the server. A 403 with `error code: 1010` is
Cloudflare's browser integrity check refusing the client's User-Agent, not
retina-server. To recognise it: the body is a short Cloudflare page naming error
1010 rather than the server's JSON error, the response has Cloudflare headers
such as `cf-ray`, the server log has nothing for that push, and the sender's log
shows `HTTP 403` followed by the start of that page.

A script's request needs a browser-like User-Agent, which is what
`deploy/verify-deploy.py` sends, or a client-specific one. The node client sends
its own, `retina-telemetry`, on every call to `/v1/nodes`. So the push should set
a `User-Agent` of its own, such as `mender-auto-accept`, instead of the HTTP
library's default. If Cloudflare still answers 1010, the maintainer adds a
Configuration Rule for the path `/api/internal/mender/snapshot` that turns
Browser Integrity Check off. The bearer token still guards the route.

Neither repo records the rule that lets node clients through to `/v1/nodes`, so
which of the two the push needs is not confirmed.

## Size and rate limit

The cap is 2 MiB, room for about 2000 devices; 500 come to under 1 MB. nginx admits
that size on this path only (`client_max_body_size 2m` on
`location = /api/internal/mender/snapshot`, 1 MiB everywhere else). The route
shares the per-address `api` limit with the rest of `/api/` (30 requests per
second, burst 60), far above one push every 30 s.

## Token

One token per environment, set as `MENDER_SYNC_TOKEN` in that server's
`backend/.env`, and as that environment's pair in the sender's
`RETINA_SYNC_TARGETS` (`url=token`, comma-separated). The token can write the
mirror and nothing else.

```bash
openssl rand -hex 32
```

Mint a new one for each environment; never reuse one across environments. To
rotate: set the new value on the server, apply it with `docker compose up -d server`
(`restart` does not re-read `backend/.env`), then set it on the sender.
Pushes answer 401 between the two steps, and the next sweep after the second
succeeds.

## Alerts

Both are sent after the snapshot is applied, so the mirror already holds the new
data. They are never sent for 401, 409 or 422 pushes.

- `mender_snapshot_source_changed`: an accepted push's `source` differs from the
  stored one.
- `mender_snapshot_count_drop`: an accepted push lists over 20% fewer devices
  than the previous one.

The messages are `mender snapshot source changed from '<old>' to '<new>'` and
`mender snapshot lists N devices, down from M (source '<source>')`. N counts every
device in the body. The first snapshot raises neither.

Either one means check whether the sync token leaked: compare the snapshot with
hosted Mender, then rotate `MENDER_SYNC_TOKEN` on the server and on the sender.
A legitimate rename of `source`, a bulk decommission or an empty fleet also trips
them once.

A stale mirror raises `mender_unreachable` when registration meets it, with the
reason in the message, for example `registration for <node_id> could not reach
Mender: mirror stale (last snapshot 412 s ago, limit 300 s)`.
