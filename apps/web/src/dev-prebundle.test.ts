import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { DEV_PREBUNDLE } from "../dev-prebundle";

const pkg = JSON.parse(readFileSync(join(process.cwd(), "package.json"), "utf8")) as { dependencies: Record<string, string> };

describe("DEV_PREBUNDLE", () => {
  it("pre-bundles the lazily loaded React libraries for the dev server", () => {
    expect(DEV_PREBUNDLE).toContain("@xyflow/react");
  });

  it("names only direct dependencies", () => {
    for (const name of DEV_PREBUNDLE) expect(Object.keys(pkg.dependencies)).toContain(name);
  });
});
