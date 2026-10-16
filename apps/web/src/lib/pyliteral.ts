/**
 * A Python expression equal to a JSON value (the TypeScript twin of
 * `mapi_spec.snippets.py_literal`), for snippets the app writes itself. Strings are written as
 * JSON double-quoted strings: every escape `JSON.stringify` emits is also a Python escape, so
 * no value can end its string early, whatever it contains.
 */
export function py(value: unknown, indent = 0): string {
  const pad = " ".repeat(indent);
  const inner = " ".repeat(indent + 4);
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("payloads are JSON: NaN and infinity are not allowed");
    return JSON.stringify(value);
  }
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    if (value.every((v) => v === null || typeof v !== "object")) return `[${value.map((v) => py(v)).join(", ")}]`;
    return `[\n${value.map((v) => `${inner}${py(v, indent + 4)},`).join("\n")}\n${pad}]`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) return "{}";
    return `{\n${entries.map(([k, v]) => `${inner}${JSON.stringify(k)}: ${py(v, indent + 4)},`).join("\n")}\n${pad}}`;
  }
  throw new Error(`not a JSON value: ${typeof value}`);
}
