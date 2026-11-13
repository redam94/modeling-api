/**
 * Query keys of onboarding progress and sample projects (v0.18 D19, D20).
 *
 * Both are tenant routes (`/v1/tenants/{t}/me/onboarding`, `/v1/tenants/{t}/samples`), so
 * the keys sit under `["tenants", t]` like `qk`'s tenant keys: switching projects inside a
 * tenant keeps the progress, switching tenants never mixes it. Imports nothing at runtime.
 */
export const ok = {
  /** Every onboarding query of tenant `t`. */
  tenant: (t: string) => ["tenants", t, "onboarding-hub"] as const,
  /** The caller's checklist and tour progress in tenant `t`. */
  state: (t: string) => ["tenants", t, "onboarding-hub", "state"] as const,
  /** The sample projects of tenant `t` as the caller sees them. */
  samples: (t: string) => ["tenants", t, "onboarding-hub", "samples"] as const,
};
