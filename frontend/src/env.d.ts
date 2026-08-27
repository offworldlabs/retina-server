/// <reference types="vite/client" />

declare module "*.css" {}

interface ImportMetaEnv {
  readonly VITE_ENABLE_TEST_RADAR?: string;
  /**
   * CARTO basemap key, baked into the bundle at build time. Optional: unset
   * means the anonymous tile endpoints, which is what shipped before this
   * existed. See utils/basemap.ts for why a bundled key is the supported
   * shape for this one.
   */
  readonly VITE_CARTO_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/* Leaflet internal icon URL fix */
declare module "leaflet" {
  namespace Icon {
    interface Default {
      _getIconUrl?: string;
    }
  }
}

/* Allow CSS custom properties in style objects */
import "react";
declare module "react" {
  interface CSSProperties {
    [key: `--${string}`]: string | number | undefined;
  }
}
