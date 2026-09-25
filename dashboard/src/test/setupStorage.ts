import { beforeEach } from "vitest";

// Node 25 and later define storage globals of their own (localStorage is
// undefined without --localstorage-file), and they hide jsdom's. Installing
// jsdom's explicitly gives every Node the same storage, from module scope on.
// Every test starts with it empty and unstubbed, so seed or stub it in the test
// or a beforeEach, never at module or describe scope or in beforeAll.

// Read off globalThis rather than declared, so no other file sees a `jsdom` global.
const dom = (globalThis as { jsdom?: { window: Window } }).jsdom;

// jsdom names itself in its user agent, which tells a jsdom environment that no
// longer exposes its instance apart from a file that opted into another one.
if (!dom && typeof navigator !== "undefined" && /\bjsdom\//.test(navigator.userAgent)) {
  throw new Error(
    "Vitest's jsdom environment no longer sets globalThis.jsdom, so setupStorage.ts cannot install jsdom's storage",
  );
}

// A file that opts into another environment has no jsdom to borrow from.
if (dom) {
  // Held here because under a VM pool `window` is jsdom's own, so a test's stub
  // would otherwise replace the only reference to the real storage.
  const stores = [
    ["localStorage", dom.window.localStorage],
    ["sessionStorage", dom.window.sessionStorage],
  ] as const;

  const install = () => {
    // Not writable, as in a browser, where assigning to either throws.
    for (const [name, storage] of stores) {
      Object.defineProperty(window, name, { value: storage, configurable: true, writable: false });
    }
  };

  install();
  beforeEach(() => {
    // Reinstalled as well as emptied, since a test may swap in a storage that throws.
    install();
    for (const [, storage] of stores) storage.clear();
  });
}
