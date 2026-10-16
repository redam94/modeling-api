import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { DEV_PREBUNDLE } from "../dev-prebundle";

const root = process.cwd();
const pkg = JSON.parse(readFileSync(join(root, "package.json"), "utf8")) as { dependencies: Record<string, string> };

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

/** Bare (package) imports of a source file. */
function bareImports(path: string): string[] {
  const text = readFileSync(path, "utf8");
  return [...text.matchAll(/^\s*import\s+(?:[^"']*from\s+)?["']([^"'.@/][^"']*|@[^/"']+\/[^"']+)["']/gm)].map((m) => m[1] ?? "");
}

describe("DEV_PREBUNDLE", () => {
  it("pre-bundles the lazily loaded React Flow for the dev server", () => {
    expect(DEV_PREBUNDLE).toContain("@xyflow/react");
  });

  it("pre-bundles every package a web worker imports (Vite's scan never reads workers)", () => {
    const workers = files(join(root, "src")).filter((p) => /\.worker\.ts$/.test(p));
    expect(workers.length).toBeGreaterThan(0);
    for (const w of workers) for (const dep of bareImports(w)) expect(DEV_PREBUNDLE, `${w} imports ${dep}`).toContain(dep);
  });

  it("names only direct dependencies", () => {
    for (const name of DEV_PREBUNDLE) {
      const pkgName = name.startsWith("@") ? name.split("/").slice(0, 2).join("/") : (name.split("/")[0] ?? name);
      expect(Object.keys(pkg.dependencies)).toContain(pkgName);
    }
  });
});
