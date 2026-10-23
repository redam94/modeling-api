import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { budgetReport, CHAT_PANEL_ONLY, checkChunkBudgets, chunkAddedBytes, checkEntryBudget, loadedBefore, MOTION_PATTERNS, SIGNED_IN_ONLY, type ChunkBudget, type ChunkInfo } from "../bundle-budget";

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
    // v0.20 (plan D22): the one deliberate change, 288 → 292 KiB (and the panel-only modules forbidden).
    expect(config).toMatch(/share-app-\[\^\/\]\*\\\.js\$\/, maxBytes: 292 \* 1024, forbidden: SHARE_STATIC_FORBIDDEN/);
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

describe("v0.18 lazy chunk budgets (requiredWhen)", () => {
  const tourHost: ChunkBudget = { match: /tour-host-[^/]*\.js$/, maxBytes: 3000, forbidden: [], requiredWhen: /\/src\/features\/onboarding\/tour-host\.tsx$/ };

  it("skips a budget whose module is not in the build yet (a feature mounted later)", () => {
    expect(checkChunkBudgets([chunk("index.js", { isEntry: true })], [tourHost])).toEqual([]);
  });

  it("fails when the module is bundled but not in its own chunk (it slipped into the entry)", () => {
    const chunks = [chunk("index.js", { isEntry: true, moduleIds: ["/w/src/main.tsx", "/w/src/features/onboarding/tour-host.tsx"] })];
    const problems = checkChunkBudgets(chunks, [tourHost]);
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("tour-host.tsx is bundled in index.js");
  });

  it("checks the size once the chunk exists", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["tour-host-a.js"] }),
      chunk("tour-host-a.js", { size: 3500, moduleIds: ["/w/src/features/onboarding/tour-host.tsx"] }),
    ];
    expect(checkChunkBudgets(chunks, [tourHost])[0]).toContain("3500");
  });
});

describe("v0.18 signed-in-only modules", () => {
  const ids = {
    motion: "/r/node_modules/.pnpm/motion@12.43.0_react@19/node_modules/motion/dist/es/react.mjs",
    framer: "/r/node_modules/.pnpm/framer-motion@12.43.0/node_modules/framer-motion/dist/es/render/components/m/proxy.mjs",
    dom: "/r/node_modules/.pnpm/motion-dom@12.43.0/node_modules/motion-dom/dist/es/index.mjs",
    wrapper: "/r/apps/web/src/lib/motion.tsx",
    tour: "/r/apps/web/src/features/onboarding/tour-host.tsx",
    library: "/r/apps/web/src/features/library/api.ts",
    extend: "/r/apps/web/src/features/extend/api.ts",
    sliders: "/r/apps/web/src/components/interactive/sliders.tsx",
    art: "/r/apps/web/src/components/illustrations/hero.tsx",
  };
  const hit = (patterns: RegExp[], id: string) => patterns.some((p) => p.test(id));

  it("the motion patterns catch the library in a pnpm layout, and its wrapper", () => {
    for (const id of [ids.motion, ids.framer, ids.dom, ids.wrapper]) expect([id, hit(MOTION_PATTERNS, id)]).toEqual([id, true]);
    // not other packages whose names merely contain the word
    expect(hit(MOTION_PATTERNS, "/r/node_modules/.pnpm/x@1/node_modules/@scope/no-motion/index.js")).toBe(false);
    expect(hit(MOTION_PATTERNS, "/r/apps/web/src/lib/onboarding-events.ts")).toBe(false);
  });

  it("share links and the portal forbid motion and the v0.18 signed-in folders", () => {
    for (const id of Object.values(ids)) expect([id, hit(SIGNED_IN_ONLY, id)]).toEqual([id, true]);
    expect(hit(SIGNED_IN_ONLY, "/r/apps/web/src/features/dashboards/readout/page.tsx")).toBe(false);
    expect(hit(SIGNED_IN_ONLY, "/r/apps/web/src/components/common.tsx")).toBe(false);
  });

  it("a share link that reached the onboarding feature fails its budget", () => {
    const share: ChunkBudget = { match: /share-app-/, maxBytes: 100_000, forbidden: SIGNED_IN_ONLY };
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["share-app-a.js"] }),
      chunk("share-app-a.js", { imports: ["index.js", "x.js"] }),
      chunk("x.js", { moduleIds: [ids.tour] }),
    ];
    expect(checkChunkBudgets(chunks, [share])[0]).toContain("onboarding/tour-host.tsx");
  });
});

