"use strict";
/* Retina Data Explorer — plain ES2020, classic script, no build step.
 *
 * Everything on this page comes from two unauthenticated same-origin endpoints:
 *   GET /api/radar/nodes                       node metadata + published positions
 *   GET /api/data/archive?date=YYYY/MM/DD&…    the file listing, one day at a time
 *   GET /api/data/archive/{key}                one file, as legacy per-frame JSON
 *
 * The listing is ALWAYS date-bounded. The no-date path caps at 5000 files and is
 * not newest-first, so it cannot answer "what is there lately?" honestly; a
 * bounded day scan can, and it is the only shape the backend sorts. One full day
 * is ~1300 files and ~3.5 s on the test droplet, so days load concurrently
 * (3 at a time), render as they arrive, and are cached per (day, node).
 */
(function () {

/* ══ Constants ═════════════════════════════════════════════════════════════ */
const DAY_MS = 86400e3;
const HOUR_MS = 3600e3;
const PAGE = 500;             // the listing's hard cap (storage.py clamps to 500)
const MAX_INFLIGHT = 3;       // concurrent day fetches
const MAX_OFFSET = 50000;     // runaway guard on the pagination loop
/* ARCHIVE_FLUSH_INTERVAL_S = 3600: one Parquet file per node per hour, written
 * at the END of the hour it covers. The listing exposes only `modified`, so the
 * start is inferred and every span on this page is labelled "est.". */
const FILE_SPAN_MS = HOUR_MS;
/* Measured on the test droplet: a 35 KB Parquet reconstructs to 255 KB of legacy
 * JSON (~7.3x). Rounded up, and stated as an approximation everywhere it shows. */
const JSON_FACTOR = 9;

/* ══ Small helpers ═════════════════════════════════════════════════════════ */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const p2 = (n) => String(n).padStart(2, "0");
function fmtBytes(b) {
  if (!Number.isFinite(b)) return "—";
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b / 1048576).toFixed(1) + " MB";
  return (b / 1073741824).toFixed(2) + " GB";
}
const hhmm = (ms) => { const d = new Date(ms); return p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes()); };
const isoDay = (d) => d.toISOString().slice(0, 10);
const minOf = (t) => { const p = String(t).split(":"); return (+p[0] || 0) * 60 + (+p[1] || 0); };
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function todayUTC() { const n = new Date(); return isoDay(new Date(Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate()))); }
function addDays(day, n) { return isoDay(new Date(Date.parse(day + "T00:00:00Z") + n * DAY_MS)); }
function daysBetween(from, to) {
  const out = [];
  if (!from || !to || from > to) return out;
  for (let d = from; d <= to; d = addDays(d, 1)) { out.push(d); if (out.length > 400) break; }
  return out;
}
const haversineKm = (a, b, c, d) => {
  const r = Math.PI / 180, x = (c - a) * r, y = (d - b) * r;
  const h = Math.sin(x / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin(y / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
};
function copy(text, btn, label) {
  const done = () => { const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = label || old; }, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, done);
  else done();
}

/* ══ Sibling hosts ═════════════════════════════════════════════════════════
 * This site is the `data` member of a per-environment family
 * (test-data / staging-data / data). Derive the siblings by rewriting the
 * `data` label rather than hard-coding three environments' worth of names:
 * test-data.retina.fm -> test-dash.retina.fm. Anything that is not a
 * recognisable `…data` host (a laptop, an IP, a preview) falls back to
 * production, which is the only family whose names are certain from here. */
function siblingUrl(role) {
  const host = location.hostname;
  const first = host.split(".")[0];
  if (/(^|-)data$/.test(first) && host.indexOf(".") !== -1) {
    return location.protocol + "//" + host.replace(first, first.replace(/(^|-)data$/, "$1" + role));
  }
  return "https://" + role + ".retina.fm";
}
$("#topnav").innerHTML =
  '<a href="' + esc(siblingUrl("dash")) + '">Dashboard</a>' +
  '<a href="' + esc(siblingUrl("map")) + '">Live map</a>';

/* ══ State ═════════════════════════════════════════════════════════════════ */
const TODAY = todayUTC();
const S = {
  nodeSel: null,           // Set of node ids, or null for "every node there is"
  from: addDays(TODAY, -2), // default range: the last 3 days
  to: TODAY,
  todFrom: "00:00",
  todTo: "23:59",
  minSize: 0,
  loc: null,               // {lat, lon, km}
  sel: new Set(),          // basket, keyed by archive key
  collapsed: new Set(),
  sort: "span",
  active: null,            // key of the file open in the drawer
};

/* Node metadata, keyed by id. Populated from /api/radar/nodes and then extended
 * by any node id seen in a key — the archive outlives the registry, so a
 * retired node still has files and must still be listed, just without a
 * position. */
const NODES = new Map();
let nodesLoaded = false;
let nodesError = null;

/* Per-(day,node) listing cache. Key `${day}|${nodeId||'*'}`. */
const cache = new Map();
/* key -> normalised file record, for the basket and the drawer. */
const filesByKey = new Map();

/* ══ Nodes ═════════════════════════════════════════════════════════════════ */
function noteNodeFromKey(id) {
  if (!id || NODES.has(id)) return;
  NODES.set(id, {
    id, name: id, status: null, synthetic: /^synth-/.test(id),
    lat: null, lon: null, alt: null, txLat: null, txLon: null, unc: null,
    fromRegistry: false,
  });
}
async function loadNodes() {
  nodesError = null;
  try {
    const r = await fetch("/api/radar/nodes", { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    const raw = (j && j.nodes) || {};
    Object.keys(raw).forEach((id) => {
      const n = raw[id] || {};
      const loc = n.location || {};
      const hasPos = Number.isFinite(loc.rx_lat) && Number.isFinite(loc.rx_lon);
      NODES.set(id, {
        id,
        name: n.name || id,
        status: n.status || null,
        synthetic: !!n.is_synthetic || /^synth-/.test(id),
        lat: hasPos ? loc.rx_lat : null,
        lon: hasPos ? loc.rx_lon : null,
        alt: Number.isFinite(loc.rx_alt_ft) ? loc.rx_alt_ft : null,
        txLat: Number.isFinite(loc.tx_lat) ? loc.tx_lat : null,
        txLon: Number.isFinite(loc.tx_lon) ? loc.tx_lon : null,
        txAlt: Number.isFinite(loc.tx_alt_ft) ? loc.tx_alt_ft : null,
        unc: Number.isFinite(loc.location_uncertainty_km) ? loc.location_uncertainty_km : null,
        fromRegistry: true,
      });
    });
    nodesLoaded = true;
  } catch (e) {
    nodesError = (e && e.message) || String(e);
  }
  render();
}

const allNodes = () => Array.from(NODES.values());
const hasSynthetic = () => allNodes().some((n) => n.synthetic);
const hasPos = (n) => Number.isFinite(n.lat) && Number.isFinite(n.lon);
const distKm = (n) => (S.loc && hasPos(n) ? haversineKm(S.loc.lat, S.loc.lon, n.lat, n.lon) : NaN);
/* A node with no published position can never be shown to be inside a radius,
 * so it never matches one — the honest answer, not a silent inclusion. */
const locOk = (n) => !S.loc || (hasPos(n) && distKm(n) <= S.loc.km);
const isSelected = (n) => !S.nodeSel || S.nodeSel.has(n.id);
/* The location filter is a way of choosing nodes: the archive is partitioned by
 * node, so "near here" reduces to a node set and the listing does the rest. */
const effectiveNodes = () => allNodes().filter((n) => isSelected(n) && locOk(n));
const effectiveIds = () => new Set(effectiveNodes().map((n) => n.id));

/* ══ Listing ═══════════════════════════════════════════════════════════════ */
function parseKey(key, sizeBytes, modified) {
  const parts = String(key).split("/").filter(Boolean);
  if (parts.length < 2) return null;
  const seg = (i) => { const p = parts[i]; return p == null ? "" : p.split("=").pop(); };
  const node = seg(parts.length - 2);
  const name = parts[parts.length - 1];
  let day = "";
  if (parts.length >= 4) day = seg(0) + "-" + seg(1) + "-" + seg(2);
  const endMs = Date.parse(modified);
  if (!day || !Number.isFinite(endMs)) return null;
  noteNodeFromKey(node);
  return {
    key: String(key), name, node, day,
    size: Number(sizeBytes) || 0,
    endMs, startMs: endMs - FILE_SPAN_MS,
  };
}

let inflight = 0;
const queue = [];
function pump() {
  while (inflight < MAX_INFLIGHT && queue.length) {
    const task = queue.shift();
    inflight++;
    task().then(pumpDone, pumpDone);
  }
}
function pumpDone() { inflight--; pump(); }

async function fetchListing(day, nodeId) {
  const out = [];
  let offset = 0;
  for (;;) {
    const qs = new URLSearchParams();
    qs.set("date", day.replace(/-/g, "/"));
    qs.set("limit", String(PAGE));
    qs.set("offset", String(offset));
    if (nodeId) qs.set("node_id", nodeId);
    const r = await fetch("/api/data/archive?" + qs.toString(), { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status + " from /api/data/archive");
    const j = await r.json();
    const files = Array.isArray(j.files) ? j.files : [];
    for (const f of files) {
      const rec = parseKey(f.key, f.size_bytes, f.modified);
      if (rec) { out.push(rec); filesByKey.set(rec.key, rec); }
    }
    offset += PAGE;
    /* `count` is not a reliable end-of-list signal: the route drops private
     * nodes AFTER paging, which shortens a page without ending the list. The
     * date-bounded path reports an exact `total` for the scan it paged over, so
     * page against that and fall back to the short-page rule only if it is
     * missing. */
    const total = Number(j.total);
    const done = Number.isFinite(total) && total >= 0 ? offset >= total : files.length < PAGE;
    if (done || offset >= MAX_OFFSET) break;
  }
  return out;
}

function cacheKey(day, nodeId) { return day + "|" + (nodeId || "*"); }

/* The entry that can answer for this day under the current node selection: a
 * whole-day listing answers for any subset, a node-scoped one only for that
 * node. */
function dayEntry(day) {
  const all = cache.get(cacheKey(day, null));
  if (all) return all;
  const eff = effectiveIds();
  if (eff.size === 1) {
    const only = Array.from(eff)[0];
    const one = cache.get(cacheKey(day, only));
    if (one) return one;
  }
  return null;
}

function startLoad(day, force) {
  const eff = effectiveIds();
  /* Exactly one node in play is ~0.4 s with node_id= against ~3.5 s for the
   * whole day, so ask the narrow question when it is the whole question. */
  const narrow = eff.size === 1 && (nodesLoaded || (S.nodeSel && S.nodeSel.size === 1));
  const nodeId = narrow ? Array.from(eff)[0] : null;
  const ck = cacheKey(day, nodeId);
  if (!force && cache.has(ck)) return;
  const entry = { day, nodeId, status: "loading", files: [], error: null };
  cache.set(ck, entry);
  queue.push(async () => {
    try {
      entry.files = await fetchListing(day, nodeId);
      entry.status = "done";
    } catch (e) {
      entry.status = "error";
      entry.error = (e && e.message) || String(e);
    }
    render();
  });
  pump();
}

function ensureLoads() {
  for (const day of daysBetween(S.from, S.to)) {
    if (day > TODAY) continue;          // the future has no archive
    if (!dayEntry(day)) startLoad(day, false);
  }
}

/* ══ Filtering ═════════════════════════════════════════════════════════════ */
function inTod(f) {
  const lo = minOf(S.todFrom), hi = minOf(S.todTo);
  if (lo === 0 && hi >= 1439) return true;
  const s = new Date(f.startMs), e = new Date(f.endMs);
  const a = s.getUTCHours() * 60 + s.getUTCMinutes(), b = e.getUTCHours() * 60 + e.getUTCMinutes();
  const spans = [a <= b ? [a, b] : [a, 1439]];
  if (a > b) spans.push([0, b]);
  return spans.some(([x, y]) => x <= hi && y >= lo);
}
function passes(f, eff) {
  return eff.has(f.node) && f.size >= S.minSize && inTod(f);
}
/* Every file in the requested range that survives the filters, newest day first. */
function matches() {
  const eff = effectiveIds();
  const out = [];
  for (const day of daysBetween(S.from, S.to)) {
    const e = dayEntry(day);
    if (!e || e.status !== "done") continue;
    for (const f of e.files) if (f.day === day && passes(f, eff)) out.push(f);
  }
  return out;
}

/* ══ Cold-storage horizon ══════════════════════════════════════════════════
 * ARCHIVE_RETENTION_DAYS keeps a few days on local disk and offloads the rest
 * to R2, which no endpoint serves. Rather than invent a tier for each file,
 * read the horizon off the data: the oldest day that actually returned files.
 * If the requested range reaches past it and every loaded day before it came
 * back empty, say so. */
function horizon() {
  const days = daysBetween(S.from, S.to);
  let earliestWithFiles = null, emptiesBefore = 0;
  for (const day of days) {
    const e = dayEntry(day);
    if (!e || e.status !== "done") continue;
    if (e.files.length) { if (!earliestWithFiles || day < earliestWithFiles) earliestWithFiles = day; }
  }
  if (!earliestWithFiles) return null;
  for (const day of days) {
    if (day >= earliestWithFiles) continue;
    const e = dayEntry(day);
    if (!e || e.status !== "done") return null;   // still loading; do not guess
    if (e.files.length) return null;
    emptiesBefore++;
  }
  return emptiesBefore ? earliestWithFiles : null;
}

/* ══ URL state ═════════════════════════════════════════════════════════════ */
function readUrl() {
  const q = new URLSearchParams(location.search);
  const nodes = q.getAll("node").filter(Boolean);
  if (nodes.length) {
    S.nodeSel = new Set(nodes);
    /* Seed the registry from the URL so a one-node link can use node_id= on its
     * very first listing instead of paying for a whole day it will discard. */
    nodes.forEach(noteNodeFromKey);
  }
  const day = /^\d{4}-\d{2}-\d{2}$/;
  if (day.test(q.get("from") || "")) S.from = q.get("from");
  if (day.test(q.get("to") || "")) S.to = q.get("to");
  if (S.from > S.to) { const t = S.from; S.from = S.to; S.to = t; }
  const tod = /^(\d{2}:\d{2})-(\d{2}:\d{2})$/.exec(q.get("tod") || "");
  if (tod) { S.todFrom = tod[1]; S.todTo = tod[2]; }
  const near = (q.get("near") || "").split(",").map(Number);
  if (near.length === 3 && near.every(Number.isFinite)) S.loc = { lat: near[0], lon: near[1], km: Math.max(2, near[2]) };
  const ms = Number(q.get("minsize"));
  if (Number.isFinite(ms) && ms > 0) S.minSize = ms;
}
function shareQuery() {
  const qs = new URLSearchParams();
  if (S.nodeSel) Array.from(S.nodeSel).sort().forEach((n) => qs.append("node", n));
  qs.set("from", S.from);
  qs.set("to", S.to);
  if (S.todFrom !== "00:00" || S.todTo !== "23:59") qs.set("tod", S.todFrom + "-" + S.todTo);
  if (S.loc) qs.set("near", S.loc.lat.toFixed(4) + "," + S.loc.lon.toFixed(4) + "," + S.loc.km);
  if (S.minSize) qs.set("minsize", String(S.minSize));
  return qs.toString();
}
function writeUrl() {
  const url = location.pathname + "?" + shareQuery();
  history.replaceState(null, "", url);
  $("#shareUrl").textContent = url;
}

/* ══ Filter bar wiring ═════════════════════════════════════════════════════ */
function fileCountFor(id) {
  let n = 0;
  for (const day of daysBetween(S.from, S.to)) {
    const e = dayEntry(day);
    if (!e || e.status !== "done") continue;
    for (const f of e.files) if (f.node === id) n++;
  }
  return n;
}
function buildMs(q) {
  const list = $("#msList");
  const synth = hasSynthetic();
  $("#msReal").hidden = !synth;
  const rows = allNodes()
    .filter((n) => !q || n.id.toLowerCase().indexOf(q.toLowerCase()) !== -1)
    .sort((a, b) => a.id.localeCompare(b.id));
  if (!rows.length) {
    list.innerHTML = '<div class="ms-empty">' + (NODES.size ? "No node id matches that." : "No nodes yet.") + "</div>";
    return;
  }
  list.innerHTML = rows.map((n) => {
    const cls = ["ms-opt"];
    if (!locOk(n)) cls.push("far");
    if (!hasPos(n)) cls.push("nopos");
    const cnt = fileCountFor(n.id);
    return '<label class="' + cls.join(" ") + '">' +
      '<input type="checkbox" data-node="' + esc(n.id) + '"' + (isSelected(n) ? " checked" : "") + ">" +
      '<span class="mono">' + esc(n.id) + "</span>" +
      '<span class="cnt">' + (cnt || "") + "</span></label>";
  }).join("");
}
function setNodeSel(ids) {
  /* null means "all", which keeps nodes discovered later (a node that only
   * exists in the archive) inside the selection instead of silently excluded. */
  S.nodeSel = ids === null ? null : new Set(ids);
  if (S.nodeSel && S.nodeSel.size === NODES.size) S.nodeSel = null;
  changed();
}
$("#msToggle").onclick = (e) => {
  e.stopPropagation();
  const open = $("#msPanel").classList.toggle("open");
  $("#msToggle").setAttribute("aria-expanded", open ? "true" : "false");
};
$("#msPanel").onclick = (e) => e.stopPropagation();
document.addEventListener("click", () => {
  $("#msPanel").classList.remove("open");
  $("#msToggle").setAttribute("aria-expanded", "false");
});
$("#msList").addEventListener("change", (e) => {
  const cb = e.target.closest("input[data-node]");
  if (!cb) return;
  const cur = new Set((S.nodeSel ? Array.from(S.nodeSel) : allNodes().map((n) => n.id)));
  if (cb.checked) cur.add(cb.dataset.node); else cur.delete(cb.dataset.node);
  setNodeSel(cur);
});
$$("[data-msact]").forEach((b) => {
  b.onclick = () => {
    const a = b.dataset.msact;
    if (a === "all") setNodeSel(null);
    else if (a === "none") setNodeSel([]);
    else setNodeSel(allNodes().filter((n) => !n.synthetic).map((n) => n.id));
    buildMs($("#msSearch").value.trim());
  };
});
const clearQuick = () => $$("#quick .chip").forEach((c) => c.classList.remove("on"));
$("#from").onchange = (e) => { if (e.target.value) { S.from = e.target.value; if (S.from > S.to) S.to = S.from; clearQuick(); changed(); } };
$("#to").onchange = (e) => { if (e.target.value) { S.to = e.target.value; if (S.to < S.from) S.from = S.to; clearQuick(); changed(); } };
$$("#quick .chip").forEach((c) => {
  c.onclick = () => {
    clearQuick(); c.classList.add("on");
    S.to = TODAY; S.from = addDays(TODAY, -(Number(c.dataset.days) - 1));
    changed();
  };
});
$("#todFrom").onchange = (e) => { S.todFrom = e.target.value || "00:00"; changed(); };
$("#todTo").onchange = (e) => { S.todTo = e.target.value || "23:59"; changed(); };
$("#minSize").onchange = (e) => { S.minSize = Number(e.target.value) || 0; changed(); };

function readNear() {
  const lat = parseFloat($("#nearLat").value), lon = parseFloat($("#nearLon").value);
  const km = Math.max(2, parseFloat($("#nearKm").value) || 50);
  S.loc = Number.isFinite(lat) && Number.isFinite(lon) ? { lat, lon, km } : null;
  changed();
}
function setNear(lat, lon, km) {
  $("#nearLat").value = lat.toFixed(4);
  $("#nearLon").value = lon.toFixed(4);
  if (km) $("#nearKm").value = km;
  GEO.mode = "centre";
  $$("#geoMode .chip").forEach((x) => x.classList.toggle("on", x.dataset.mode === "centre"));
  readNear();
}
["#nearLat", "#nearLon", "#nearKm"].forEach((id) => { $(id).onchange = readNear; });
$("#nearClear").onclick = () => { $("#nearLat").value = ""; $("#nearLon").value = ""; readNear(); };
$("#geoToggle").onclick = () => {
  const g = $("#geo");
  g.hidden = !g.hidden;
  $("#geoToggle").textContent = g.hidden ? "Map" : "Hide map";
  if (!g.hidden) renderGeo();
};
$("#copyUrl").onclick = () => copy(location.origin + location.pathname + "?" + shareQuery(), $("#copyUrl"), "Copy");
$("#reset").onclick = () => {
  S.nodeSel = null; S.to = TODAY; S.from = addDays(TODAY, -2);
  S.todFrom = "00:00"; S.todTo = "23:59"; S.minSize = 0; S.loc = null;
  S.sel.clear(); S.collapsed.clear();
  $("#nearLat").value = ""; $("#nearLon").value = ""; $("#nearKm").value = "50";
  clearQuick(); $$("#quick .chip")[1].classList.add("on");
  buildMs(""); changed();
};
$("#tlClear").onclick = () => {
  S.todFrom = "00:00"; S.todTo = "23:59";
  S.to = TODAY; S.from = addDays(TODAY, -2);
  clearQuick(); $$("#quick .chip")[1].classList.add("on");
  changed();
};

/* ══ Inline map ════════════════════════════════════════════════════════════
 * No basemap: an image host is not on this vhost's CSP and would be a third
 * party watching who looks at what. An equirectangular frame with a graticule
 * is enough to place a radius against the published positions. */
const GEO = { x0: -125, x1: -66, y0: 50, y1: 24, w: 900, h: 300, mode: "fit" };
const gx = (lon) => (lon - GEO.x0) / (GEO.x1 - GEO.x0) * GEO.w;
const gy = (lat) => (GEO.y0 - lat) / (GEO.y0 - GEO.y1) * GEO.h;
function fitExtent(pts) {
  const r = Math.PI / 180;
  if (GEO.mode === "centre" && S.loc) {
    const H = Math.max(S.loc.km * 2.2, 40), dLat = H / 110.57, dLon = H * 3 / (111.32 * Math.cos(S.loc.lat * r));
    GEO.y0 = S.loc.lat + dLat; GEO.y1 = S.loc.lat - dLat; GEO.x0 = S.loc.lon - dLon; GEO.x1 = S.loc.lon + dLon;
    return;
  }
  if (!pts.length) return;
  const lats = pts.map((n) => n.lat), lons = pts.map((n) => n.lon);
  const midLat = (Math.max.apply(null, lats) + Math.min.apply(null, lats)) / 2;
  const cos = Math.max(0.1, Math.cos(midLat * r));
  let hLat = (Math.max.apply(null, lats) - Math.min.apply(null, lats)) / 2 * 1.3 + 0.5;
  let hLonKm = (Math.max.apply(null, lons) - Math.min.apply(null, lons)) / 2 * 111.32 * cos * 1.15 + 50;
  const hLatKm = hLat * 110.57;
  if (hLonKm < hLatKm * 3) hLonKm = hLatKm * 3; else hLat = hLonKm / 3 / 110.57;
  const midLon = (Math.max.apply(null, lons) + Math.min.apply(null, lons)) / 2;
  const hLon = hLonKm / (111.32 * cos);
  GEO.y0 = midLat + hLat; GEO.y1 = midLat - hLat; GEO.x0 = midLon - hLon; GEO.x1 = midLon + hLon;
}
const gridStep = (span) => { for (const st of [0.1, 0.2, 0.5, 1, 2, 5, 10, 20]) if (span / st <= 8) return st; return 30; };
function renderGeo() {
  if ($("#geo").hidden) return;
  const pts = allNodes().filter(hasPos);
  const uncs = pts.map((n) => n.unc).filter(Number.isFinite);
  $("#geoUnc").textContent = uncs.length ? String(Math.max.apply(null, uncs)) : "—";
  fitExtent(pts);
  const r = Math.PI / 180;
  const stLon = gridStep(GEO.x1 - GEO.x0), stLat = gridStep(GEO.y0 - GEO.y1);
  const dec = (st) => (st < 1 ? 1 : 0);
  let h = "";
  for (let lon = Math.ceil(GEO.x0 / stLon) * stLon; lon <= GEO.x1; lon += stLon) {
    h += '<line class="grid" x1="' + gx(lon) + '" y1="0" x2="' + gx(lon) + '" y2="' + GEO.h + '"/>' +
         '<text class="gridlab" x="' + (gx(lon) + 3) + '" y="' + (GEO.h - 4) + '">' + lon.toFixed(dec(stLon)) + "&#176;</text>";
  }
  for (let lat = Math.ceil(GEO.y1 / stLat) * stLat; lat <= GEO.y0; lat += stLat) {
    h += '<line class="grid" x1="0" y1="' + gy(lat) + '" x2="' + GEO.w + '" y2="' + gy(lat) + '"/>' +
         '<text class="gridlab" x="3" y="' + (gy(lat) - 3) + '">' + lat.toFixed(dec(stLat)) + "&#176;</text>";
  }
  if (S.loc) {
    const rx = S.loc.km / (111.32 * Math.max(0.1, Math.cos(S.loc.lat * r))) * GEO.w / (GEO.x1 - GEO.x0);
    const ry = S.loc.km / 110.57 * GEO.h / (GEO.y0 - GEO.y1);
    const cx = gx(S.loc.lon), cy = gy(S.loc.lat);
    h += '<ellipse class="radius" cx="' + cx + '" cy="' + cy + '" rx="' + Math.max(rx, 6) + '" ry="' + Math.max(ry, 6) + '"/>' +
         '<line class="pin" x1="' + (cx - 7) + '" y1="' + cy + '" x2="' + (cx + 7) + '" y2="' + cy + '"/>' +
         '<line class="pin" x1="' + cx + '" y1="' + (cy - 7) + '" x2="' + cx + '" y2="' + (cy + 7) + '"/>';
  }
  const placed = [];
  const synth = hasSynthetic();
  for (const n of pts) {
    const x = gx(n.lon), y = gy(n.lat), ok = locOk(n), sel = isSelected(n);
    const unc = Number.isFinite(n.unc) ? n.unc : "?";
    const t = n.id + "\n" + n.lat.toFixed(4) + ", " + n.lon.toFixed(4) + " (published, ±" + unc + " km)" +
      (S.loc ? "\n" + distKm(n).toFixed(1) + " km from centre" : "");
    if (sel && ok && S.loc) h += '<circle class="ring" cx="' + x + '" cy="' + y + '" r="9"/>';
    h += '<circle class="node ' + (synth && n.synthetic ? "synth" : "real") + (ok ? "" : " far") +
      '" cx="' + x + '" cy="' + y + '" r="5" data-node="' + esc(n.id) + '"><title>' + esc(t) + "</title></circle>";
    /* Label placement: nudge down past anything already there, flip to the left
     * of the dot rather than running off the right edge, and give up rather
     * than overprint — a dense cluster (the synthetic fleet is 50 nodes inside
     * two degrees) is legible as dots with tooltips and illegible as a stack of
     * overlapping ids. */
    const w = n.id.length * 6.2;
    let lx = x + w + 8 > GEO.w ? x - w - 8 : x + 8;
    let ly = y - 6;
    const clash = () => placed.some((q) => Math.abs(q.y - ly) < 11 && lx < q.x + q.w && lx + w > q.x);
    let k = 0;
    while (k < 8 && clash()) { ly += 12; k++; }
    if (!clash()) {
      placed.push({ x: lx, y: ly, w });
      h += '<text class="nlab' + (ok ? "" : " far") + '" x="' + lx + '" y="' + ly + '">' + esc(n.id) + "</text>";
    }
  }
  $("#geoSvg").innerHTML = h;
}
$$("#geoMode .chip").forEach((c) => {
  c.onclick = () => {
    $$("#geoMode .chip").forEach((x) => x.classList.remove("on"));
    c.classList.add("on"); GEO.mode = c.dataset.mode; renderGeo();
  };
});
$("#geoSvg").onclick = (e) => {
  const hit = e.target.closest("[data-node]");
  if (hit) { const n = NODES.get(hit.dataset.node); if (n && hasPos(n)) setNear(n.lat, n.lon); return; }
  const box = $("#geoSvg").getBoundingClientRect();
  if (!box.width || !box.height) return;
  const x = (e.clientX - box.left) / box.width * GEO.w, y = (e.clientY - box.top) / box.height * GEO.h;
  setNear(GEO.y0 - y / GEO.h * (GEO.y0 - GEO.y1), GEO.x0 + x / GEO.w * (GEO.x1 - GEO.x0));
};

/* ══ Timeline ══════════════════════════════════════════════════════════════ */
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const dayOf = (ms) => new Date(ms).toISOString().slice(0, 10);
function mergeIntervals(fs) {
  return fs.slice().sort((a, b) => a.startMs - b.startMs).reduce((acc, f) => {
    const last = acc[acc.length - 1];
    if (last && f.startMs <= last[1] + 60e3) last[1] = Math.max(last[1], f.endMs);
    else acc.push([f.startMs, f.endMs]);
    return acc;
  }, []);
}
function timelineRows() {
  const eff = effectiveIds();
  const files = matches();
  const accent = cssVar("--accent") || "#3b82f6";
  const muted = cssVar("--text-muted") || "#94a3b8";
  const sel = effectiveNodes();
  $("#tlAggHint").hidden = sel.length <= 3;
  /* The component draws at most 3 rows (Earthdata Search's "3 collections"
   * limit, hardcoded), so past 3 nodes the rows have to aggregate. */
  if (sel.length <= 3) {
    return sel.map((n) => ({
      id: n.id, title: n.id,
      color: hasSynthetic() && n.synthetic ? muted : accent,
      intervals: mergeIntervals(files.filter((f) => f.node === n.id)),
    }));
  }
  if (!hasSynthetic()) {
    return [{
      id: "all", title: sel.length + " nodes", color: accent,
      intervals: mergeIntervals(files.filter((f) => eff.has(f.node))),
    }];
  }
  const real = sel.filter((n) => !n.synthetic), synth = sel.filter((n) => n.synthetic);
  const rows = [];
  if (real.length) rows.push({
    id: "real", title: real.length + " real node" + (real.length === 1 ? "" : "s"), color: accent,
    intervals: mergeIntervals(files.filter((f) => real.some((n) => n.id === f.node))),
  });
  if (synth.length) rows.push({
    id: "synth", title: synth.length + " synth node" + (synth.length === 1 ? "" : "s"), color: muted,
    intervals: mergeIntervals(files.filter((f) => synth.some((n) => n.id === f.node))),
  });
  return rows;
}
/* Every callback reports center/zoom; persist them or a re-render (any filter
 * change) snaps the view back to where it started. */
const TL = { center: null, zoom: 2 };
const syncTl = (p) => { if (p && p.center) TL.center = p.center; if (p && p.zoom != null) TL.zoom = p.zoom; };
function clampDay(d) { return d > TODAY ? TODAY : d; }
function onTemporalSet(p) {
  syncTl(p);
  const { temporalStart, temporalEnd } = p || {};
  if (!temporalStart || !temporalEnd) { $("#tlClear").click(); return; }
  S.from = clampDay(dayOf(temporalStart));
  S.to = clampDay(dayOf(temporalEnd - 1));
  if (S.to < S.from) S.to = S.from;
  if (temporalEnd - temporalStart < DAY_MS && S.from === S.to) {
    S.todFrom = hhmm(temporalStart); S.todTo = hhmm(temporalEnd);
  } else { S.todFrom = "00:00"; S.todTo = "23:59"; }
  clearQuick(); changed();
}
function onFocusedSet(p) {
  syncTl(p);
  const { focusedStart, focusedEnd } = p || {};
  if (!focusedStart || !focusedEnd) return;
  const span = focusedEnd - focusedStart;
  if (span > DAY_MS * 1.5) return;      // a month/year focus is too coarse to act on
  S.from = S.to = clampDay(dayOf(focusedStart));
  if (span < DAY_MS * 0.9) { S.todFrom = hhmm(focusedStart); S.todTo = hhmm(focusedEnd); }
  else { S.todFrom = "00:00"; S.todTo = "23:59"; }
  clearQuick(); changed();
}
function renderTimeline() {
  const rows = timelineRows();
  $("#tlRange").textContent = S.from + " → " + S.to +
    (S.todFrom !== "00:00" || S.todTo !== "23:59" ? " · " + S.todFrom + "–" + S.todTo + "Z" : "");
  if (!window.__tl) return;
  const start = Date.parse(S.from + "T00:00:00Z"), end = Date.parse(S.to + "T00:00:00Z") + DAY_MS;
  const partial = S.todFrom !== "00:00" || S.todTo !== "23:59";
  const tr = partial
    ? { start: Date.parse(S.from + "T" + S.todFrom + ":00Z"), end: Date.parse(S.to + "T" + S.todTo + ":00Z") }
    : { start, end };
  if (TL.center == null) TL.center = (start + end) / 2;
  window.__tl.render({
    data: rows, center: TL.center, zoom: TL.zoom, minZoom: 1, maxZoom: 3,
    temporalRange: tr, onTemporalSet, onFocusedSet,
    onTimelineMoveEnd: syncTl, onButtonZoom: syncTl, onScrollZoom: syncTl,
    onButtonPan: syncTl, onArrowKeyPan: syncTl, onDragPan: syncTl, onScrollPan: syncTl,
  });
}
window.addEventListener("timeline-ready", () => renderTimeline());

/* ══ Results ═══════════════════════════════════════════════════════════════ */
/* day -> the node set last rendered for it. A day here can hold ~1300 files
 * across ~50 nodes; opening all of them at once is a 100,000-pixel page nobody
 * can read, so node groups default to collapsed past three and the day shows
 * one summary row per node. The default is (re)applied only when the day's node
 * set actually changes, so a group the user opens stays open across renders —
 * and narrowing to a handful of nodes opens them again. */
const autoGrouped = new Map();

const SORTS = {
  span: (a, b) => b.endMs - a.endMs,
  size: (a, b) => b.size - a.size,
  key: (a, b) => a.name.localeCompare(b.name),
};
function spanCell(f) {
  return hhmm(f.startMs) + " → " + hhmm(f.endMs) + ' <span class="est">est.</span>';
}
function renderResults(m) {
  const box = $("#results");
  const days = daysBetween(S.from, S.to).filter((d) => d <= TODAY).reverse();
  const byDay = {};
  for (const f of m) (byDay[f.day] = byDay[f.day] || []).push(f);
  if (!days.length) { box.innerHTML = '<div class="empty-state">Pick a date range.</div>'; return; }

  let h = "";
  for (const day of days) {
    const e = dayEntry(day);
    const dfs = byDay[day] || [];
    const dcol = S.collapsed.has(day);
    const bytes = dfs.reduce((a, f) => a + f.size, 0);
    let statusCell = "";
    if (!e || e.status === "loading") statusCell = '<span class="muted">loading…</span>';
    else if (e.status === "error") statusCell = '<span style="color:var(--error)">failed</span>';
    h += '<div class="group' + (dcol ? " collapsed" : "") + '"><div class="day-head" data-toggle="' + day + '">' +
      '<span class="caret">&#9662;</span>' +
      '<input type="checkbox" data-selday="' + day + '"' + (dfs.length && dfs.every((f) => S.sel.has(f.key)) ? " checked" : "") + ">" +
      "<span>" + day + "</span>" + statusCell +
      '<span class="spacer"><span>' + dfs.length + " files</span><span>" + fmtBytes(bytes) + "</span></span></div>";
    if (!dcol) {
      if (e && e.status === "error") {
        h += '<div class="day-state err">Could not list this day: ' + esc(e.error) +
          '<button class="btn btn-secondary btn-sm" data-retry="' + day + '">Retry</button></div>';
      } else if (!e || e.status === "loading") {
        h += '<div class="day-state">Listing ' + day + "…</div>";
      } else if (!dfs.length) {
        h += '<div class="day-state">No files for this day under the current filters.</div>';
      } else {
        const byNode = {};
        for (const f of dfs) (byNode[f.node] = byNode[f.node] || []).push(f);
        const nodeIds = Object.keys(byNode).sort();
        const sig = nodeIds.join(",");
        if (autoGrouped.get(day) !== sig) {
          autoGrouped.set(day, sig);
          const many = nodeIds.length > 3;
          nodeIds.forEach((n) => {
            if (many) S.collapsed.add(day + "|" + n); else S.collapsed.delete(day + "|" + n);
          });
        }
        for (const node of nodeIds) {
          const nfs = byNode[node].slice().sort(SORTS[S.sort]);
          const gk = day + "|" + node;
          const ncol = S.collapsed.has(gk);
          h += '<div class="' + (ncol ? "collapsed" : "") + '"><div class="node-head" data-toggle="' + esc(gk) + '">' +
            '<span class="caret">&#9662;</span>' +
            '<input type="checkbox" data-selnode="' + esc(gk) + '"' + (nfs.every((f) => S.sel.has(f.key)) ? " checked" : "") + ">" +
            '<span class="nid">' + esc(node) + "</span>" +
            '<span class="spacer"><span>' + nfs.length + " files</span><span>" +
            fmtBytes(nfs.reduce((a, f) => a + f.size, 0)) + "</span></span></div>";
          if (!ncol) for (const f of nfs) {
            h += '<div class="frow-file' + (S.active === f.key ? " active" : "") + '" data-open="' + esc(f.key) + '">' +
              '<input type="checkbox" data-selfile="' + esc(f.key) + '"' + (S.sel.has(f.key) ? " checked" : "") + ">" +
              '<div><div class="kname">' + esc(f.name) + '</div><div class="ksub">' + esc(f.key) + "</div></div>" +
              '<div class="span">' + spanCell(f) + "</div>" +
              "<div>" + fmtBytes(f.size) + ' <span class="est">≈ ' + fmtBytes(f.size * JSON_FACTOR) + " JSON</span></div>" +
              '<div class="acts">' +
              '<button class="btn btn-outline btn-sm" data-open="' + esc(f.key) + '">Preview</button>' +
              '<a class="btn btn-outline btn-sm" href="' + esc(downloadUrl(f)) + '" target="_blank" rel="noopener" data-json="1">Open JSON</a>' +
              "</div></div>";
          }
          h += "</div>";
        }
      }
    }
    h += "</div>";
  }
  box.innerHTML = h;
}

/* ══ Stats & notices ═══════════════════════════════════════════════════════ */
function renderStats(m) {
  const bytes = m.reduce((a, f) => a + f.size, 0);
  const nodes = new Set(m.map((f) => f.node)).size;
  const days = daysBetween(S.from, S.to).filter((d) => d <= TODAY);
  const loaded = days.filter((d) => { const e = dayEntry(d); return e && e.status === "done"; }).length;
  const failed = days.filter((d) => { const e = dayEntry(d); return e && e.status === "error"; }).length;
  $("#stats").innerHTML =
    '<div class="stat-card accent"><div class="stat-label">Files matching</div>' +
      '<div class="stat-value">' + m.length.toLocaleString() + "</div>" +
      '<div class="stat-sub">' + loaded + " of " + days.length + " days listed" +
      (failed ? " · " + failed + " failed" : "") + "</div></div>" +
    '<div class="stat-card"><div class="stat-label">Stored size</div>' +
      '<div class="stat-value">' + fmtBytes(bytes) + "</div>" +
      '<div class="stat-sub">≈ ' + fmtBytes(bytes * JSON_FACTOR) + " as JSON (≈ " + JSON_FACTOR + "×)</div></div>" +
    '<div class="stat-card success"><div class="stat-label">Nodes with data</div>' +
      '<div class="stat-value">' + nodes + "</div>" +
      '<div class="stat-sub">of ' + NODES.size + " known</div></div>" +
    '<div class="stat-card warning"><div class="stat-label">Range</div>' +
      '<div class="stat-value">' + days.length + "d</div>" +
      '<div class="stat-sub">' + S.from + " → " + S.to + " UTC</div></div>";
}
function renderNotices() {
  const h = horizon();
  $("#horizonNotice").hidden = !h;
  if (h) {
    $("#horizonText").innerHTML = "<b>Files before " + esc(h) + " have been moved to cold storage and are not yet served here.</b> " +
      "Local disk keeps a few days (<code class=\"mono\">ARCHIVE_RETENTION_DAYS</code>); older files are offloaded to R2 " +
      "and no endpoint reads them back. The timeline and the list simply show nothing before that date.";
  }
  $("#nodesNotice").hidden = !nodesError;
  if (nodesError) $("#nodesErr").textContent = "/api/radar/nodes: " + nodesError + ".";
}
$("#nodesRetry").onclick = () => { nodesError = null; renderNotices(); loadNodes(); };

/* ══ Basket ════════════════════════════════════════════════════════════════ */
function selFiles() {
  return Array.from(S.sel).map((k) => filesByKey.get(k)).filter(Boolean)
    .sort((a, b) => b.endMs - a.endMs);
}
const downloadUrl = (f) => location.origin + "/api/data/archive/" + f.key;
/* No Content-Disposition on the download route, so a bare `curl -O` would write
 * the key's last segment (part-021907.parquet) over a JSON body, and -J has no
 * header to read. Name the output explicitly instead. */
const curlLine = (f) => 'curl -sS -o ' + f.node + "-" + f.name.replace(/\.[^.]+$/, "") + '.json "' + downloadUrl(f) + '"';
function renderBasket() {
  const s = selFiles();
  const bytes = s.reduce((a, f) => a + f.size, 0);
  $("#bCount").textContent = String(s.length);
  $("#bBytes").textContent = fmtBytes(bytes);
  $("#bJsonBytes").textContent = s.length ? "(≈ " + fmtBytes(bytes * JSON_FACTOR) + " downloaded as JSON)" : "";
  $("#manifest").value = s.map(downloadUrl).join("\n");
  $("#bCmd").textContent = s.length
    ? s.slice(0, 3).map(curlLine).join("\n") + (s.length > 3 ? "\n# … " + (s.length - 3) + " more; Copy curl takes all " + s.length : "")
    : "—";
  $("#bCurl").disabled = !s.length;
}
$("#bToggle").onclick = () => {
  const open = $("#bBody").classList.toggle("open");
  $("#bToggle").textContent = open ? "Hide manifest" : "Show manifest";
};
$("#bClear").onclick = () => { S.sel.clear(); render(); };
$("#bCurl").onclick = () => copy(selFiles().map(curlLine).join("\n"), $("#bCurl"), "Copy curl");

/* ══ Drawer ════════════════════════════════════════════════════════════════ */
const previews = new Map();   // key -> {status, data|error}

function drawerShell(f, inner) {
  const n = NODES.get(f.node);
  const posLine = n && hasPos(n)
    ? n.lat.toFixed(4) + ", " + n.lon.toFixed(4) + ' <span class="muted">published, ±' +
      (Number.isFinite(n.unc) ? n.unc : "?") + " km</span>"
    : '<span class="muted">no published position</span>';
  return '<dl class="kv">' +
    "<dt>Node</dt><dd>" + esc(f.node) + (n && !n.fromRegistry ? ' <span class="muted">(not in the node registry — offline or retired)</span>' : "") + "</dd>" +
    "<dt>Day</dt><dd>" + esc(f.day) + "</dd>" +
    "<dt>Coverage</dt><dd>" + hhmm(f.startMs) + " → " + hhmm(f.endMs) + " UTC " +
      '<span class="muted">est. — the listing gives only the write time; files cover the hour ending there</span></dd>' +
    "<dt>Stored size</dt><dd>" + fmtBytes(f.size) + " Parquet · ≈ " + fmtBytes(f.size * JSON_FACTOR) + " as JSON (≈ " + JSON_FACTOR + "×)</dd>" +
    "<dt>Published rx</dt><dd>" + posLine + "</dd>" +
    "</dl>" + inner;
}
function fetchBlock(f) {
  return '<div class="sec">Fetch</div><div class="codeblock">' + esc(curlLine(f)) + "</div>" +
    '<div class="hint">The download route answers <code class="mono">application/json</code> and sets no ' +
    "<code class=\"mono\">Content-Disposition</code>, so it opens inline and <code class=\"mono\">-O</code>/<code class=\"mono\">-J</code> " +
    "would misname it. Parquet, CSV and zip bundles are not served yet.</div>";
}
function renderDrawer() {
  const f = filesByKey.get(S.active);
  if (!f) return;
  $("#dTitle").textContent = f.name;
  $("#dKey").textContent = f.key;
  $("#dOpen").href = downloadUrl(f);
  const p = previews.get(f.key);
  let inner;
  if (!p) {
    inner = '<button class="btn btn-primary btn-sm" id="dLoad">Load preview · ≈ ' +
      fmtBytes(f.size * JSON_FACTOR) + "</button>" +
      '<div class="hint">The preview downloads the whole file — there is no range or head endpoint — ' +
      "so it is on demand rather than automatic.</div>" + fetchBlock(f);
  } else if (p.status === "loading") {
    inner = '<div class="muted">Downloading ≈ ' + fmtBytes(f.size * JSON_FACTOR) + "…</div>" + fetchBlock(f);
  } else if (p.status === "error") {
    inner = '<div class="errline">Preview failed: ' + esc(p.error) + "</div>" +
      '<button class="btn btn-secondary btn-sm" id="dLoad">Retry</button>' + fetchBlock(f);
  } else {
    inner = previewHtml(f, p.data) + fetchBlock(f);
  }
  $("#dBody").innerHTML = drawerShell(f, inner);
  const load = $("#dLoad");
  if (load) load.onclick = () => loadPreview(f);
}
function previewHtml(f, d) {
  const frames = Array.isArray(d.detections) ? d.detections : [];
  if (!frames.length) return '<div class="errline">The file decoded to zero frames.</div>';
  const ts = frames.map((fr) => Number(fr.timestamp)).filter(Number.isFinite);
  const t0 = Math.min.apply(null, ts), t1 = Math.max.apply(null, ts);
  const dets = frames.reduce((a, fr) => a + (Array.isArray(fr.delay) ? fr.delay.length : 0), 0);
  let adsbSlots = 0, adsbHits = 0;
  for (const fr of frames) {
    if (!Array.isArray(fr.adsb)) continue;
    adsbSlots += fr.adsb.length;
    for (const a of fr.adsb) if (a) adsbHits++;
  }
  const g = frames.find((fr) => Number.isFinite(fr.rx_lat)) || frames[0];
  const mode = frames.find((fr) => fr._signing_mode)?._signing_mode || null;
  const anyValid = frames.some((fr) => fr._signature_valid === true);
  const iso = (ms) => new Date(ms).toISOString().replace("T", " ").slice(0, 19) + "Z";
  const num = (v, dp) => (Number.isFinite(v) ? v.toFixed(dp) : "—");
  const rows = frames.slice(0, 5).map((fr) => {
    const has = Array.isArray(fr.delay) && fr.delay.length;
    return "<tr><td>" + (Number.isFinite(fr.timestamp) ? iso(fr.timestamp) : "—") + "</td>" +
      "<td>" + (has ? num(fr.delay[0], 3) : '<span class="muted">no detections</span>') + "</td>" +
      "<td>" + (has && Array.isArray(fr.doppler) ? num(fr.doppler[0], 2) : "—") + "</td>" +
      "<td>" + (has && Array.isArray(fr.snr) ? num(fr.snr[0], 2) : "—") + "</td></tr>";
  }).join("");
  return '<dl class="kv">' +
    "<dt>Reported node</dt><dd>" + esc(d.node_id || "—") + "</dd>" +
    "<dt>Frame span</dt><dd>" + iso(t0) + " → " + iso(t1) +
      " <span class=\"muted\">(" + ((t1 - t0) / 60000).toFixed(1) + " min, from the frames themselves)</span></dd>" +
    "<dt>Frames</dt><dd>" + frames.length.toLocaleString() + "</dd>" +
    "<dt>Detections</dt><dd>" + dets.toLocaleString() + " (" + (dets / frames.length).toFixed(2) + " per frame)</dd>" +
    "<dt>ADS-B match</dt><dd>" + (adsbSlots
      ? (100 * adsbHits / adsbSlots).toFixed(1) + "% of " + adsbSlots.toLocaleString() + " detections carry an ADS-B match"
      : '<span class="muted">no adsb column in this file</span>') + "</dd>" +
    "<dt>Signing</dt><dd>" + (mode ? esc(mode) + (anyValid ? " · signature valid" : "") : '<span class="muted">not recorded</span>') + "</dd>" +
    "<dt>rx (published)</dt><dd>" + num(g.rx_lat, 4) + ", " + num(g.rx_lon, 4) + " · " + num(g.rx_alt_ft, 1) + " ft</dd>" +
    "<dt>tx</dt><dd>" + num(g.tx_lat, 4) + ", " + num(g.tx_lon, 4) + " · " + num(g.tx_alt_ft, 1) + " ft</dd>" +
    "<dt>fc / fs</dt><dd>" + (Number.isFinite(g.fc_hz) ? (g.fc_hz / 1e6).toFixed(3) + " MHz" : "—") + " / " +
      (Number.isFinite(g.fs_hz) ? (g.fs_hz / 1e6).toFixed(3) + " MHz" : "—") + "</dd>" +
    "</dl>" +
    '<div class="warnline">⚠ <b>rx_lat / rx_lon are the published (fuzzed) receiver position</b>, not the true site. ' +
      "tx_* are true — transmitters are licensed towers.</div>" +
    '<div class="sec">First detection of the first 5 frames</div>' +
    '<div class="prev-wrap"><table class="prev"><thead><tr><th>timestamp</th><th>delay (µs)</th><th>doppler (Hz)</th><th>snr (dB)</th></tr></thead>' +
    "<tbody>" + rows + "</tbody></table></div>";
}
async function loadPreview(f) {
  previews.set(f.key, { status: "loading" });
  renderDrawer();
  try {
    const r = await fetch(downloadUrl(f), { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    previews.set(f.key, { status: "done", data: await r.json() });
  } catch (e) {
    previews.set(f.key, { status: "error", error: (e && e.message) || String(e) });
  }
  renderDrawer();
}
function openDrawer(key) {
  if (!filesByKey.has(key)) return;
  S.active = key;
  renderDrawer();
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  renderResults(matches());
}
function closeDrawer() {
  S.active = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  renderResults(matches());
}
$("#dClose").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("#dAdd").onclick = () => {
  if (!S.active) return;
  S.sel.add(S.active);
  if (!$("#bBody").classList.contains("open")) {
    $("#bBody").classList.add("open");
    $("#bToggle").textContent = "Hide manifest";
  }
  render();
};

/* ══ Result-list events ════════════════════════════════════════════════════ */
$("#results").addEventListener("click", (e) => {
  const retry = e.target.closest("[data-retry]");
  if (retry) { startLoad(retry.dataset.retry, true); render(); return; }
  if (e.target.closest("a[data-json]")) return;      // let the link do its job
  const cb = e.target.closest("input[type=checkbox]");
  if (cb) {
    e.stopPropagation();
    const m = matches();
    if (cb.dataset.selfile) { if (cb.checked) S.sel.add(cb.dataset.selfile); else S.sel.delete(cb.dataset.selfile); }
    if (cb.dataset.selday) {
      m.filter((f) => f.day === cb.dataset.selday).forEach((f) => (cb.checked ? S.sel.add(f.key) : S.sel.delete(f.key)));
    }
    if (cb.dataset.selnode) {
      const parts = cb.dataset.selnode.split("|");
      m.filter((f) => f.day === parts[0] && f.node === parts[1])
        .forEach((f) => (cb.checked ? S.sel.add(f.key) : S.sel.delete(f.key)));
    }
    render();
    return;
  }
  const op = e.target.closest("[data-open]");
  if (op) { openDrawer(op.dataset.open); return; }
  const tg = e.target.closest("[data-toggle]");
  if (tg) {
    const k = tg.dataset.toggle;
    if (S.collapsed.has(k)) S.collapsed.delete(k); else S.collapsed.add(k);
    render();
  }
});
$$("[data-sort]").forEach((s) => { s.onclick = () => { S.sort = s.dataset.sort; render(); }; });
$("#expandAll").onclick = () => { S.collapsed.clear(); render(); };
$("#collapseAll").onclick = () => { daysBetween(S.from, S.to).forEach((d) => S.collapsed.add(d)); render(); };
$("#selectAll").onclick = () => { matches().forEach((f) => S.sel.add(f.key)); render(); };

/* ══ Render ════════════════════════════════════════════════════════════════ */
let msSearchValue = "";
$("#msSearch").addEventListener("input", (e) => { msSearchValue = e.target.value.trim(); buildMs(msSearchValue); });

function syncControls() {
  $("#from").value = S.from;
  $("#to").value = S.to;
  $("#todFrom").value = S.todFrom;
  $("#todTo").value = S.todTo;
  $("#minSize").value = String(S.minSize);
  if (S.loc) { $("#nearLat").value = S.loc.lat.toFixed(4); $("#nearLon").value = S.loc.lon.toFixed(4); $("#nearKm").value = S.loc.km; }
  $("#nearClear").disabled = !S.loc;
  const synth = hasSynthetic();
  $("#swSynthWrap").hidden = !synth;
  $("#swRealLabel").textContent = synth ? "real node" : "node";
  $("#msReal").hidden = !synth;
  const eff = effectiveNodes();
  const total = NODES.size;
  const chosen = S.nodeSel ? S.nodeSel.size : total;
  $("#msLabel").textContent = S.loc
    ? eff.length + " of " + chosen + " within " + S.loc.km + " km"
    : !S.nodeSel ? "All nodes" : chosen === 0 ? "No nodes" : chosen === 1
      ? Array.from(S.nodeSel)[0] : chosen + " nodes";
}
function render() {
  ensureLoads();
  syncControls();
  writeUrl();
  const m = matches();
  $("#resultCount").textContent = m.length + " files · " + fmtBytes(m.reduce((a, f) => a + f.size, 0));
  renderNotices();
  renderStats(m);
  buildMs(msSearchValue);
  renderGeo();
  renderTimeline();
  renderResults(m);
  renderBasket();
  if (S.active) renderDrawer();
}
/* A filter change can invalidate what is cached for a day (a narrower node
 * scope needs a different listing), so `changed` re-enters the load planner. */
function changed() { render(); }

/* ══ Go ════════════════════════════════════════════════════════════════════ */
readUrl();
$$("#quick .chip").forEach((c) => {
  const days = Number(c.dataset.days);
  c.classList.toggle("on", S.to === TODAY && S.from === addDays(TODAY, -(days - 1)));
});
render();
loadNodes();

})();
