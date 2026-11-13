/**
 * The web mirror of `mapi_spec.schema_safety.sanitize_schema(mode="strip")` (v0.18 D11):
 * defence in depth before a JSON Schema from a catalog or a library document becomes a form.
 *
 * The server already strips unsafe schemas when it loads a catalog and refuses them in library
 * documents. The web repeats the keyword allow-list so a form never renders from a keyword it
 * does not know: unknown keywords, remote or dangling `$ref`s, reference cycles, oversized
 * enums, long or control-character text, unknown `format`s and `x-ui-*` keywords outside the
 * allow-list are removed. Each removal is reported as the JSON pointer of the removed part.
 *
 * One deliberate difference: the web never compiles a regular expression, so it drops every
 * `pattern` and `patternProperties` keyword (the server validates them; the form does not
 * need them). The parity test against `packages/spec/tests/golden/settings_schemas.json`
 * allows exactly that difference. Pure functions, no regular expressions (the
 * `features/forms` lint test forbids `RegExp(`).
 */

export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };

export const SCHEMA_MAX_BYTES = 65_536;
export const SCHEMA_MAX_DEPTH = 12;
export const SCHEMA_MAX_PROPS = 256;
export const ENUM_MAX_ITEMS = 64;
export const ENUM_ITEM_MAX_CHARS = 200;
export const TEXT_MAX_CHARS = 2000;
/** Containers nested deeper than this are refused before any recursion. */
const JSON_MAX_NESTING = 64;
const DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema";
const LOCAL_REF_PREFIX = "#/$defs/";

export const ALLOWED_FORMATS: ReadonlySet<string> = new Set(["date", "date-time"]);
export const UI_WIDGETS: ReadonlySet<string> = new Set(["slider", "select", "textarea", "var_picker", "dim_picker", "checkbox"]);
const UI_LEVELS: ReadonlySet<string> = new Set(["basic", "advanced"]);

const SCHEMA_MAPS = new Set(["properties", "patternProperties", "$defs", "dependentSchemas"]);
const SCHEMA_LISTS = new Set(["anyOf", "oneOf", "allOf", "prefixItems"]);
const SCHEMA_ONE = new Set(["additionalProperties", "items", "not", "if", "then", "else", "contains", "propertyNames", "unevaluatedProperties", "unevaluatedItems"]);
const TEXT = new Set(["title", "description", "$comment"]);
const VALUES = new Set([
  "type",
  "required",
  "enum",
  "const",
  "default",
  "examples",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "multipleOf",
  "minLength",
  "maxLength",
  "pattern",
  "minItems",
  "maxItems",
  "uniqueItems",
  "minContains",
  "maxContains",
  "minProperties",
  "maxProperties",
  "dependentRequired",
  "format",
  "deprecated",
  "readOnly",
  "writeOnly",
  "$ref",
  "$schema",
  "discriminator",
]);
const UI_KEYWORDS = new Set(["x-ui-widget", "x-ui-group", "x-ui-order", "x-ui-level"]);

/** Every keyword a safe schema may use (`mapi_spec.schema_safety.ALLOWED_KEYWORDS`). */
export const ALLOWED_KEYWORDS: ReadonlySet<string> = new Set([...SCHEMA_MAPS, ...SCHEMA_LISTS, ...SCHEMA_ONE, ...TEXT, ...VALUES, ...UI_KEYWORDS]);

/** The keywords the web removes although the server may keep them (it never runs a regex). */
export const WEB_DROPPED_KEYWORDS: ReadonlySet<string> = new Set(["pattern", "patternProperties"]);

export interface SanitizedSchema {
  schema: JsonObject;
  /** JSON pointers (RFC 6901) of the removed parts, in walk order. */
  removed: string[];
}

const DROP = Symbol("drop");

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function pointer(path: readonly (string | number)[]): string {
  return path.map((s) => `/${String(s).split("~").join("~0").split("/").join("~1")}`).join("");
}

