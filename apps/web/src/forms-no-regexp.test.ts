/**
 * v0.18 D11 and §5 WP-W2: the form and library code never compiles a regular expression from
 * data (a schema `pattern` is the server's business: the web drops it), and never writes HTML.
 * Every user-authored string in a library document is untrusted text rendered as React text.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const SRC = resolve(__dirname);
const DIRS = ["features/forms", "features/library"];

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) out.push(...sources(path));
    else if ((name.endsWith(".ts") || name.endsWith(".tsx")) && !name.includes(".test.")) out.push(path);
  }
  return out;
}

const FILES = DIRS.flatMap((d) => sources(join(SRC, d)));

/** The source without comments (the doc comments may name what is forbidden). */
function code(path: string): string {
  return readFileSync(path, "utf-8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

/** `needle` spelled out of parts, so this file never matches its own scan. */
const word = (...parts: string[]) => parts.join("");

describe("forms and library: no RegExp, no HTML", () => {
  it("scans the form and library sources", () => {
    expect(FILES.some((f) => f.endsWith("json-schema-form.tsx"))).toBe(true);
    expect(FILES.some((f) => f.endsWith("schema-sanitize.ts"))).toBe(true);
    expect(FILES.filter((f) => f.includes("/features/library/")).length).toBeGreaterThan(3);
  });

  it.each([word("Reg", "Exp("), word("inner", "HTML"), word("outer", "HTML"), word("dangerously", "SetInnerHTML"), word("insertAdjacent", "HTML"), word("document.", "write")])(
    "no %s",
    (needle) => {
      const hits = FILES.filter((f) => code(f).includes(needle)).map((f) => relative(SRC, f));
      expect(hits).toEqual([]);
    },
  );
});
