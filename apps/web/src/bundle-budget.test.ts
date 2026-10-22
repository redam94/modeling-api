import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { checkChunkBudgets, checkEntryBudget, loadedBefore, type ChunkBudget, type ChunkInfo } from "../bundle-budget";

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

describe("checkChunkBudgets", () => {
  const router = "/x/node_modules/.pnpm/@tanstack+react-router@1.0.0/node_modules/@tanstack/react-router/dist/esm/index.js";
  const share: ChunkBudget = {
    match: /share-app-[^/]*\.js$/,
    maxBytes: 3000,
    forbidden: [/node_modules\/(?:\.pnpm\/[^/]+\/node_modules\/)?@tanstack\/react-router\//, /\/src\/api\/queries\.ts$/],
  };
  const app = (over: { entry?: Partial<ChunkInfo>; share?: Partial<ChunkInfo>; extra?: ChunkInfo[] } = {}): ChunkInfo[] => [
    chunk("index.js", { isEntry: true, size: 50_000, dynamicImports: ["share-app-a1.js", "signed-in.js"], moduleIds: ["/src/app.tsx"], ...over.entry }),
    chunk("share-app-a1.js", { imports: ["index.js", "ui.js"], size: 1000, ...over.share }),
    chunk("ui.js", { size: 1500 }),
    chunk("signed-in.js", { imports: ["index.js", "ui.js"], moduleIds: [router, "/w/src/api/queries.ts"], size: 9000 }),
    ...(over.extra ?? []),
  ];

  it("counts what the chunk adds beyond the entry, and passes a lean share app", () => {
    // 1000 + 1500 added; the 50 000-byte entry has its own budget.
    expect(checkChunkBudgets(app(), [share])).toEqual([]);
  });

  it("fails when the added bytes go over the budget", () => {
    const problems = checkChunkBudgets(app({ share: { size: 2000 } }), [share]);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("share-app-a1.js");
    expect(problems[0]).toContain("3500");
  });

  it("finds a forbidden module anywhere in the static closure, the entry included", () => {
    const inEntry = checkChunkBudgets(app({ entry: { moduleIds: ["/src/app.tsx", router] } }), [share]);
    expect(inEntry.some((p) => p.includes("index.js") && p.includes("react-router"))).toBe(true);
    const inShared = checkChunkBudgets(
      app({ share: { imports: ["index.js", "ui.js", "q.js"] }, extra: [chunk("q.js", { moduleIds: ["/w/src/api/queries.ts"], size: 10 })] }),
      [share],
    );
    expect(inShared.some((p) => p.includes("q.js") && p.includes("queries.ts"))).toBe(true);
  });

  it("ignores what the chunk only loads with import()", () => {
    const chunks = app({ share: { dynamicImports: ["signed-in.js"] } });
    expect(checkChunkBudgets(chunks, [share])).toEqual([]);
  });

  it("subtracts what every dynamic importer already loaded (a portal loaded from the gate)", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["gate.js"] }),
      chunk("gate.js", { imports: ["index.js", "big.js"], dynamicImports: ["client-app-b2.js"], size: 5000 }),
      chunk("big.js", { size: 100_000 }),
      chunk("client-app-b2.js", { imports: ["gate.js", "big.js", "own.js"], size: 800 }),
      chunk("own.js", { size: 700 }),
    ];
    expect([...loadedBefore(chunks, "client-app-b2.js")].sort()).toEqual(["big.js", "gate.js", "index.js"]);
    expect(checkChunkBudgets(chunks, [{ match: /client-app-/, maxBytes: 1500, forbidden: [] }])).toEqual([]);
    expect(checkChunkBudgets(chunks, [{ match: /client-app-/, maxBytes: 1499, forbidden: [] }])[0]).toContain("1500");
  });

  it("reports a budget whose chunk is missing", () => {
    expect(checkChunkBudgets([chunk("index.js", { isEntry: true })], [share])[0]).toMatch(/no chunk matches/);
  });

  it("runs from checkEntryBudget with the entry checks", () => {
    const problems = checkEntryBudget(app({ share: { size: 9000 } }), { maxEntryBytes: 60_000, forbidden: [], chunkBudgets: [share] });
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("share-app-a1.js");
  });
});

describe("the v0.17 readout budgets (vite.config.ts)", () => {
  const config = readFileSync(resolve(__dirname, "../vite.config.ts"), "utf8");

  it("keeps the share and client budgets and budgets each lazy readout chunk at 48 KiB with the share link's forbidden list", () => {
    expect(config).toMatch(/share-app-\[\^\/\]\*\\\.js\$\/, maxBytes: 288 \* 1024, forbidden: SHARE_FORBIDDEN/);
    expect(config).toMatch(/client-app-\[\^\/\]\*\\\.js\$\/,\s+maxBytes: 260 \* 1024/);
    for (const name of ["readout-extra", "try-a-case", "check-accuracy", "score-file"]) {
      expect([name, new RegExp(`${name}-\\[\\^/\\]\\*\\\\\\.js\\$/, maxBytes: 48 \\* 1024, forbidden: SHARE_FORBIDDEN`).test(config)]).toEqual([name, true]);
    }
  });

  it("a lazy readout chunk counts the shared chunks it adds, not what the readout page already loaded", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["share-app-a.js"] }),
      chunk("share-app-a.js", { imports: ["index.js", "readout-page-b.js"] }),
      chunk("readout-page-b.js", { dynamicImports: ["readout-extra-c.js", "try-a-case-d.js"], size: 90_000 }),
      chunk("readout-extra-c.js", { imports: ["results-e.js"], size: 3000 }),
      chunk("try-a-case-d.js", { imports: ["results-e.js", "readout-page-b.js"], size: 7000 }),
      chunk("results-e.js", { size: 30_000 }),
    ];
    const budget = (m: RegExp, maxBytes: number): ChunkBudget => ({ match: m, maxBytes, forbidden: [] });
    expect(checkChunkBudgets(chunks, [budget(/readout-extra-/, 33_000), budget(/try-a-case-/, 37_000)])).toEqual([]);
    expect(checkChunkBudgets(chunks, [budget(/try-a-case-/, 36_999)])[0]).toContain("37000");
  });
});
