/**
 * The promotion gate, explained before anyone presses "Promote" (plan §11 Guided task 7).
 *
 * The preview is `previewScorecardGate` of `@/lib/gate` (a port of the scorecard part of
 * `mapi_spec.scorecard.promotion_gate`); this module adds the sentence Guided mode shows. The
 * server decides; this only lets the screen say what will happen.
 */
import type { PromotionGateDecision } from "./types";

export type GateOutcome = PromotionGateDecision["outcome"];

/** The preview the Integrations hub shows too (`@/lib/gate`). */
export { previewScorecardGate as previewGate } from "@/lib/gate";

/** What the gate means for this user, in one sentence. */
export function gateSentence(outcome: GateOutcome, isAdmin: boolean): string {
  if (outcome === "allowed") return "All checks passed. This model can go live now.";
  if (outcome === "blocked") return "This model cannot go live because some checks failed. Correct the failed checks and fit the model again.";
  return isAdmin
    ? "Some checks need a second examination. As a project admin, you can approve the model. Give a short reason. The audit log keeps the reason."
    : "Some checks need a second examination. A project admin must approve this model before it can go live.";
}
