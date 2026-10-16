/**
 * The promotion gate, explained before anyone presses "Promote" (plan §11 Guided task 7).
 *
 * A port of the scorecard part of `mapi_spec.scorecard.promotion_gate`: a `pass` scorecard is
 * allowed, a `warn` (or a missing scorecard) needs a project admin's override with a reason, a
 * `fail`, an interrupted (`partial`) fit or a `train_only` fit is blocked. The server decides;
 * this only lets the screen say what will happen. Input-schema breaks are known only to the
 * server for an existing alias and arrive in the `409` problem.
 */
import type { Scorecard } from "@/api/types";

import type { GateReason, PromotionGateDecision } from "./types";

export type GateOutcome = PromotionGateDecision["outcome"];

const RANK: Record<GateOutcome, number> = { allowed: 0, needs_override: 1, blocked: 2 };

export function previewGate(
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
  let outcome: GateOutcome = "allowed";
  for (const r of reasons) if (RANK[r.outcome] > RANK[outcome]) outcome = r.outcome;
  return { outcome, scorecard_overall: scorecard?.overall ?? null, reasons, failing_checks: failing };
}

/** What the gate means for this user, in one sentence. */
export function gateSentence(outcome: GateOutcome, isAdmin: boolean): string {
  if (outcome === "allowed") return "Every check passed, so this model can go live straight away.";
  if (outcome === "blocked") return "This model cannot go live: fix the failing checks and fit again.";
  return isAdmin
    ? "Some checks need a second look. As a project admin you can approve it anyway, with a short reason that is kept in the audit log."
    : "Some checks need a second look, so a project admin has to approve this model before it goes live.";
}
