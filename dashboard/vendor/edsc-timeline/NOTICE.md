# @edsc/timeline, vendored

`index.js` is `dist/index.js` from `@edsc/timeline` 1.1.9 (NASA Earthdata Search,
Apache-2.0, text in `LICENSE`), byte for byte:
<https://cdn.jsdelivr.net/npm/@edsc/timeline@1.1.9/dist/index.js>, SHA-256
`4110a38491bdcba3672b0f37b3c5f681026551a68f644c1ea48a41b3071c2665`.

It is vendored rather than installed because the package declares its development tooling
(react-icons, bootstrap, cypress-real-events and through it Cypress) as runtime dependencies,
which is about 170 packages and 130 MB for one file. The built bundle itself needs only `react`,
`react-dom`, `lodash` and `classnames`, which the root `package.json` lists as dependencies for
it.

## How it is wired

The bundle is CommonJS (`module.exports`, four `require()` calls). `vite.config.js` aliases
`@edsc/timeline` to this file and lists it in `optimizeDeps.include` and
`build.commonjsOptions.include`, which is what Vite needs to convert CommonJS that lives outside
`node_modules`. The types the dashboard relies on are declared in `src/edsc-timeline.d.ts`.

The bundle injects its own stylesheet through a `<style>` element at import time, which the page
CSP allows (`style-src 'self' 'unsafe-inline'`). The dashboard re-skins it onto its tokens in
`src/pages/user/dataExplorer/dataExplorer.css`.

## Upgrading

Replace `index.js`, update the version, URL and hash above, and re-check the behaviour
`AvailabilityTimeline.tsx` depends on. None of it is documented and it has changed between majors:

- zoom levels are 0-based, and only 1 (hour), 2 (day) and 3 (month) are useful here;
- it draws at most three rows, hardcoded (Earthdata Search's three-collection limit);
- a range is selected by dragging along the top strip, a day is focused by clicking its date
  label, and a drag in the middle pans;
- every callback reports `{center, zoom, temporalStart, temporalEnd, focusedStart, focusedEnd}`,
  and `center` and `zoom` must be passed back or each re-render snaps the view back to where it
  started.
