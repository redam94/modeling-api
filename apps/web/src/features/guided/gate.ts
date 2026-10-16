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
  if (outcome === "allowed") return "Every check passed, so this model can go live straight away.";
  if (outcome === "blocked") return "This model cannot go live: fix the failing checks and fit again.";
  return isAdmin
    ? "Some checks need a second look. As a project admin you can approve it anyway, with a short reason that is kept in the audit log."
    : "Some checks need a second look, so a project admin has to approve this model before it goes live.";
}
