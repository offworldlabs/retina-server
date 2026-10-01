import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { reloadOnStaleChunk } from "../utils/staleChunk";
import main from "../main.tsx?raw";

const page = (entry: string) =>
  `<!doctype html><html><head><script type="module" crossorigin src="${entry}"></script></head><body></body></html>`;

const realLocation = window.location;
const reload = vi.fn();
const fetchPage = vi.fn();

beforeEach(() => {
  document.head.innerHTML = '<script type="module" crossorigin src="/assets/index-old.js"></script>';
  reload.mockReset();
  fetchPage.mockReset();
  vi.stubGlobal("fetch", fetchPage);
  Object.defineProperty(window, "location", {
    value: { href: "https://app.example/users", reload },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
  vi.unstubAllGlobals();
  document.head.innerHTML = "";
});

describe("a page chunk that will not load", () => {
  it("is caught for every page, from the entry point", () => {
    expect(main).toContain('window.addEventListener("vite:preloadError", reloadOnStaleChunk);');
  });

  it("reloads the document when a newer build is live", async () => {
    fetchPage.mockResolvedValue(new Response(page("/assets/index-new.js")));
    await reloadOnStaleChunk();
    expect(fetchPage).toHaveBeenCalledWith("https://app.example/users", { cache: "no-store" });
    expect(reload).toHaveBeenCalledOnce();
  });

  it("reaches the error boundary when this tab already runs the live build", async () => {
    fetchPage.mockResolvedValue(new Response(page("/assets/index-old.js")));
    await reloadOnStaleChunk();
    expect(reload).not.toHaveBeenCalled();
  });

  it("knows its own build beside a module script an extension added first", async () => {
    document.head.insertAdjacentHTML("afterbegin", '<script type="module" src="chrome-extension://abc/inject.js"></script>');
    fetchPage.mockResolvedValue(new Response(page("/assets/index-old.js")));
    await reloadOnStaleChunk();
    expect(reload).not.toHaveBeenCalled();
  });

  it("sees a newer entry behind a module script both builds share", async () => {
    document.head.insertAdjacentHTML("afterbegin", '<script type="module" src="/polyfill.js"></script>');
    fetchPage.mockResolvedValue(
      new Response(page("/assets/index-new.js").replace("<head>", '<head><script type="module" src="/polyfill.js"></script>')),
    );
    await reloadOnStaleChunk();
    expect(reload).toHaveBeenCalledOnce();
  });

  it("keeps the console up when the server cannot be reached", async () => {
    fetchPage.mockRejectedValue(new TypeError("Failed to fetch"));
    await reloadOnStaleChunk();
    expect(reload).not.toHaveBeenCalled();
  });

  it("keeps the console up while the server answers with an error", async () => {
    fetchPage.mockResolvedValue(new Response("Bad gateway", { status: 502 }));
    await reloadOnStaleChunk();
    expect(reload).not.toHaveBeenCalled();
  });

  it("keeps the console up when a sign-in page answers in its place", async () => {
    fetchPage.mockResolvedValue(new Response("<!doctype html><html><body><form></form></body></html>"));
    await reloadOnStaleChunk();
    expect(reload).not.toHaveBeenCalled();
  });
});