describe("the v0.18 budgets (vite.config.ts)", () => {
  const config = readFileSync(resolve(__dirname, "../vite.config.ts"), "utf8");

  it("keeps the entry at 640 KiB and forbids the animation library in it (critic)", () => {
    expect(config).toMatch(/maxEntryBytes: 640 \* 1024,/);
    expect(config).toContain("|apache-arrow|@xyflow)\\//, ...MOTION_PATTERNS, ...CHAT_PANEL_ONLY],");
  });

  it("share links and the portal forbid every signed-in-only module", () => {
    expect(config).toMatch(/const SHARE_FORBIDDEN = \[[^\]]*\.\.\.SIGNED_IN_ONLY,\s*\];/);
    expect(config).toMatch(/client-app-[\s\S]*?forbidden: \[[^\]]*\.\.\.SIGNED_IN_ONLY, \.\.\.CHAT_PANEL_ONLY\]/);
  });

  it("budgets the new lazy chunks (I-8)", () => {
    const budgets: [string, number, string][] = [
      ["tour-host-", 48, "features\\/onboarding\\/tour-host"],
      ["playground-", 84, "features\\/onboarding\\/playground"], // 72 in I-8; raised by E1 (measured 83,203 B)
      ["recipe-playground-", 32, "features\\/plots\\/recipe-playground"],
      ["library-", 160, "routes\\/library"],
      ["checklist)-", 48, "features\\/onboarding\\/(?:start-here|checklist)"],
      ["plugins-", 96, "routes\\/plugins"], // v0.19 (I-8)
    ];
    for (const [name, kib, module] of budgets) {
      const at = config.indexOf(`${name}[^/]*\\.js$/`);
      expect([name, at >= 0]).toEqual([name, true]);
      const entry = config.slice(at, config.indexOf("}", at));
      expect([name, entry.includes(`maxBytes: ${kib} * 1024`), entry.includes(module)]).toEqual([name, true, true]);
    }
  });
});

describe("v0.19 uploaded code (I-8)", () => {
  const plugins = "/r/apps/web/src/features/plugins/upload-page.tsx";

  it("the uploaded-code feature is signed-in only (share links and the portal never load it)", () => {
    expect(SIGNED_IN_ONLY.some((p) => p.test(plugins))).toBe(true);
    expect(SIGNED_IN_ONLY.some((p) => p.test("/r/apps/web/src/mocks/plugins.ts"))).toBe(false);
    expect(SIGNED_IN_ONLY.some((p) => p.test("/r/apps/web/src/routes/plugins.tsx"))).toBe(false);
  });

  it("a client portal that reached the uploaded-code feature fails its budget", () => {
    const portal: ChunkBudget = { match: /client-app-/, maxBytes: 100_000, forbidden: SIGNED_IN_ONLY };
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["client-app-a.js"] }),
      chunk("client-app-a.js", { imports: ["index.js", "y.js"] }),
      chunk("y.js", { moduleIds: [plugins] }),
    ];
    expect(checkChunkBudgets(chunks, [portal])[0]).toContain("features/plugins/upload-page.tsx");
  });

  it("the about card and the run page never import the feature statically (the chip and badge are inline)", () => {
    for (const file of ["features/about/about-card.tsx", "routes/run-detail.tsx"]) {
      const src = readFileSync(resolve(__dirname, file), "utf8");
      expect([file, /from "@\/features\/plugins\//.test(src)]).toEqual([file, false]);
    }
  });
});

