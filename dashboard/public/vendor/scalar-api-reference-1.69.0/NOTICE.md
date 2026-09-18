# @scalar/api-reference, vendored

`standalone.js` is `dist/browser/standalone.js` from `@scalar/api-reference` 1.69.0 (Scalar,
MIT, text in `LICENSE`, taken from the repository root since the package ships none), byte for
byte: <https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.69.0/dist/browser/standalone.js>,
SHA-256 `48289f8a965d73ae510c469462cac0d8f72865de705e186278d5485058b2a5fc`.

It is the same file the public reference at the api host's root loads from jsDelivr, pinned there
by its SRI hash (`SCALAR_URL` and `SCALAR_INTEGRITY` in `backend/routes/reference.py`). Upgrade
both together.

It is vendored rather than installed because the console's page CSP admits scripts from its own
origin only (`script-src 'self'`), and the package's dependency tree (Vue, CodeMirror and the
rest) is a large addition to the lockfile for one prebuilt file.

## How it is wired

`src/pages/admin/ApiDocsPage.tsx` adds it as a plain `<script>` the first time the page opens. It
defines `window.Scalar` (typed in `src/scalar.d.ts`) and injects its stylesheet through `<style>`
elements, which the page CSP allows (`style-src 'self' 'unsafe-inline'`).

It lives under `public/` so that Vite never processes it, in the dev server or in a build. The
file opens with a UMD header that calls `require()` when `exports` is defined: imported as a
module, a production build wraps it in a CommonJS shim and it throws on load, while the dev server
serves it without that shim and hides the failure.

nginx serves `.js` outside Vite's hashed `assets/` tree as `no-store` (`deploy/nginx/snippets/spa.conf`),
so the page downloads it afresh on each visit. The version in the directory name is what keeps an
upgrade from being served stale by anything that does cache it.

## Upgrading

Replace `standalone.js`, rename the directory and `SCALAR_BUNDLE` in `ApiDocsPage.tsx` to the new
version, update the URL and hash above, and update the pin in `backend/routes/reference.py` to the
same version and its new SRI hash.
