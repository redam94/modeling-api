import { describe, expect, it } from "vitest";
import { parse } from "yaml";

import { STARTER_SPEC } from "@/features/editor/starter";

import { majorRange, specStarterFor } from "./spec-starter";

type Doc = {
  kind: string;
  data: { bind: { dims: Record<string, string>; vars: Record<string, string> }; split: { dim: string } };
  model: { use: string; settings: Record<string, unknown> };
  outputs: { derived: string[]; plots: string[] };
};

describe("spec starters for the marketing-mix models", () => {
  it("keeps the geo starter for mmm.bayesian", () => {
    expect(specStarterFor("mmm.bayesian")).toBe(STARTER_SPEC);
    expect(majorRange("2.3.0")).toBe("^2");
  });

  it("binds a mediator dim and variable for mmm.nested", () => {
    const doc = parse(specStarterFor("mmm.nested", { version: "1.0.0" })) as Doc;
    expect(doc.kind).toBe("ModelSpec");
    expect(doc.model.use).toBe("mmm.nested@^1");
    expect(Object.keys(doc.data.bind.dims).sort()).toEqual(["channel", "mediator", "time"]);
    expect(Object.keys(doc.data.bind.vars).sort()).toEqual(["mediators", "spend", "y"]);
    expect(doc.data.split.dim).toBe("time");
    expect(doc.model.settings.mediators).toEqual([]);
    expect(doc.outputs.derived).toEqual(["contributions", "response_curves", "marginal_cost", "adstock", "mediation"]);
    expect(doc.outputs.plots).toContain("mmm.mediation");
    expect(doc.outputs.plots).toContain("mmm.roi");
  });

  it("binds an outcome dim with y over it for mmm.multivariate", () => {
    const doc = parse(specStarterFor("mmm.multivariate", { version: "1.0.0" })) as Doc;
    expect(doc.model.use).toBe("mmm.multivariate@^1");
    expect(Object.keys(doc.data.bind.dims).sort()).toEqual(["channel", "outcome", "time"]);
    expect(Object.keys(doc.data.bind.vars).sort()).toEqual(["spend", "y"]);
    expect(doc.model.settings).toMatchObject({ outcomes: [], cross_effects: [] });
    // no cross-effects set, so no cross_effects output and no plot that is always skipped
    expect(doc.outputs.derived).not.toContain("cross_effects");
    expect(doc.outputs.plots).not.toContain("mmm.cross_effects");
    expect(doc.outputs.plots).toContain("mmm.decomposition");
    expect(doc.outputs.plots).not.toContain("mmm.mediation");
  });
});

describe("spec starters for the causal and state-space models", () => {
  const load = (id: string) => parse(specStarterFor(id, { version: "1.0.0" })) as Doc & { model: { fit: Record<string, unknown> } };

  it("binds the t dim and a seasonality for ts.bsts", () => {
    const doc = load("ts.bsts");
    expect(doc.model.use).toBe("ts.bsts@^1");
    expect(doc.data.bind.dims).toEqual({ t: "date" });
    expect(Object.keys(doc.data.bind.vars)).toEqual(["y"]);
    expect(doc.data.split.dim).toBe("t");
    expect(doc.model.settings).toMatchObject({ trend: "local_linear", regressors: [] });
    expect(doc.outputs.plots).toContain("core.decomposition");
  });

  it("gives causal.impact a start date and no holdout", () => {
    const doc = load("causal.impact");
    expect(doc.model.use).toBe("causal.impact@^1");
    expect(doc.data.bind.dims).toEqual({ time: "week" });
    expect(doc.data).not.toHaveProperty("split");
    expect(doc.model.settings.intervention_start).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(doc.model.settings.controls).toEqual([]);
    expect(doc.model.fit.mode).toBe("train_only");
    expect(doc.outputs.plots[0]).toBe("causal.impact");
  });

  it("binds a unit dim and the treated markets for causal.synthetic_control", () => {
    const doc = load("causal.synthetic_control");
    expect(Object.keys(doc.data.bind.dims).sort()).toEqual(["time", "unit"]);
    expect(doc.model.settings).toMatchObject({ treated: ["geo_03"] });
    expect(doc.model.settings.intervention_start).toBeTruthy();
    expect(doc.model.settings.intervention_end).toBeTruthy();
    expect(doc.outputs.plots).toContain("causal.donor_weights");
  });

  it("binds the treatment column for causal.did", () => {
    const doc = load("causal.did");
    expect(Object.keys(doc.data.bind.dims).sort()).toEqual(["time", "unit"]);
    expect(Object.keys(doc.data.bind.vars).sort()).toEqual(["treatment", "y"]);
    expect(doc.model.settings).toMatchObject({ design: "event_study" });
    expect(doc.model.settings).not.toHaveProperty("intervention_start");
    expect(doc.outputs.plots[0]).toBe("causal.event_study");
  });
});
