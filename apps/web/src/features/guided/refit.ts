/**
 * "Fit again, more carefully": what a Guided user can do when the scorecard says the model
 * did not settle (the convergence checks). Guided screens do not show the fit settings, so the
 * remedy the checks suggest - more tuning and draws, a higher `target_accept` - is one button.
 */

/** Ceilings that keep a careful refit within the template's time budget. */
export const MAX_CAREFUL = { tune: 4000, draws: 4000, targetAccept: 0.99 } as const;

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** `model.fit` for a more careful refit: twice the tuning and draws (at least 1000 each, at
 * most {@link MAX_CAREFUL}) and a higher `target_accept` (0.95, then 0.98, then 0.99). Every
 * other setting (mode, chains, seed, engine) is kept. */
export function carefulFit(fit: unknown): Record<string, unknown> {
  const f: Record<string, unknown> = typeof fit === "object" && fit !== null && !Array.isArray(fit) ? { ...(fit as Record<string, unknown>) } : {};
  const tune = num(f.tune) ?? 500;
  const draws = num(f.draws) ?? 500;
  const ta = num(f.target_accept) ?? 0.8;
  f.tune = Math.min(MAX_CAREFUL.tune, Math.max(1000, tune * 2));
  f.draws = Math.min(MAX_CAREFUL.draws, Math.max(1000, draws * 2));
  f.target_accept = ta < 0.95 ? 0.95 : ta < 0.98 ? 0.98 : MAX_CAREFUL.targetAccept;
  return f;
}

/** Whether a careful refit would change anything (false once every ceiling is reached). */
export function canFitMoreCarefully(fit: unknown): boolean {
  return JSON.stringify(carefulFit(fit)) !== JSON.stringify(fit ?? {});
}

/** The document with its fit made more careful (a copy). */
export function withCarefulFit<T extends Record<string, unknown>>(document: T): T {
  const model = (document.model ?? {}) as Record<string, unknown>;
  return { ...document, model: { ...model, fit: carefulFit(model.fit) } };
}
