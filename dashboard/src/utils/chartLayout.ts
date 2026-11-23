import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";
import type { LegendProps, XAxisProps, YAxisProps } from "recharts";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { COARSE_TABLET, PHONE, TABLET } from "../styles/breakpoints";

/**
 * Chart layout for phones and tablets.
 *
 * Recharts lays a chart out from its props and knows nothing of the screen: an
 * axis keeps every label at the angle it was given however narrow the chart
 * gets, and it has no responsive tick setting to ask for. So a chart states its
 * desktop props once, as a ChartFrame, and this module works out the narrow
 * version from the width the chart actually has. From 1024px up the frame comes
 * back as the very object passed in, so a desktop chart draws as it always did.
 *
 *   const { ref, frame } = useChartLayout(TRUST_FRAME, { kind: "bar", categories: rows.length, longestLabel });
 *   <div className="chart-container" ref={ref} style={{ height: frame.height }}>
 *     <ResponsiveContainer width="100%" height="100%"><BarChart data={rows} layout={frame.layout} margin={frame.margin}>
 *       <XAxis {...frame.xAxis} stroke={chart.axis} /><YAxis {...frame.yAxis} stroke={chart.axis} />
 *       {frame.legend && <Legend {...frame.legend} />}
 *
 * Keep the desktop frame at module scope, so it is one object across renders.
 * Everything that belongs to an axis's role goes in its frame, tickFormatter
 * included: when bars turn on their side the category axis moves from X to Y,
 * and only what is in the frame moves with it. Colours stay on the element.
 */

/** The axis props this module reads or sets, typed so a frame spreads onto
 *  Recharts' XAxis or YAxis as it is. */
export interface AxisFrame {
  dataKey?: XAxisProps["dataKey"];
  type?: "number" | "category";
  hide?: boolean;
  /** Only the size is read: label widths are estimated from it. */
  tick?: { fontSize?: number };
  tickFormatter?: XAxisProps["tickFormatter"];
  ticks?: XAxisProps["ticks"];
  scale?: XAxisProps["scale"];
  domain?: YAxisProps["domain"];
  allowDecimals?: boolean;
  interval?: XAxisProps["interval"];
  minTickGap?: number;
  angle?: number;
  textAnchor?: "start" | "middle" | "end";
  height?: number;
  width?: number;
}

/** The Legend props this module moves. Content props such as `formatter` stay
 *  on the element. */
export type LegendFrame = Pick<LegendProps, "layout" | "align" | "verticalAlign" | "iconSize" | "wrapperStyle">;

export interface ChartMargin {
  top?: number;
  right?: number;
  bottom?: number;
  left?: number;
}

/** A chart's layout props. `height` is the wrapper's height in px, padding
 *  included, as the stylesheet sets it (250 for .chart-container); the page
 *  applies it to the wrapper so a chart that grows takes its card with it. */
export interface ChartFrame {
  layout?: "horizontal" | "vertical";
  margin?: ChartMargin;
  height: number;
  xAxis: AxisFrame;
  yAxis: AxisFrame;
  /** Omitted or null for a chart without one. */
  legend?: LegendFrame | null;
}

export type ChartKind = "bar" | "line" | "area" | "pie";

/** What the layout needs to know about the data. */
export interface ChartShape {
  kind: ChartKind;
  /** Bars, points or slices along the category axis. */
  categories: number;
  /** Characters in the longest category label as drawn, after any
   *  tickFormatter. Eight, a clock time with seconds, when omitted. */
  longestLabel?: number;
}

/** Where the chart is drawn. */
export interface ChartPlacement extends ChartShape {
  /** A desktop screen: the desktop frame is returned untouched. */
  desktop: boolean;
  /** The wrapper's content width in px. 0 is a chart not laid out yet. */
  width: number;
  /** The wrapper's vertical padding and borders, which `height` includes and
   *  the chart does not get. 0 when omitted. */
  inset?: number;
}

