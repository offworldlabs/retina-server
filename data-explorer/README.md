# Retina Data Explorer

The public archive browser served at `data.retina.fm`
(`test-data.retina.fm` / `staging-data.retina.fm` per environment). Static files,
no build step, no npm: nginx serves this directory as-is from `/app/data-explorer`
and the page talks to the same-origin API.

```
data-explorer/
  index.html   markup + the CommonJS shim that loads the vendored timeline
  app.css      tokens (light palette verbatim from dashboard/src/App.css) + layout
  app.js       all behaviour, one classic script, plain ES2020
  vendor/      react, react-dom, lodash, classnames, @edsc/timeline — see NOTICE.md
```

Edit the files and redeploy; there is nothing to compile. `Dockerfile` copies the
directory verbatim, so a change here ships with any image build.

## Why the libraries are vendored

The page vhost sends `script-src 'self'` (`deploy/nginx/snippets/security-headers-page.conf`),
so a CDN `<script src>` is blocked in every deployed environment while still
working on a laptop — the worst possible failure shape. `vendor/NOTICE.md` lists
each file with its version, license and upstream URL, and documents the
`require()` shim that loads `@edsc/timeline`'s CommonJS bundle without a bundler.

## Data it reads

| Endpoint | Used for |
| --- | --- |
| `GET /api/radar/nodes` | node ids, `is_synthetic`, and the **published** (fuzzed) receiver position with its `location_uncertainty_km` |
| `GET /api/data/archive?date=YYYY/MM/DD&limit=500&offset=N[&node_id=X]` | the file listing |
| `GET /api/data/archive/{key}` | one file, as legacy per-frame JSON |

All three are unauthenticated and same-origin. Private nodes are filtered out by
the backend and never appear here.

A few properties of those endpoints drive the design, and are worth knowing
before changing the loading code:

- **The listing is always date-bounded.** The no-date path caps at 5000 files and
  is not newest-first, so it cannot answer "what is there lately?". Days are
  fetched one at a time, at most 3 concurrently, cached per `(day, node)`, and
  rendered progressively with a per-day loading state and a per-day Retry.
- **`node_id=` is roughly 10× faster** (~0.4 s versus ~3.5 s for a full day of
  ~1300 files on the test droplet), so it is used whenever exactly one node is in
  play. A whole-day listing already in the cache answers for any subset without a
  refetch.
- **Paging follows `total`, not `count`.** The route drops private nodes *after*
  paging, which shortens a page without ending the list.
- **Coverage spans are estimated.** `ARCHIVE_FLUSH_INTERVAL_S = 3600` writes one
  file per node per hour at the *end* of the hour it covers, and the listing
  exposes only `modified` — so the start is `modified − 1 h` and every span on
  the page is labelled `est.`
- **Everything listed is on local disk.** Files older than
  `ARCHIVE_RETENTION_DAYS` are offloaded to R2 and no endpoint serves them. The
  page does not invent a hot/cold tier per file: it reads the horizon off the
  data (the oldest day that returned anything) and, if the requested range
  reaches past it, says so in one banner.
- **Node ids in keys outlive the node registry.** A file whose node is absent
  from `/api/radar/nodes` is still listed, just without a position — and a node
  with no published position never matches the location filter.

## Downloads

`GET /api/data/archive/{key}` returns `application/json`: the archived Parquet
reconstructed into the legacy per-frame shape, roughly **9×** the stored size. It
sets no `Content-Disposition`, so it opens inline in a browser and `curl -O`/`-J`
would misname it — the manifest and the curl block therefore name the output
explicitly (`curl -sS -o <node>-<part>.json "<url>"`).

Raw Parquet, CSV-per-detection and a zipped multi-file bundle
(`POST /api/data/archive/bundle`) are **future backend work**; nothing in this
page offers them, because offering a button for a route that does not exist is
worse than not having it.

## Privacy

`rx_lat` / `rx_lon` — in `/api/radar/nodes`, in the map, and inside every archived
frame — are the **published**, fuzzed receiver position, not the true site; the
per-node `location_uncertainty_km` is shown alongside. `tx_*` are true:
transmitters are licensed towers. The page says this wherever it shows a
position.

## Synthetic nodes

The production fleet has none. Everything synthetic-related — the legend swatch,
the "Real only" preset, the real/synth split of aggregated timeline rows, the
muted colour — appears only when at least one node reports `is_synthetic: true`
or a listed key names a `synth-*` node. With none present the concept is invisible,
and more than 3 selected nodes aggregate into a single `N nodes` row.

## URL state

Every filter is in the query string and written back with `history.replaceState`
on each change, so any view is a link:

```
?node=a&node=b&from=YYYY-MM-DD&to=YYYY-MM-DD&tod=HH:MM-HH:MM&near=lat,lon,km&minsize=bytes
```

Omitting `node` means "every node there is", which keeps nodes discovered later
(from the archive rather than the registry) inside the selection.

## Infrastructure

`HOST_DATA` mirrors `HOST_DASH` throughout: the port-80 redirect list and a
server block in `deploy/nginx/nginx.conf.template`, the allowlist in
`deploy/render-nginx-config.py`, `HOST_VARS` in `deploy/check-env-parity.py`, the
three compose overlays (plus `CORS_ORIGINS`), the `COPY` in `Dockerfile`, and the
`DATA_URL` checks in `deploy/staging-smoke-test.sh`. The vhost includes the
tower-finder proxy for the same reason `dash` does — one path must have one
answer per environment — which is also why
`backend/tests/test_towers_vhost_coverage.py` requires it to be probed.
