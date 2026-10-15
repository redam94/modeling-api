/**
 * Renderer interface used by the sandbox controller. Real implementations live in
 * `vega-renderer.ts` and `plotly-renderer.ts` (loaded lazily so each plot only pulls the
 * library it needs); tests inject fakes.
 */
import type { JsonObject, PlotDatasets, Row, Theme } from "./protocol";

export interface RenderContext {
  container: HTMLElement;
  spec: JsonObject;
  datasets: PlotDatasets;
  theme: Theme;
  onSelect: (items: Row[]) => void;
}

export interface Rendered {
  /** A base64 `data:` URL of the current view. */
  export(format: "png" | "svg"): Promise<string>;
  destroy(): void;
}

export type Renderer = (ctx: RenderContext) => Promise<Rendered>;

/** Encode a UTF-8 string as base64 without `btoa`'s Latin-1 limitation. */
export function utf8ToBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** Normalise an image data URL to the base64 form the protocol requires. */
export function toBase64DataUrl(url: string, format: "png" | "svg"): string {
  const mime = format === "png" ? "image/png" : "image/svg+xml";
  const comma = url.indexOf(",");
  if (!url.startsWith("data:") || comma < 0) throw new Error("renderer returned a non-data URL");
  const header = url.slice(5, comma);
  const body = url.slice(comma + 1);
  if (!header.startsWith(mime)) throw new Error(`renderer returned ${header}, expected ${mime}`);
  if (header.endsWith(";base64")) return `data:${mime};base64,${body}`;
  return `data:${mime};base64,${utf8ToBase64(decodeURIComponent(body))}`;
}
