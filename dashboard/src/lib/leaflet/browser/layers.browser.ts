import { expect, test } from "@playwright/test";
import type { Scene } from "./harness";
import { BLUE, RED, openHarness, render, settersCalledBy, view } from "./scene";

const TILE_A = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
const TILE_B = "data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==";

test.beforeEach(async ({ page }) => openHarness(page));

test("a prop change calls only the matching Leaflet setter and keeps the layer", async ({ page }) => {
  const scene = (radius: number, position: number[], url: string) =>
    view(
      ["TileLayer", { url, ref: "tiles" }],
      ["Circle", { center: [10, 20], radius, pathOptions: "red", ref: "circle" }],
      ["Marker", { position, icon: "dot", ref: "marker" }],
    );
  const onMap = () =>
    page.evaluate(() => Object.values(window.lf.refs).map((r) => window.lf.map.hasLayer(r.current) && window.lf.L.stamp(r.current)));
  await render(page, scene(500, [10, 20], TILE_A));
  const before = await onMap();

  expect(await settersCalledBy(page, scene(900, [10, 20], TILE_A))).toEqual(["circle.setRadius"]);
  expect(await settersCalledBy(page, scene(900, [11, 21], TILE_A))).toEqual(["marker.setLatLng"]);
  expect(await settersCalledBy(page, scene(900, [11, 21], TILE_B))).toEqual(["tiles.setUrl"]);
  expect(await onMap()).toEqual(before);
});

test("pathOptions with the same reference does not call setStyle", async ({ page }) => {
  const scene = view(["Circle", { center: [10, 20], radius: 500, pathOptions: "red", ref: "circle" }]);
  await render(page, scene);

  expect(await settersCalledBy(page, scene)).toEqual([]);
});

test("className comes from its own prop, never from pathOptions, and survives a pathOptions change", async ({ page }) => {
  const scene = (style: string) =>
    view(["Circle", { center: [10, 20], radius: 500, className: "probe-ring", pathOptions: style, ref: "circle" }]);
  await render(page, scene("redClassed"));
  await expect(page.locator("path.probe-ring")).toHaveAttribute("stroke", RED);

  expect(await settersCalledBy(page, scene("blueClassed"))).toEqual(["circle.setStyle"]);
  await expect(page.locator("path.probe-ring")).toHaveAttribute("stroke", BLUE);
  expect(await page.evaluate(() => window.lf.refs.circle.current.options.className)).toBe("probe-ring");
  await expect(page.locator(".probe-dropped")).toHaveCount(0);
});

test("changing eventHandlers unbinds the old handlers", async ({ page }) => {
  const scene = (handlers: string) => view(["Marker", { position: [10, 20], icon: "dot", eventHandlers: handlers }]);
  await render(page, scene("a"));
  await page.locator(".probe-icon").click();
  await render(page, scene("b"));
  await page.locator(".probe-icon").click();

  expect(await page.evaluate(() => window.lf.hits)).toEqual(["a", "b"]);
});

test("unmounting removes the layer, and the map when MapContainer unmounts", async ({ page }) => {
  await render(page, view(["Circle", { center: [10, 20], radius: 500, className: "probe-ring", ref: "circle" }]));

  const after = await page.evaluate((withoutCircle) => {
    const { lf } = window;
    const circle = lf.refs.circle.current;
    lf.render(withoutCircle as Scene);
    const onMap = lf.map.hasLayer(circle);
    const paths = document.querySelectorAll("path.probe-ring").length;
    let unloads = 0;
    lf.map.on("unload", () => (unloads += 1));
    lf.render(null);
    return { onMap, paths, unloads };
  }, view() as unknown);
  expect(after).toEqual({ onMap: false, paths: 0, unloads: 1 });
});

