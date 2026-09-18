import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vendored CommonJS (see vendor/edsc-timeline/NOTICE.md). Vite converts CommonJS
// only inside node_modules unless told otherwise, in dev and build separately.
const EDSC_TIMELINE = fileURLToPath(new URL("./vendor/edsc-timeline/index.js", import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@edsc/timeline": EDSC_TIMELINE },
  },
  optimizeDeps: {
    include: ["@edsc/timeline"],
  },
  test: {
    environment: "jsdom",
    globals: true,
    // So `App.css?raw` reaches the theme tests as its text. Vitest stubs every
    // CSS import with an empty string by default, the raw query included.
    css: true,
    setupFiles: ["@testing-library/jest-dom/vitest"],
    alias: {
      "@edsc/timeline": fileURLToPath(new URL("./src/test/stubs/edscTimeline.tsx", import.meta.url)),
    },
  },
  build: {
    commonjsOptions: {
      include: [/node_modules/, /vendor\/edsc-timeline\//],
    },
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-charts": ["recharts"],
          "vendor-leaflet": ["leaflet", "react-leaflet"],
          "vendor-timeline": ["@edsc/timeline"],
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