// Labels are estimated rather than measured, because the layout has to be
// known before Recharts draws. 0.6 of the font size is the average advance of
// digits and capitals in the console's system sans; lower case runs nearer
// 0.5, so the estimate errs wide and a label it says fits does fit.
const CHAR_EM = 0.6;
const DEFAULT_LABEL = 8;

// Recharts' own: the gap it keeps between ticks, the 6px tick plus 2px margin
// between an axis line and its labels, and its default axis and margin sizes.
const TICK_GAP = 5;
const TICK_SPACE = 8;
const AXIS_HEIGHT = 30;
const AXIS_WIDTH = 60;
const RECHARTS_MARGIN = 5;
const LEGEND_ICON = 14;
const LEGEND_ICON_GAP = 4;
const LEGEND_ITEM_GAP = 10;

// Text Recharts draws without a size inherits the page's, which the console
// leaves at the browser's 16px and a body line height of 1.5.
const INHERITED_FONT = 16;
const BODY_LINE = 1.5;

// Narrow charts keep Recharts' 5px above and to the right, where a line's
// active dot (radius 4, stroke 2) overhangs the plot, and drop it on the left
// and bottom, where the axes already hold the labels.
const COMPACT_MARGIN = { top: 5, right: 5, bottom: 0, left: 0 };

// Slanted labels sit at 45°, where a label's footprint along the axis and its
// drop below it are both its length times sin 45°.
const ROTATION = -45;
const SLANT = Math.SQRT1_2;

/** The widest container that counts as a phone, read from the query the
 *  stylesheets switch on so the two cannot drift. */
const PHONE_WIDTH = parseFloat(PHONE.replace(/^\D+/, ""));

// A bar on its side gets a 28px row: over the 24px minimum touch target in
// WCAG 2.5.8, since on a phone the row is what a finger taps for its tooltip,
// and tall enough for a label line at the console's 9 to 12px tick sizes.
const ROW_HEIGHT = 28;

// A sideways chart's label column takes at most this share of its width, so
// the bars keep the rest.
const CATEGORY_SHARE = 0.4;

// A slanted axis deeper than this share of the chart leaves the bars no room;
// on a phone the bars turn on their side instead.
const ROTATED_SHARE = 0.4;

/**
 * The frame to draw a chart with, given its desktop frame and where it is.
 *
 * - Desktop, or a width of 0: `desktop` itself, untouched.
 * - A chart already drawn sideways keeps its layout; only its label column is
 *   capped at 40% of the width.
 * - Bars: labels lie flat if each fits its bar's width with a 5px gap, and
 *   slant at -45° if every one fits that way. Slanted labels need
 *   (font size + 5) / sin 45° of axis each, the axis drops by their length
 *   times sin 45° plus the tick, and the left margin grows if the first one
 *   would hang past the Y axis. When even slanted labels would have to be
 *   thinned, or would take over 40% of the chart's height, a phone-width chart
 *   (640px or less) turns its bars on their side: a ranking of nodes is
 *   unreadable with every other name missing, and on its side each gets a row.
 *   A wider chart keeps them upright and labels every nth bar.
 * - Bars on their side: the categories move to Y, sized to the longest label
 *   and capped at 40% of the width; the value axis moves to X with its domain;
 *   the height becomes 28px a row plus the axis, legend, margins and inset, or
 *   the desktop height if that is more.
 * - Lines and areas: Recharts thins the time axis itself, keeping the first and
 *   last label and a label's width of space between the rest.
 * - Pies: a legend on the right moves underneath, and the frame grows by the
 *   legend's estimated height so the pie keeps its size.
 * - Every narrow chart's legend sits underneath, centred, in rows.
 * - A hidden axis stays hidden and gets no label maths.
 */
