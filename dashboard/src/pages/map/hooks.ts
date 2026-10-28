import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { API_BASE, ARC_TOTAL_LIFE_MS, MAX_HISTORY } from "./constants";
import { upsertArcEntries } from "./arcBuffer";
import { updateDetections } from "./detections";
import { MAX_TRAIL_POINTS, mergeTrailPositions } from "./trails";
import { validLatLon } from "./geo";
import type { RadarNode } from "./types";
import { defaultFeedMode, type FeedMode } from "./feedMode";
import { isSyntheticNode } from "../../utils/nodeKind";
import {
  fromSyntheticNode,
  scrubToSyntheticNodes,
  syntheticDetectingNodes,
} from "./syntheticOnly";
import { inFleet, ownedMapNodes, radarNodeFrom, withOwnedNodes, type AnalyticsEntry } from "./nodeListing";
import { UnauthorizedError, request, type CurrentUser } from "@retina/shared";
import { api } from "../../api/client";
import type { OwnedNode } from "../../types";
import { usePolling } from "../../hooks/usePolling";
import { useAuth as useConsoleAuth } from "../../context/AuthContext";

/** The close code the server ends the owner socket with when the session behind
 *  it no longer resolves (backend/services/owner_feed.py). */
const POLICY_VIOLATION = 1008;

/** Exponential backoff: 3s, 6s, 12s … capped at 30s */
function reconnectDelay(attempt: number): number {
  return Math.min(3000 * Math.pow(2, attempt), 30000);
}

/** The mounted useMapAuth calls, each taking in an owner list fetched outside
 *  it (sessionHolds). */
const ownedListTakers = new Set<(nodes: OwnedNode[]) => void>();

/**
 * Whether the session behind the owner socket still holds, asked of an owner
 * route through the console's client: a 401 there ends the session for the
 * whole console (api/client.ts), and the map goes on as the visitor's. Any
 * other failure leaves it undecided, which counts as holding: the socket is
 * retried as it always was. An answer is the owned set as it stands now,
 * which the map takes in.
 */
async function sessionHolds(): Promise<boolean> {
  try {
    const nodes = await api.myNodes();
    for (const take of ownedListTakers) take(nodes);
    return true;
  } catch (err) {
    return !(err instanceof UnauthorizedError);
  }
}

/**
 * The `recent_positions` array a feed trail was merged from, keyed by the
 * trail. Replay hands a track the same array while its row is unchanged
 * (synthesizeFrame), and merging that array again into the trail it made adds
 * nothing: each of its points is either no newer than the trail's last point
 * or at that point's position (mergeTrailPositions). So the trail is kept, not
 * copied. Keyed by the trail, the record goes with it on a prune, an append or
 * a reset. Live frames are parsed fresh, so they always merge.
 */
const trailMergedFrom = new WeakMap<object, unknown>();

/** A frame handed to the feed instead of a socket message (archive replay). */
export interface InjectedFrame {
  now: number;                 // seconds since epoch: the frame's clock (the replay instant)
  aircraft: any[];
  ground_truth?: Record<string, unknown>;
  ground_truth_meta?: Record<string, unknown>;
  anomaly_hexes?: string[];
  detecting_nodes?: Record<string, unknown>;
  detection_arcs?: any[];
  /** Clear every store before ingesting this frame (a seek). */
  reset?: boolean;
}

export interface FeedSource {
  /** Start delivering frames; returns the unsubscribe. */
  subscribe(onFrame: (frame: InjectedFrame) => void): () => void;
}

