import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConfigPage from "../pages/admin/ConfigPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    adminNodeConfig: vi.fn(),
    adminTowerConfig: vi.fn(),
    adminConfigHistory: vi.fn(),
    adminUpdateNodeConfig: vi.fn(),
  },
}));

const NODE_CONFIG = { _source: "live", total: 1, nodes: { "ret-a": { rx_lat: 51.5 } } };
const TOWER_CONFIG = { _source: "live", total: 0, towers: {} };

beforeEach(() => {
  vi.mocked(api.adminNodeConfig).mockReset().mockResolvedValue(NODE_CONFIG);
  vi.mocked(api.adminTowerConfig).mockReset().mockResolvedValue(TOWER_CONFIG);
  vi.mocked(api.adminConfigHistory).mockReset().mockResolvedValue([]);
  vi.mocked(api.adminUpdateNodeConfig).mockReset();
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function openEditor(text: string) {
  render(<ConfigPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

describe("ConfigPage saving the node config", () => {
  it("reports text that will not parse without sending it", async () => {
    await openEditor("{ not json");

    expect(await screen.findByRole("alert")).toHaveTextContent("Not saved: the text is not valid JSON");
    expect(api.adminUpdateNodeConfig).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toHaveValue("{ not json");
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("reports a failed request as a failed save, not as bad JSON", async () => {
    vi.mocked(api.adminUpdateNodeConfig).mockRejectedValue(
      new HttpError(422, "Unprocessable Entity", { detail: "nodes must be an object" }),
    );
    await openEditor('{"nodes": []}');

    const notice = await screen.findByRole("alert");
    expect(notice).toHaveTextContent("Could not save the node config: nodes must be an object");
    expect(notice).not.toHaveTextContent("JSON");
    expect(screen.getByRole("textbox")).toHaveValue('{"nodes": []}');
    expect(window.alert).not.toHaveBeenCalled();
  });

  it("fetches the configuration and its history again after a save", async () => {
    vi.mocked(api.adminUpdateNodeConfig).mockResolvedValue({});
    await openEditor('{"nodes": {}}');

    await waitFor(() => expect(api.adminNodeConfig).toHaveBeenCalledTimes(2));
    expect(api.adminConfigHistory).toHaveBeenCalledTimes(2);
    expect(api.adminUpdateNodeConfig).toHaveBeenCalledWith({ nodes: {} });
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ConfigPage history", () => {
  it("says the history failed to load rather than that there is none", async () => {
    vi.mocked(api.adminConfigHistory).mockRejectedValue(new Error("HTTP 502"));
    render(<ConfigPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the version history");
    expect(screen.queryByText("No config changes recorded yet.")).toBeNull();
    expect(screen.getByText("ret-a")).toBeInTheDocument();
  });
});

describe("ConfigPage search", () => {
  it("says when nothing matches", async () => {
    render(<ConfigPage />);
    fireEvent.change(await screen.findByPlaceholderText("Search…"), { target: { value: "zzz" } });

    expect(screen.getByText('No nodes match "zzz"')).toBeInTheDocument();
  });
});
