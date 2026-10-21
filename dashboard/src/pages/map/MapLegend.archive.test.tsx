import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import MapLegend from "./MapLegend";
import { MapThemeProvider } from "./useMapTheme";

const legend = (props: { hasArchive?: boolean; hasPlayback?: boolean }) => {
  const { container } = render(
    <MapThemeProvider>
      <MapLegend colorByAlt={false} showGroundTruth={false} showIlluminators={false} {...props} />
    </MapThemeProvider>,
  );
  return container.querySelector(".map-legend");
};

describe("MapLegend over the archive bar", () => {
  it("rides above the archive bar with hasArchive", () => {
    expect(legend({ hasArchive: true })).toHaveClass("with-archive");
  });

  it("is not also lifted for the playback bar when both are set", () => {
    const el = legend({ hasArchive: true, hasPlayback: true });
    expect(el).toHaveClass("with-archive");
    expect(el).not.toHaveClass("with-playback");
  });

  it("rides above the playback bar with only hasPlayback", () => {
    const el = legend({ hasPlayback: true });
    expect(el).toHaveClass("with-playback");
    expect(el).not.toHaveClass("with-archive");
  });

  it("is lifted for neither by default", () => {
    const el = legend({});
    expect(el).toHaveClass("map-legend");
    expect(el).not.toHaveClass("with-archive");
    expect(el).not.toHaveClass("with-playback");
  });
});
