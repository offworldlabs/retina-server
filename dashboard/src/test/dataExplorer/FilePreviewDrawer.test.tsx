import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { UnauthorizedError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onSessionEnded } from "../../api/client";
import { curlLine, downloadUrl, SESSION_READ, SESSION_UNSET } from "../../pages/user/dataExplorer/download";
import {
  FilePreviewDrawer,
  PREVIEW_TIMEOUT_MS,
} from "../../pages/user/dataExplorer/FilePreviewDrawer";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import type { RegistryNode } from "../../pages/user/dataExplorer/nodes";
import { stubViewport } from "../matchMedia";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({
  request: requestMock,
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

const FILE: ArchiveFile = {
  key: "year=2026/month=09/day=17/node_ref=ret-a/part-000001.parquet",
  name: "part-000001.parquet",
  node: "ret-a",
  day: "2026-09-17",
  size: 2048,
  endMs: Date.parse("2026-09-17T06:00:00Z"),
  startMs: Date.parse("2026-09-17T05:00:00Z"),
  firstFrameMs: null, publishedLat: null, publishedLon: null,
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
    positions: over.positions,
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

describe("FilePreviewDrawer, for a file from the owner's own archive", () => {
  const OWN = { ...FILE, source: "owner" as const };

  it("downloads the preview from the owner's route, which answers their session", async () => {
    setup({ file: OWN });
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    expect(requestMock).toHaveBeenCalledWith(
      `/api/auth/me/archive/${FILE.key}`,
      expect.objectContaining({ timeoutMs: PREVIEW_TIMEOUT_MS }),
    );
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("Frames"));
  });

  it("ends the session on the page at once when the owner's route answers 401", async () => {
    const told = vi.fn();
    const stop = onSessionEnded(told);
    requestMock.mockRejectedValue(new UnauthorizedError("Not authenticated"));
    setup({ file: OWN });
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("Not authenticated"));
    expect(told).toHaveBeenCalledTimes(1);
    stop();
  });

  it("links the owner's route, and gives a curl text that asks for the session itself", () => {
    setup({ file: OWN });
    expect(screen.getByRole("link", { name: "Open JSON" })).toHaveAttribute("href", downloadUrl(OWN));
    // As copied from the block, it runs as it stands: the prompt, the line, then
    // the value forgotten.
    expect(screen.getByRole("dialog").querySelector(".de-codeblock")!.textContent).toBe(
      ["{", SESSION_READ, curlLine(OWN), SESSION_UNSET, "}"].join("\n"),
    );
    expect(screen.getByTestId("de-drawer-session")).toHaveTextContent(
      "asks for your auth_token cookie before it runs",
    );
    expect(screen.getByTestId("de-drawer-session")).toHaveTextContent("neither shown nor kept in your shell history");
  });

  it("says nothing of a session for a public file", () => {
    setup();
    expect(screen.queryByTestId("de-drawer-session")).not.toBeInTheDocument();
  });

  it("downloads a public file's preview sending only the origin as the referrer", async () => {
    setup();
    fireEvent.click(screen.getByRole("button", { name: /Load preview/ }));
    expect(requestMock).toHaveBeenCalledWith(
      `/api/data/archive/${FILE.key}`,
      expect.objectContaining({ timeoutMs: PREVIEW_TIMEOUT_MS, referrerPolicy: "strict-origin" }),
    );
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("Frames"));
  });

  it("names an owner's position as exact rather than published", () => {
    setup({ file: OWN, positions: "exact" });
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Exact rx51.5000, -0.1235 your node's own position, sent only to you");
    expect(dialog).not.toHaveTextContent("±2 km");
  });

  it("says an owner's node that has left the account is no longer theirs", () => {
    setup({ file: OWN, node: undefined, positions: "exact" });
    expect(screen.getByRole("dialog")).toHaveTextContent("(no longer on your account)");
    expect(screen.getByRole("dialog")).toHaveTextContent("no position on record");
  });
});

