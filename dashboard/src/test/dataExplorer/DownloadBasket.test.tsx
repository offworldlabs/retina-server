import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DownloadBasket } from "../../pages/user/dataExplorer/DownloadBasket";
import { curlLine, downloadUrl, SESSION_READ, SESSION_UNSET } from "../../pages/user/dataExplorer/download";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { COPY_FEEDBACK_MS } from "../../pages/user/dataExplorer/useCopy";
import { stubViewport } from "../matchMedia";

const file = (node: string, name: string, size = 1024): ArchiveFile => ({
  key: `year=2026/month=09/day=17/node_ref=${node}/${name}`,
  name,
  node,
  day: "2026-09-17",
  size,
  endMs: Date.parse("2026-09-17T06:00:00Z"),
  startMs: Date.parse("2026-09-17T05:00:00Z"),
  firstFrameMs: null, publishedLat: null, publishedLon: null,
});

const TWO = [file("ret-a", "a.parquet"), file("ret-b", "b.parquet")];

function setup(over: Partial<Parameters<typeof DownloadBasket>[0]> = {}) {
  const onToggleManifest = vi.fn();
  const onClear = vi.fn();
  const view = render(
    <DownloadBasket
      files={over.files ?? TWO}
      manifestOpen={over.manifestOpen ?? false}
      onToggleManifest={onToggleManifest}
      onClear={onClear}
    />,
  );
  return { onToggleManifest, onClear, view };
}

let writeText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("DownloadBasket", () => {
  it("counts the selection and sizes it both ways", () => {
    setup();
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("2 files selected · 2.0 KB");
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("18.0 KB downloaded as JSON");
  });

  it("reads as empty with nothing selected, and cannot copy", () => {
    setup({ files: [] });
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("0 files selected · 0 B");
    expect(screen.queryByText(/as JSON/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy curl" })).toBeDisabled();
  });

  it("keeps the manifest and the curl excerpt out of the way until asked", () => {
    const { onToggleManifest } = setup();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show manifest" }));
    expect(onToggleManifest).toHaveBeenCalled();
  });

  it("lists one download URL per line in the manifest", () => {
    setup({ manifestOpen: true });
    expect(screen.getByRole("button", { name: "Hide manifest" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /manifest/i })).toHaveValue(
      `${downloadUrl(TWO[0])}\n${downloadUrl(TWO[1])}`,
    );
  });

  it("shows the first few curl lines and says how many the copy takes", () => {
    const five = ["a", "b", "c", "d", "e"].map((n) => file(`ret-${n}`, `${n}.parquet`));
    setup({ files: five, manifestOpen: true });
    const excerpt = screen.getByTestId("de-curl-excerpt").textContent;
    expect(excerpt).toContain(curlLine(five[0]));
    expect(excerpt).toContain(curlLine(five[2]));
    expect(excerpt).not.toContain(curlLine(five[3]));
    expect(excerpt).toContain("# … 2 more; Copy curl takes all 5");
  });

  it("copies every curl line, not just the excerpt, and says so briefly", async () => {
    vi.useFakeTimers();
    setup();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy curl" }));
    });
    expect(writeText).toHaveBeenCalledWith(`${curlLine(TWO[0])}\n${curlLine(TWO[1])}`);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(COPY_FEEDBACK_MS);
    });
    expect(screen.getByRole("button", { name: "Copy curl" })).toBeInTheDocument();
  });

  it("says when the clipboard refuses", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    setup();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy curl" }));
    });
    expect(screen.getByRole("button", { name: "Copy failed" })).toBeInTheDocument();
  });

  it("clears on request", () => {
    const { onClear } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalled();
  });
});

describe("DownloadBasket, with files from the owner's own archive", () => {
  const own = { ...file("nde-roof", "r.parquet"), source: "owner" as const };
  const pub = { ...file("ret-a", "a.parquet"), source: "public" as const };

  it("says the link needs the session when it is the only one", () => {
    setup({ files: [own] });
    expect(screen.getByTestId("de-basket-session")).toHaveTextContent("Its link needs your signed-in session");
  });

  it("says how many of the links need it in a mixed basket", () => {
    setup({ files: [own, pub], manifestOpen: true });
    expect(screen.getByTestId("de-basket-session")).toHaveTextContent("1 of 2 links need your signed-in session");
    expect(screen.getByTestId("de-session-hint")).toHaveTextContent(
      "asks for its value first (read -rs RETINA_SESSION) and forgets it once the downloads are done",
    );
    expect(screen.getByTestId("de-session-hint")).not.toHaveTextContent("export");
    expect(screen.getByTestId("de-curl-excerpt").textContent).toContain('-b "auth_token=$RETINA_SESSION"');
  });

  it("copies a script that asks for the session before its first line and forgets it after its last", async () => {
    setup({ files: [own, pub] });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy curl" }));
    });
    expect(writeText).toHaveBeenCalledWith(
      ["{", SESSION_READ, curlLine(own), curlLine(pub), SESSION_UNSET, "}"].join("\n"),
    );
  });

  it("says nothing of a session for public links alone", () => {
    setup({ files: [pub], manifestOpen: true });
    expect(screen.queryByTestId("de-basket-session")).not.toBeInTheDocument();
    expect(screen.queryByTestId("de-session-hint")).not.toBeInTheDocument();
  });
});

