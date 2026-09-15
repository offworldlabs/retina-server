/* Stamps the theme attribute before the first paint, so someone who chose dark
   is not shown a white page while the bundle loads.

   Its own file rather than an inline <script> because every page vhost sends
   `script-src 'self'` (deploy/nginx/snippets/security-headers-page.conf), which
   does not cover inline code — and the dev server sends no CSP at all, so an
   inline version works in every local check and silently never runs once
   deployed. That is the same trap data-explorer/ vendors its libraries to avoid.

   Only an explicit choice is stamped: `system` is the default, and App.css's own
   prefers-color-scheme block answers it with no flash to avoid. The key and the
   attribute are ThemeContext.tsx's; this file has to agree with it. */
try {
  var t = localStorage.getItem("retina.theme");
  if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
} catch (e) {
  /* private browsing — fall through to the OS preference */
}
