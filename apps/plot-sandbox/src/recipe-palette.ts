/**
 * Dark-mode variants of the platform recipe palette (`mapi_plots.palette`).
 *
 * Vega-Lite applies an explicit mark colour over any theme config, so the recipes draw with a
 * fixed light palette and the sandbox swaps each of those colours for its dark variant when it
 * renders in dark mode. Plotly recipes (`sci.*`) use the same palette in trace colours, so
 * their specs get the same swap. A Python test (plots-core `test_the_plot_sandbox_mirrors_the_dark_variants`)
 * keeps this table equal to `mapi_plots.palette.dark_variants()`.
 */
import type { JsonValue, Theme } from "./protocol";

/** `{light: dark}`; keys and values are lowercase `#rrggbb`. */
export const DARK_VARIANTS: Readonly<Record<string, string>> = {
  "#bfdcec": "#0072b2", // band
  "#0072b2": "#56b4e9", // estimate
  "#1d1d1f": "#f5f5f7", // observed
  "#c00f2c": "#ff6b6b", // alert
  "#00704e": "#34d399", // ok
  "#86868b": "#86868b", // reference
  "#68686d": "#a1a1a6", // muted
  "#424245": "#d2d2d7", // strong
  "#6e6e73": "#8e8e93", // baseline
  "#8f5600": "#ffc929", // scenario
  "#1d93a5": "#5fe8f2", // positive
  "#e8e8ed": "#2a2a2c", // empty
  "#ffffff": "#161617", // surface
};

/** Fills that need no text-level contrast (empty cells, separators in the card colour). */
export const NON_TEXT_FILLS: ReadonlySet<string> = new Set(["#e8e8ed", "#ffffff"]);

/** Data and inline datasets are values, not styling: never rewritten (Vega-Lite). */
const DATA_KEYS: ReadonlySet<string> = new Set(["data", "datasets"]);

/**
 * Plotly keeps its styling inside the traces (`data`), so only the data-array attributes of a
 * trace are values there. Dataset references (`{"$ref": ...}`) hold no colours either way.
 */
export const PLOTLY_DATA_KEYS: ReadonlySet<string> = new Set([
  "x", "y", "z", "text", "hovertext", "customdata", "ids", "labels", "values", "parents",
]);

/** The spec with every recipe-palette colour replaced by its `theme` variant. */
export function themeRecipeColours(node: JsonValue, theme: Theme, skip: ReadonlySet<string> = DATA_KEYS): JsonValue {
  if (theme !== "dark") return node;
  if (typeof node === "string") return DARK_VARIANTS[node.toLowerCase()] ?? node;
  if (Array.isArray(node)) return node.map((v) => themeRecipeColours(v, theme, skip));
  if (node === null || typeof node !== "object") return node;
  const out: Record<string, JsonValue> = {};
  for (const [k, v] of Object.entries(node)) out[k] = skip.has(k) ? v : themeRecipeColours(v, theme, skip);
  return out;
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio of two `#rrggbb` colours. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
