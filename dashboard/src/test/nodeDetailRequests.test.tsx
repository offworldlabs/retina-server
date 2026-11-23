import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import NodeDetailPage from "../pages/user/NodeDetailPage";
import { ownedNode } from "./fixtures/ownedNode";

// The node page over the real API client, with fetch standing in for the
// server: what is asserted here is every request that leaves the page, by
// path, whichever client call made it.

vi.mock("recharts", async (importOriginal) => ({
  ...await importOriginal<typeof import("recharts")>(),
  ResponsiveContainer: () => null,
}));

const REF = "nde0a1b2c3d4e5f";
const ID = "ret0a1b2c3d";
const PRIVATE = ownedNode({
  node_id: ID,
  node_ref: REF,
  name: "Roof",
  label: "Roof",
  location_private: true,
  claimed_with: "ada@example.com",
});

function json(body: unknown, status = 200, statusText = "OK") {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "Content-Type": "application/json" } });
}

/** Every request the page made, as "METHOD path". */
let requests: string[];
/** What the server answers, by "METHOD path", asked afresh for each request.
 *  Anything else is the server's 404, which is also what the public routes
 *  answer anyone for a private node. */
let answers: Map<string, () => Response | Promise<Response>>;

const serve = (route: string, answer: () => Response | Promise<Response>) => answers.set(route, answer);

/** Requests outside the owner routes that carry the node's ref. */
const refOutsideOwnerRoutes = () =>
  requests.filter((request) => !request.split(" ")[1].startsWith("/api/auth/") && request.includes(REF));

const settle = () => act(() => new Promise<void>((done) => setTimeout(done, 0)));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/nodes/${REF}`]}>
      <Routes>
        <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const realLocation = window.location;

beforeEach(() => {
  requests = [];
  answers = new Map();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      const route = `${init?.method ?? "GET"} ${path}`;
      requests.push(route);
      const answer = answers.get(route);
      return answer ? answer() : json({ detail: "Node not found" }, 404, "Not Found");
    }),
  );
  // The caller owns the private node, and every owner route answers for it.
  serve("GET /api/auth/me/nodes", () => json([PRIVATE]));
  serve(`GET /api/auth/me/nodes/${ID}`, () => json(PRIVATE));
  serve(`GET /api/auth/me/nodes/${ID}/analytics`, () => json(null));
  serve(`GET /api/auth/me/nodes/${ID}/report`, () => json(null));
  serve(`GET /api/auth/me/nodes/${ID}/config-history?limit=50`, () => json({ versions: [] }));
  serve(`GET /api/auth/me/nodes/${ID}/events?limit=100`, () => json({ events: [] }));
  // The public listing leaves a private node out.
  serve("GET /api/radar/nodes", () => json({ nodes: {} }));
  // usePolling logs every failure it keeps; the tests below fail on purpose.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("the requests the node page sends for its owner's private node", () => {
  it.each([
    ["429", () => json({ detail: "Too many requests" }, 429, "Too Many Requests")],
    ["503", () => json(null, 503, "Service Unavailable")],
    ["timeout", () => Promise.reject(new DOMException("No response within 10000 ms", "TimeoutError"))],
  ])("carry its ref nowhere but the owner routes when the owner's list answers %s", async (_, failure) => {
    let first = true;
    serve("GET /api/auth/me/nodes", () => {
      if (!first) return json([PRIVATE]);
      first = false;
      return failure();
    });
    renderPage();

    const notice = (await screen.findByText(/^Could not load this node: /)).closest(".notice") as HTMLElement;
    await settle();
    expect(requests).toContain("GET /api/radar/nodes");
    expect(refOutsideOwnerRoutes()).toEqual([]);

    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "Label and privacy" })).toBeInTheDocument();
    await settle();
    expect(refOutsideOwnerRoutes()).toEqual([]);
  });

  it("carry its ref nowhere but the owner routes when it is released", async () => {
    serve(`DELETE /api/auth/me/nodes/${ID}/claim`, () => {
      serve("GET /api/auth/me/nodes", () => json([]));
      return json({ ok: true });
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    expect(await screen.findByText(/^Released\. This node is no longer on your account\./)).toBeInTheDocument();
    await settle();
    expect(requests).toContain(`DELETE /api/auth/me/nodes/${ID}/claim`);
    expect(refOutsideOwnerRoutes()).toEqual([]);
  });

  it("carry its ref nowhere but the owner routes when a change finds it gone", async () => {
    serve(`PATCH /api/auth/me/nodes/${ID}`, () => {
      serve("GET /api/auth/me/nodes", () => json([]));
      return json({ detail: "Node not found" }, 404, "Not Found");
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Edit label" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Label" }), { target: { value: "Mast" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Label" }).closest("form")!);

    expect(await screen.findByText(/^This node is no longer on your account\./)).toHaveAttribute("role", "status");
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    await settle();
    expect(requests).toContain(`PATCH /api/auth/me/nodes/${ID}`);
    expect(refOutsideOwnerRoutes()).toEqual([]);
  });
});

describe("the requests the node page sends on the admin console", () => {
  it("ask nothing of /api/auth/me, which the admin surface has no session routes for", async () => {
    Object.defineProperty(window, "location", {
      value: { hostname: "admin.retina.fm", pathname: `/nodes/${REF}`, search: "", hash: "", href: `https://admin.retina.fm/nodes/${REF}` },
      writable: true,
      configurable: true,
    });
    serve(`GET /api/radar/analytics/${REF}`, () => json({ node_ref: REF, metrics: { total_frames: 9 } }));
    renderPage();

    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    await settle();
    expect(requests).toContain(`GET /api/radar/analytics/${REF}`);
    expect(requests.filter((request) => request.includes("/api/auth/"))).toEqual([]);
  });
});
