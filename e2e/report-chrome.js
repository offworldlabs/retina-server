// Launches the browser the suite drives and prints its version, failing if
// Playwright cannot launch it. The channel is playwright.config.ts's.
import { chromium } from "@playwright/test";

const browser = await chromium.launch({ channel: "chrome" });
console.log(`Google Chrome ${browser.version()}`);
await browser.close();
