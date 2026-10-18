/**
 * The postMessage protocol between the web app (parent) and the plot sandbox (iframe).
 *
 * Both sides validate every inbound message with the strict parsers below (plan §10.3):
 * unknown types, unknown keys, wrong types, oversized payloads and non-JSON values are
 * rejected. Neither side ever navigates or calls an API because of a message.
 *
 * Transport rules (enforced by the callers, documented here so both sides agree):
 *
 * - The iframe is `sandbox="allow-scripts"` without `allow-same-origin`, so its origin is
 *   opaque: messages from it arrive with `event.origin === "null"`. The parent therefore
 *   checks `event.source === iframe.contentWindow` (the strong check) **and** that the
 *   origin is `"null"`, and posts to it with target origin `"*"` (an opaque origin cannot be
 *   named). The parent tears the frame down if it ever navigates after the first load.
 * - The sandbox accepts messages only from `window.parent` whose origin is one of the
 *   configured app origins, and posts back with that exact origin as target.
 *
 * This module is dependency-free and is imported by the web app as
 * `@mapi/plot-sandbox/protocol`.
 */

export const PROTOCOL = "mapi.plot/v1" as const;

export type Theme = "light" | "dark";
export type PlotKind = "vega-lite" | "plotly";
export type Scalar = string | number | boolean | null;
export type Row = Record<string, Scalar>;
export type PlotDatasets = Record<string, Row[]>;
export type JsonValue = Scalar | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

// ------------------------------------------------------------------------------- limits

export const LIMITS = {
  idLength: 128,
  messageLength: 2000,
  maxDatasets: 64,
  maxRowsTotal: 250_000,
  maxColumns: 256,
  maxStringLength: 10_000,
  maxSpecDepth: 64,
  maxSpecNodes: 200_000,
  maxSelectItems: 100,
  maxExportBytes: 25 * 1024 * 1024,
  maxDimension: 20_000,
} as const;

const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/;
const COLUMN_RE = /^[^\u0000-\u001f]{1,256}$/;
const FORBIDDEN_KEYS = new Set(["__proto__", "constructor", "prototype"]);

// ------------------------------------------------------------------------------- messages

export interface RenderMessage {
  protocol: typeof PROTOCOL;
  type: "render";
  id: string;
  kind: PlotKind;
  spec: JsonObject;
  datasets: PlotDatasets;
  theme: Theme;
}

export interface ExportRequestMessage {
  protocol: typeof PROTOCOL;
  type: "export";
  id: string;
  format: "png" | "svg";
}

export interface ThemeMessage {
  protocol: typeof PROTOCOL;
  type: "theme";
  theme: Theme;
}

/** Messages the parent (web app) sends to the sandbox. */
export type ParentMessage = RenderMessage | ExportRequestMessage | ThemeMessage;

export interface ReadyMessage {
  protocol: typeof PROTOCOL;
  type: "ready";
}

export interface RenderedMessage {
  protocol: typeof PROTOCOL;
  type: "rendered";
  id: string;
}

export interface ResizeMessage {
  protocol: typeof PROTOCOL;
  type: "resize";
  height: number;
}

export interface SelectMessage {
  protocol: typeof PROTOCOL;
  type: "select";
  id: string;
  items: Row[];
}

export interface ExportedMessage {
  protocol: typeof PROTOCOL;
  type: "exported";
  id: string;
  format: "png" | "svg";
  dataUrl: string;
}

export type SandboxErrorCode = "INVALID_MESSAGE" | "UNSAFE_SPEC" | "RENDER_FAILED" | "EXPORT_FAILED";

export interface ErrorMessage {
  protocol: typeof PROTOCOL;
  type: "error";
  id: string | null;
  code: SandboxErrorCode;
  message: string;
}

/** Messages the sandbox sends to the parent. */
export type SandboxMessage =
  | ReadyMessage
  | RenderedMessage
  | ResizeMessage
  | SelectMessage
  | ExportedMessage
  | ErrorMessage;

// ------------------------------------------------------------------------------- helpers