/** Length in code points (Python's `len`), not UTF-16 units. */
function chars(s: string): number {
  let n = 0;
  for (const _ of s) n++;
  return n;
}

function hasControl(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c <= 0x08 || c === 0x0b || c === 0x0c || (c >= 0x0e && c <= 0x1f) || c === 0x7f) return true;
  }
  return false;
}

function plainText(v: unknown): boolean {
  return typeof v === "string" && chars(v) <= TEXT_MAX_CHARS && !hasControl(v);
}

function tooDeep(value: unknown, limit: number): boolean {
  const stack: [unknown, number][] = [[value, 1]];
  while (stack.length) {
    const [cur, depth] = stack.pop()!;
    if (Array.isArray(cur)) {
      if (depth > limit) return true;
      for (const v of cur) stack.push([v, depth + 1]);
    } else if (isObject(cur)) {
      if (depth > limit) return true;
      for (const v of Object.values(cur)) stack.push([v, depth + 1]);
    }
  }
  return false;
}

/** `#/$defs/<name>` → `<name>` (unescaped), or `null` for any other reference. */
export function localRefName(ref: unknown): string | null {
  if (typeof ref !== "string" || !ref.startsWith(LOCAL_REF_PREFIX)) return null;
  const raw = ref.slice(LOCAL_REF_PREFIX.length);
  if (!raw || raw.includes("/") || chars(raw) > 128) return null;
  return raw.split("~1").join("/").split("~0").join("~");
}

function refsIn(value: unknown, out: Set<string>): Set<string> {
  if (Array.isArray(value)) for (const v of value) refsIn(v, out);
  else if (isObject(value)) {
    for (const [k, v] of Object.entries(value)) {
      if (k === "$ref") {
        const name = localRefName(v);
        if (name !== null) out.add(name);
      } else refsIn(v, out);
    }
  }
  return out;
}

/** The `$defs` names on a reference cycle (each could expand forever). */
function cyclicDefs(defs: Record<string, unknown>): Set<string> {
  const names = new Set(Object.keys(defs));
  const graph = new Map<string, string[]>();
  for (const [name, body] of Object.entries(defs)) graph.set(name, [...refsIn(body, new Set())].filter((n) => names.has(n)));
  const cyclic = new Set<string>();
  for (const start of graph.keys()) {
    const seen = new Set<string>();
    const todo = [...(graph.get(start) ?? [])];
    while (todo.length) {
      const cur = todo.pop()!;
      if (cur === start) {
        cyclic.add(start);
        break;
      }
      if (seen.has(cur)) continue;
      seen.add(cur);
      todo.push(...(graph.get(cur) ?? []));
    }
  }
  return cyclic;
}

class Walker {
  removed: string[] = [];
  props = 0;
  constructor(
    private readonly defs: ReadonlySet<string>,
    private readonly cyclic: ReadonlySet<string>,
  ) {}

  drop(path: readonly (string | number)[]): typeof DROP {
    this.removed.push(pointer(path));
    return DROP;
  }

  schema(node: unknown, path: (string | number)[], depth: number): Json {
    if (typeof node === "boolean") return node;
    if (!isObject(node) || depth > SCHEMA_MAX_DEPTH) {
      this.removed.push(pointer(path));
      return {};
    }
    const out: JsonObject = {};
    for (const [key, value] of Object.entries(node)) {
      const kept = this.keyword(key, value, [...path, key], depth, path.length === 0);
      if (kept !== DROP) out[key] = kept;
    }
    return out;
  }

