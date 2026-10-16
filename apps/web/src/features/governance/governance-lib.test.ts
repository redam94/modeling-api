import { describe, expect, it } from "vitest";

import { gateFromProblem } from "@/lib/gate";
import generated from "@/mocks/workflows.gen.json";

import { nullDistributionPlot, sortResults, thresholdText } from "./evidence";
import { thresholdProblem } from "./policy-editor";
import { nextEvents, TRANSITIONS } from "./status";
import type { CheckResult, GovernanceCheckDef } from "./types";

const checks = new Map((generated.checks as unknown as GovernanceCheckDef[]).map((c) => [c.id, c]));

describe("governance helpers", () => {
  it("mirror the policy threshold rule (fail at least as strict as warn)", () => {
    const whiteness = checks.get("residuals.whiteness")!; // higher p is better: direction min
    const permutation = checks.get("null.permutation_target")!; // lower p is better: direction max
    expect(whiteness.direction).toBe("min");
    expect(thresholdProblem(whiteness, 0.05, 0.01)).toBeNull();
    expect(thresholdProblem(whiteness, 0.05, 0.2)).toMatch(/at most the warn limit/);
    expect(thresholdProblem(permutation, 0.05, 0.1)).toBeNull();
    expect(thresholdProblem(permutation, 0.1, 0.05)).toMatch(/at least the warn limit/);
    expect(thresholdProblem(permutation, Number.NaN, 0.05)).toBe("Both limits must be numbers.");
    expect(thresholdText(whiteness, 0.05)).toBe("passes at 0.05 or more");
    expect(thresholdText(permutation, 0.05)).toBe("passes at 0.05 or less");
    expect(thresholdText(checks.get("recovery.simulated"), 0.1)).toBe("passes within 0.1 of 0.9");
  });

  it("draw the shuffled-result scores with the real one marked", () => {
    const permutation = (generated.validation_passed as unknown as CheckResult[]).find((r) => r.check === "null.permutation_target")!;
    const item = permutation.evidence!.find((e) => e.name === "permutation_scores")!;
    const plot = nullDistributionPlot(item.inline)!;
    expect(plot.datasets.shuffled).toHaveLength(19);
    expect(plot.datasets.real).toEqual([{ score: (item.inline as { real: number }).real }]);
    expect(JSON.stringify(plot.spec)).toContain('"text":{"value":"Your model"}');
    expect(nullDistributionPlot({ real: 1, shuffled: [2] })).toBeNull();
    expect(nullDistributionPlot("nope")).toBeNull();
  });

  it("order results failures first", () => {
    const results = generated.validation_failed as unknown as CheckResult[];
    expect(sortResults(results).map((r) => r.status).slice(0, 3)).toEqual(["fail", "fail", "fail"]);
  });

  it("follow the validation state machine", () => {
    expect(nextEvents("awaiting_approval").sort()).toEqual(["approve", "reject"]);
    expect(nextEvents("validated")).toEqual(["deprecate"]);
    expect(TRANSITIONS["rejected:resubmit"]).toBe("checks_running");
  });

  it("read NOT_VALIDATED refusals as a blocked gate", () => {
    const message = "The governance policy requires a validated model; this version is awaiting approval. Submit it for validation first.";
    const decision = gateFromProblem({
      type: "urn:mapi:error:NOT_VALIDATED",
      title: "Conflict",
      status: 409,
      code: "NOT_VALIDATED",
      detail: message,
      errors: [{ code: "NOT_VALIDATED", pointer: "", message, severity: "error", context: { gate_code: "NOT_VALIDATED", check: null } }],
    });
    expect(decision).toMatchObject({ outcome: "blocked", reasons: [{ code: "NOT_VALIDATED", outcome: "blocked", message_plain: message }] });
    expect(gateFromProblem({ type: "x", title: "Conflict", status: 409, code: "NOT_VALIDATED", detail: "rollback", errors: [] })?.reasons?.[0]?.code).toBe("NOT_VALIDATED");
  });
});
