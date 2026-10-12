import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";

/** The fields of a row from GET /api/admin/users that this page reads. */
interface AdminUser {
  id: string;
  name: string;
  email: string;
  last_seen_at: number | null;
}

export default function UserManagementPage() {
  const polled = useFetch(() =>
    Promise.all([api.adminUsers(), api.adminNodeOwners()]).then(([u, o]) => ({
      users: (Array.isArray(u) ? u : []) as AdminUser[],
      owners: (o && typeof o === "object" ? o : {}) as Record<string, { user_id: string }>,
    })),
  );
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>User Management</h1>
      <p>Accounts opened by an emailed sign-in or claim link. Administrators come from Cloudflare Access and are not listed here.</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="the user list" />
      </>
    );
  }

  const { users, owners } = data ?? { users: [], owners: {} };

  const nodeCount = (uid: string) =>
    Object.values(owners).filter((v) => v.user_id === uid).length;

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="the user list" />

      <div className="stats-grid">
        <StatCard label="Total Users" value={users.length} tone="accent" />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Registered Users</h3>
        </div>
        <DataTable
          headers={["User", "Email", "Nodes", "Last Visit"]}
          count={users.length}
          empty="No users registered yet"
        >
          {users.map((user) => (
            <tr key={user.id}>
              <td className="cell-primary">{user.name}</td>
              <td>{user.email}</td>
              <td className="mono muted">
                {nodeCount(user.id)}
              </td>
              <td>
                {user.last_seen_at
                  ? new Date(user.last_seen_at * 1000).toLocaleString()
                  : "—"}
              </td>
            </tr>
          ))}
        </DataTable>
      </div>
    </>
  );
}
