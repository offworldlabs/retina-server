import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NearControls } from "../../pages/user/dataExplorer/NearControls";
import {
  DEFAULT_RADIUS_KM,
  defaultFilters,
  type ExplorerFilters,
  MIN_RADIUS_KM,
} from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

function setup(over: Partial<ExplorerFilters> = {}) {
  const onChange = vi.fn();
  const onToggleMap = vi.fn();
  const onRadiusChange = vi.fn();
  const view = render(
    <NearControls
      filters={{ ...defaultFilters(TODAY), ...over }}
      radiusKm={over.near?.km ?? DEFAULT_RADIUS_KM}
      onRadiusChange={onRadiusChange}
      mapOpen={false}
      onToggleMap={onToggleMap}
      onChange={onChange}
    />,
  );
  return { onChange, onToggleMap, onRadiusChange, view };
}

const nearFrom = (onChange: ReturnType<typeof vi.fn>) => {
  const { calls } = onChange.mock;
  return (calls[calls.length - 1][0] as ExplorerFilters).near;
};

const lat = () => screen.getByLabelText("centre latitude");
const lon = () => screen.getByLabelText("centre longitude");
const km = () => screen.getByLabelText("radius in km");

/** The fields commit on blur, as a native change event would. */
const type = (field: HTMLElement, value: string) => {
  fireEvent.change(field, { target: { value } });
  fireEvent.blur(field);
};

describe("NearControls", () => {
  it("shows the centre the filter is set to", () => {
    setup({ near: { lat: 51.5074, lon: -0.1278, km: 25 } });
    expect(lat()).toHaveValue(51.5074);
    expect(lon()).toHaveValue(-0.1278);
    expect(km()).toHaveValue(25);
  });

  it("takes a centre typed in rather than clicked", () => {
    const { onChange } = setup();
    fireEvent.change(lat(), { target: { value: "51.5" } });
    type(lon(), "-0.12");
    expect(nearFrom(onChange)).toEqual({ lat: 51.5, lon: -0.12, km: DEFAULT_RADIUS_KM });
  });

  it("waits for the field to be left, so half a coordinate is not a centre", () => {
    const { onChange } = setup();
    fireEvent.change(lat(), { target: { value: "5" } });
    expect(onChange).not.toHaveBeenCalled();
  });

  it("takes the filter off when the coordinates are emptied", () => {
    const { onChange } = setup({ near: { lat: 51.5, lon: -0.1, km: 25 } });
    type(lat(), "");
    expect(nearFrom(onChange)).toBeNull();
  });

  it("holds a lone coordinate rather than centring on the equator", () => {
    const { onChange } = setup();
    type(lat(), "51.5");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("floors the radius at the minimum", () => {
    const { onChange } = setup({ near: { lat: 51.5, lon: -0.1, km: 25 } });
    type(km(), "0.5");
    expect(nearFrom(onChange)!.km).toBe(MIN_RADIUS_KM);
  });

  it("does not rewrite the query string when nothing was edited", () => {
    const { onChange } = setup({ near: { lat: 51.5, lon: -0.1, km: 25 } });
    fireEvent.blur(lat());
    expect(onChange).not.toHaveBeenCalled();
  });

  it("takes a centre placed on the map into its own fields", () => {
    const { view } = setup({ near: { lat: 51.5, lon: -0.1, km: 25 } });
    view.rerender(
      <NearControls
        filters={{ ...defaultFilters(TODAY), near: { lat: 52.2, lon: 0.13, km: 25 } }}
        radiusKm={25}
        onRadiusChange={vi.fn()}
        mapOpen={false}
        onToggleMap={vi.fn()}
        onChange={vi.fn()}
      />,
    );
    expect(lat()).toHaveValue(52.2);
  });

  it("holds a radius chosen before there is a centre to measure from", () => {
    const { onChange, onRadiusChange } = setup();
    type(km(), "12");
    expect(onRadiusChange).toHaveBeenCalledWith(12);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("names the map button for what pressing it will do", () => {
    const { onToggleMap } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Map" }));
    expect(onToggleMap).toHaveBeenCalled();
  });
});
