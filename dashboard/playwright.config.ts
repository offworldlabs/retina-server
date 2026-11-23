import { defineConfig, devices } from "@playwright/test";

/**
 * Browser tests of the map wrapper (src/lib/leaflet), on a harness page the
 * Vite dev server serves. Development React, so StrictMode really mounts
 * twice. `vite build` takes only index.html, so the harness never ships.
 *
 * BROWSER_TEST_PORT moves the dev server, to run another checkout beside this one.
 */
const port = Number(process.env.BROWSER_TEST_PORT ?? 5175);
const origin = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./src/lib/leaflet",
  testMatch: "**/*.browser.ts",
  timeout: 30_000,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "github" : "list",
  use: { baseURL: origin, headless: true },
  projects: [
    {
      name: "chromium",
      // The installed Google Chrome, as e2e uses: CI's runner image ships it.
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
  ],
  webServer: {
    command: `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
    url: `${origin}/src/lib/leaflet/browser/harness.html`,
    // A server already on the port may be another checkout's, so never reuse one.
    reuseExistingServer: false,
  },
});
