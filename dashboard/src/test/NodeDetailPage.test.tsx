import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import NodeDetailPage from "../pages/user/NodeDetailPage";
import { HttpError } from "@retina/shared";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    nodeAnalytics: vi.fn(), nodes: vi.fn(), myNodes: vi.fn(),
    myNodeLocationPrivacy: vi.fn(), clearMyNodeLocationPrivacy: vi.fn(), releaseNode: vi.fn(),
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

const privateRadio = () => screen.getByRole("radio", { name: /^Private/ });
const publicRadio = () => screen.getByRole("radio", { name: /^Public/ });

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

  it("removes A's details and privacy actions while B is loading, including a failed B fetch", async () => {
    const next = deferred<object>();
    vi.mocked(api.nodeAnalytics).mockResolvedValueOnce({ node_ref: "a" }).mockReturnValueOnce(next.promise);
    renderPage();
    await screen.findByRole("radio", { name: /^Public/ });
    fireEvent.click(screen.getByText("Go to B"));
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    const errorLog = vi.spyOn(console, "error").mockImplementation(() => {});
    await act(async () => next.reject(new HttpError(404, "Not Found", null)));
    expect(screen.getByText("Node not found")).toBeInTheDocument();
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    errorLog.mockRestore();
  });

  it("does not carry A's saved privacy setting into B", async () => {
    vi.mocked(api.myNodeLocationPrivacy).mockResolvedValue({
      node_id: "ret-a", location_private: true, location_privacy_source: "override",
    });
    renderPage();
    await screen.findByRole("radio", { name: /^Private/ });
    fireEvent.click(privateRadio());
    await waitFor(() => expect(privateRadio()).not.toBeDisabled());
    expect(privateRadio()).toBeChecked();
    fireEvent.click(screen.getByText("Go to B"));
    await screen.findByRole("link", { name: "b" });
    expect(publicRadio()).toBeChecked();
    fireEvent.click(privateRadio());
    expect(api.myNodeLocationPrivacy).toHaveBeenLastCalledWith("ret-b", true);
  });

  it("ignores an A save that completes after moving to B", async () => {
    const save = deferred<any>();
    vi.mocked(api.myNodeLocationPrivacy).mockReturnValueOnce(save.promise);
    renderPage();
    await screen.findByRole("radio", { name: /^Private/ });
    fireEvent.click(privateRadio());
    fireEvent.click(screen.getByText("Go to B"));
    await screen.findByRole("link", { name: "b" });
    await act(async () => save.resolve({
      node_id: "ret-a", location_private: true, location_privacy_source: "override",
    }));
    expect(publicRadio()).toBeChecked();
    expect(publicRadio()).not.toBeDisabled();
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
    expect(screen.getByText(/Location privacy/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /yes, release it/i }));

    await waitFor(() => expect(screen.queryByText(/Release this node/i)).not.toBeInTheDocument());
    // The privacy routes answer 404 to an ex-owner, so the card goes too.
    expect(screen.queryByText(/Location privacy/)).not.toBeInTheDocument();
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