describe("FilePreviewDrawer on a phone", () => {
  afterEach(() => vi.unstubAllGlobals());

  /** The full-screen sheet, named by the file it previews. */
  function sheet(name = FILE.name) {
    const dialog = screen.getByRole("dialog", { name });
    expect(dialog).toHaveClass("sheet-full", "de-preview-sheet");
    return dialog;
  }

  it("opens as a sheet over the whole screen, with no drawer or scrim", () => {
    stubViewport(390);
    setup();
    expect(sheet()).toHaveAccessibleDescription(FILE.key);
    expect(document.querySelector("aside")).toBeNull();
    expect(screen.queryByTestId("de-scrim")).not.toBeInTheDocument();
  });

  it("closes once per Escape, the sheet's own, and from its close button", () => {
    stubViewport(390);
    const { onClose } = setup();

    fireEvent.keyDown(sheet(), { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    // Nothing listens on the document as the drawer does.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.click(within(sheet()).getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("loads the preview inside the sheet", async () => {
    stubViewport(390);
    setup();
    const dialog = sheet();
    fireEvent.click(within(dialog).getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(dialog).toHaveTextContent("Frames"));
    expect(dialog).toHaveTextContent("ed25519 · signature valid");
    expect(within(dialog).getAllByRole("row")).toHaveLength(3);
  });

  it("adds the file to the basket from the sheet's foot", () => {
    stubViewport(390);
    const { onAddToBasket } = setup();
    fireEvent.click(within(sheet()).getByRole("button", { name: "Add to basket" }));
    expect(onAddToBasket).toHaveBeenCalledWith(FILE.key);
    expect(within(sheet()).getByRole("link", { name: "Open JSON" })).toHaveAttribute("href", downloadUrl(FILE));
  });

  it("offers to take the file out of the basket while it is in it", () => {
    stubViewport(390);
    const { view, props, onAddToBasket } = setup();
    const onRemoveFromBasket = vi.fn();

    fireEvent.click(within(sheet()).getByRole("button", { name: "Add to basket" }));
    expect(onAddToBasket).toHaveBeenCalledWith(FILE.key);

    view.rerender(<FilePreviewDrawer {...props} inBasket onRemoveFromBasket={onRemoveFromBasket} />);
    expect(within(sheet()).queryByRole("button", { name: "Add to basket" })).not.toBeInTheDocument();
    fireEvent.click(within(sheet()).getByRole("button", { name: "Remove from basket" }));
    expect(onRemoveFromBasket).toHaveBeenCalledWith(FILE.key);
    expect(onAddToBasket).toHaveBeenCalledTimes(1);
  });

  it("keeps the drawer's own Add to basket whatever the basket holds", () => {
    const viewport = stubViewport(390);
    const { view, props } = setup();
    view.rerender(<FilePreviewDrawer {...props} inBasket onRemoveFromBasket={vi.fn()} />);
    expect(within(sheet()).getByRole("button", { name: "Remove from basket" })).toBeInTheDocument();

    viewport.resize(1440);

    expect(within(screen.getByTestId("de-drawer")).getByRole("button", { name: "Add to basket" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove from basket" })).not.toBeInTheDocument();
  });

  it("stays open, the same sheet, when another file is chosen", () => {
    stubViewport(390);
    const { view, props } = setup();
    const first = sheet();
    const other = { ...FILE, key: FILE.key.replace("000001", "000002"), name: "part-000002.parquet" };

    view.rerender(<FilePreviewDrawer {...props} file={other} />);

    expect(sheet("part-000002.parquet")).toBe(first);
    expect(first).toHaveAccessibleDescription(other.key);
  });

  it("becomes the drawer when the screen widens", () => {
    const viewport = stubViewport(390);
    setup();
    sheet();

    viewport.resize(1440);

    expect(screen.getByRole("dialog", { name: FILE.name })).toBe(screen.getByTestId("de-drawer"));
    expect(screen.getByTestId("de-scrim")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });
});
