import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";

import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import type { AdminUser } from "../../types";
import { DASH, formatRelativeTime } from "../../utils/format";
import { UserActions, type UserChange } from "./users/UserActions";
import "./users/users.css";

const PAGE_SIZE = 25;
const SEARCH_DELAY_MS = 300;

/** The zero-based page a `page` parameter names. The address counts from one,
 *  and anything that is not a later page reads as the first. */
function pageFrom(param: string | null): number {
  const n = Number(param);
  return Number.isInteger(n) && n > 1 ? n - 1 : 0;
}

function count(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

/** The zero-based last page that holds any of `total` accounts. */
function lastPage(total: number): number {
  return Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);
}

function LastVisit({ at }: { at: number | null }) {
  if (!at) return <>Never</>;
  const when = new Date(at * 1000);
  return (
    <time dateTime={when.toISOString()} title={when.toLocaleString()}>
      {formatRelativeTime(at)}
    </time>
  );
}

/** Whose row it is: the name as the link to the account's page and the
 *  address under it, or the address alone as the link while there is no
 *  name. `back` lets that page link back to this view of the list. */
function Who({ user, back }: { user: AdminUser; back: string }) {
  const name = user.name?.trim();
  return (
    <div className="user-who">
      <Link to={`/users/${encodeURIComponent(user.id)}`} state={{ from: back }} className="user-link">
        {name || user.email}
      </Link>
      {name && <span className="user-who-email">{user.email}</span>}
    </div>
  );
}