type Obj = Record<string, unknown>;

function isPlainObject(v: unknown): v is Obj {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return false;
  const proto: unknown = Object.getPrototypeOf(v);
  return proto === Object.prototype || proto === null;
}

function hasExactKeys(o: Obj, required: readonly string[], optional: readonly string[] = []): boolean {
  const keys = Object.keys(o);
  for (const k of required) if (!Object.prototype.hasOwnProperty.call(o, k)) return false;
  const allowed = new Set([...required, ...optional]);
  return keys.every((k) => allowed.has(k));
}

function isId(v: unknown): v is string {
  return typeof v === "string" && ID_RE.test(v);
}

function isTheme(v: unknown): v is Theme {
  return v === "light" || v === "dark";
}

function isScalar(v: unknown): v is Scalar {
  if (v === null || typeof v === "boolean") return true;
  if (typeof v === "number") return Number.isFinite(v);
  return typeof v === "string" && v.length <= LIMITS.maxStringLength;
}

function isFiniteInRange(v: unknown, min: number, max: number): v is number {
  return typeof v === "number" && Number.isFinite(v) && v >= min && v <= max;
}

/**
 * Validates that `value` is plain JSON (no prototypes, functions, Dates, typed arrays,
 * non-finite numbers or forbidden keys) within depth/node limits.
 */
export function isPlainJson(value: unknown, maxDepth: number = LIMITS.maxSpecDepth): value is JsonValue {
  let nodes = 0;
  const walk = (v: unknown, depth: number): boolean => {
    nodes += 1;
    if (nodes > LIMITS.maxSpecNodes || depth > maxDepth) return false;
    if (v === null || typeof v === "boolean") return true;
    if (typeof v === "number") return Number.isFinite(v);
    if (typeof v === "string") return v.length <= 1_000_000;
    if (Array.isArray(v)) return v.every((x) => walk(x, depth + 1));
    if (!isPlainObject(v)) return false;
    for (const [k, x] of Object.entries(v)) {
      if (FORBIDDEN_KEYS.has(k)) return false;
      if (!walk(x, depth + 1)) return false;
    }
    return true;
  };
  return walk(value, 0);
}

/** Validates the datasets map: names, row shape, scalar cells and total size. */
export function isPlotDatasets(value: unknown): value is PlotDatasets {
  if (!isPlainObject(value)) return false;
  const entries = Object.entries(value);
  if (entries.length > LIMITS.maxDatasets) return false;
  let total = 0;
  for (const [name, rows] of entries) {
    if (!NAME_RE.test(name) || !Array.isArray(rows)) return false;
    total += rows.length;
    if (total > LIMITS.maxRowsTotal) return false;
    for (const row of rows) {
      if (!isPlainObject(row)) return false;
      const cols = Object.keys(row);
      if (cols.length > LIMITS.maxColumns) return false;
      for (const c of cols) {
        if (FORBIDDEN_KEYS.has(c) || !COLUMN_RE.test(c)) return false;
        if (!isScalar(row[c])) return false;
      }
    }
  }
  return true;
}

function isSelectItems(value: unknown): value is Row[] {
  if (!Array.isArray(value) || value.length > LIMITS.maxSelectItems) return false;
  return value.every((row) => {
    if (!isPlainObject(row)) return false;
    const cols = Object.keys(row);
    if (cols.length > 64) return false;
    return cols.every((c) => !FORBIDDEN_KEYS.has(c) && COLUMN_RE.test(c) && isScalar(row[c]));
  });
}

const EXPORT_PREFIX: Record<"png" | "svg", string> = {
  png: "data:image/png;base64,",
  svg: "data:image/svg+xml;base64,",
};

function isDataUrl(v: unknown, format: "png" | "svg"): v is string {
  if (typeof v !== "string" || v.length > LIMITS.maxExportBytes * 1.4) return false;
  const prefix = EXPORT_PREFIX[format];
  return v.startsWith(prefix) && /^[A-Za-z0-9+/]*={0,2}$/.test(v.slice(prefix.length));
}

