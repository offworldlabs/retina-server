import { useState } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { Pager } from "../../components/Pager";
import { useFetch } from "../../hooks/usePolling";
import { StatusBadge } from "../../components/StatusBadge";

const PAGE_SIZE = 25;

/** Why a save did not land: the editor's text would not parse, so nothing
 *  was sent, or it was sent and the request failed. */
type SaveError = { kind: "parse" | "request"; message: string };

export default function ConfigPage() {
  const configFetch = useFetch(() => Promise.all([api.adminNodeConfig(), api.adminTowerConfig()]));
  // Its own fetch, so a failure here leaves the configuration on screen and
  // is reported in the history card rather than read as "no history".
  const historyFetch = useFetch(() =>
    api.adminConfigHistory().then((h) => (Array.isArray(h) ? h : h?.versions || [])),
  );
  const [activeTab, setActiveTab] = useState("nodes");
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<SaveError | null>(null);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");

  if (configFetch.loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>Configuration</h1>
      <p>View and manage node and tower configurations</p>
    </div>
  );
  if (nothingLoaded(configFetch)) {
    return (
      <>
        {header}
        <FetchNotice polled={configFetch} what="the configuration" />
      </>
    );
  }

  const [nodeConfig, towerConfig] = configFetch.data ?? [null, null];
  const history = historyFetch.data ?? [];

  const save = async () => {
    setSaveError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(editText);
    } catch (err) {
      setSaveError({ kind: "parse", message: (err as Error).message });
      return;
    }
    setSaving(true);
    try {
      await api.adminUpdateNodeConfig(parsed);
      setEditing(false);
      configFetch.refresh();
      historyFetch.refresh();
    } catch (err) {
      setSaveError({ kind: "request", message: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  const isLiveNodes = nodeConfig?._source === "live" && nodeConfig?.nodes;
  const isLiveTowers = towerConfig?._source === "live" && towerConfig?.towers;

  /* ── Nodes table data ── */
  const nodeEntries = isLiveNodes ? Object.entries(nodeConfig.nodes) : [];
  const filteredNodes = search
    ? nodeEntries.filter(([id]) => id.toLowerCase().includes(search.toLowerCase()))
    : nodeEntries;
  const nodeTotalPages = Math.ceil(filteredNodes.length / PAGE_SIZE);
  const pagedNodes = filteredNodes.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  /* ── Towers table data ── */
  const towerEntries = isLiveTowers ? Object.entries(towerConfig.towers) : [];
  const filteredTowers = search
    ? towerEntries.filter(([key, t]) =>
        key.includes(search) || ((t as any).nodes_using || []).some((n) => n.toLowerCase().includes(search.toLowerCase()))
      )
    : towerEntries;
  const towerTotalPages = Math.ceil(filteredTowers.length / PAGE_SIZE);
  const pagedTowers = filteredTowers.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const showLiveTable = (activeTab === "nodes" && isLiveNodes && !editing) || (activeTab === "towers" && isLiveTowers && !editing);

  return (
    <>
      {header}
      <FetchNotice polled={configFetch} what="the configuration" />

      <div className="tabs">
        <button
          className={`tab ${activeTab === "nodes" ? "active" : ""}`}
          onClick={() => { setActiveTab("nodes"); setPage(0); setSearch(""); setEditing(false); setSaveError(null); }}
        >
          Node Config
        </button>
        <button
          className={`tab ${activeTab === "towers" ? "active" : ""}`}
          onClick={() => { setActiveTab("towers"); setPage(0); setSearch(""); setEditing(false); setSaveError(null); }}
        >
          Tower Config
        </button>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>
            {activeTab === "nodes"
              ? isLiveNodes ? `Live Node Config (${nodeConfig.total} nodes)` : "nodes_config.json"
              : isLiveTowers ? `Live Tower Config (${towerConfig.total} towers)` : "tower_config.json"}
          </h3>
          {/* Towers is view-only: the live view is derived from what nodes
              report, not editable data, and the ranking config an operator
              would want to edit lives in tower-finder-service (PUT /api/config
              on any tower vhost). The PUT route this tab's Save used to call
              was deleted with the monolith tower stack — and saving was
              already broken whenever no overlay file existed, because the
              live view never validated as tower config. */}
          {activeTab === "nodes" && (
            <div style={{ display: "flex", gap: 8 }}>
              {editing ? (
                <>
                  <button className="btn btn-primary btn-sm" disabled={saving} onClick={save}>
                    {saving ? "Saving…" : "Save"}
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => {
                      setEditing(false);
                      setSaveError(null);
                    }}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  className="btn btn-outline btn-sm"
                  onClick={() => {
                    setEditText(JSON.stringify(nodeConfig, null, 2));
                    setEditing(true);
                  }}
                >
                  Edit
                </button>
              )}
            </div>
          )}
        </div>
        <div className="card-body">
          {editing ? (
            <>
              {saveError?.kind === "parse" && (
                <Notice>Not saved: the text is not valid JSON ({saveError.message}).</Notice>
              )}
              {saveError?.kind === "request" && (
                <Notice onRetry={saving ? undefined : save}>Could not save the node config: {saveError.message}</Notice>
              )}
              <textarea
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                style={{
                  width: "100%",
                  minHeight: 400,
                  fontFamily: "monospace",
                  fontSize: 12,
                  background: "var(--bg-input)",
                  color: "var(--text-primary)",
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: 12,
                  resize: "vertical",
                }}
              />
            </>
          ) : showLiveTable ? (
            <>
              <div style={{ marginBottom: 12 }}>
                <input
                  type="text"
                  placeholder="Search…"
                  value={search}
                  onChange={(e) => { setSearch(e.target.value); setPage(0); }}
                  style={{ padding: "6px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, width: 260 }}
                />
              </div>

              {activeTab === "nodes" ? (
                <>
                  <DataTable
                    headers={["Node ID", "Status", "RX Lat", "RX Lon", "TX Lat", "TX Lon", "Frequency"]}
                    count={pagedNodes.length}
                    empty={search ? `No nodes match "${search}"` : "No nodes configured"}
                  >
                    {pagedNodes.map(([id, n]: [string, any]) => (
                      <tr key={id}>
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>{id}</td>
                        <td><StatusBadge status={n.status} /></td>
                        <td>{n.rx_lat != null ? n.rx_lat.toFixed(4) : "—"}</td>
                        <td>{n.rx_lon != null ? n.rx_lon.toFixed(4) : "—"}</td>
                        <td>{n.tx_lat != null ? n.tx_lat.toFixed(4) : "—"}</td>
                        <td>{n.tx_lon != null ? n.tx_lon.toFixed(4) : "—"}</td>
                        <td>{n.frequency || "—"}</td>
                      </tr>
                    ))}
                  </DataTable>
                  <Pager page={page} totalPages={nodeTotalPages} onPage={setPage} />
                </>
              ) : (
                <>
                  <DataTable
                    headers={["Location", "Lat", "Lon", "Frequency", "Nodes Using"]}
                    count={pagedTowers.length}
                    empty={search ? `No towers match "${search}"` : "No towers reported"}
                  >
                    {pagedTowers.map(([key, t]: [string, any]) => (
                      <tr key={key}>
                        <td style={{ fontFamily: "monospace", fontSize: 12 }}>{key}</td>
                        <td>{t.lat?.toFixed(4)}</td>
                        <td>{t.lon?.toFixed(4)}</td>
                        <td>{t.frequency || "—"}</td>
                        <td style={{ fontSize: 11 }}>{(t.nodes_using || []).length}</td>
                      </tr>
                    ))}
                  </DataTable>
                  <Pager page={page} totalPages={towerTotalPages} onPage={setPage} />
                </>
              )}
            </>
          ) : (
            <div className="config-block">
              {JSON.stringify(
                activeTab === "nodes" ? nodeConfig : towerConfig,
                null,
                2
              )}
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Version History</h3>
        </div>
        {historyFetch.error && (
          <div className="card-body">
            <FetchNotice polled={historyFetch} what="the version history" />
          </div>
        )}
        {historyFetch.loading ? (
          <div className="card-body">
            <div className="empty-state">Loading…</div>
          </div>
        ) : nothingLoaded(historyFetch) ? null : history.length === 0 ? (
          <div className="card-body">
            <div className="empty-state">
              <p>No config changes recorded yet.</p>
            </div>
          </div>
        ) : (
          <DataTable headers={["Date", "Type", "File"]} count={Math.min(history.length, 20)}>
            {history.slice(0, 20).map((v, i) => (
              <tr key={i}>
                <td style={{ fontSize: 12 }}>{v.timestamp ? new Date(v.timestamp).toLocaleString() : v.file || "—"}</td>
                <td>{v.type || "config"}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{v.file || v.name || "—"}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </div>
    </>
  );
}
