import { describe, expect, it } from "vitest";

import { checkEntryBudget, type ChunkInfo } from "../bundle-budget";

const chunk = (fileName: string, over: Partial<ChunkInfo> = {}): ChunkInfo => ({
  fileName,
  isEntry: false,
  imports: [],
  moduleIds: [],
  size: 1000,
  ...over,
});

const opts = { maxEntryBytes: 5000, forbidden: [/node_modules\/monaco-editor\//, /node_modules\/apache-arrow\//] };

describe("checkEntryBudget", () => {
  it("passes when heavy modules are only reachable through dynamic imports", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, imports: ["shared.js"], moduleIds: ["/src/main.tsx"] }),
      chunk("shared.js", { moduleIds: ["\0vite/preload-helper.js"] }),
      chunk("monaco.js", { size: 3_000_000, moduleIds: ["/x/node_modules/monaco-editor/esm/vs/editor/editor.api.js"] }),
    ];
    expect(checkEntryBudget(chunks, opts)).toEqual([]);
  });

  it("flags a forbidden module in the entry's static import closure (e.g. the preload helper pulled into monaco)", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, imports: ["monaco.js"] }),
      chunk("monaco.js", { moduleIds: ["\0vite/preload-helper.js", "/x/node_modules/monaco-editor/esm/a.js"] }),
    ];
    const problems = checkEntryBudget(chunks, opts);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("monaco.js");
    expect(problems[0]).toContain("monaco-editor");
  });

  it("follows transitive static imports and enforces the size budget", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, imports: ["a.js"], size: 3000 }),
      chunk("a.js", { imports: ["b.js"], size: 1500 }),
      chunk("b.js", { imports: ["a.js"], size: 1000, moduleIds: ["/x/node_modules/apache-arrow/Arrow.mjs"] }),
    ];
    const problems = checkEntryBudget(chunks, opts);
    expect(problems.some((p) => p.includes("b.js") && p.includes("apache-arrow"))).toBe(true);
    expect(problems.some((p) => p.includes("5500") && p.includes("5000"))).toBe(true);
  });

  it("reports a build with no entry chunk", () => {
    expect(checkEntryBudget([chunk("a.js")], opts)).toEqual(["no entry chunk was produced"]);
  });
});
