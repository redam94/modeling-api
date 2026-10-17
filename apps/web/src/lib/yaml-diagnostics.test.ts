import { describe, expect, it } from "vitest";

import type { Issue } from "@/api/types";

import { locateIssues, markerMessage, YamlLocator } from "./yaml-diagnostics";

const SPEC = `apiVersion: mapi/v1
kind: ModelSpec
metadata: {name: eu-geo-mmm, project: default}
data:
  dataset: dataset://default/eu_media_daily@latest
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}
pipeline:
  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN}}
  - id: scale_y
    use: core.mean_scale@^1
    with:
      vars: [y]
      over: [time]
model:
  use: mmm.bayesian@^1
  settings:
    priors:
      "a/b": {sigma: 1}
`;

const issue = (pointer: string, extra: Partial<Issue> = {}): Issue => ({
  code: "SCHEMA_INVALID",
  severity: "error",
  pointer,
  message: "bad",
  ...extra,
});

describe("YamlLocator", () => {
  const loc = new YamlLocator(SPEC);

  it("maps a scalar value pointer to the value's range", () => {
    const { range, exact } = loc.locate("/model/use");
    expect(exact).toBe(true);
    expect(range.startLineNumber).toBe(15);
    expect(SPEC.split("\n")[14]?.slice(range.startColumn - 1, range.endColumn - 1)).toBe("mmm.bayesian@^1");
  });

  it("maps flow-style mapping values", () => {
    const { range, exact } = loc.locate("/data/split/after_step");
    expect(exact).toBe(true);
    expect(range.startLineNumber).toBe(6);
    expect(SPEC.split("\n")[5]?.slice(range.startColumn - 1, range.endColumn - 1)).toBe("weekly");
  });

  it("maps sequence items to the first line of the item", () => {
    const { range, exact } = loc.locate("/pipeline/1");
    expect(exact).toBe(true);
    expect(range.startLineNumber).toBe(9);
    expect(range.endLineNumber).toBe(9);
  });

  it("points at the key of a block collection", () => {
    const { range } = loc.locate("/pipeline/1/with");
    expect(range.startLineNumber).toBe(11);
    expect(SPEC.split("\n")[10]?.slice(range.startColumn - 1, range.endColumn - 1)).toBe("with");
  });

  it("decodes ~1 and ~0 escapes in pointers", () => {
    const { range, exact } = loc.locate("/model/settings/priors/a~1b");
    expect(exact).toBe(true);
    expect(range.startLineNumber).toBe(18);
  });

  it("falls back to the deepest existing node for missing keys", () => {
    const { range, exact } = loc.locate("/pipeline/1/with/missing_key");
    expect(exact).toBe(false);
    expect(range.startLineNumber).toBe(11);
  });

  it("falls back to the first line for an empty, root or invalid pointer", () => {
    expect(loc.locate("").range.startLineNumber).toBe(1);
    expect(loc.locate("no-slash").exact).toBe(false);
    expect(loc.locate("/pipeline/9").exact).toBe(false);
  });

  it("survives unparseable YAML", () => {
    const broken = new YamlLocator("a: [1, 2\nb: {");
    expect(broken.locate("/a/0").range.startLineNumber).toBeGreaterThanOrEqual(1);
  });
});

describe("locateIssues", () => {
  it("locates every issue and keeps the issue attached", () => {
    const located = locateIssues(SPEC, [issue("/pipeline/0/use"), issue("/kind", { severity: "warning" })]);
    expect(located).toHaveLength(2);
    expect(located[0]?.range.startLineNumber).toBe(8);
    expect(located[1]?.issue.severity).toBe("warning");
  });
});

describe("markerMessage", () => {
  it("includes the code and suggested action but never untrusted text", () => {
    const text = markerMessage(issue("/x", { code: "LEAKAGE_RISK", message: "m", suggested_action: "do x", untrusted_text: "<b>evil</b>" }));
    expect(text).toBe("LEAKAGE_RISK: m\nSuggested: do x");
  });
});
