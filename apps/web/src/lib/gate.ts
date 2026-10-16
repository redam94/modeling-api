/**
 * The endpoint promotion gate in words (`mapi_spec.scorecard.promotion_gate`). The server
 * decides; the app previews the decision from a version's scorecard and reads the server's
 * reasons back out of a `409 OVERRIDE_REQUIRED` / `PROMOTION_BLOCKED` problem.
 */
import type { GateOutcome, GateReason, ModelVersion, Problem, PromotionGateDecision } from "@/api/types";

export const GATE_META: Record<GateOutcome, { label: string; variant: "success" | "warning" | "danger"; description: string }> = {
  allowed: {
    label: "Passes the gate",
    variant: "success",
    description: "The sanity scorecard passes: any project admin (or an endpoints:promote token) can promote it.",
  },
  needs_override: {
    label: "Needs an override",
    variant: "warning",
    description: "A project admin signed in to the app must override the gate with a reason; the override is audited.",
  },
  blocked: {
    label: "Blocked",
    variant: "danger",
    description: "The version cannot serve this endpoint. Fix the cause and promote a new version.",
  },
};

export const GATE_REASON_LABEL: Record<GateReason["code"], string> = {
  SCORECARD_WARN: "Scorecard warning",
  SCORECARD_FAIL: "Scorecard failure",
  NO_SCORECARD: "No scorecard",
  TRAIN_ONLY: "Fitted without the final refit",
  PARTIAL_MODEL: "Interrupted fit",
  SCHEMA_CHANGE: "Input contract changed",
};

const RANK: Record<GateOutcome, number> = { allowed: 0, needs_override: 1, blocked: 2 };

/** The stricter of two outcomes. */
export function worstOutcome(a: GateOutcome, b: GateOutcome): GateOutcome {
  return RANK[a] >= RANK[b] ? a : b;
}

/**
 * What the gate will most likely say about `mv`, from what the app can see (the scorecard
 * and the partial flag). The server also checks the fit mode and input-schema breaks, so a
 * preview of `allowed` can still come back `blocked`.
 */
export function previewGate(mv: Pick<ModelVersion, "scorecard" | "status">): PromotionGateDecision {
  const reasons: GateReason[] = [];
  if (mv.status === "partial") {
    reasons.push({ code: "PARTIAL_MODEL", outcome: "blocked", message_plain: "The fit was interrupted before it finished." });
  }
  const card = mv.scorecard ?? null;
  if (!card) {
    reasons.push({ code: "NO_SCORECARD", outcome: "needs_override", message_plain: "This version has no sanity scorecard, so nothing vouches for it." });
  } else {
    for (const item of card.items ?? []) {
      if (item.status === "pass") continue;
      reasons.push({
        code: item.status === "fail" ? "SCORECARD_FAIL" : "SCORECARD_WARN",
        outcome: item.status === "fail" ? "blocked" : "needs_override",
        message_plain: item.message_plain,
        check: item.check ?? item.code,
      });
    }
    if (card.overall !== "pass" && !reasons.some((r) => r.code === "SCORECARD_WARN" || r.code === "SCORECARD_FAIL")) {
      reasons.push({
        code: card.overall === "fail" ? "SCORECARD_FAIL" : "SCORECARD_WARN",
        outcome: card.overall === "fail" ? "blocked" : "needs_override",
        message_plain: `The scorecard's overall verdict is ${card.overall}.`,
      });
    }
  }
  const outcome = reasons.reduce<GateOutcome>((o, r) => worstOutcome(o, r.outcome), "allowed");
  return {
    outcome,
    reasons,
    scorecard_overall: card?.overall ?? null,
    failing_checks: reasons.map((r) => r.check).filter((c): c is string => typeof c === "string"),
  };
}

const REASON_CODES = new Set<string>(Object.keys(GATE_REASON_LABEL));

/**
 * The gate decision a refused promotion carries: `409 OVERRIDE_REQUIRED` (needs an override)
 * or `PROMOTION_BLOCKED`, one issue per reason with `context.gate_code` and `context.check`.
 * `null` for any other problem.
 */
export function gateFromProblem(problem: Problem | null | undefined): PromotionGateDecision | null {
  if (!problem || (problem.code !== "OVERRIDE_REQUIRED" && problem.code !== "PROMOTION_BLOCKED")) return null;
  const blocked = problem.code === "PROMOTION_BLOCKED";
  const reasons: GateReason[] = (problem.errors ?? []).map((issue) => {
    const ctx = issue.context ?? {};
    const raw = typeof ctx.gate_code === "string" ? ctx.gate_code : "";
    const code = (REASON_CODES.has(raw) ? raw : blocked ? "SCORECARD_FAIL" : "SCORECARD_WARN") as GateReason["code"];
    return {
      code,
      outcome: issue.severity === "warning" ? "needs_override" : blocked ? "blocked" : "needs_override",
      message_plain: issue.message,
      check: typeof ctx.check === "string" ? ctx.check : null,
    };
  });
  if (!reasons.length) {
    reasons.push({
      code: blocked ? "SCORECARD_FAIL" : "SCORECARD_WARN",
      outcome: blocked ? "blocked" : "needs_override",
      message_plain: problem.detail ?? problem.title,
    });
  }
  return {
    outcome: blocked ? "blocked" : "needs_override",
    reasons,
    failing_checks: reasons.map((r) => r.check).filter((c): c is string => typeof c === "string"),
  };
}
