import { useState } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";

export default function UserManagementPage() {
  const polled = useFetch(() =>
    Promise.all([api.adminUsers(), api.adminNodeOwners()]).then(([u, o]) => ({
      users: Array.isArray(u) ? u : [],
      owners: (o && typeof o === "object" ? o : {}) as Record<string, { user_id: string }>,
    })),
  );
  const { data, loading, pending, refresh } = polled;
  // The user whose role change is in flight, and what went wrong with the last one.
  const [changing, setChanging] = useState<string | null>(null);
  const [roleError, setRoleError] = useState<string | null>(null);

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>User Management</h1>
      <p>Manage operators and admin access</p>
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

  const toggleRole = async (user) => {
    const newRole = user.role === "admin" ? "user" : "admin";
    setRoleError(null);
    setChanging(user.id);
    try {
      await api.adminSetRole(user.id, newRole);
      refresh();
    } catch (err) {
      const becoming = newRole === "admin" ? "an admin" : "a user";
      setRoleError(`Could not make ${user.name || user.email} ${becoming}: ${(err as Error).message}`);
    } finally {
      setChanging(null);
    }
  };

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="the user list" />
      {roleError && <Notice>{roleError}</Notice>}

      <div className="stats-grid">
        <StatCard label="Total Users" value={users.length} tone="accent" />
        <StatCard label="Admins" value={users.filter((u) => u.role === "admin").length} tone="accent" />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Registered Users</h3>
        </div>
        <DataTable
          headers={["User", "Email", "Provider", "Role", "Nodes", "Last Login", "Actions"]}
          count={users.length}
          empty="No users registered yet"
        >
          {users.map((user) => (
            <tr key={user.id}>
              <td style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {user.avatar ? (
                  <img
                    src={user.avatar}
                    alt=""
                    style={{ width: 24, height: 24, borderRadius: "50%" }}
                    referrerPolicy="no-referrer"
                  />
                ) : null}
                <span style={{ color: "var(--text-primary)" }}>{user.name}</span>
              </td>
              <td>{user.email}</td>
              <td style={{ textTransform: "capitalize" }}>{user.provider}</td>
              <td>
                <span className="badge plain">{user.role}</span>
              </td>
              <td className="mono muted">
                {nodeCount(user.id)}
              </td>
              <td>
                {user.last_login
                  ? new Date(user.last_login * 1000).toLocaleString()
                  : "—"}
              </td>
              <td>
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={changing !== null || pending}
                  onClick={() => toggleRole(user)}
                >
                  {user.role === "admin" ? "Demote" : "Promote"}
                </button>
              </td>
            </tr>
          ))}
        </DataTable>
      </div>
    </>
  );
}