test("a StrictMode double mount inside a page with effects leaves one map and one of each layer", async ({ page }) => {
  await render(
    page,
    [
      "Page",
      {},
      view(
        ["Circle", { center: [10, 20], radius: 500, className: "probe-ring" }],
        ["Marker", { position: [10, 20], icon: "dot" }, ["Popup", {}, "Hi"]],
      ),
    ],
    true,
  );

  const counts = await page.evaluate(() => {
    let layers = 0; // the SVG renderer Leaflet adds for the circle is not counted
    window.lf.map.eachLayer((layer) => (layers += layer instanceof window.lf.L.Renderer ? 0 : 1));
    const count = (selector: string) => document.querySelectorAll(selector).length;
    return { probeMounts: window.lf.mounts, mapPanes: count(".leaflet-map-pane"), layers, rings: count("path.probe-ring"), icons: count(".probe-icon") };
  });
  expect(counts).toEqual({ probeMounts: 2, mapPanes: 1, layers: 2, rings: 1, icons: 1 });
});

test("a Suspense boundary that hides the map keeps it live, and its layers come back to it", async ({ page }) => {
  const scene = (pending: boolean): Scene => [
    "Suspense",
    { fallback: null },
    view(["Circle", { center: [10, 20], radius: 500, className: "probe-ring", ref: "circle" }]),
    ["Suspender", { pending }],
  ];
  await render(page, scene(false));

  const seen = await page.evaluate(([hiding, showing]) => {
    const { lf } = window;
    const map = lf.map;
    lf.render(hiding as Scene);
    const hidden = map.getContainer().style.display === "none";
    const centerWhileHidden = map.getCenter();
    lf.render(showing as Scene);
    return { hidden, centerWhileHidden, circleOnMap: map.hasLayer(lf.refs.circle.current), rings: document.querySelectorAll("path.probe-ring").length };
  }, [scene(true), scene(false)] as unknown[]);
  expect(seen).toEqual({ hidden: true, centerWhileHidden: { lat: 10, lng: 20 }, circleOnMap: true, rings: 1 });
});

test("ref exposes the Leaflet instance", async ({ page }) => {
  await render(
    page,
    view(
      ["TileLayer", { url: TILE_A, ref: "tiles" }],
      ["Circle", { center: [10, 20], radius: 500, ref: "circle" }],
      ["Marker", { position: [10, 20], icon: "dot", ref: "marker" }, ["Popup", { ref: "popup" }, "Hi"]],
    ),
  );

  const instances = await page.evaluate(() => {
    const { L, map, refs } = window.lf;
    return {
      tiles: refs.tiles.current instanceof L.TileLayer && map.hasLayer(refs.tiles.current),
      circle: refs.circle.current instanceof L.Circle && map.hasLayer(refs.circle.current),
      marker: refs.marker.current instanceof L.Marker && map.hasLayer(refs.marker.current),
      popup: refs.popup.current instanceof L.Popup && refs.marker.current?.getPopup() === refs.popup.current,
      markerAt: refs.marker.current?.getLatLng() ?? null,
    };
  });
  expect(instances).toEqual({ tiles: true, circle: true, marker: true, popup: true, markerAt: { lat: 10, lng: 20 } });
});

test("Popup content renders when it first opens, sized for it, and changes in place without losing focus", async ({ page }) => {
  const scene = (width: number, label: string) =>
    view(["Marker", { position: [10, 20], icon: "dot" }, ["Popup", {}, ["div", { style: { width } }, ["button", { ref: "button" }, label]]]]);
  const content = page.locator(".leaflet-popup-content");
  const button = content.locator("button");
  await render(page, scene(120, "Show"));
  expect(await page.evaluate(() => window.lf.refs.button.current === null)).toBe(true);

  // The width Leaflet gave the popup as it opened; an empty one gets 51 px (its minWidth + 1).
  const openedAt = page.evaluate(
    () => new Promise((done) => window.lf.map.once("popupopen", () => done(document.querySelector<HTMLElement>(".leaflet-popup-content").offsetWidth))),
  );
  await page.locator(".probe-icon").click();
  expect(await openedAt).toBe(121);

  await button.focus();
  await render(page, scene(200, "Hide"));
  await expect(button).toHaveText("Hide");
  await expect(button).toBeFocused();
  expect(await content.evaluate((el: HTMLElement) => el.offsetWidth)).toBe(201);
});
