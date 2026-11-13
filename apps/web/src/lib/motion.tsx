/**
 * The animation library, for the few things CSS cannot do well (D27): the tour coach-mark spring
 * and re-anchoring, the checklist reorder and the success burst.
 *
 * Import this module ONLY from lazily loaded, signed-in chunks (`tour-host-*`, `onboarding-*`).
 * The bundle budget forbids `motion` and this file in the entry closure and in the share and
 * client chunks; everything ambient stays CSS keyframes in app.css (no JS bytes).
 *
 * `MotionRoot` loads the small `domAnimation` feature set behind `LazyMotion` (strict: only the
 * lightweight `m.*` components are allowed under it, never the full `motion.*` ones) and follows
 * the user's reduced-motion setting, so springs and tweens jump to their end state there.
 */
import { domAnimation, LazyMotion, MotionConfig } from "motion/react";
import type * as React from "react";

export { AnimatePresence, m, useReducedMotion } from "motion/react";

/** The Studio spring: a quick settle with a small overshoot (the coach-mark's CSS curve, as a spring). */
export const STUDIO_SPRING = { type: "spring", stiffness: 420, damping: 32, mass: 0.8 } as const;

export function MotionRoot({ children }: { children: React.ReactNode }) {
  return (
    <LazyMotion features={domAnimation} strict>
      <MotionConfig reducedMotion="user">{children}</MotionConfig>
    </LazyMotion>
  );
}
