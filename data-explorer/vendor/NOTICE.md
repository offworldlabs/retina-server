# Vendored third-party libraries

These files are committed rather than fetched from a CDN because the page vhost
serving this site sends `Content-Security-Policy: … script-src 'self' …`
(`deploy/nginx/snippets/security-headers-page.conf`). A `<script src>` pointing at
cdnjs, jsDelivr or unpkg is blocked outright, with no fallback, so a CDN build of
the timeline would leave the availability view permanently empty in every
deployed environment while working fine on a laptop.

All five are unmodified upstream distribution files, byte-for-byte.

| File | Package | Version | License | Source |
| --- | --- | --- | --- | --- |
| `react.production.min.js` | react (UMD, production) | 18.3.1 | MIT | https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js |
| `react-dom.production.min.js` | react-dom (UMD, production) | 18.3.1 | MIT | https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js |
| `lodash.min.js` | lodash | 4.17.21 | MIT | https://cdnjs.cloudflare.com/ajax/libs/lodash.js/4.17.21/lodash.min.js |
| `classnames.min.js` | classnames | 2.5.1 | MIT | https://cdnjs.cloudflare.com/ajax/libs/classnames/2.5.1/index.min.js |
| `edsc-timeline-1.1.9.js` | @edsc/timeline (NASA Earthdata Search) | 1.1.9 | Apache-2.0 | https://cdn.jsdelivr.net/npm/@edsc/timeline@1.1.9/dist/index.js |

`edsc-timeline-LICENSE` is the Apache-2.0 text shipped with `@edsc/timeline`.
React, react-dom, lodash and classnames are MIT; their license text is carried in
the banner comment of each minified file.

## How the timeline bundle is loaded

`@edsc/timeline@1.1.9` ships as a **CommonJS** bundle: it ends `module.exports=n`
and calls `require()` for exactly four externals — `react`, `react-dom`,
`lodash`, `classnames`. jsDelivr's ESM conversion of it does not parse, and this
site has no bundler, so `index.html` loads the four UMD globals first and then
defines a five-line shim before the bundle:

```html
<script>
var module = { exports: {} };
function require(n) {
  return { react: window.React, "react-dom": window.ReactDOM, lodash: window._, classnames: window.classNames }[n];
}
</script>
<script src="vendor/edsc-timeline-1.1.9.js"></script>
```

The component is then `module.exports.default || module.exports`.

## Upgrading

Replace the file, update the version and URL in the table above, and re-check the
component facts `app.js` depends on — they are behavioural, not documented, and
changed between majors:

- zoom levels are 0-based, and only 1 (hour), 2 (day) and 3 (month) are useful here;
- it draws **at most 3 rows**, hardcoded (Earthdata Search's "3 collections" limit);
- a range is selected by dragging along the top ~20 px strip; a day is focused by
  clicking a bottom date label; a middle drag pans;
- every callback returns `{center, zoom, temporalStart, temporalEnd, focusedStart,
  focusedEnd, …}`, and `center`/`zoom` **must** be persisted and passed back or
  each re-render snaps the view back to where it started;
- `.edsc-timeline-primary-section` must never get an opaque background: it spans
  the row list and would hide every interval bar.
