# Detection mirror: real v1 node frames on the test droplet

Production forwards every accepted v1 detection frame to the test droplet's existing bulk ingest, so
analysis can run against real node data there. The test droplet holds no part of the node lifecycle
and never answers a node.

Shipped via #270 on 2026-08-27.

## Shape

```
node ──▶ api.retina.fm  POST /v1/nodes/detection
             │
             ├─▶ state.frame_queue                     (production, unchanged)
             └─▶ detection_mirror queue ──▶ 1 Hz batch ──▶ test-api.retina.fm
                                                            POST /api/radar/detections/bulk
```

`POST /api/radar/detections/bulk` already took `{node_id, config, frames}` per node behind one
`X-API-Key`, registering a node from the config it is handed and queueing its frames. It is not
under `/v1/nodes`, it mints nothing, and it answers only the caller holding the key, so the
receiving environment needed no new code: only `RADAR_API_KEY`, which it should have regardless.

Nodes talk only to production. The mirror is one-way, and nothing the receiver returns can reach a
board.

## Why production forwards rather than the edge

A Cloudflare Worker on the `/v1/nodes` route could tee each request to the test droplet, leaving
production untouched. It was rejected for two reasons that outlast this change.

The copy would have to be translated from the v1 wire shape into an ingest the test droplet can
accept without running the node lifecycle, and that translation would live in JavaScript at the
edge: outside the repository, outside CI, and free to drift from the generated contract.

More fundamentally, the edge cannot supply the node's configuration, and detections alone are
useless. `frame_processor` builds a node's pipeline only when its config carries `rx_lat` and
`tx_lat`, and a detection frame on the wire carries no geometry at all. The config lives in
production's database, so whatever forwards the frames must also be able to read it.

The cost accepted in exchange is coupling: production's ingest path depends on the test droplet's
availability. The failure behaviour below bounds it.

## The hook

`_file_frame` in `routes/node_stream.py` is the single point where a v1 frame is accepted, and the
mirror is offered the frame there rather than on arrival. Mirroring after acceptance keeps the
receiving environment's data a subset of what production filed: a frame declined because its node is
absent from the pipeline registries would otherwise arrive as data production itself refused to
solve.

`offer()` takes the wire model rather than the dict `submit_frame` queued. That dict is stamped with
`_node_id` and mutated further by the frame workers, so sharing it would let the mirror send
something a worker had since altered. Conversion happens in the drain task, off the path that runs
at frame rate, which is why `pipeline_frame` lives in `services/node_pipeline.py`: a service
importing a route module inverts the layering and becomes an import cycle once the route imports the
mirror.

## Failure behaviour

A failed batch is dropped, never retried and never buffered to disk. A retrying mirror accumulates a
backlog on production, which is what forwarding from production has to avoid to be worth doing at
all. When the receiver is slow or unreachable the queue fills and `offer()` drops at the door; a
fixed small connection pool and a two second timeout bound a hung receiver.

Accounting is deliberate rather than optimistic, because the obvious version hides the failure that
matters. The receiving endpoint answers 200 with `frames_queued: 0` when its own frame queue is
full, which on the test droplet is shared with a 25 node synthetic fleet. Crediting what was sent
rather than what landed would therefore let a receiver discarding everything look perfectly healthy.
The mirror reads `frames_queued`, credits only that, counts the shortfall separately, and routes it
through the health note like any other failure.

One consequence worth remembering: the "mirror armed" line is `logger.info`, and uvicorn's root
logger sits at WARNING in every deployed environment, so its absence proves nothing. Confirmation
has to come from the receiving end.

## What the bulk endpoint had to learn

`ingest_detections_bulk` stored an empty config hash and ignored `config` entirely for a node it had
already seen, so a node whose geometry changed kept the old one until the process restarted, while
the v1 and TCP paths both hash the config, store the hash and evict the cached pipeline when it is
replaced. Bringing the third path into line is what makes the mirror's per-batch config mean
anything, and it cleared the same latent staleness for the synthetic fleet.

Two constraints shape how narrowly it re-registers, both learned from review rather than designed in:

- Only for a node this endpoint itself created (`peer == "http-bulk"`). Otherwise a caller holding
  `RADAR_API_KEY` could strip a live v1 or TCP node's geometry by naming it in a bulk entry.
- An omitted `config` on an already-known node means no change, not a change to the trivial
  `{"node_id": ...}` dict. That dict can never hash to match a real stored config, so hashing it
  re-registered the node with no geometry on every such call and quietly solved its detections
  against the process-wide default receiver and transmitter.

## Consequences accepted

Real receiver and transmitter geometry now lands on a droplet running
`AUTH_ALLOW_ANONYMOUS_ADMIN=1`.

The test droplet keeps its 25 node synthetic fleet running beside the real feed. The synthetic nodes
sit near Greenville, roughly 230 km from the real nodes, with a 50 km maximum range, so the
associator's overlap grid should not pair the two groups.
