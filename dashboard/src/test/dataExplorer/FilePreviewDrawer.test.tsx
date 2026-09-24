import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { downloadUrl } from "../../pages/user/dataExplorer/download";
import {
  FilePreviewDrawer,
  PREVIEW_TIMEOUT_MS,
} from "../../pages/user/dataExplorer/FilePreviewDrawer";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import type { RegistryNode } from "../../pages/user/dataExplorer/nodes";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));

const FILE: ArchiveFile = {
  key: "year=2026/month=09/day=17/node_ref=ret-a/part-000001.parquet",
  name: "part-000001.parquet",
  node: "ret-a",
  day: "2026-09-17",
  size: 2048,
  endMs: Date.parse("2026-09-17T06:00:00Z"),
  startMs: Date.parse("2026-09-17T05:00:00Z"),
};

const NODE: RegistryNode = {
  id: "ret-a",
  name: "Alpha",
  status: "online",
  synthetic: false,
  lat: 51.50001,
  lon: -0.12345,
  uncertaintyKm: 2,
};

const T0 = Date.parse("2026-09-17T05:10:00Z");
const frame = (offsetS: number) => ({
  timestamp: T0 + offsetS * 1000,
  delay: [12.345],
  doppler: [-30.5],
  snr: [14.25],
  adsb: [{ hex: "abc123" }],
  rx_lat: 51.5,
  rx_lon: -0.1,
  rx_alt_ft: 100,
  tx_lat: 51.6,
  tx_lon: -0.2,
  tx_alt_ft: 900,
  fc_hz: 204_640_000,
  fs_hz: 2_000_000,
  _signing_mode: "ed25519",
  _signature_valid: true,
});

function setup(over: Partial<Parameters<typeof FilePreviewDrawer>[0]> = {}) {
  const onClose = vi.fn();
  const onAddToBasket = vi.fn();
  const props = {
    file: "file" in over ? over.file : FILE,
    node: "node" in over ? over.node : NODE,
    onClose,
    onAddToBasket,
  };
  const view = render(<FilePreviewDrawer {...props} />);
  return { onClose, onAddToBasket, view, props };
}

beforeEach(() => {
  requestMock.mockReset();
  requestMock.mockResolvedValue({ node_ref: "ret-a", detections: [frame(0), frame(60)] });
});

describe("FilePreviewDrawer", () => {
  it("is absent until a file is chosen", () => {
    setup({ file: null });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("describes the file before anything is downloaded", () => {
    setup();
    const dialog = screen.getByRole("dialog", { name: "part-000001.parquet" });
    expect(dialog).toHaveTextContent(FILE.key);
    expect(dialog).toHaveTextContent("ret-a");
    expect(dialog).toHaveTextContent("2026-09-17");
    expect(dialog).toHaveTextContent("05:00 → 06:00 UTC");
    expect(dialog).toHaveTextContent("2.0 KB Parquet · ≈ 18.0 KB as JSON");
    expect(dialog).toHaveTextContent("51.5000, -0.1235");
    expect(dialog).toHaveTextContent("±2 km");
    expect(requestMock).not.toHaveBeenCalled();
  });

  it("says when the node is not in the registry", () => {
    setup({ node: undefined });
    expect(screen.getByRole("dialog")).toHaveTextContent("not in the node registry");
    expect(screen.getByRole("dialog")).toHaveTextContent("no published position");
  });

  it("links the JSON and shows the curl line", () => {
    setup();
    expect(screen.getByRole("link", { name: "Open JSON" })).toHaveAttribute("href", downloadUrl(FILE));
    expect(screen.getByRole("dialog")).toHaveTextContent(
      `curl -sS -o ret-a-part-000001.json "${downloadUrl(FILE)}"`,
    );
  });

  it("downloads the file only on request, with a long timeout, and summarises it", async () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    expect(requestMock).toHaveBeenCalledWith(
      `/api/data/archive/${FILE.key}`,
      expect.objectContaining({ timeoutMs: PREVIEW_TIMEOUT_MS }),
    );
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("Frames"));
    expect(dialog).toHaveTextContent("2026-09-17 05:10:00Z → 2026-09-17 05:11:00Z");
    expect(dialog).toHaveTextContent("1.0 min");
    expect(dialog).toHaveTextContent("2 (1.00 per frame)");
    expect(dialog).toHaveTextContent("100.0% of 2 detections carry an ADS-B match");
    expect(dialog).toHaveTextContent("ed25519 · signature valid");
    expect(dialog).toHaveTextContent("204.640 MHz / 2.000 MHz");
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(screen.getAllByRole("cell", { name: "12.345" })).toHaveLength(2);
  });

  it("says how many signatures verified when not all of them did", async () => {
    requestMock.mockResolvedValue({
      node_ref: "ret-a",
      detections: [frame(0), { ...frame(60), _signature_valid: false }],
    });
    setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    await waitFor(() =>
      expect(screen.getByRole("dialog")).toHaveTextContent("ed25519 · 1 of 2 signatures valid"),
    );
  });

  it("reports a failed download and offers to try again", async () => {
    requestMock.mockRejectedValueOnce(new Error("HTTP 503"));
    setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(screen.getByText(/Preview failed: HTTP 503/)).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("Frames"));
    expect(requestMock).toHaveBeenCalledTimes(2);
  });

  it("says when a file decoded to nothing", async () => {
    requestMock.mockResolvedValue({ node_ref: "ret-a", detections: [] });
    setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(screen.getByText(/decoded to zero frames/)).toBeInTheDocument());
  });

  it("keeps a preview across a close and a reopen", async () => {
    const { view, props } = setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("Frames"));
    view.rerender(<FilePreviewDrawer {...props} file={null} />);
    view.rerender(<FilePreviewDrawer {...props} file={FILE} />);
    expect(screen.getByRole("dialog")).toHaveTextContent("Frames");
    expect(requestMock).toHaveBeenCalledTimes(1);
  });

  it("closes from the button, the scrim and the Escape key", () => {
    const { onClose } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByTestId("de-scrim"));
    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("adds the open file to the basket", () => {
    const { onAddToBasket } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Add to basket" }));
    expect(onAddToBasket).toHaveBeenCalledWith(FILE.key);
  });
});
