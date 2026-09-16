import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import ErrorBoundary from "./components/ErrorBoundary";
import { ROUTER_BASENAME } from "./utils/basePath";
import "./App.css";

// Outermost, so the theme outlives a crash in the tree below: the boundary's
// fallback is drawn with the same tokens as the app, and a dark console must
// not turn white to tell you something went wrong.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter basename={ROUTER_BASENAME}>
        <ErrorBoundary>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ErrorBoundary>
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>
);