export function chartLayout(desktop: ChartFrame, at: ChartPlacement): ChartFrame {
  if (at.desktop || !(at.width > 0)) return desktop;

  if (desktop.layout === "vertical") {
    const width = Math.min(desktop.yAxis.width ?? AXIS_WIDTH, Math.floor(at.width * CATEGORY_SHARE));
    return { ...desktop, yAxis: { ...desktop.yAxis, width } };
  }

  const legend = legendBelow(desktop.legend);
  // A side legend took width; underneath it takes height, which the frame adds
  // so the plot keeps what it had.
  const height = desktop.height + (legend && isSideLegend(desktop.legend) ? legendHeight(legend, at, desktop.margin) : 0);
  if (at.kind === "pie") return { ...desktop, legend, height };

  const margin = compactMargin(desktop.margin);
  const x = desktop.xAxis;
  if (x.hide) return { ...desktop, margin, legend, height };

  const fontSize = x.tick?.fontSize ?? INHERITED_FONT;
  const label = (at.longestLabel ?? DEFAULT_LABEL) * fontSize * CHAR_EM;

  if (at.kind !== "bar" || x.type === "number") {
    // Recharts measures these labels and drops the ones that collide; the gap
    // keeps one label's width clear between those it shows, so a phone's time
    // axis reads as a few times rather than a solid strip.
    const xAxis: AxisFrame = { ...x, interval: "preserveStartEnd", minTickGap: Math.ceil(label) };
    return { ...desktop, margin, legend, height, xAxis };
  }

  const yWidth = desktop.yAxis.hide ? 0 : (desktop.yAxis.width ?? AXIS_WIDTH);
  const plot = at.width - margin.left - margin.right - yWidth;
  if (label + TICK_GAP <= plot / at.categories) {
    const xAxis: AxisFrame = { ...x, interval: 0, angle: 0, textAnchor: "middle", height: AXIS_HEIGHT };
    return { ...desktop, margin, legend, height, xAxis };
  }

  // The first slanted label hangs left of its tick by its length times
  // cos 45°; the Y axis column covers part of that and the margin the rest.
  const left = Math.max(margin.left, Math.ceil(label * SLANT - yWidth));
  const band = (plot - (left - margin.left)) / at.categories;
  const pitch = (fontSize + TICK_GAP) / SLANT;
  const every = band > 0 ? Math.max(0, Math.ceil(pitch / band) - 1) : at.categories;
  const drop = Math.ceil(TICK_SPACE + SLANT * (label + fontSize));

  if (at.width <= PHONE_WIDTH && (every > 0 || drop > desktop.height * ROTATED_SHARE)) {
    return sideways(desktop, at, margin, legend, label);
  }
  const xAxis: AxisFrame = { ...x, interval: every, angle: ROTATION, textAnchor: "end", height: drop };
  return { ...desktop, margin: { ...margin, left }, legend, height, xAxis };
}

/** Bars on their side: one row per category, labels in a column on the left. */
function sideways(
  desktop: ChartFrame,
  at: ChartPlacement,
  margin: Required<ChartMargin>,
  legend: LegendFrame | null | undefined,
  label: number,
): ChartFrame {
  const yAxis: AxisFrame = {
    ...without(desktop.xAxis, ["height", "angle", "textAnchor", "interval", "minTickGap", "width"]),
    type: "category",
    interval: 0,
    width: Math.min(Math.ceil(label + TICK_SPACE), Math.floor(at.width * CATEGORY_SHARE)),
  };
  const xAxis: AxisFrame = { ...without(desktop.yAxis, ["width"]), type: "number" };
  const chrome =
    (at.inset ?? 0) +
    margin.top +
    margin.bottom +
    (xAxis.hide ? 0 : AXIS_HEIGHT) +
    (legend ? legendRow(legend) : 0);
  const height = Math.max(desktop.height, Math.ceil(chrome + at.categories * ROW_HEIGHT));
  return { ...desktop, layout: "vertical", margin, height, xAxis, yAxis, legend };
}

