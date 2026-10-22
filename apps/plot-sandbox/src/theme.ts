import type { Theme } from "./protocol";

/** Shared chart palette; mirrors the web app's `--series-*` tokens. */
export const SERIES: Record<Theme, string[]> = {
  light: ["#3a2bd6", "#e0452b", "#0a8768", "#b56a00", "#c0287f", "#0a7fb5", "#5f7f00", "#6e6a8a"],
  dark: ["#aaa3ff", "#ff8a70", "#3fd6aa", "#ffb84d", "#ff7ac0", "#5cc4f5", "#c8ef4f", "#aeabc8"],
};

/** Mirrors `--foreground`, `--muted-foreground`, `--chart-grid` and `--chart-base`. */
export const INK: Record<Theme, { text: string; muted: string; grid: string; domain: string }> = {
  light: { text: "#18152f", muted: "#48455f", grid: "#e0deec", domain: "#c3c0d5" },
  dark: { text: "#f2f1fa", muted: "#b6b3d2", grid: "#2e2866", domain: "#433c86" },
};

// The sandbox loads no web font of its own: Inter only shows where the viewer has it installed.
const FONT = "'Inter Variable', Inter, ui-sans-serif, system-ui, sans-serif";

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