describe("v0.20 chat (plan D21, D22)", () => {
  const config = readFileSync(resolve(__dirname, "../vite.config.ts"), "utf8");
  const ids = {
    panel: "/r/apps/web/src/features/dashboards/chat/chat-panel.tsx",
    renderer: "/r/apps/web/src/features/dashboards/chat/render-safe.tsx",
    docs: "/r/apps/web/src/features/dashboards/chat/docs-links.ts",
    reducer: "/r/apps/web/src/features/dashboards/chat/conversation.ts",
  };
  const hit = (id: string) => CHAT_PANEL_ONLY.some((p) => p.test(id));

  it("the panel, the renderer, the docs allow-list and the reducer load only with the panel", () => {
    for (const id of Object.values(ids)) expect([id, hit(id)]).toEqual([id, true]);
    // The launcher and the transports are small and sit in the readout page's closure.
    for (const f of ["chat-launcher.tsx", "transport-share.ts", "transport-member.ts", "types.ts", "keys.ts"]) {
      expect([f, hit(`/r/apps/web/src/features/dashboards/chat/${f}`)]).toEqual([f, false]);
    }
  });

  it("a share link that loads the panel statically fails its budget", () => {
    const share: ChunkBudget = { match: /share-app-/, maxBytes: 100_000, forbidden: CHAT_PANEL_ONLY };
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["share-app-a.js"] }),
      chunk("share-app-a.js", { imports: ["index.js", "z.js"] }),
      chunk("z.js", { moduleIds: [ids.renderer] }),
    ];
    expect(checkChunkBudgets(chunks, [share])[0]).toContain("chat/render-safe.tsx");
  });

  it("the panel must be its own chunk once it is in the build", () => {
    const panel: ChunkBudget = { match: /chat-panel-/, maxBytes: 40 * 1024, forbidden: [], requiredWhen: /\/chat\/chat-panel\.tsx$/ };
    const merged = [chunk("index.js", { isEntry: true, imports: ["share-app-a.js"] }), chunk("share-app-a.js", { moduleIds: [ids.panel] })];
    expect(checkChunkBudgets(merged, [panel])[0]).toContain("load it with import() in its own chunk");
    const lazy = [chunk("index.js", { isEntry: true, dynamicImports: ["chat-panel-a.js"] }), chunk("chat-panel-a.js", { moduleIds: [ids.panel] })];
    expect(checkChunkBudgets(lazy, [panel])).toEqual([]);
  });

  it("reports what each budgeted chunk adds (MAPI_BUDGET_REPORT=1)", () => {
    const chunks = [
      chunk("index.js", { isEntry: true, dynamicImports: ["share-app-a.js"], size: 500 }),
      chunk("share-app-a.js", { imports: ["index.js", "page.js"], dynamicImports: ["chat-panel-b.js"], size: 300 }),
      chunk("page.js", { size: 200 }),
      chunk("chat-panel-b.js", { imports: ["page.js", "radix.js"], size: 100 }),
      chunk("radix.js", { size: 50 }),
    ];
    expect(chunkAddedBytes(chunks, "share-app-a.js")).toBe(500);
    expect(chunkAddedBytes(chunks, "chat-panel-b.js")).toBe(150);
    const report = budgetReport(chunks, { maxEntryBytes: 1000, forbidden: [], chunkBudgets: [{ match: /chat-panel-/, maxBytes: 40, forbidden: [] }, { match: /nope-/, maxBytes: 1, forbidden: [] }] });
    expect(report).toEqual(["entry closure: 500 / 1000 bytes", "chat-panel-b.js: 150 / 40 bytes", "/nope-/: no chunk"]);
  });

  it("budgets the chat chunk at 40 KiB with the share link's forbidden list, and share links at 292 KiB", () => {
    const at = config.indexOf("chat-panel-[^/]*\\.js$/");
    expect(at).toBeGreaterThan(0);
    const entry = config.slice(at, config.indexOf("}", at));
    expect(entry).toContain("maxBytes: 40 * 1024");
    expect(entry).toContain("forbidden: SHARE_FORBIDDEN");
    expect(entry).toContain("chat\\/chat-panel\\.tsx$/");
    expect(config).toMatch(/share-app-\[\^\/\]\*\\\.js\$\/, maxBytes: 292 \* 1024, forbidden: SHARE_STATIC_FORBIDDEN/);
    expect(config).toMatch(/client-app-[\s\S]*?maxBytes: 260 \* 1024/);
    expect(config).toContain("const SHARE_STATIC_FORBIDDEN = [...SHARE_FORBIDDEN, ...CHAT_PANEL_ONLY];");
  });
});
