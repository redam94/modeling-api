import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { ALLOWED_KEYWORDS, safeSchema, sanitizeSchema, WEB_DROPPED_KEYWORDS, type Json, type JsonObject } from "./schema-sanitize";

interface GoldenCase {
  name: string;
  checks: string[];
  input: JsonObject;
  output: JsonObject;
  removed: string[];
}

interface Golden {
  allowed_keywords: string[];
  cases: GoldenCase[];
}

const GOLDEN = JSON.parse(readFileSync(resolve(__dirname, "../../../../../packages/spec/tests/golden/settings_schemas.json"), "utf-8")) as Golden;

/** The golden output without the keywords the web always drops (it never compiles a regex). */
function withoutWebDropped(value: Json): Json {
  if (Array.isArray(value)) return value.map(withoutWebDropped);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).filter(([k]) => !WEB_DROPPED_KEYWORDS.has(k)).map(([k, v]) => [k, withoutWebDropped(v)]));
  }
  return value;
}

describe("schema-sanitize parity with mapi_spec.schema_safety (strip mode)", () => {
  it("allows exactly the server's keywords", () => {
    expect([...ALLOWED_KEYWORDS].sort()).toEqual([...GOLDEN.allowed_keywords].sort());
  });

  it.each(GOLDEN.cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const { schema, removed } = sanitizeSchema(c.input);
    if (c.checks.includes("regex")) {
      // The web drops every pattern; everything else matches the server.
      expect(schema).toEqual(withoutWebDropped(c.output));
      return;
    }
    expect(schema).toEqual(c.output);
    expect(removed).toEqual(c.removed);
  });
});

describe("schema-sanitize", () => {
  it("drops every pattern, safe or not", () => {
    const { schema, removed } = sanitizeSchema({ type: "object", properties: { a: { type: "string", pattern: "^[a-z]+$" } }, patternProperties: { "^x_": { type: "number" } } });
    expect(schema).toEqual({ type: "object", properties: { a: { type: "string" } } });
    expect(removed).toEqual(["/properties/a/pattern", "/patternProperties"]);
  });

  it("keeps markup in titles as text (rendering escapes it)", () => {
    expect(sanitizeSchema({ title: "<img src=x onerror=alert(1)>", type: "string" }).schema.title).toBe("<img src=x onerror=alert(1)>");
  });

  it("refuses what is not a schema, too deep or too large", () => {
    expect(sanitizeSchema(null).schema).toEqual({});
    expect(sanitizeSchema([1, 2]).schema).toEqual({});
    let deep: JsonObject = { type: "string" };
    for (let i = 0; i < 70; i++) deep = { items: deep };
    expect(sanitizeSchema(deep).schema).toEqual({});
    expect(sanitizeSchema({ description: "x".repeat(70_000) }).schema).toEqual({});
  });

  it("cuts subschemas nested deeper than 12 levels", () => {
    let s: JsonObject = { type: "number" };
    for (let i = 0; i < 13; i++) s = { type: "array", items: s };
    const out = sanitizeSchema(s);
    expect(out.removed.length).toBe(1);
    expect(JSON.stringify(out.schema)).not.toContain("number");
  });

  it("caps the number of properties at 256", () => {
    const properties = Object.fromEntries(Array.from({ length: 260 }, (_, i) => [`p${i}`, { type: "number" }]));
    const out = sanitizeSchema({ type: "object", properties });
    expect(Object.keys(out.schema.properties as JsonObject)).toHaveLength(256);
    expect(out.removed).toHaveLength(4);
  });

  it("checks the x-ui keywords and the root-only keywords", () => {
    const out = sanitizeSchema({
      $schema: "https://json-schema.org/draft/2020-12/schema",
      properties: {
        a: { type: "number", "x-ui-order": 2.5, "x-ui-group": "g\u0001" },
        b: { $schema: "https://json-schema.org/draft/2020-12/schema", "x-ui-level": "expert" },
      },
    });
    expect(out.schema).toEqual({ $schema: "https://json-schema.org/draft/2020-12/schema", properties: { a: { type: "number" }, b: {} } });
    expect(out.removed).toEqual(["/properties/a/x-ui-order", "/properties/a/x-ui-group", "/properties/b/$schema", "/properties/b/x-ui-level"]);
  });

  it("memoises per schema object", () => {
    const s: JsonObject = { type: "object", properties: { a: { type: "string", format: "uri" } } };
    expect(safeSchema(s)).toBe(safeSchema(s));
    expect(safeSchema(s)).toEqual({ type: "object", properties: { a: { type: "string" } } });
  });
});