/**
 * Manages the WebSocket connection to /ws/aircraft with auto-reconnect,
 * plus an HTTP polling fallback when WS is unavailable.
 *
 * When `ownerOnly` is true, connects to /ws/aircraft/owner instead — a
 * server-filtered feed authenticated by the auth_token cookie that only emits
 * aircraft/arcs for nodes the logged-in user owns. The HTTP polling fallback
 * is disabled in this mode because the public aircraft.json is unfiltered and
 * would leak other nodes' data. The server closes that socket with 1008 when
 * the session behind it ends, which is not retried like a dropped connection:
 * see onclose below.
 *
 * `mode` names which fleet the caller shows (see feedMode.ts). It arrives as an
 * argument rather than being read from the hostname here, because it is a
 * property of the page: /map takes the hostname's answer, and the admin
 * console's /sim asks for the synthetic fleet whatever the host. It is in
 * every dependency list below — a different mode is a different feed, and the
 * socket has to be reopened onto it rather than left streaming the one the
 * page opened with.
 *
 * `source` stands in for the socket and the poll (archive replay, see
 * replay/). While one is set, no socket opens, nothing is polled and
 * `connected` stays false; each frame it delivers is ingested as a socket
 * message would be, but on the frame's own clock (`now`), so trails, arcs and
 * detections age against the replay instant rather than the wall clock. A
 * different source, or none, is a different scope, like a different mode.
 *
 * `resetEpoch` counts the times the stores were emptied: every scope change,
 * and every injected frame that asks for it (a seek).
 */
