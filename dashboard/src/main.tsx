import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import ErrorBoundary from "./components/ErrorBoundary";
import { reloadOnStaleChunk } from "./utils/staleChunk";
import "@retina/shared/css/tokens.css";
import "@retina/shared/css/ui.css";
import "./App.css";
import "./styles/phone.css";
import "./styles/timeline.touch.css";

window.addEventListener("vite:preloadError", reloadOnStaleChunk);

// The browser bar wears the header's colour, and index.html carries it once
// per OS scheme. A theme chosen in the app outranks the OS, so while one is
// stamped both tags carry its colour; with none stamped (`system`) each takes
// its own back and the OS picks between them. That is also why an OS change
// needs no listener here: the tags' own media queries answer it.
const themeColors = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"][media]')];
const ownColor = new Map(themeColors.map((meta) => [meta, meta.content]));

function syncThemeColor() {
  const chosen = document.documentElement.getAttribute("data-theme");
  const source = themeColors.find((meta) => meta.getAttribute("media") === `(prefers-color-scheme: ${chosen})`);
  for (const meta of themeColors) meta.content = ownColor.get(source ?? meta);
}

// theme-boot.js may have stamped a choice already; ThemeContext stamps the
// later ones.
syncThemeColor();
new MutationObserver(syncThemeColor).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ["data-theme"],
});

// Outermost, so the theme outlives a crash in the tree below: the boundary's
// fallback is drawn with the same tokens as the app, and a dark console must
// not turn white to tell you something went wrong.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <ErrorBoundary>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ErrorBoundary>
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>
);
