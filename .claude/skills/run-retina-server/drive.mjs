// Sign in to the laptop stack in headless Chromium and screenshot the signed-in
// console. Run from the repo root once up.sh has finished:
//
//   node .claude/skills/run-retina-server/drive.mjs [email] [out-dir]
//
// Mints and redeems its own link, so a completed run leaves none outstanding.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "@playwright/test";

const APP = "http://app.localhost:8080";
const email = process.argv[2] ?? "you@example.com";
const out = process.argv[3] ?? join(tmpdir(), "retina-shots");

const link = execFileSync(
  "docker",
  ["exec", "-i", "-w", "/app/backend", "retina-local-server", "python", "-", "link", email],
  { input: readFileSync(new URL("./seed.py", import.meta.url)) },
).toString().trim();
if (!link.startsWith(APP)) throw new Error(link);

mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
try {
  // The link page POSTs the token and navigates away once the session is set;
  // an invalid link stays put with an error, so a timeout here means that.
  await page.goto(link);
  await page.waitForURL((url) => !url.pathname.startsWith("/auth/link/"), { timeout: 15_000 });

  const me = await page.evaluate(() => fetch("/api/auth/me").then((r) => r.json()));
  if (me.email !== email.toLowerCase()) throw new Error(`not signed in: ${JSON.stringify(me)}`);
  const mine = await page.evaluate(() => fetch("/api/auth/me/nodes").then((r) => r.json()));
  const owned = mine.map((n) => `${n.node_id} (${n.status}${n.location_private ? ", private" : ""})`);
  console.log(`signed in as ${me.email}; owns ${owned.join(", ") || "nothing"}`);

  const shot = async (name) => {
    const path = join(out, `${name}.png`);
    await page.screenshot({ path });
    console.log(path);
  };
  // Waits are on text, not networkidle: every page polls, so the network never idles.
  await page.goto(`${APP}/onboarding`);
  await page.getByRole("heading", { name: "My nodes" }).waitFor();
  await shot("my-nodes");
  await page.goto(`${APP}/overview`);
  await page.getByRole("heading", { name: "My Nodes Overview" }).waitFor();
  await shot("overview");
  await page.goto(`${APP}/map`);
  await page.getByLabel("My nodes only").check();
  await page.waitForTimeout(3_000); // tiles and the owner-only feed
  await shot("map-my-nodes-only");
} finally {
  await browser.close();
}