export function useAircraftFeed(
  ownerOnly = false,
  mode: FeedMode = defaultFeedMode(),
  source: FeedSource | null = null,
) {
  const [aircraft, setAircraft] = useState([]);
  const [connected, setConnected] = useState(false);
  const [resetEpoch, setResetEpoch] = useState(0);
  const injected = source != null;

  const trailsRef = useRef({});
  const groundTruthRef = useRef({});
  const groundTruthMetaRef = useRef({});
  const anomalyHexesRef = useRef(new Set());
  const [trailTick, setTrailTick] = useState(0);
  // Separate tick that only increments when ground-truth data is replaced —
  // prevents the 8000-entry truthOnlyAircraft memo from re-running on every
  // trail update (which happens every WS message via updateTrails).
  const [groundTruthTick, setGroundTruthTick] = useState(0);

  const wsRef = useRef(null);
  const reconnectTimer = useRef(null);
  const reconnectAttempts = useRef(0);
  // Owner sockets the server has closed with 1008 in this scope. Unlike the
  // attempts above, an open does not reset it (see onclose).
  const policyCloses = useRef(0);
  // Bumped when the owning effect cleans up, so a session check still out
  // when the scope changes or the map unmounts reopens nothing.
  const feedScope = useRef(0);
  // True once the owning effect has cleaned up — stops the async onclose
  // handler from resurrecting the socket after unmount.
  const wsClosedRef = useRef(false);
  const pausedRef = useRef(false);
  const historyRef = useRef([]);
  // Watchdog: timestamp of last received WS message — detects zombie connections
  // where the server has dropped us but onclose never fires (dead TCP, no FIN)
  const lastMsgRef = useRef(Date.now());

  // Detection arc accumulation buffer: key → ArcBufferEntry (see arcBuffer.ts:
  // {hex, node_ref, ambiguity_arc, delay_us, alt_baro, doppler_hz,
  // target_class, ts}).  Keyed by hex + node + measured delay (quantized to
  // 0.1 µs) so an unchanged measurement refreshes one entry's fade clock
  // rather than stacking parallel strokes.  Entries persist for
  // ARC_TOTAL_LIFE_MS after last refresh, enabling fade-out per detection.
  const arcsBufferRef = useRef({});

  // Detection-presence oracle: "hex|node_ref" → ts of last time that node
  // contributed to a track for that aircraft.  Populated from EVERY detection
  // shape (single-node arc, single-node no-arc, and multinode via
  // contributing_node_refs), so it is a complete "is this aircraft currently
  // detected by this node" record — unlike the arc buffer, which only holds
  // arc-bearing single-node detections.  TTL-pruned on the same grace window
  // as the arc buffer (don't flag a detection that only just expired).
  const detectionsRef = useRef({});

  const setPaused = useCallback((val) => {
    pausedRef.current = val;
  }, []);

  // Prune trails for aircraft gone > 5 minutes — keeps memory bounded over long sessions
  const trailPruneRef = useRef(0);

  // Shared trail update logic used by both WS and HTTP polling. `nowS` is the
  // feed's clock in seconds: the wall clock live, the frame's in replay, so a
  // replayed trail hours old is pruned against the replay instant.
  const updateTrails = useCallback((newAircraft, nowS = Date.now() / 1000) => {
    const trails = trailsRef.current;
    for (const ac of newAircraft) {
      if (!validLatLon(ac.lat, ac.lon)) continue;
      const hex = ac.hex;
      if (ac.recent_positions && ac.recent_positions.length > 0) {
        const held = trails[hex];
        // Skipping relies on mergeTrailPositions giving the same trail when the same
        // array is merged again, so a change there must keep that.
        if (!held || trailMergedFrom.get(held) !== ac.recent_positions) {
          const merged = mergeTrailPositions(held || [], ac.recent_positions);
          trailMergedFrom.set(merged, ac.recent_positions);
          trails[hex] = merged;
        }
      } else {
        const existing = trails[hex] || [];
        const last = existing[existing.length - 1];
        if (
          !last ||
          Math.abs(last[0] - ac.lat) > 0.00005 ||
          Math.abs(last[1] - ac.lon) > 0.00005
        ) {
          trails[hex] = [...existing, [ac.lat, ac.lon, ac.alt_baro || 0, nowS]].slice(-MAX_TRAIL_POINTS);
        }
      }
    }
    // Prune stale trail entries every 60 updates (~60s) to prevent unbounded growth
    trailPruneRef.current += 1;
    if (trailPruneRef.current >= 60) {
      trailPruneRef.current = 0;
      const activeHexes = new Set(newAircraft.map((ac) => ac.hex));
      const cutoff = nowS - 300; // 5 minutes
      for (const hex of Object.keys(trails)) {
        if (activeHexes.has(hex)) continue;
        const trail = trails[hex];
        const lastTs = trail?.[trail.length - 1]?.[3] ?? 0;
        if (lastTs < cutoff) delete trails[hex];
      }
    }
    setTrailTick((t) => t + 1);
  }, []);

  // Shared history + state update. `nowMs` is the clock every store is stamped
  // and aged on; `fromSource` marks a frame from an injected source.
  const ingestAircraft = useCallback(
    (
      rawAircraft, groundTruth, groundTruthMeta, anomalyHexes, rawDetectingNodes, rawArcs,
      { nowMs = Date.now(), fromSource = false } = {},
    ) => {
      // A synthetic page reads the unfiltered feed, because the real-only one
      // carries no synthetic fleet to show, so the real nodes come off here
      // instead. Everything node-attributed goes together or the map contradicts
      // itself: an aircraft kept without its node is a detection nothing on the
      // map made, and the detail panel prints detecting_nodes by name. Each kept
      // entry is then scrubbed as well, because one can name both fleets at once
      // (see syntheticOnly.ts).
      const syntheticOnly = mode === "synthetic";
      const newAircraft = syntheticOnly
        ? (rawAircraft || []).filter(fromSyntheticNode).map(scrubToSyntheticNodes)
        : rawAircraft;
      const detectionArcs = syntheticOnly
        ? (rawArcs || []).filter(fromSyntheticNode).map(scrubToSyntheticNodes)
        : rawArcs;
      const detectingNodes = syntheticOnly
        ? syntheticDetectingNodes(rawDetectingNodes)
        : rawDetectingNodes;

      // The ring is the live feed's pause buffer, and an injected frame stays
      // out of it. In archive replay the archive is the history and the live
      // PlaybackBar is hidden, and 150 replay frames of up to 500 aircraft
      // carrying 400-point trails would hold hundreds of MB.
      if (!fromSource) {
        historyRef.current.push({ aircraft: newAircraft, ts: nowMs });
        if (historyRef.current.length > MAX_HISTORY) historyRef.current.shift();
      }

      if (!pausedRef.current) setAircraft(newAircraft);
      if (groundTruth && typeof groundTruth === "object") {
        groundTruthRef.current = groundTruth;
        setGroundTruthTick((t) => t + 1);
      }
      if (groundTruthMeta && typeof groundTruthMeta === "object") {
        groundTruthMetaRef.current = groundTruthMeta;
      }
      if (Array.isArray(anomalyHexes)) {
        anomalyHexesRef.current = new Set(anomalyHexes);
      }

      // Accumulate detection arcs as a radar-style afterglow trail.  Keyed
      // by hex + node + MEASURED delay (see arcBuffer.ts): re-ingesting an
      // unchanged measurement refreshes the one existing ellipse's fade
      // clock — a stationary aircraft stays visibly bright as a single
      // stroke instead of stacking five parallel offset strokes — while a
      // changed delay lays a new ellipse at new geometry, which is the
      // trail.  Also prunes entries older than ARC_MAX_AGE_MS (already
      // faded to zero opacity in the renderer) to keep the buffer bounded.
      const ARC_MAX_AGE_MS = ARC_TOTAL_LIFE_MS;
      // Aircraft entries carry at most ONE arc per hex (the feed dedups to a
      // single winner); the top-level detection_arcs channel carries every
      // node's measured-delay arc, ADS-B tracks included.  Same measurement
      // via both channels lands on one buffer key (hex + node + quantized
      // delay), so ingesting both never double-draws.  The hex filter drops
      // entries from a backend too old to stamp them (rolling deploy).
      upsertArcEntries(
        arcsBufferRef.current,
        [...newAircraft, ...(detectionArcs || []).filter((a) => a.hex)],
        nowMs,
        ARC_MAX_AGE_MS,
      );

      // Detection-presence oracle (consumed by InBeamDiagnostic and the
      // GT debug panel).  Unions per-aircraft signals with the top-level
      // detecting_nodes feed key — see detections.ts.
      updateDetections(
        detectionsRef.current, newAircraft, detectingNodes, nowMs, ARC_MAX_AGE_MS,
      );

      updateTrails(newAircraft, nowMs / 1000);
    },
    [updateTrails, mode],
  );

  // Empties every store the feed accumulates: on a scope change, and before
  // an injected frame that asks for it (a seek in archive replay).
  const clearStores = useCallback(() => {
    setAircraft([]);
    trailsRef.current = {};
    groundTruthRef.current = {};
    groundTruthMetaRef.current = {};
    anomalyHexesRef.current = new Set();
    arcsBufferRef.current = {};
    detectionsRef.current = {};
    historyRef.current = [];
    trailPruneRef.current = 0;
    setTrailTick((t) => t + 1);
    setGroundTruthTick((t) => t + 1);
    setResetEpoch((e) => e + 1);
  }, []);

  // --- WebSocket connection with reconnect ---
  const connectWs = useCallback(() => {
    if (wsRef.current || wsClosedRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    // Owner mode overrides the public feeds with a server-filtered, cookie-authed feed.
    // Otherwise a real-fleet page streams only the real nodes; the synthetic and
    // all-fleet pages take the unfiltered feed and sort it out client-side.
    const wsPath = ownerOnly
      ? "/ws/aircraft/owner"
      : mode === "real" ? "/ws/aircraft/live" : "/ws/aircraft";
    const ws = new WebSocket(`${proto}//${window.location.host}${wsPath}`);
    // A closed socket may still have queued callbacks. A scope switch resets
    // wsClosedRef for the new connection, so also check the socket identity.
    const isCurrent = () => !wsClosedRef.current && wsRef.current === ws;

    ws.onopen = () => {
      if (!isCurrent()) return;
      setConnected(true);
      reconnectAttempts.current = 0;  // reset backoff on successful connect
      lastMsgRef.current = Date.now(); // reset watchdog so we don't misfire on slow first message
    };

    ws.onmessage = (evt) => {
      if (!isCurrent()) return;
      lastMsgRef.current = Date.now(); // keep watchdog alive
      try {
        const data = JSON.parse(evt.data);
        ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes, data.detecting_nodes, data.detection_arcs);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = (event) => {
      // Unmounted: onclose fires *after* the effect cleanup ran, so without
      // this guard it re-scheduled connectWs and opened a fresh socket that
      // outlived the component (and called setConnected on an unmounted one).
      if (!isCurrent()) return;
      setConnected(false);
      wsRef.current = null;
      if (ownerOnly && event?.code === POLICY_VIOLATION) {
        // The owner socket closed by policy, which is how the server ends it
        // once the session behind it has gone; reopening it would only be
        // refused. So the session is asked first. Ended, the console signs out
        // (sessionHolds) and this scope gives way to the visitor's, with the
        // socket left closed. Still there, the socket closed for some other
        // reason and is reopened, on a backoff an open does not reset: a
        // server that keeps accepting it and closing it again is asked at most
        // every 30 s, not every 3.
        const delay = reconnectDelay(policyCloses.current);
        policyCloses.current += 1;
        const scope = feedScope.current;
        sessionHolds().then((holds) => {
          if (holds && scope === feedScope.current) reconnectTimer.current = setTimeout(connectWs, delay);
        });
        return;
      }
      const delay = reconnectDelay(reconnectAttempts.current);
      reconnectAttempts.current += 1;
      reconnectTimer.current = setTimeout(connectWs, delay);
    };

    ws.onerror = () => { if (isCurrent()) ws.close(); };
    wsRef.current = ws;
  }, [ingestAircraft, ownerOnly, mode]);

  useEffect(() => {
    // Feed scope owns all accumulated state, including optional channels the
    // next feed may never send. Reconnects within the same scope keep history.
    clearStores();
    setConnected(false);
    pausedRef.current = false;
    reconnectAttempts.current = 0;
    policyCloses.current = 0;
    if (source) {
      // The source is the whole feed for this scope: no socket opens (an old
      // one's queued callbacks stay ignored) and nothing is polled. A frame it
      // delivers after the scope ended is dropped.
      wsClosedRef.current = true;
      const scope = feedScope.current;
      const unsubscribe = source.subscribe((frame) => {
        if (scope !== feedScope.current) return;
        if (frame.reset) clearStores();
        ingestAircraft(
          frame.aircraft || [], frame.ground_truth, frame.ground_truth_meta, frame.anomaly_hexes,
          frame.detecting_nodes, frame.detection_arcs, { nowMs: frame.now * 1000, fromSource: true },
        );
      });
      return () => {
        feedScope.current += 1;
        unsubscribe();
      };
    }
    wsClosedRef.current = false;
    connectWs();
    return () => {
      wsClosedRef.current = true;
      feedScope.current += 1;
      clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onopen = null;
        wsRef.current.onmessage = null;
        wsRef.current.onclose = null;
        wsRef.current.onerror = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [clearStores, connectWs, ingestAircraft, source]);

  // --- Zombie-connection watchdog ---
  // Server sends aircraft data every ~2s. If we've had no message for 12s while
  // the WS appears OPEN, the connection is a zombie (server dropped us, TCP
  // still "open" with no FIN — onclose never fires). Force-close to trigger
  // the reconnect path and restart HTTP polling fallback. An injected source
  // has no socket to watch.
  useEffect(() => {
    if (injected) return;
    const WATCHDOG_MS = 12_000;
    const id = setInterval(() => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        if (Date.now() - lastMsgRef.current > WATCHDOG_MS) {
          ws.close(); // triggers onclose → reconnect + HTTP fallback
        }
      }
    }, 5_000);
    return () => clearInterval(id);
  }, [injected]);

  // --- HTTP polling fallback ---
  useEffect(() => {
    if (connected) return;
    // An injected source replaces the live feed, the fallback included.
    if (injected) return;
    // No HTTP fallback in owner mode: the public aircraft.json is unfiltered,
    // so polling it would leak other nodes' data. Wait for the WS to reconnect.
    if (ownerOnly) return;
    // On a real-fleet page use the real-node-only endpoint so unfiltered synthetic
    // aircraft never appear even when the WS is temporarily disconnected.
    const pollPath = mode === "real"
      ? `${API_BASE}/radar/data/aircraft-live.json`
      : `${API_BASE}/radar/data/aircraft.json`;
    const controller = new AbortController();
    let nextRequest = 0;
    let latestSettled = 0;
    const doFetch = async () => {
      const seq = ++nextRequest;
      try {
        const data = await request(pollPath, { signal: controller.signal });
        if (controller.signal.aborted || seq < latestSettled) return;
        latestSettled = seq;
        ingestAircraft(data.aircraft || [], data.ground_truth, data.ground_truth_meta, data.anomaly_hexes, data.detecting_nodes, data.detection_arcs);
      } catch {
        /* a non-2xx answer, a network error or the timeout: the next tick asks again; the abort is the unmount */
      }
    };
    // Fire immediately so data appears before the first interval tick.
    // This cuts the blank-map startup window from ~1s to the HTTP round-trip.
    doFetch();
    const interval = setInterval(doFetch, 1000);
    return () => {
      clearInterval(interval);
      controller.abort();
    };
  }, [connected, injected, ingestAircraft, ownerOnly, mode]);

  return {
    aircraft,
    connected,
    trailsRef,
    groundTruthRef,
    groundTruthMetaRef,
    anomalyHexesRef,
    trailTick,
    groundTruthTick,
    historyRef,
    setPaused,
    arcsBufferRef,
    detectionsRef,
    resetEpoch,
  };
}

// Receiver anonymization is entirely the backend's, applied where the bytes
// are serialized (backend/services/public_location.py): coordinates are
// displaced deterministically per node before they go on the wire, so no
// public route hands the browser a true receiver position and the client does
// no fuzzing of its own. What the client does own is disclosing that: the feed
// declares the outer displacement radius as location_uncertainty_km, and the
// map draws it (see NodeMarkersLayer in LiveAircraftMap.tsx). The one true
// position the map ever holds is a signed-in owner's own node, read from the
// owner API, which answers only that owner's session (see useNodes).

/**
 * Fetch radar node positions for coverage zones.
 *
 * `mode` is the page's fleet (see feedMode.ts), on the same footing as the
 * aircraft feed's: the node listing has to agree with the tracks drawn over it,
 * so both take the mode from the page and the poll re-runs when it changes.
 *
 * `owner` is the signed-in owner, as useMapAuth gives it: null when nobody is
 * signed in and where the page withholds the owner view. The public listing is
 * the same for every caller and leaves out every private node, its owner's
 * included, so an owner's own nodes come from the owner API instead and take
 * the place of their public entries (nodeListing.ts): the private ones are
 * added, and every one is at its true position. With no owner, no owned node
 * or no answer from the owner API, this is the public listing, as it was.
 */
export function useNodes(mode: FeedMode = defaultFeedMode(), owner: Pick<CurrentUser, "id"> | null = null): RadarNode[] {
  // A failed poll keeps the last listing on the map; the next one asks again.
  const { data } = usePolling(() => loadNodes(mode), 30_000, mode);
  const owned = useOwnedMapNodes(mode, owner);
  const listed = data ?? NO_NODES;
  return useMemo(() => withOwnedNodes(listed, owned), [listed, owned]);
}

async function loadNodes(mode: FeedMode): Promise<RadarNode[]> {
  // A real-fleet page asks the backend for the real nodes only — avoids
  // relying on a client-side filter to drop 900+ synthetic markers.
  const url = mode === "real"
    ? `${API_BASE}/radar/analytics?real_only=true`
    : `${API_BASE}/radar/analytics`;
  const data = await request(url);
  const nodeList: RadarNode[] = [];
  // The analytics nodes map is keyed on node_ref; the values carry no
  // identifier of their own.
  for (const [ref, info] of Object.entries(data.nodes || {})) {
    const synthetic = isSyntheticNode(info as { is_synthetic?: boolean }, ref);
    // On a real-fleet page: the backend already strips synthetic nodes from
    // real_only feeds; this is defence in depth against a leftover leak,
    // decided from the server's own is_synthetic flag rather than parsed from
    // the identifier. See src/utils/nodeKind.ts.
    //
    // On a synthetic page it is the mirror, and the only filter standing
    // between a real node and that page: this listing has no real_only-style
    // parameter for "synthetic only", so the page has to drop them itself.
    if (!inFleet(mode, synthetic)) continue;
    const node = radarNodeFrom(ref, info as AnalyticsEntry, synthetic);
    if (node) nodeList.push(node);
  }
  return nodeList;
}

/** An owner's nodes, with the owner and fleet they were asked for. */
interface OwnedAnswer {
  key: string;
  nodes: RadarNode[];
}

// Nothing to ask for: settles at once, so no request leaves the page.
const nothing = () => Promise.resolve(null);

/**
 * The owner's own nodes as the map draws them, or null when there are none to
 * draw: nobody signed in, or the owner API has not answered yet or failed.
 *
 * Polled beside the public listing rather than inside its request, so the
 * public nodes never wait on the owner's (on a cold cache the owner's summaries
 * are built under a lock the refresh task may be holding), and an answer is
 * kept through a failed refresh as the public one is. The answer carries whose
 * nodes on which fleet it holds, so one owner's nodes are never drawn for the
 * next, or for nobody, while the new answer is on its way.
 */
function useOwnedMapNodes(mode: FeedMode, owner: Pick<CurrentUser, "id"> | null): RadarNode[] | null {
  const key = owner ? `${mode}\u0000${String(owner.id)}` : null;
  const { data } = usePolling<OwnedAnswer | null>(
    key === null
      ? nothing
      : () =>
        Promise.all([api.myNodes(), api.myAnalytics()])
          .then(([nodes, analytics]) => ({ key, nodes: ownedMapNodes(nodes, analytics, mode) })),
    key === null ? 0 : 30_000,
    key ?? "",
  );
  return key !== null && data?.key === key ? data.nodes : null;
}

/** Shared across renders so a caller memoising on this list is not woken by a
 *  fresh empty array each time. */
const NO_NODES = [];

/**
 * The console's identity plus the node refs that user owns. `user` is null
 * when nobody is signed in, and also where `ownerView` is off, so the owner
 * view is never offered and ownership is never asked. Passes on whether the
 * server runs a synthetic fleet, which gates ground truth.
 */
export function useMapAuth(ownerView = true) {
  const { user: signedIn, loading, syntheticFleet } = useConsoleAuth();
  const user = ownerView ? signedIn : null;
  // null while ownership is unsettled, which the map must not read as owning
  // nothing: the owner panel would render mid-flight with a count of zero.
  const [ownedNodeRefs, setOwnedNodeRefs] = useState(null);

  useEffect(() => {
    if (loading || !user) return;
    let cancelled = false;
    // /api/auth/me/nodes carries both identifiers; take the ref, which is
    // the key space the analytics node map and the aircraft feed use. It
    // is null for an owned node with no registry row, and a null in this
    // list counts as a node the owner has on the map.
    const take = (myNodes: OwnedNode[]) => {
      if (!cancelled) setOwnedNodeRefs((myNodes || []).map((n) => n.node_ref).filter(Boolean));
    };
    // The owner socket's session check reads the same list (sessionHolds).
    ownedListTakers.add(take);
    (async () => {
      let myNodes = [];
      try {
        // Through the console's client, so a 401 here ends the session as it
        // does on every other page.
        myNodes = await api.myNodes();
      } catch {
        // Unanswered ownership is owning nothing, not unsettled: null would
        // hold the owner panel in its loading state for the session.
      }
      take(myNodes);
    })();
    return () => {
      cancelled = true;
      ownedListTakers.delete(take);
    };
  }, [loading, user]);

  // Derived from `user` rather than held: nobody signed in owns nothing and
  // waits for nothing, whatever a previous session fetched.
  return {
    user,
    ownedNodeRefs: user ? ownedNodeRefs ?? NO_NODES : NO_NODES,
    loading: loading || (!!user && ownedNodeRefs === null),
    syntheticFleet,
  };
}
