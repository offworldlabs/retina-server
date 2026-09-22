---
name: run-retina-server
description: Run, start, sign in to and screenshot the retina-server stack locally in Docker (app.localhost:8080), with seeded accounts that own some synthetic nodes and not others. Use when asked to run the app, try the signed-in console or My Nodes, test node ownership by hand, or take screenshots of the running console.
---

Brings up the laptop compose stack with magic-link sign-in switched on, seeds two
accounts that own some of the synthetic fleet's nodes, and drives the signed-in
console in headless Chromium through Playwright. `up.sh` launches and seeds,
`drive.mjs` signs in and screenshots, and `seed.py` runs inside the server
container to seed or mint a sign-in link. Paths are relative to the repo root.
Verified on macOS with Docker Desktop.

## Prerequisites

Docker with Compose 2.24 or later, and the worktree setup from ONBOARDING,
"Working in a git worktree". The image installs `libs/*`, and the driver needs
the root workspace's `@playwright/test` with its Chromium already downloaded.

```bash
git submodule update --init --recursive
npm ci
```

## Run (agent path)

```bash
bash .claude/skills/run-retina-server/up.sh
```

This builds and starts the stack, seeds it, waits for the fleet's 25 nodes (from
one to five minutes on a cold start) and prints a single-use sign-in link. Then:

```bash
node .claude/skills/run-retina-server/drive.mjs
```

This mints its own link, signs in as `you@example.com` and prints what the account
owns, then writes `my-nodes.png`, `overview.png` and `map-my-nodes-only.png` to
`$TMPDIR/retina-shots/`. The two optional arguments are an email and an output
directory. Look at the screenshots: a zero exit code only proves the headings
rendered.

What the seed sets up (`seed.py` holds the plan):

| Account | Owns |
|---|---|
| `you@example.com` | `synth-GVL-0001` to `0004` (`0002` location-private), plus `ret0c0ffee0001`, registered but never connected |
| `neighbour@example.com` | `synth-GVL-0005` to `0007` |
| nobody | the other 18 synthetic nodes |

## Run (human path)

Open the link `up.sh` printed, in Chrome. For another one, or to sign in as the
other account:

```bash
docker exec -i -w /app/backend retina-local-server python - link neighbour@example.com < .claude/skills/run-retina-server/seed.py
```

Stop the stack. Its volume keeps the accounts and claims for the next `up.sh`:

```bash
docker compose -p retina-signed-in down
```

## Test

The suites are in ONBOARDING, "Running tests". This skill runs the app; it does
not replace them.

## Gotchas

- **A session belongs to one browser.** Opening `app.localhost:8080` without the
  link shows the console signed out, even when another browser (or the driver)
  is signed in. Links are single-use, last 15 minutes, and an address may hold
  five at once.
- **The anonymous-admin bypass outranks a cookie.** `signed-in.compose.yml` sets
  `AUTH_ALLOW_ANONYMOUS_ADMIN=0`, without which every caller is the admin and
  sign-in appears to do nothing. The cost is that `admin.localhost` answers 401.
- **A private node disappears from `/api/radar/nodes`**, so the public count drops
  to 24 and the overview reads "24 / 24". This is why `seed.py` names its ids
  instead of reading that list, and why `up.sh` waits on `nodes.active` in
  `/api/test/dashboard`. The fleet's own log is no substitute: it reports 25
  active nodes even while the server is down. The ids follow
  `docker-compose.local.yml`'s fleet layout, so update `seed.py` if that changes.
- **Sign-in mail is logged, not sent.** Using the form writes the link to
  `docker logs retina-local-server` without the `:8080`, so add the port by hand.
  The line is only there because the overlay sets `RADAR_API_KEY`. With it unset,
  an import-time warning fixes the root logger at WARNING and the INFO line never
  prints.
- **The session cookie is `Secure` over plain HTTP.** Chromium accepts it on
  `app.localhost`. No other browser has been checked.
- **Don't trust the toolbar's aircraft count.** The unfiltered map has read
  "0 aircraft" while `/api/radar/data/aircraft.json` carried seven, so judge by
  the API. The basemap is watermarked "API KEY REQUIRED" because Carto requires a
  key off its allowed domains, and nodes still draw over it.
- **One stack per machine.** `docker-compose.local.yml` pins the container names
  and port 8080. `up.sh` from another worktree replaces this stack, and a stack
  started any other way (ONBOARDING's command, for one) has to be stopped first.- **Docker Desktop's keychain can hang a build** at `resolve image config`.
  `up.sh` builds with a scratch `DOCKER_CONFIG` that has no credential helper
  whenever `~/.docker/config.json` names `credsStore: desktop`.
- **`node` may be off PATH** (keg-only Homebrew); call it by absolute path, such
  as `/usr/local/opt/node/bin/node`.

## Troubleshooting

- **The console is signed out after `up.sh`** (`/api/auth/me` answers 401): the
  browser has not redeemed a link (see the first gotcha). Mint one with the `link` command above and open it
  in that same browser.
