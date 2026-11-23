import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useHref, useSearchParams } from "react-router-dom";

import { ClaimPathNote } from "../../components/ClaimPath";
import { Notice } from "../../components/Notice";
import { ScopeSwitch } from "../../components/ScopeSwitch";
import { StatCard } from "../../components/StatCard";
import { useAuth } from "../../context/AuthContext";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { useOwnedNodes } from "../../hooks/useOwnedNodes";
import type { Scope } from "../../hooks/useScopeParam";
import { PHONE } from "../../styles/breakpoints";
import { formatBytes } from "../../utils/format";
import { type ArchiveSource, entryForScope, horizon, routeFor } from "./dataExplorer/archive";
import { AvailabilityTimeline } from "./dataExplorer/AvailabilityTimeline";
import { DateRangeControls, sizeLabel } from "./dataExplorer/DateRangeControls";
import { daysBetween, todayUTC } from "./dataExplorer/dates";
import { DownloadBasket } from "./dataExplorer/DownloadBasket";
import type { RoutedFile } from "./dataExplorer/download";
import { FilePreviewDrawer } from "./dataExplorer/FilePreviewDrawer";
import { makePredicate } from "./dataExplorer/filters";
import { JSON_FACTOR, type ArchiveFile } from "./dataExplorer/keys";
import { NearControls } from "./dataExplorer/NearControls";
import { NodeMap } from "./dataExplorer/NodeMap";
import { NodePicker, selectionLabel } from "./dataExplorer/NodePicker";
import {
  anyNodeSynthetic,
  centreOf,
  effectiveNodeIds,
  isSynthetic,
  nodeMarks,
  privateRefs,
  scopeAdmits,
  selectionIn,
} from "./dataExplorer/nodes";
import { ResultsTree, type SortKey } from "./dataExplorer/ResultsTree";
import { useArchiveScan } from "./dataExplorer/useArchiveScan";
import { useCopy } from "./dataExplorer/useCopy";
import { type NodeRegistry, ownerRegistry, useNodeRegistry } from "./dataExplorer/useNodeRegistry";
import {
  DEFAULT_RADIUS_KM,
  defaultFilters,
  type ExplorerFilters,
  isNearNode,
  readFilters,
  writeFilters,
} from "./dataExplorer/urlState";
import { rememberPrivate, withheldFrom } from "./dataExplorer/withheld";

import "./dataExplorer/dataExplorer.css";

/** What the header says the page lists. A visitor's, and an account's with no
 *  nodes, is the archive as it always was. */
function description(scope: Scope, hasNodes: boolean, hasPrivate: boolean): string {
  if (!hasNodes) return "Browse and download the detection archive";
  if (scope === "mine") {
    return hasPrivate
      ? "Browse and download your nodes' archive, private ones included"
      : "Browse and download your nodes' archive";
  }
  return hasPrivate
    ? "Browse and download the whole network's archive, which leaves your private nodes out"
    : "Browse and download the whole network's archive";
}

/** The filters card's body, which a phone folds behind its summary line. */
const FILTERS_BODY = "de-filters-body";

/**
 * The filters in one line, for the button a phone folds them behind. Each part
 * reads as its own field does: the picker's words for the selection, which
 * count nodes and never name one; the size the select shows; a centre on a
 * node by the name its chip shows, never by where the node is; and a point by
 * the four decimals its fields show.
 */
function summaryOf(filters: ExplorerFilters, nodes: string, centreName: string | undefined): string {
  const { from, to, todFrom, todTo, minSize, near } = filters;
  const parts = [nodes, from === to ? from : `${from} → ${to}`, `${todFrom}–${todTo} UTC`];
  if (minSize > 0) parts.push(sizeLabel(minSize));
  if (isNearNode(near)) parts.push(`within ${near.km} km of ${centreName ?? near.node}`);
  else if (near) parts.push(`within ${near.km} km of ${near.lat.toFixed(4)}, ${near.lon.toFixed(4)}`);
  return parts.join(" · ");
}

function union(a: ReadonlySet<string>, b: ReadonlySet<string>): ReadonlySet<string> {
  if (!b.size) return a;
  if (!a.size) return b;
  return new Set([...a, ...b]);
}