function compactMargin(margin: ChartMargin = {}): Required<ChartMargin> {
  return {
    top: Math.min(margin.top ?? RECHARTS_MARGIN, COMPACT_MARGIN.top),
    right: Math.min(margin.right ?? RECHARTS_MARGIN, COMPACT_MARGIN.right),
    bottom: Math.min(margin.bottom ?? RECHARTS_MARGIN, COMPACT_MARGIN.bottom),
    left: Math.min(margin.left ?? RECHARTS_MARGIN, COMPACT_MARGIN.left),
  };
}

function legendBelow(legend: LegendFrame | null | undefined): LegendFrame | null | undefined {
  return legend && { ...legend, layout: "horizontal", align: "center", verticalAlign: "bottom" };
}

/** A legend in a column, or centred on the side, took the chart's width. */
function isSideLegend(legend: LegendFrame | null | undefined): boolean {
  return !!legend && (legend.layout === "vertical" || legend.verticalAlign === "middle");
}

/** The height of a legend underneath. A pie's legend names its slices, so it
 *  wraps into rows at the width; a cartesian chart's names its series, which
 *  in the console fit one row. */
function legendHeight(legend: LegendFrame, at: ChartPlacement, margin: ChartMargin = {}): number {
  if (at.kind !== "pie") return legendRow(legend);
  const style = legend.wrapperStyle ?? {};
  const fontSize = cssPx(style.fontSize) ?? INHERITED_FONT;
  // Each entry is Recharts' icon, 4px, the text, and 10px before the next.
  const icon = legend.iconSize ?? LEGEND_ICON;
  const item = icon + LEGEND_ICON_GAP + (at.longestLabel ?? DEFAULT_LABEL) * fontSize * CHAR_EM + LEGEND_ITEM_GAP;
  const room = at.width - (margin.left ?? RECHARTS_MARGIN) - (margin.right ?? RECHARTS_MARGIN);
  const perRow = Math.max(1, Math.floor(room / item));
  return Math.ceil(at.categories / perRow) * legendRow(legend);
}

/** One row of legend entries: the line box or the icon, whichever is taller. */
function legendRow(legend: LegendFrame): number {
  const style = legend.wrapperStyle ?? {};
  const fontSize = cssPx(style.fontSize) ?? INHERITED_FONT;
  return Math.max(lineHeight(style.lineHeight, fontSize), legend.iconSize ?? LEGEND_ICON);
}

/** A CSS length as px, for a number or a "12px" string. */
function cssPx(value: CSSProperties["fontSize"]): number | undefined {
  if (typeof value === "number") return value;
  if (typeof value === "string" && /^\s*[\d.]+px\s*$/.test(value)) return parseFloat(value);
  return undefined;
}

/** A line height in px. A bare number multiplies the font size, as in CSS. */
function lineHeight(value: CSSProperties["lineHeight"], fontSize: number): number {
  const px = typeof value === "string" ? cssPx(value) : undefined;
  if (px !== undefined) return px;
  const factor = typeof value === "number" ? value : parseFloat(String(value));
  return (Number.isFinite(factor) ? factor : BODY_LINE) * fontSize;
}

function without<T extends object, K extends keyof T>(source: T, keys: readonly K[]): Omit<T, K> {
  const rest = { ...source };
  for (const key of keys) delete rest[key];
  return rest;
}

type Box = { width: number; inset: number };

function measure(node: HTMLElement): Box {
  const style = getComputedStyle(node);
  const px = (value: string) => parseFloat(value) || 0;
  return {
    width: Math.max(0, node.clientWidth - px(style.paddingLeft) - px(style.paddingRight)),
    inset: node.offsetHeight - node.clientHeight + px(style.paddingTop) + px(style.paddingBottom),
  };
}

/**
 * chartLayout for a chart on screen. Attach `ref` to the chart's wrapper, the
 * sized element around its ResponsiveContainer. The viewport decides desktop:
 * from 1024px up the frame is `desktop` itself whatever the wrapper's width, and
 * the wrapper is not observed. Below that the wrapper's width is read when it
 * mounts, before the first paint, and again whenever a ResizeObserver reports
 * a change. Without ResizeObserver (jsdom) it is read once, as 0 there.
 */