  keyword(key: string, value: unknown, here: (string | number)[], depth: number, atRoot: boolean): Json | typeof DROP {
    if (!ALLOWED_KEYWORDS.has(key) || WEB_DROPPED_KEYWORDS.has(key)) return this.drop(here);
    if (SCHEMA_MAPS.has(key)) {
      if (!isObject(value) || (key === "$defs" && !atRoot)) return this.drop(here);
      const sub: JsonObject = {};
      for (const [name, body] of Object.entries(value)) {
        if (key === "properties" && ++this.props > SCHEMA_MAX_PROPS) {
          this.removed.push(pointer([...here, name]));
          continue;
        }
        sub[name] = this.schema(body, [...here, name], depth + 1);
      }
      return sub;
    }
    if (SCHEMA_LISTS.has(key)) {
      if (!Array.isArray(value)) return this.drop(here);
      return value.map((body, i) => this.schema(body, [...here, i], depth + 1));
    }
    if (SCHEMA_ONE.has(key)) return this.schema(value, here, depth + 1);
    if (TEXT.has(key)) return plainText(value) ? (value as string) : this.drop(here);
    switch (key) {
      case "enum": {
        if (!Array.isArray(value) || value.length > ENUM_MAX_ITEMS) return this.drop(here);
        const long = value.some((v) => chars(typeof v === "string" ? v : (JSON.stringify(v) ?? "")) > ENUM_ITEM_MAX_CHARS);
        return long ? this.drop(here) : (value as Json[]);
      }
      case "format":
        return typeof value === "string" && ALLOWED_FORMATS.has(value) ? value : this.drop(here);
      case "$ref": {
        const name = localRefName(value);
        return name !== null && this.defs.has(name) && !this.cyclic.has(name) ? (value as string) : this.drop(here);
      }
      case "$schema":
        return atRoot && value === DRAFT_2020_12 ? value : this.drop(here);
      case "discriminator":
        return this.discriminator(value, here);
      case "x-ui-widget":
        return typeof value === "string" && UI_WIDGETS.has(value) ? value : this.drop(here);
      case "x-ui-level":
        return typeof value === "string" && UI_LEVELS.has(value) ? value : this.drop(here);
      case "x-ui-group":
        return typeof value === "string" && chars(value) <= 64 && !hasControl(value) ? value : this.drop(here);
      case "x-ui-order":
        return typeof value === "number" && Number.isInteger(value) && Math.abs(value) <= 10_000 ? value : this.drop(here);
      default:
        return value as Json;
    }
  }

  discriminator(value: unknown, here: (string | number)[]): Json | typeof DROP {
    if (!isObject(value) || typeof value.propertyName !== "string") return this.drop(here);
    if (Object.keys(value).some((k) => k !== "propertyName" && k !== "mapping")) return this.drop(here);
    const mapping = value.mapping ?? {};
    if (!isObject(mapping)) return this.drop(here);
    for (const target of Object.values(mapping)) {
      const name = localRefName(target);
      if (name === null || !this.defs.has(name)) return this.drop(here);
    }
    return value as JsonObject;
  }
}

function byteLength(value: unknown): number | null {
  try {
    const text = JSON.stringify(value);
    return text === undefined ? null : new TextEncoder().encode(text).length;
  } catch {
    return null;
  }
}

/**
 * The schema with every unsafe or unknown part removed, and where each removal was. A value
 * that is not a JSON object, nests deeper than 64 levels or is larger than 64 KiB becomes `{}`.
 */
export function sanitizeSchema(schema: unknown): SanitizedSchema {
  if (!isObject(schema) || tooDeep(schema, JSON_MAX_NESTING)) return { schema: {}, removed: [""] };
  const size = byteLength(schema);
  if (size === null || size > SCHEMA_MAX_BYTES) return { schema: {}, removed: [""] };
  const defs = isObject(schema.$defs) ? schema.$defs : {};
  const walker = new Walker(new Set(Object.keys(defs)), cyclicDefs(defs));
  const out = walker.schema(schema, [], 1);
  return { schema: isObject(out) ? (out) : {}, removed: walker.removed };
}

const cache = new WeakMap<object, JsonObject>();

/** {@link sanitizeSchema}'s schema, memoised per input object (forms re-render often). */
export function safeSchema(schema: JsonObject): JsonObject {
  const hit = cache.get(schema);
  if (hit) return hit;
  const out = sanitizeSchema(schema).schema;
  cache.set(schema, out);
  return out;
}
