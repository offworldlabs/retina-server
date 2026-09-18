declare module "*.css" {}

declare global {
  interface ImportMetaEnv {
    /**
     * CARTO basemap key, baked into the bundle at build time. Unset means
     * unkeyed tile URLs, which CARTO serves stamped "API KEY REQUIRED". See
     * pages/map/utils/basemap.ts for why a bundled key is the supported shape
     * for this one.
     */
    readonly VITE_CARTO_API_KEY?: string;
    /**
     * Ships the radar sandbox (`/test-radar`) into a production build when
     * set to "1". Dev builds always carry it; see App.tsx's TEST_RADAR_ENABLED.
     */
    readonly VITE_ENABLE_TEST_RADAR?: string;
  }
}

/* Allow CSS custom properties in style objects */
import "react";
declare module "react" {
  interface CSSProperties {
    [key: `--${string}`]: string | number | undefined;
  }
}
