/* Stamps the theme attribute before the first paint, so someone who chose dark
   is not shown a white page while the deferred app.js loads.

   Its own file rather than an inline <script> for the same reason the libraries
   under vendor/ are vendored: this vhost sends `script-src 'self'`, which does
   not cover inline code, and a laptop serving these files sends no CSP at all —
   so an inline version works in every local check and silently never runs once
   deployed.

   Only an explicit choice is stamped: `system` is the default, and app.css's own
   prefers-color-scheme block answers it with no flash to avoid. The key and the
   attribute are the names the dashboard console uses too; app.js has the other
   half of this, and the reasoning. */
try {
  var t = localStorage.getItem("retina.theme");
  if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
} catch (e) {
  /* private browsing — fall through to the OS preference */
}