// ------------------------------------------------------------------------------- parsers

/** Parse a message received by the sandbox. Returns `null` if it is not a valid message. */
export function parseParentMessage(data: unknown): ParentMessage | null {
  if (!isPlainObject(data) || data.protocol !== PROTOCOL) return null;
  switch (data.type) {
    case "render": {
      if (!hasExactKeys(data, ["protocol", "type", "id", "kind", "spec", "datasets", "theme"])) {
        return null;
      }
      if (!isId(data.id) || (data.kind !== "vega-lite" && data.kind !== "plotly")) return null;
      if (!isTheme(data.theme)) return null;
      if (!isPlainObject(data.spec) || !isPlainJson(data.spec)) return null;
      if (!isPlotDatasets(data.datasets)) return null;
      return data as unknown as RenderMessage;
    }
    case "export": {
      if (!hasExactKeys(data, ["protocol", "type", "id", "format"])) return null;
      if (!isId(data.id) || (data.format !== "png" && data.format !== "svg")) return null;
      return data as unknown as ExportRequestMessage;
    }
    case "theme": {
      if (!hasExactKeys(data, ["protocol", "type", "theme"]) || !isTheme(data.theme)) return null;
      return data as unknown as ThemeMessage;
    }
    default:
      return null;
  }
}

const ERROR_CODES: ReadonlySet<string> = new Set([
  "INVALID_MESSAGE",
  "UNSAFE_SPEC",
  "RENDER_FAILED",
  "EXPORT_FAILED",
]);

/** Parse a message received by the parent. Returns `null` if it is not a valid message. */
export function parseSandboxMessage(data: unknown): SandboxMessage | null {
  if (!isPlainObject(data) || data.protocol !== PROTOCOL) return null;
  switch (data.type) {
    case "ready":
      return hasExactKeys(data, ["protocol", "type"]) ? (data as unknown as ReadyMessage) : null;
    case "rendered":
      return hasExactKeys(data, ["protocol", "type", "id"]) && isId(data.id)
        ? (data as unknown as RenderedMessage)
        : null;
    case "resize":
      return hasExactKeys(data, ["protocol", "type", "height"]) &&
        isFiniteInRange(data.height, 0, LIMITS.maxDimension)
        ? (data as unknown as ResizeMessage)
        : null;
    case "select":
      return hasExactKeys(data, ["protocol", "type", "id", "items"]) &&
        isId(data.id) &&
        isSelectItems(data.items)
        ? (data as unknown as SelectMessage)
        : null;
    case "exported": {
      if (!hasExactKeys(data, ["protocol", "type", "id", "format", "dataUrl"])) return null;
      if (!isId(data.id) || (data.format !== "png" && data.format !== "svg")) return null;
      return isDataUrl(data.dataUrl, data.format) ? (data as unknown as ExportedMessage) : null;
    }
    case "error":
      return hasExactKeys(data, ["protocol", "type", "id", "code", "message"]) &&
        (data.id === null || isId(data.id)) &&
        typeof data.code === "string" &&
        ERROR_CODES.has(data.code) &&
        typeof data.message === "string" &&
        data.message.length <= LIMITS.messageLength
        ? (data as unknown as ErrorMessage)
        : null;
    default:
      return null;
  }
}

// ------------------------------------------------------------------------------- builders

export function renderMessage(
  id: string,
  kind: PlotKind,
  spec: JsonObject,
  datasets: PlotDatasets,
  theme: Theme,
): RenderMessage {
  return { protocol: PROTOCOL, type: "render", id, kind, spec, datasets, theme };
}

export function exportMessage(id: string, format: "png" | "svg"): ExportRequestMessage {
  return { protocol: PROTOCOL, type: "export", id, format };
}

export function themeMessage(theme: Theme): ThemeMessage {
  return { protocol: PROTOCOL, type: "theme", theme };
}

export function errorMessage(id: string | null, code: SandboxErrorCode, message: string): ErrorMessage {
  return {
    protocol: PROTOCOL,
    type: "error",
    id,
    code,
    message: message.slice(0, LIMITS.messageLength),
  };
}

