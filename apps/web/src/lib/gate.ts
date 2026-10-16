/**
 * The endpoint promotion gate in words (`mapi_spec.scorecard.promotion_gate`). The server
 * decides; the app previews the decision from a version's scorecard (the Integrations hub and
 * Guided mode's Share step use the same preview) and reads the server's reasons back out of a
 * `409 OVERRIDE_REQUIRED` / `PROMOTION_BLOCKED` problem.
 */
import type { GateOutcome, GateReason, ModelVersion, Problem, PromotionGateDecision, Scorecard } from "@/api/types";

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
 * A port of the scorecard part of `mapi_spec.scorecard.promotion_gate` (same reasons, same
 * words): an interrupted (`partial`) fit or a `train_only` fit is blocked; no scorecard needs an
 * override; a `fail` item blocks, a `warn` item needs a project admin's override with a reason.
 * Input-schema breaks are known only to the server for an existing alias and arrive in the
 * `409` problem, so a preview of `allowed` can still come back refused.
 */
export function previewScorecardGate(
  scorecard: Scorecard | null | undefined,
  opts: { partial?: boolean; fitMode?: string | null } = {},
): PromotionGateDecision {
  const reasons: GateReason[] = [];
  const add = (code: GateReason["code"], outcome: GateOutcome, message_plain: string, check: string | null = null) =>
    reasons.push({ code, outcome, message_plain: message_plain.slice(0, 500), check });
  if (opts.partial) add("PARTIAL_MODEL", "blocked", "The fit was interrupted before it finished.");
  if (opts.fitMode === "train_only") add("TRAIN_ONLY", "blocked", "The model was fitted without its holdout data; endpoints need a refit on all data.");
  const failing: string[] = [];
  if (!scorecard) {
    add("NO_SCORECARD", "needs_override", "The model has no sanity scorecard.");
  } else {
    for (const item of scorecard.items ?? []) {
      if (item.status === "pass") continue;
      failing.push(item.check ?? item.code);
      add(item.status === "fail" ? "SCORECARD_FAIL" : "SCORECARD_WARN", item.status === "fail" ? "blocked" : "needs_override", `${item.title}: ${item.message_plain}`, item.check ?? null);
    }
    if (scorecard.overall !== "pass" && !failing.length) {
      add(scorecard.overall === "fail" ? "SCORECARD_FAIL" : "SCORECARD_WARN", scorecard.overall === "fail" ? "blocked" : "needs_override", "The scorecard did not pass.");
    }
  }
  const outcome = reasons.reduce<GateOutcome>((o, r) => worstOutcome(o, r.outcome), "allowed");
  return { outcome, scorecard_overall: scorecard?.overall ?? null, reasons, failing_checks: failing };
}

/** {@link previewScorecardGate} for a model version (its scorecard and partial flag). */
export function previewGate(mv: Pick<ModelVersion, "scorecard" | "status">, opts: { fitMode?: string | null } = {}): PromotionGateDecision {
  return previewScorecardGate(mv.scorecard, { partial: mv.status === "partial", fitMode: opts.fitMode ?? null });
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