export default function UserManagementPage() {
  const { pathname, search } = useLocation();
  const [params, setParams] = useSearchParams();
  const q = (params.get("q") ?? "").trim();
  const page = pageFrom(params.get("page"));

  // The search and the page live in the address, so Back from an account's
  // page lands where the list was left. Replaced rather than pushed: another
  // view of the same page, not a new place.
  const show = useCallback(
    (next: { q?: string; page: number }) =>
      setParams(
        (current) => {
          const updated = new URLSearchParams(current);
          if (next.q !== undefined) {
            if (next.q) updated.set("q", next.q);
            else updated.delete("q");
          }
          if (next.page > 0) updated.set("page", String(next.page + 1));
          else updated.delete("page");
          return updated;
        },
        { replace: true },
      ),
    [setParams],
  );

  // What is typed goes to the address once typing pauses. It follows the
  // address when that changes under it: Back, or the sidebar's bare link.
  const [draft, setDraft] = useState(q);
  const [draftFor, setDraftFor] = useState(q);
  if (q !== draftFor) {
    setDraftFor(q);
    if (draft.trim() !== q) setDraft(q);
  }

  useEffect(() => {
    const next = draft.trim();
    if (next === q) return;
    const timer = setTimeout(() => show({ q: next, page: 0 }), SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [draft, q, show]);

  // The answer carries what it was asked for, so the rows, the count and the
  // empty text agree with each other while the next search is in flight.
  const polled = useFetch(
    () =>
      api.adminUsers({ q, limit: PAGE_SIZE, offset: page * PAGE_SIZE }).then((answer) => ({
        q,
        page,
        users: Array.isArray(answer?.users) ? answer.users : [],
        total: typeof answer?.total === "number" ? answer.total : 0,
      })),
    `${q}\u0000${page}`,
  );
  const { data: shown, loading, pending, refresh } = polled;

  // A page past the end, from an old link or once the last account on it has
  // gone, turns back to the last page that has any.
  useEffect(() => {
    if (!shown || shown.q !== q || shown.page !== page) return;
    const last = lastPage(shown.total);
    if (page > last) show({ page: last });
  }, [shown, q, page, show]);

  const [notice, setNotice] = useState<string | null>(null);
  // What a delete left said stays until the list moves on: another search,
  // another page, or an action on a row. The turn back from a page that the
  // delete emptied is the delete's own doing, so it keeps the notice.
  const [viewed, setViewed] = useState({ q, page });
  if (viewed.q !== q || viewed.page !== page) {
    const turnedBack =
      viewed.q === q && shown?.q === q && shown.page === viewed.page && viewed.page > lastPage(shown.total);
    setViewed({ q, page });
    if (!turnedBack) setNotice(null);
  }
  const noticeRef = useRef<HTMLParagraphElement>(null);
  const focusNotice = useRef(false);
  useEffect(() => {
    if (!focusNotice.current) return;
    focusNotice.current = false;
    noticeRef.current?.focus();
  });

  const changed = (user: AdminUser, change: UserChange) => {
    if (change.kind === "deleted" || change.kind === "gone") {
      // The row leaves with the refetch, and the focus in it with the row, so
      // focus comes to what is said about it.
      const released =
        change.kind === "deleted" && change.released > 0 ? ` ${count(change.released, "node")} released.`
          : "";
      setNotice(
        change.kind === "deleted"
          ? `Deleted ${user.email}.${released}`
          : `The account for ${user.email} no longer exists.`,
      );
      focusNotice.current = true;
    }
    refresh();
  };

  const rows = shown?.users ?? [];
  const total = shown?.total ?? 0;
  const searched = shown?.q ?? q;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const first = (shown?.page ?? 0) * PAGE_SIZE + 1;
  const back = `${pathname}${search}`;

  return (
    <>
      <div className="page-header">
        <h1>User Management</h1>
        <p>
          Accounts opened by an emailed sign-in or claim link, or made for a polled radar. Administrators come from
          Cloudflare Access and are not listed here.
        </p>
      </div>
      <FetchNotice polled={polled} what="the user list" />
      <p ref={noticeRef} className="users-notice" role="status" tabIndex={-1}>
        {notice}
      </p>

      {!nothingLoaded(polled) && (
        <div className="stats-grid">
          <StatCard
            label={searched ? "Matching Users" : "Total Users"}
            value={loading ? DASH : total.toLocaleString()}
            tone="accent"
          />
        </div>
      )}

      <div className="toolbar">
        <input
          type="search"
          className="input"
          placeholder="Search by email…"
          aria-label="Search users by email"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        {rows.length > 0 && (
          <span className="card-note">
            Showing {first.toLocaleString()}–{(first + rows.length - 1).toLocaleString()} of {total.toLocaleString()}
          </span>
        )}
      </div>

      {!nothingLoaded(polled) && (
        <div className={pending && !loading ? "card users-card updating" : "card users-card"} aria-busy={pending}>
          <div className="card-header">
            <h3>Registered Users</h3>
          </div>
          <DataTable
            headers={["User", "Nodes", "Last visit", "Verified", "State", "Actions"]}
            count={rows.length}
            loading={loading}
            empty={searched ? `No users match “${searched}”` : "No users registered yet"}
          >
            {rows.map((user) => (
              <tr key={user.id}>
                <td className="cell-primary" data-label="User">
                  <Who user={user} back={back} />
                </td>
                <td className="mono" data-label="Nodes">
                  {user.node_count}
                </td>
                <td data-label="Last visit">
                  <LastVisit at={user.last_seen_at} />
                </td>
                <td data-label="Verified">
                  <span className={user.is_verified ? "badge online" : "badge warning"}>
                    {user.is_verified ? "Verified" : "Unverified"}
                  </span>
                </td>
                <td data-label="State">
                  <span className={user.is_active ? "badge online" : "badge offline"}>
                    {user.is_active ? "Active" : "Deactivated"}
                  </span>
                </td>
                <td className="user-actions-cell" data-label="Actions">
                  <UserActions
                    user={user}
                    onChanged={(change) => changed(user, change)}
                    onAction={() => setNotice(null)}
                  />
                </td>
              </tr>
            ))}
          </DataTable>
          <Pager
            page={clampPage(page, totalPages)}
            totalPages={totalPages}
            onPage={(next) => show({ page: next })}
            note={count(total, "user")}
          />
        </div>
      )}
    </>
  );
}