/** Clamp an arbitrary datum (from a Vega item or a Plotly point) to a flat scalar row. */
export function toSelectRow(datum: unknown): Row {
  const out: Row = {};
  if (!isPlainObject(datum) && (typeof datum !== "object" || datum === null)) return out;
  let n = 0;
  for (const [k, v] of Object.entries(datum as Obj)) {
    if (n >= 64) break;
    if (FORBIDDEN_KEYS.has(k) || k.startsWith("Symbol(") || !COLUMN_RE.test(k)) continue;
    let value: Scalar | undefined;
    if (v instanceof Date) value = Number.isFinite(v.getTime()) ? v.toISOString() : null;
    else if (typeof v === "string") value = v.slice(0, 500);
    else if (isScalar(v)) value = v;
    if (value !== undefined) {
      out[k] = value;
      n += 1;
    }
  }
  return out;
}

// ------------------------------------------------------------------------------- spec safety

const TILE_TRACES = new Set([
  "scattermapbox",
  "choroplethmapbox",
  "densitymapbox",
  "scattermap",
  "choroplethmap",
  "densitymap",
]);

/** Plotly renders a pseudo-HTML subset in text; only b, i, br, sub and sup are allowed. */
const UNSAFE_PLOTLY_TEXT = /<\/?(?!(?:b|i|br|sub|sup)\s*\/?\s*>)[a-z!]|javascript:/i;
/** Hover templates may also use `<extra>` (Plotly's secondary hover box), as the server allows. */
const UNSAFE_PLOTLY_HOVERTEMPLATE = /<\/?(?!(?:b|i|br|sub|sup|extra)\s*\/?\s*>)[a-z!]|javascript:/i;

function unsafePlotlyText(key: string | null, text: string): boolean {
  return (key?.startsWith("hovertemplate") ? UNSAFE_PLOTLY_HOVERTEMPLATE : UNSAFE_PLOTLY_TEXT).test(text);
}

function escapePointer(key: string): string {
  return key.replace(/~/g, "~0").replace(/\//g, "~1");
}

/**
 * Client-side defence in depth for the server's plot sanitizer (plan §10.3): returns the
 * JSON pointers of constructs that could load external resources or execute markup.
 * An empty list means the spec passed.
 */
export function findUnsafeConstructs(kind: PlotKind, spec: unknown): string[] {
  const issues: string[] = [];
  const walk = (node: unknown, path: string, parentKey: string | null): void => {
    if (Array.isArray(node)) {
      node.forEach((x, i) => {
        if (kind === "plotly" && typeof x === "string" && unsafePlotlyText(parentKey, x)) {
          issues.push(`${path}/${i}`);
        }
        walk(x, `${path}/${i}`, parentKey);
      });
      return;
    }
    if (typeof node !== "object" || node === null) return;
    for (const [key, value] of Object.entries(node as Obj)) {
      const here = `${path}/${escapePointer(key)}`;
      if (FORBIDDEN_KEYS.has(key)) {
        issues.push(here);
        continue;
      }
      if (kind === "vega-lite") {
        if (key === "url") issues.push(here);
        else if (key === "href" && (parentKey === "encoding" || parentKey === "mark")) issues.push(here);
        else if (key === "mark" && (value === "image" || (isPlainObject(value) && value.type === "image"))) {
          issues.push(here);
        }
      } else {
        if (key === "images" && parentKey === "layout") issues.push(here);
        else if (key === "mapbox" || key === "map") issues.push(here);
        else if (key === "type" && typeof value === "string" && TILE_TRACES.has(value)) issues.push(here);
        else if (key === "source" && typeof value === "string" && /^(https?:|\/\/)/i.test(value)) {
          issues.push(here);
        } else if ((key === "href" || key === "link") && typeof value === "string") issues.push(here);
        else if (typeof value === "string" && unsafePlotlyText(key, value)) issues.push(here);
      }
      walk(value, here, key);
    }
  };
  walk(spec, "/spec", null);
  return issues;
}
