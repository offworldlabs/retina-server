import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));
const manifest = (dir) => JSON.parse(readFileSync(`${root}${dir}/package.json`, "utf8"));

// Every workspace whose own `test` script runs Vitest, each under its own
// config (environment, setup files, aliases). e2e has none: its specs are
// Playwright's.
export default defineConfig({
  test: {
    // Project paths resolve against this, which is otherwise the cwd.
    root,
    projects: manifest(".").workspaces.filter((dir) => /\bvitest\b/.test(manifest(dir).scripts?.test ?? "")),
  },
});
