import type { Theme } from "./protocol";

/** Shared chart palette; mirrors the web app's `--series-*` tokens. */
export const SERIES: Record<Theme, string[]> = {
  light: ["#2954ff", "#0068ad", "#12871c", "#cc4b00", "#b41880", "#832eea", "#646c72", "#0e8a6a"],
  dark: ["#85a3ff", "#7ab6ff", "#6ce07b", "#ff9e66", "#ed78c6", "#b794ff", "#a2a9b0", "#45e4b6"],
};

export const INK: Record<Theme, { text: string; muted: string; grid: string; domain: string }> = {
  light: { text: "#121619", muted: "#4d5358", grid: "#e7eaee", domain: "#c1c7cd" },
  dark: { text: "#f4f5f7", muted: "#a2a9b0", grid: "#2c3440", domain: "#3c4552" },
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