function without(a: ReadonlySet<string>, b: ReadonlySet<string>): ReadonlySet<string> {
  if (!b.size || ![...a].some((ref) => b.has(ref))) return a;
  return new Set([...a].filter((ref) => !b.has(ref)));
}

/** A selection by its contents: `readFilters` builds a new Set each time the
 *  query string is touched, and the scan's effect compares by identity. */
function useSelection(sel: Set<string> | null): Set<string> | null {
  const key = sel === null ? null : JSON.stringify(Array.from(sel).sort());
  return useMemo(() => (key === null ? null : new Set<string>(JSON.parse(key) as string[])), [key]);
}

/**
 * Keyed on whoever is signed in, so a change of session starts the explorer
 * afresh: the owner's listings, the basket filled from them and the previews
 * already downloaded are theirs alone, and must not stay on screen for the
 * visitor or the account that comes after.
 */
export default function DataExplorerPage() {
  const { user } = useAuth();
  const viewer = user ? `user:${String(user.id)}` : "visitor";
  return <DataExplorer key={viewer} viewer={viewer} />;
}

function DataExplorer({ viewer }: { viewer: string }) {
  const [search, setSearch] = useSearchParams();
  const [sort, setSort] = useState<SortKey>("span");
  // Off by default: the map is a second way to say what the coordinate fields
  // already say, and it is half the height of the page when it is open.
  const [mapOpen, setMapOpen] = useState(false);
  // The radius to apply to the next centre. It cannot live in the query string,
  // which spells a radius only as part of `near`, and it has to outlive the gap
  // between choosing a distance and choosing a point to measure it from.
  const [pendingKm, setPendingKm] = useState(DEFAULT_RADIUS_KM);
  // The basket and the open preview hold keys, not files: they resolve
  // through the scan's key map, which is never pruned, so a selection
  // outlives the filters that produced it.
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [active, setActive] = useState<string | null>(null);
  const [manifestOpen, setManifestOpen] = useState(false);
  // A phone folds the filters behind a line that sums them up, so the results
  // come straight after the stats rather than after a screen and a half of
  // fields. Folded on every visit: the line already says what is set.
  const phone = useMediaQuery(PHONE);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const folded = phone && !filtersOpen;

  const today = useMemo(() => todayUTC(), []);
  // The filters as the link spells them.
  const asked = useMemo(() => readFilters(search.toString(), today), [search, today]);

  const writeLink = useCallback(
    (next: ExplorerFilters) => setSearch(writeFilters(next), { replace: true }),
    [setSearch],
  );

  // Whose archive this is. The owner's own, by default, where there is one; a
  // visitor, an account with no nodes and one whose list did not load read
  // the network's, whatever the link asks for.
  const owned = useOwnedNodes();
  const ready = owned.status === "ready";
  const hasNodes = ready && owned.nodes.length > 0;
  const scope: Scope = hasNodes && asked.scope === "mine" ? "mine" : "network";
  // Nothing is listed until the owner's list is in: it says which archive is
  // theirs to read, and which of their nodes the network's must never be
  // asked about.
  const source: ArchiveSource | null =
    owned.status === "loading" ? null : scope === "mine" ? "owner" : "public";
  // Whether this viewer's own list has said which of the link's nodes are
  // theirs: it has once it is in, and a visitor has none to wait for.
  const settled = ready || owned.status === "signed-out";

  // What the lists read earlier in this document said was private. Read as
  // the page mounts, which it does afresh for each viewer, and kept up to
  // date with this viewer's for whoever comes after.
  const [earlier] = useState(() => withheldFrom(viewer));
  useEffect(() => {
    if (ready) rememberPrivate(viewer, privateRefs(owned.nodes));
  }, [ready, viewer, owned.nodes]);
  // Another viewer's private nodes, unless this one's own list says they are
  // theirs now. No public route is asked about them, and no link keeps them.
  const theirs = useMemo(() => without(earlier.others, owned.refs), [earlier.others, owned.refs]);
  // What the network's archive is never asked about by name: those, and,
  // while this viewer's own list is not in, their own private nodes as it
  // last had them.
  const withheld = useMemo(() => (ready ? theirs : union(theirs, earlier.own)), [ready, theirs, earlier.own]);

  // The selection as this scope can list it. A link can name a node the
  // scope does not hold, a private one in the network's archive above all,
  // and it is dropped here, before any request is built from it.
  const admits = useMemo(() => scopeAdmits(scope, owned.nodes, withheld), [scope, owned.nodes, withheld]);
  const nodeSel = useSelection(selectionIn(asked.nodeSel, admits));

  // A centre on a node holds in the owner's own scope alone, where their own
  // list places it. Anywhere else it names a node this viewer cannot place.
  const ownNodes = useMemo(() => ownerRegistry(owned.nodes), [owned.nodes]);
  const near =
    isNearNode(asked.near) && (scope !== "mine" || !centreOf(asked.near, ownNodes)) ? null : asked.near;

  // What this viewer may never be shown leaves the address bar as well, and
  // before any request the page makes: another viewer's private nodes, their
  // own asked of the network by name, and a centre on a node they cannot
  // place. Not while their list is on its way, which can say a node is
  // theirs; and a list that failed leaves their own nodes where they are, for
  // a Retry to show.
  const scrubbed = useMemo(() => {
    if (owned.status === "loading") return null;
    const keeps = scope === "network" && ready ? admits : (id: string) => !theirs.has(id);
    const nodes = selectionIn(asked.nodeSel, keeps);
    const centre =
      isNearNode(asked.near) && (theirs.has(asked.near.node) || (settled && near === null)) ? null : asked.near;
    if (nodes === asked.nodeSel && centre === asked.near) return null;
    return writeFilters({ ...asked, nodeSel: nodes, near: centre });
  }, [owned.status, scope, ready, admits, theirs, asked, settled, near]);
  // Ahead of the hooks below in this component, so it runs before any of
  // their effects ask a public route for anything.
  useEffect(() => {
    if (scrubbed) setSearch(scrubbed, { replace: true });
  }, [scrubbed, setSearch]);

  // The future holds no archive, so those days are never listed.
  const days = useMemo(
    () => daysBetween(asked.from, asked.to).filter((d) => d <= today),
    [asked.from, asked.to, today],
  );

  // The network's nodes as the radar publishes them, asked for only while
  // the network's archive is the one on screen. The owner's come from their
  // own list: true positions, private nodes included.
  const network = useNodeRegistry(source === "public");

  // Whether the archive may be asked about a lone node by name. The owner's
  // always may: it is theirs. The network's only about a node the radar
  // publishes, which a private one never is, and only once the viewer's own
  // list is in, or there is none: until then a node they made private a
  // moment ago could still be on the radar's.
  const lone = nodeSel !== null && nodeSel.size === 1 ? Array.from(nodeSel)[0] : null;
  const published = lone !== null && network.nodes.has(lone);
  const nameable = scope === "mine" || (settled && published);
  // A lone node that may not be named is listed for with the whole day, and
  // picked out of it.
  const listFor = lone !== null && !nameable ? null : nodeSel;
  // Until the radar answers, a lone node cannot be told from a private one.
  // The scan waits rather than list the whole day, which would then stand in
  // for the node's own, much quicker, listing.
  const waiting = source === "public" && settled && lone !== null && !published && network.loading;

  const scan = useArchiveScan(days, listFor, waiting ? null : source);

  // What every control reads, so the first change made writes the scoped
  // selection and centre back to the link.
  const filters = useMemo(() => ({ ...asked, nodeSel, near }), [asked, nodeSel, near]);

  // What a control writes. Until this viewer's own list is in, the page shows
  // less than the link asks for: no centre on one of their nodes, none of the
  // private nodes their last list named. A field the control left as it was
  // keeps the link's own value meanwhile, so a date changed before Retry does
  // not take those out of the link Retry is to show; another viewer's nodes
  // excepted, which no link of this viewer's keeps.
  const setFilters = useCallback(
    (next: ExplorerFilters) => {
      if (settled) {
        writeLink(next);
        return;
      }
      const keptNear = isNearNode(asked.near) && theirs.has(asked.near.node) ? null : asked.near;
      writeLink({
        ...next,
        nodeSel:
          next.nodeSel === filters.nodeSel ? selectionIn(asked.nodeSel, (id) => !theirs.has(id)) : next.nodeSel,
        near: next.near === filters.near ? keptNear : next.near,
      });
    },
    [settled, writeLink, asked, filters, theirs],
  );

  // A switch writes the scope and the selection in one navigation: two in the
  // same tick would each start from the same query string, and the second
  // would undo the first.
  const switchScope = useCallback(
    (next: Scope) =>
      writeLink({
        ...filters,
        scope: next,
        nodeSel: selectionIn(filters.nodeSel, scopeAdmits(next, owned.nodes, withheld)),
        // A centre on a node is the owner's own scope's alone.
        near: isNearNode(filters.near) ? null : filters.near,
      }),
    [filters, owned.nodes, withheld, writeLink],
  );

  const select = useCallback((keys: string[], on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const key of keys) {
        if (on) next.add(key);
        else next.delete(key);
      }
      return next;
    });
  }, []);

  // Adding from the drawer also opens the manifest, so the addition is seen
  // to land somewhere.
  const addToBasket = useCallback(
    (key: string) => {
      select([key], true);
      setManifestOpen(true);
    },
    [select],
  );
  const removeFromBasket = useCallback((key: string) => select([key], false), [select]);

  const closePreview = useCallback(() => setActive(null), []);
  const toggleManifest = useCallback(() => setManifestOpen((open) => !open), []);
  const clearBasket = useCallback(() => setSelected(new Set()), []);
  // The results' heading, where focus goes when a phone's basket empties
  // with focus in it.
  const treeHeading = useRef<HTMLHeadingElement>(null);

  // Each file goes out through a route whose archive listed it, the one on
  // screen first. The basket outlives a switch, so a private node's file
  // ticked in the owner's view still fetches from the owner's route in the
  // network's, and never from the public one, which does not serve it.
  const prefer: ArchiveSource = source ?? "public";
  const routed = useCallback(
    (f: ArchiveFile): RoutedFile => ({ ...f, source: routeFor(f.key, scan.listed, prefer) }),
    [scan.listed, prefer],
  );

  // The key map keeps its identity as listings land, which is enough: an
  // archive key is written once, so a record re-listed later is the same
  // record. The selection changes what the basket holds, and a listing
  // landing can change the route a file is fetched by.
  const basket = useMemo(
    () =>
      Array.from(selected, (key) => scan.files.get(key))
        .filter((f): f is ArchiveFile => f !== undefined)
        .map(routed)
        .sort((a, b) => b.endMs - a.endMs),
    [selected, scan.files, routed],
  );
  const activeFile = useMemo(() => {
    const file = active === null ? undefined : scan.files.get(active);
    return file ? routed(file) : null;
  }, [active, scan.files, routed]);

  const registry: NodeRegistry = useMemo(() => {
    if (scope === "mine") return { nodes: ownNodes, loading: false, error: null, retry: owned.refresh };
    // Held with the scan, and loading meanwhile rather than an empty fleet.
    return source === null ? { ...network, loading: true } : network;
  }, [scope, ownNodes, owned.refresh, source, network]);
  const positions = scope === "mine" ? "exact" : "published";
  const marks = useMemo(() => nodeMarks(scope, owned.nodes), [scope, owned.nodes]);
  const ownPrivate = useMemo(() => privateRefs(owned.nodes), [owned.nodes]);
  const centre = useMemo(() => centreOf(filters.near, registry.nodes), [filters.near, registry.nodes]);
  const centreName = isNearNode(filters.near) ? registry.nodes.get(filters.near.node)?.name : undefined;
  const allLabel = scope === "mine" ? "All my nodes" : "All nodes";

  // Which cached listing is allowed to answer. This has to be what was asked
  // for, never what survives the filters: a radius that narrows the view to a
  // single node has not changed the request, and that node's listing cannot
  // stand in for the whole fleet's.
  const entryScope = useMemo(() => nodeSel ?? new Set<string>(), [nodeSel]);

  // What the page filters and counts by, which the radius does narrow.
  const effective = useMemo(
    () => effectiveNodeIds(registry.nodes, scan.nodeIds, nodeSel, centre),
    [registry.nodes, scan.nodeIds, nodeSel, centre],
  );

  const entryFor = useCallback(
    (day: string) => entryForScope(scan.entries, day, entryScope),
    [scan.entries, entryScope],
  );

  // Files this node has in the range, regardless of every other filter. A
  // count that fell to zero the moment you deselected the node would say
  // nothing about whether to select it again.
  const fileCountFor = useCallback(
    (id: string) => {
      let n = 0;
      for (const day of days) {
        const entry = entryForScope(scan.entries, day, entryScope);
        if (!entry || entry.status !== "done") continue;
        for (const f of entry.files) if (f.day === day && f.node === id) n += 1;
      }
      return n;
    },
    [days, scan.entries, entryScope],
  );

  const { byDay, matched } = useMemo(() => {
    const predicate = makePredicate(filters, effective);
    const out = new Map<string, ArchiveFile[]>();
    const all: ArchiveFile[] = [];
    for (const day of days) {
      const entry = entryForScope(scan.entries, day, entryScope);
      if (!entry || entry.status !== "done") continue;
      const files = entry.files.filter((f) => f.day === day && predicate(f));
      out.set(day, files);
      all.push(...files);
    }
    return { byDay: out, matched: all };
  }, [days, scan.entries, entryScope, effective, filters]);

  const effectiveIds = useMemo(() => Array.from(effective).sort(), [effective]);
  const synthetic = useCallback((id: string) => isSynthetic(registry.nodes, id), [registry.nodes]);
  const anySynthetic = useMemo(
    () => anyNodeSynthetic(registry.nodes, scan.nodeIds),
    [registry.nodes, scan.nodeIds],
  );

  const listed = days.filter((d) => entryFor(d)?.status === "done").length;
  const failed = days.filter((d) => entryFor(d)?.status === "error").length;
  const bytes = matched.reduce((sum, f) => sum + f.size, 0);
  const nodesWithData = new Set(matched.map((f) => f.node)).size;
  const coldFrom = horizon(scan.entries, days, entryScope);
  const shareQuery = writeFilters(filters).toString();
  // From the filters rather than the address bar, which keeps a link's query
  // as it arrived until a filter changes.
  const shareHref = useHref({ search: `?${shareQuery}` });
  const [shareCopyOutcome, copyShare] = useCopy();
  const summary = phone
    ? summaryOf(filters, selectionLabel(filters.nodeSel, registry.nodes, scan.nodeIds, allLabel), centreName)
    : "";

  const map = mapOpen && (
    <NodeMap
      filters={filters}
      nodes={registry.nodes}
      effective={effective}
      radiusKm={filters.near?.km ?? pendingKm}
      loading={registry.loading}
      onChange={setFilters}
      positions={positions}
    />
  );

  const title = (
    <>
      <h1>Data Explorer</h1>
      <p>{description(scope, hasNodes, owned.nodes.some((n) => n.location_private))}</p>
    </>
  );

  // The owner's list failing leaves the network's archive, which needs no
  // list, so this warns above it rather than standing in its place.
  let ownerNote: ReactNode = null;
  if (owned.status === "error") {
    ownerNote = (
      <Notice onRetry={owned.refresh}>
        Could not load your nodes: {owned.error?.message}. Showing the whole network.
      </Notice>
    );
  } else if (owned.status === "ready" && owned.nodes.length === 0) {
    ownerNote = <ClaimPathNote lead="No nodes on your account yet, so this is the whole network's archive." />;
  }

  return (
    <>
      {hasNodes ? (
        <div className="page-header de-header">
          <div className="de-heading">{title}</div>
          <ScopeSwitch
            value={scope}
            onChange={switchScope}
            mineCount={owned.nodes.length}
            label="Show archive files from"
          />
        </div>
      ) : (
        <div className="page-header">{title}</div>
      )}

      {ownerNote}

      {coldFrom && (
        <Notice>Files before {coldFrom} have been moved to cold storage and are not served here.</Notice>
      )}

      {registry.error && (
        <Notice onRetry={registry.retry}>
          The node list could not be loaded ({registry.error}), so names and positions are missing
          and the radius filter has nothing to measure against.
        </Notice>
      )}

      <div className="stats-grid">
        <StatCard
          label="Files matching"
          value={<span data-testid="de-stat-files">{matched.length.toLocaleString()}</span>}
          tone="accent"
          sub={`${listed} of ${days.length} days listed${failed ? ` · ${failed} failed` : ""}`}
        />
        <StatCard
          label="Stored size"
          value={<span data-testid="de-stat-bytes">{formatBytes(bytes)}</span>}
          sub={`≈ ${formatBytes(bytes * JSON_FACTOR)} as JSON (≈ ${JSON_FACTOR}×)`}
        />
        <StatCard
          label="Nodes with data"
          value={nodesWithData}
          tone="success"
          sub={scope === "mine" ? `of your ${registry.nodes.size}` : `of ${registry.nodes.size} known`}
        />
        <StatCard
          label="Range"
          value={`${days.length}d`}
          sub={`${filters.from} → ${filters.to} UTC`}
          tone="warning"
        />
      </div>

      <div className="card de-card">
        <div className="card-header">
          <h3>Filters</h3>
          {/* The filters in this card, not whose archive it is: that is the
              switch's to say, and Reset leaves it where it was. */}
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => writeLink({ ...defaultFilters(today), scope: asked.scope })}
          >
            Reset
          </button>
        </div>
        {phone && (
          <button
            type="button"
            className="de-fsummary"
            aria-expanded={filtersOpen}
            aria-controls={FILTERS_BODY}
            onClick={() => setFiltersOpen(!filtersOpen)}
          >
            <span className="de-fsummary-text">{summary}</span>
            <span className="de-fsummary-caret" aria-hidden="true">▾</span>
          </button>
        )}
        {/* Hidden rather than unmounted while folded, so a coordinate typed
            without its partner, or the picker's search, is still there when
            it opens again. */}
        <div className="card-body" id={FILTERS_BODY} hidden={folded}>
          <div className="de-filters">
            <NodePicker
              filters={filters}
              nodes={registry.nodes}
              discovered={scan.nodeIds}
              fileCountFor={fileCountFor}
              onChange={setFilters}
              marks={marks}
              allLabel={allLabel}
            />
            <DateRangeControls filters={filters} today={today} onChange={setFilters} />
            <NearControls
              filters={filters}
              radiusKm={filters.near?.km ?? pendingKm}
              onRadiusChange={setPendingKm}
              mapOpen={mapOpen}
              onToggleMap={() => setMapOpen(!mapOpen)}
              onChange={setFilters}
              positions={positions}
              centreName={centreName}
            />
          </div>
          <div className="de-urlbar">
            <span>Shareable:</span>
            <code className="mono" data-testid="de-share">?{shareQuery}</code>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => copyShare(`${window.location.origin}${shareHref}`)}
            >
              {shareCopyOutcome ?? "Copy link"}
            </button>
          </div>
        </div>
        {/* Folded with the fields on a phone, and kept rather than redrawn, so
            the view it was left at is the one it opens at. */}
        {map && (phone ? <div hidden={folded}>{map}</div> : map)}
      </div>

      <AvailabilityTimeline
        filters={filters}
        today={today}
        ids={effectiveIds}
        files={matched}
        synthetic={synthetic}
        anySynthetic={anySynthetic}
        onChange={setFilters}
      />

      <div className="card">
        <ResultsTree
          days={days}
          byDay={byDay}
          entryFor={entryFor}
          sort={sort}
          onSort={setSort}
          onRetry={scan.retry}
          selected={selected}
          onSelect={select}
          active={active}
          onPreview={setActive}
          marks={marks}
          source={prefer}
          scope={scope}
          privateRefs={ownPrivate}
          synthetic={synthetic}
          headingRef={treeHeading}
        />
      </div>

      <DownloadBasket
        files={basket}
        manifestOpen={manifestOpen}
        onToggleManifest={toggleManifest}
        onClear={clearBasket}
        focusWhenEmptied={treeHeading}
      />

      <FilePreviewDrawer
        file={activeFile}
        node={activeFile ? registry.nodes.get(activeFile.node) : undefined}
        positions={positions}
        onClose={closePreview}
        onAddToBasket={addToBasket}
        inBasket={active !== null && selected.has(active)}
        onRemoveFromBasket={removeFromBasket}
      />
    </>
  );
}