export function useChartLayout(
  desktop: ChartFrame,
  opts: ChartShape,
): { ref: (node: HTMLElement | null) => void; frame: ChartFrame } {
  const narrow = useMediaQuery(TABLET);
  const [box, setBox] = useState<Box>({ width: 0, inset: 0 });
  const observer = useRef<ResizeObserver | null>(null);

  // A new callback when the viewport crosses 1024px, so React detaches the old
  // one, which stops observing, and attaches this one to the same element.
  const ref = useCallback(
    (node: HTMLElement | null) => {
      observer.current?.disconnect();
      observer.current = null;
      if (!node || !narrow) return;
      const update = () => {
        const next = measure(node);
        setBox((prev) => (prev.width === next.width && prev.inset === next.inset ? prev : next));
      };
      update();
      if (typeof ResizeObserver !== "function") return;
      observer.current = new ResizeObserver(update);
      observer.current.observe(node);
    },
    [narrow],
  );

  const { kind, categories, longestLabel } = opts;
  const frame = useMemo(
    () => chartLayout(desktop, { desktop: !narrow, width: box.width, inset: box.inset, kind, categories, longestLabel }),
    [desktop, narrow, box, kind, categories, longestLabel],
  );
  return { ref, frame };
}

/** Props for a chart whose tooltip closes when a touch lands outside it. */
export interface TapOutTooltip {
  /** For the chart's wrapper element. */
  wrapper: { "data-tooltip-scope"?: string };
  /** For the chart itself (BarChart, LineChart, PieChart …). */
  chart: { onMouseMove?: () => void };
  /** For its Tooltip. */
  tooltip: { active?: boolean };
}

const NO_TAP_OUT: TapOutTooltip = { wrapper: {}, chart: {}, tooltip: {} };

/**
 * A tooltip that a tap outside the chart closes, on touch screens below 1024px.
 *
 *   const tip = useTapOutTooltip();
 *   <div className="chart-container" {...tip.wrapper}> … <BarChart data={rows} {...tip.chart}>
 *     <Tooltip {...tip.tooltip} contentStyle={chart.tooltip} cursor={chart.cursor} />
 *
 * Recharts 2 shows the tooltip while its own pointer state says to, unless the
 * Tooltip's `active` prop overrides that: true pins it open, false keeps it
 * shut, and leaving it out hands it back to the pointer. A tap sets that state
 * through the browser's emulated mouse events, and nothing unsets it until a
 * mouse event lands elsewhere in the same chart, so on a phone a tooltip stays
 * up after the finger has gone. A pointerdown anywhere outside the wrapper
 * therefore sets `active` false. It is lifted when Recharts next reports the
 * pointer moving in the chart, a later tap or a drag, which is after it has
 * moved its state to the new spot, so the old tooltip never flashes back.
 *
 * With a fine pointer, or from 1024px up, every prop set is empty and nothing
 * listens: hover works as it always has.
 */
export function useTapOutTooltip(): TapOutTooltip {
  const touch = useMediaQuery(COARSE_TABLET);
  const scope = useId();
  const [closed, setClosed] = useState(false);

  useEffect(() => {
    if (!touch) return;
    const inside = `[data-tooltip-scope="${scope}"]`;
    const close = (event: PointerEvent) => {
      if (!(event.target instanceof Element && event.target.closest(inside))) setClosed(true);
    };
    // Capture, so a control that stops the event on its way up still closes it.
    document.addEventListener("pointerdown", close, true);
    return () => document.removeEventListener("pointerdown", close, true);
  }, [touch, scope]);

  const reopen = useCallback(() => setClosed(false), []);
  return useMemo(
    () =>
      touch
        ? {
            wrapper: { "data-tooltip-scope": scope },
            chart: { onMouseMove: reopen },
            tooltip: closed ? { active: false } : {},
          }
        : NO_TAP_OUT,
    [touch, scope, closed, reopen],
  );
}