describe("DownloadBasket on a phone", () => {
  afterEach(() => vi.unstubAllGlobals());

  /** Opens the sheet from the bar, as a tap would. */
  function openSheet() {
    fireEvent.click(screen.getByRole("button", { name: "Download" }));
    return screen.getByRole("dialog", { name: "Download basket" });
  }

  it("renders nothing while the basket is empty", () => {
    stubViewport(390);
    const { view } = setup({ files: [] });
    expect(view.container).toBeEmptyDOMElement();
  });

  it("is one slim bar: the count, the size, and Download in place of the three buttons", () => {
    stubViewport(390);
    setup();
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent(/^2 files · 2\.0 KB$/);
    expect(screen.getByRole("button", { name: "Download" })).toHaveAttribute("aria-haspopup", "dialog");
    expect(screen.queryByRole("button", { name: "Show manifest" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("opens the whole basket in a sheet that copies the curl and clears", async () => {
    stubViewport(390);
    const { onClear } = setup();
    const sheet = openSheet();

    expect(sheet).toHaveAccessibleDescription("2 files selected · 2.0 KB (≈ 18.0 KB downloaded as JSON)");
    expect(within(sheet).getByRole("textbox", { name: "Manifest of download URLs" })).toHaveValue(
      `${downloadUrl(TWO[0])}\n${downloadUrl(TWO[1])}`,
    );
    expect(within(sheet).getByTestId("de-curl-excerpt").textContent).toContain(curlLine(TWO[1]));

    await act(async () => {
      fireEvent.click(within(sheet).getByRole("button", { name: "Copy curl" }));
    });
    expect(writeText).toHaveBeenCalledWith(`${curlLine(TWO[0])}\n${curlLine(TWO[1])}`);
    expect(within(sheet).getByRole("button", { name: "Copied" })).toBeInTheDocument();

    fireEvent.click(within(sheet).getByRole("button", { name: "Clear" }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("closes the sheet when the basket empties, and leaves it closed when files come back", () => {
    stubViewport(390);
    const { view } = setup();
    openSheet();
    const rerender = (files: ArchiveFile[]) =>
      view.rerender(
        <DownloadBasket files={files} manifestOpen={false} onToggleManifest={() => {}} onClear={() => {}} />,
      );

    rerender([]);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByTestId("de-basket")).not.toBeInTheDocument();

    rerender(TWO);
    expect(screen.getByRole("button", { name: "Download" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("marks the bar with the padlock and puts the session notes in the sheet", () => {
    stubViewport(390);
    const own = { ...file("nde-roof", "r.parquet"), source: "owner" as const };
    setup({ files: [own] });
    expect(screen.getByTestId("de-basket-count").querySelector(".de-lock")).not.toBeNull();
    expect(screen.queryByTestId("de-basket-session")).not.toBeInTheDocument();

    const sheet = openSheet();
    expect(within(sheet).getByTestId("de-basket-session")).toHaveTextContent("Its link needs your signed-in session");
    expect(within(sheet).getByTestId("de-session-hint")).toHaveTextContent("read -rs RETINA_SESSION");
  });

  it("says whether its sheet is open, and takes focus back when the sheet closes", () => {
    stubViewport(390);
    setup();
    const download = screen.getByRole("button", { name: "Download" });
    expect(download).toHaveAttribute("aria-expanded", "false");

    const sheet = openSheet();
    expect(download).toHaveAttribute("aria-expanded", "true");

    fireEvent.keyDown(sheet, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(download).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(download);
  });

  it("gives way to the wider bar when the screen widens, with no sheet left open", () => {
    const viewport = stubViewport(390);
    setup();
    openSheet();

    viewport.resize(1440);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("2 files selected · 2.0 KB");
    for (const name of ["Show manifest", "Copy curl", "Clear"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
  });
});
