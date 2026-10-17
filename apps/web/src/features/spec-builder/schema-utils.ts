/**
 * JSON Schema helpers for the visual builder's forms: the forms do not follow `$ref`, so a
 * block's schema is inlined against the document it came from (the ModelSpec schema's
 * `$defs`, or a plugin settings schema's own).
 */
import type { SchemaNode } from "@/features/forms/json-schema-form";

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

/** `schema` with local `#/$defs/…` references replaced by their targets (cycles cut at `depth`). */
export function inlineRefs(schema: unknown, root: unknown = schema, depth = 6): SchemaNode {
  const defs = isObj(root) && isObj(root.$defs) ? root.$defs : {};
  const walk = (v: unknown, d: number): unknown => {
    if (Array.isArray(v)) return v.map((x) => walk(x, d));
    if (!isObj(v)) return v;
    const ref = typeof v.$ref === "string" && v.$ref.startsWith("#/$defs/") ? v.$ref.slice(8) : null;
    if (ref !== null) {
      const target = defs[ref];
      if (!isObj(target) || d <= 0) return { type: "object" };
      const { $ref: _, ...siblings } = v;
      // The definition's own title (`AdstockSettings`) is a class name, not a label.
      const { title: __, ...body } = walk(target, d - 1) as Obj;
      return { ...body, ...(walk(siblings, d) as Obj) };
    }
    const out: Obj = {};
    for (const [k, x] of Object.entries(v)) if (k !== "$defs") out[k] = walk(x, d);
    return out;
  };
  return walk(schema, depth) as SchemaNode;
}

/** A definition of the ModelSpec schema, inlined. */
export function defOf(root: unknown, name: string): SchemaNode | null {
  const def = isObj(root) && isObj(root.$defs) ? root.$defs[name] : undefined;
  return isObj(def) ? inlineRefs(def, root) : null;
}

/** The `kind → $def name` of the data split union (`DataBlock.split`'s discriminator). */
export function splitKinds(root: unknown): { kind: string; def: string }[] {
  const block = isObj(root) && isObj(root.$defs) ? root.$defs.DataBlock : undefined;
  const split = isObj(block) && isObj(block.properties) ? block.properties.split : undefined;
  const branches = isObj(split) && Array.isArray(split.anyOf) ? split.anyOf : [];
  for (const b of branches) {
    const mapping = isObj(b) && isObj(b.discriminator) && isObj(b.discriminator.mapping) ? b.discriminator.mapping : null;
    if (mapping) {
      return Object.entries(mapping)
        .filter((e): e is [string, string] => typeof e[1] === "string")
        .map(([kind, ref]) => ({ kind, def: ref.replace("#/$defs/", "") }));
    }
  }
  return [];
}

/** `schema` without some properties (rendered elsewhere, or fixed like a union's `kind`). */
export function omitProps(schema: SchemaNode, keys: string[]): SchemaNode {
  const props = isObj(schema.properties) ? { ...schema.properties } : {};
  for (const k of keys) delete props[k];
  const required = Array.isArray(schema.required) ? schema.required.filter((r) => typeof r === "string" && !keys.includes(r)) : undefined;
  return { ...schema, properties: props, ...(required ? { required } : {}) };
}
