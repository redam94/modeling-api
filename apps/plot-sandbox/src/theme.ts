import type { Theme } from "./protocol";

/** Shared chart palette; mirrors the web app's `--series-*` tokens. */
export const SERIES: Record<Theme, string[]> = {
  light: ["#0072b2", "#d55e00", "#00865f", "#c8102e", "#a23aa8", "#1d93a5", "#8f5600", "#6e6e73"],
  dark: ["#56b4e9", "#ff9a52", "#34d399", "#ff6b6b", "#f58fc8", "#5fe8f2", "#ffc929", "#8e8e93"],
};

/** Mirrors `--foreground`, `--muted-foreground`, `--chart-grid` and `--chart-base`. */
export const INK: Record<Theme, { text: string; muted: string; grid: string; domain: string }> = {
  light: { text: "#1d1d1f", muted: "#68686d", grid: "#e8e8ed", domain: "#d2d2d7" },
  dark: { text: "#f5f5f7", muted: "#a1a1a6", grid: "#2a2a2c", domain: "#424245" },
};

// The app's text stack. The sandbox loads no web font of its own: the system face draws the
// plot, and Inter only shows where the viewer has it installed.
const FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Inter Variable', Inter, 'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif";

/** Vega-Lite config merged under the spec's own config. */
export function vegaConfig(theme: Theme): Record<string, unknown> {
  const ink = INK[theme];
  return {
    background: null,
    font: FONT,
    padding: 8,
    view: { stroke: null },
    axis: {
      labelColor: ink.muted,
      titleColor: ink.text,
      gridColor: ink.grid,
      domainColor: ink.domain,
      tickColor: ink.domain,
      labelFontSize: 11,
      titleFontSize: 12,
      titleFontWeight: 600,
    },
    legend: { labelColor: ink.muted, titleColor: ink.text, labelFontSize: 11, titleFontSize: 12 },
    header: { labelColor: ink.text, titleColor: ink.text },
    title: { color: ink.text, subtitleColor: ink.muted, fontSize: 14, anchor: "start" },
    range: {
      category: SERIES[theme],
      heatmap: theme === "dark" ? { scheme: "magma" } : { scheme: "blues" },
    },
    mark: { color: SERIES[theme][0] },
    // Vega-Lite colours these black by default, whatever `mark.color` says: on the dark card
    // that hid interval lines, split lines and their labels. A tick (a median, a strip) is
    // drawn in ink so it stands apart from the series-coloured bar it often sits on.
    rule: { color: ink.muted },
    text: { color: ink.text },
    tick: { color: ink.text },
    style: { "guide-label": { fill: ink.muted }, "guide-title": { fill: ink.text } },
  };
}

/** Plotly layout defaults (the spec's own layout wins). */
export function plotlyLayout(theme: Theme): Record<string, unknown> {
  const ink = INK[theme];
  const axis = { gridcolor: ink.grid, linecolor: ink.domain, zerolinecolor: ink.domain, color: ink.muted };
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: FONT, color: ink.text, size: 12 },
    colorway: SERIES[theme],
    margin: { l: 56, r: 16, t: 32, b: 48 },
    xaxis: axis,
    yaxis: axis,
    legend: { font: { color: ink.muted } },
  };
}

/** Defaults for a polar subplot (`layout.polar`, `layout.polar2`, ...). */
export function plotlyPolar(theme: Theme): Record<string, unknown> {
  const ink = INK[theme];
  const axis = { gridcolor: ink.grid, linecolor: ink.domain, color: ink.muted };
  return { bgcolor: "rgba(0,0,0,0)", angularaxis: axis, radialaxis: axis };
}
