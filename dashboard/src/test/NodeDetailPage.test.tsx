import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import NodeDetailPage from "../pages/user/NodeDetailPage";
import { HttpError } from "@retina/shared";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    nodeAnalytics: vi.fn(), nodes: vi.fn(), myNodes: vi.fn(), releaseNode: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...await importOriginal<typeof import("recharts")>(),
  ResponsiveContainer: () => null,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function Navigation() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/nodes/b")}>Go to B</button>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/nodes/a"]}>
      <Navigation />
      <Routes><Route path="/nodes/:nodeId" element={<NodeDetailPage />} /></Routes>
    </MemoryRouter>,
  );
}

describe("a node's availability", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
    vi.mocked(api.myNodes).mockResolvedValue([]);
  });

  it("gives the week, the day and how much of the week it rests on", async () => {
    vi.mocked(api.nodeAnalytics).mockResolvedValue({
      node_ref: "a",
      metrics: { availability_7d: 0.9731, availability_24h: 1, availability_measured_s: 42 * 3600 },
    });
    renderPage();
    const row = (label: string) => screen.getByText(label).closest("tr")!;
    await screen.findByText("Availability (7 d)");
    expect(within(row("Availability (7 d)")).getByText("97.3%")).toBeInTheDocument();
    expect(within(row("Availability (24 h)")).getByText("100.0%")).toBeInTheDocument();
    expect(within(row("Availability measured over")).getByText("42.0h")).toBeInTheDocument();
  });
});

describe("NodeDetailPage route identity", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
    vi.mocked(api.myNodes).mockResolvedValue([
      { node_ref: "a", node_id: "ret-a", location_private: false },
      { node_ref: "b", node_id: "ret-b", location_private: false },
    ]);
    vi.mocked(api.nodeAnalytics).mockImplementation(async (nodeId) => ({ node_ref: nodeId }));
  });

  it("removes A's details and owner actions while B is loading, including a failed B fetch", async () => {
    const next = deferred<object>();
    vi.mocked(api.nodeAnalytics).mockResolvedValueOnce({ node_ref: "a" }).mockReturnValueOnce(next.promise);
    renderPage();
    await screen.findByRole("button", { name: /release this node/i });
    fireEvent.click(screen.getByText("Go to B"));
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(async () => next.reject(new HttpError(404, "Not Found", null)));
    expect(screen.getByText("Node not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    errorLog.mockRestore();
  });
});

describe("NodeDetailPage location privacy", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
    vi.mocked(api.nodeAnalytics).mockImplementation(async (nodeId) => ({ node_ref: nodeId }));
  });

  it("marks the owner's private node as private", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ node_ref: "a", node_id: "ret-a", location_private: true }]);

    renderPage();

    const heading = await screen.findByRole("heading", { level: 1 });
    await waitFor(() => expect(within(heading).getByText("Private")).toBeInTheDocument());
  });

  it("marks nothing on a public node", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ node_ref: "a", node_id: "ret-a", location_private: false }]);

    renderPage();

    await screen.findByRole("button", { name: /release this node/i });
    expect(screen.queryByText("Private")).not.toBeInTheDocument();
  });

  it("offers no setting to change it", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ node_ref: "a", node_id: "ret-a", location_private: true }]);

    renderPage();

    await screen.findByRole("button", { name: /release this node/i });
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
  });
});

describe("NodeDetailPage ownership", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
    vi.mocked(api.nodeAnalytics).mockImplementation(async (nodeId) => ({ node_ref: nodeId }));
    vi.mocked(api.myNodes).mockResolvedValue([
      { node_ref: "a", node_id: "ret-a", location_private: false, claimed_with: "ada@example.com" },
    ]);
  });

  it("shows the address the node was claimed with", async () => {
    renderPage();

    expect(await screen.findByText(/ada@example.com/)).toBeInTheDocument();
  });

  it("says so plainly when a node was assigned rather than claimed", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([
      { node_ref: "a", node_id: "ret-a", location_private: false, claimed_with: null },
    ]);

    renderPage();

    expect(await screen.findByText(/assigned to you rather than claimed/i)).toBeInTheDocument();
  });

  it("shows no ownership card to somebody who does not own the node", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([]);

    renderPage();

    await screen.findByText(/Detection Area|Loading/);
    expect(screen.queryByText(/Release this node/i)).not.toBeInTheDocument();
  });

  it("asks before releasing, naming what happens next", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    expect(screen.getByText(/whoever claims it next/i)).toBeInTheDocument();
    expect(api.releaseNode).not.toHaveBeenCalled();
  });

  it("releases only on the confirmation, and by node_id rather than the route ref", async () => {
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: /yes, release it/i }));

    await waitFor(() => expect(api.releaseNode).toHaveBeenCalledWith("ret-a"));
  });

  it("takes the owner's controls away once the node is gone", async () => {
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: /yes, release it/i }));

    await waitFor(() => expect(screen.queryByText(/Release this node/i)).not.toBeInTheDocument());
  });

  it("keeps the node when the release fails, and says why", async () => {
    vi.mocked(api.releaseNode).mockRejectedValue(new Error("Node not found"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: /yes, release it/i }));

    expect(await screen.findByText(/Node not found/)).toBeInTheDocument();
    expect(screen.getByText(/ada@example.com/)).toBeInTheDocument();
  });
});
