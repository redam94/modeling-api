/**
 * The state chip of an uploaded version (D20, I-8): the relaxed words of `STATE_LABEL` on the
 * run-state tones (coral `danger` for refusals), a live dot while the checks run, and "Only you
 * can use this" beside a private version.
 */
import { Lock } from "lucide-react";

import { TONE_CLASSES } from "@/lib/run-state";
import { cn } from "@/lib/utils";

import { ONLY_YOU_LABEL, STATE_DESCRIPTION, STATE_LABEL, STATE_TONE } from "./states";
import type { PluginScope, PluginVersionState } from "./types";

export function PluginStateChip({ state, className }: { state: PluginVersionState; className?: string }) {
  const tone = STATE_TONE[state];
  return (
    <span
      data-testid="plugin-state-chip"
      data-state={state}
      title={STATE_DESCRIPTION[state]}
      className={cn("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap", TONE_CLASSES[tone], className)}
    >
      <span
        aria-hidden
        className={cn("size-1.5 rounded-full bg-current", tone === "active" && "animate-pulse shadow-[0_0_0_3px_color-mix(in_oklab,currentColor_22%,transparent)]")}
      />
      {STATE_LABEL[state]}
    </span>
  );
}

/**
 * A private version (scope `private`): "Only you can use this" for its uploader, "Only the
 * uploader can use this" for everyone else; nothing for other scopes.
 */
export function OnlyYouChip({ scope, mine, className }: { scope: PluginScope; mine: boolean; className?: string }) {
  if (scope !== "private") return null;
  return (
    <span data-testid="plugin-only-you" className={cn("inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap text-muted-foreground", className)}>
      <Lock aria-hidden className="size-3" />
      {mine ? ONLY_YOU_LABEL : "Only the uploader can use this"}
    </span>
  );
}
