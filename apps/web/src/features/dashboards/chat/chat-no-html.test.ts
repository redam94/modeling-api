/**
 * Lint test of the chat feature (v0.20, plan D23): no HTML sink anywhere in
 * `features/dashboards/chat/**`, and the only `href` is the allow-listed `DocsLink` (whose URL
 * comes from `docs-links.ts`, never from an answer).
 */
import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob<string>(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], { query: "?raw", import: "default", eager: true });

const SINKS = /dangerouslySetInnerHTML|\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser|srcDoc|createHTML\s*[(:]|createPolicy/;

describe("chat sources", () => {
  const files = Object.entries(SOURCES);

  it("finds the chat modules", () => {
    const names = files.map(([f]) => f);
    for (const f of ["./chat-panel.tsx", "./chat-launcher.tsx", "./render-safe.tsx", "./docs-links.ts"]) expect(names).toContain(f);
  });

  it("no module writes HTML strings", () => {
    for (const [file, src] of files) expect([file, SINKS.exec(src)?.[0] ?? null]).toEqual([file, null]);
  });

  it("the only href is DocsLink's, built from the docs allow-list", () => {
    const hrefs = files.flatMap(([file, src]) => [...src.matchAll(/\bhref\s*=\s*(\{[^}]*\}|"[^"]*"|'[^']*')/g)].map((m) => `${file} ${m[1]}`));
    expect(hrefs).toEqual(["./chat-panel.tsx {href}"]);
    const panel = SOURCES["./chat-panel.tsx"] ?? "";
    const docsLink = panel.slice(panel.indexOf("function DocsLink("), panel.indexOf("function ToolChips("));
    expect(docsLink).toContain("const href = docsHref(k);");
    expect(docsLink).toContain("href={href}");
  });

  it("no image, frame or script element is ever created", () => {
    for (const [file, src] of files) expect([file, /<(?:img|iframe|script|object|embed|video|audio|source)\b|createElement\(\s*["'](?:img|iframe|script)/.exec(src)?.[0] ?? null]).toEqual([file, null]);
  });
});
