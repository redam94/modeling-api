/**
 * The sample project card (v0.18 D20, D22): one card per sample state, on the Home hero (the
 * ink block, with the page's one lime action) or in the Guided strip (a plain card).
 *
 * - not made yet: "Make the sample project" (or, for a viewer, who can make it);
 * - being made: the stages and the fit's progress, with "keep exploring" reassurance;
 * - ready: "Open the sample project" (switches to its project and opens the Guided report);
 * - failed: the plain reason and "Try again" while attempts are left;
 * - hidden: a quiet way to bring it back.
 *
 * The card carries the `sample.progress` tour anchor in every state (the "fit" tour points
 * at it to say how the sample's own fit went). Server text (title, summary, problem) is
 * rendered as React text.
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, EyeOff, RotateCcw, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import type { SampleInfo } from "./api";
import { SampleProgress } from "./sample-progress";
import { minutesWords, sampleView, useSampleActions, type SampleView } from "./use-samples";

// ------------------------------------------------------------------------------ Studio buttons

/** A Studio pill (44 px; 36 px small). */
export const PILL =
  "inline-flex min-h-11 items-center justify-center gap-2 rounded-full px-5 text-sm font-semibold whitespace-nowrap transition-[background-color,box-shadow] disabled:pointer-events-none disabled:opacity-60 [&_svg]:size-4 [&_svg]:shrink-0";
export const PILL_SM = "min-h-9 px-3.5 text-xs";
/** Focus on the ink block: a lime outline (the indigo ring would vanish on ink). */
export const BLOCK_FOCUS = "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-highlight";
/** The one lime call to action on a block. */
export const LIME = cn(PILL, BLOCK_FOCUS, "bg-highlight text-highlight-foreground hover:shadow-[inset_0_0_0_2px_var(--highlight-foreground)]");
/** A secondary action on the block: an outline in the block's ink. */
export const GHOST_ON_BLOCK = cn(PILL, BLOCK_FOCUS, "text-block-foreground shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised");
/** The primary action on a card. */
export const PRIMARY = cn(PILL, "focus-ring bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-active");
/** A quiet action on a card. */
export const QUIET = cn(PILL, "focus-ring bg-muted text-foreground hover:bg-surface-hover");

// ------------------------------------------------------------------------------ the card

export type SampleCardVariant = "hero" | "strip";

export interface SampleCardProps {
  info: SampleInfo;
  variant: SampleCardVariant;
  /** A second action beside the main one (Home: "Upload your own data"). */
  secondary?: ReactNode;
  className?: string;
}

export function SampleCard({ info, variant, secondary, className }: SampleCardProps) {
  const view = sampleView(info);
  const actions = useSampleActions();
  const hero = variant === "hero";
  if (view === "none") return null;

  const note = (children: ReactNode) => <p className={cn("text-xs leading-relaxed", hero ? "text-block-muted" : "text-muted-foreground")}>{children}</p>;
  const main = hero ? LIME : PRIMARY;

  let body: ReactNode;
  switch (view) {
    case "hidden":
      body = (
        <button type="button" className={cn("w-fit text-xs underline underline-offset-4", hero ? cn(BLOCK_FOCUS, "text-block-muted hover:text-block-foreground") : "focus-ring text-muted-foreground hover:text-foreground")} onClick={() => actions.setHidden(info, false)}>
          Show the sample project again
        </button>
      );
      break;
    case "offer":
      body = (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className={main} disabled={actions.pending} onClick={() => actions.start(info)}>
              <Sparkles /> Make the sample project
            </button>
            {secondary}
          </div>
          {note(
            <>
              {info.summary} It's a real fit on our servers, and nothing in your own projects changes.
            </>,
          )}
        </>
      );
      break;
    case "ask":
      body = (
        <>
          {secondary ? <div className="flex flex-wrap items-center gap-3">{secondary}</div> : null}
          {note(<>{info.summary} An editor in your team can make it with one click on their Home page. Once it is ready, it shows up here for you too.</>)}
        </>
      );
      break;
    case "working":
      body = (
        <>
          <SampleProgress info={info} tone={hero ? "block" : "card"} className="max-w-md" />
          {note(<>Your sample is a real fit, so it takes {minutesWords(info.minutes)}. Feel free to keep exploring, we'll keep going in the background.</>)}
        </>
      );
      break;
    case "ready":
      body = (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className={main} onClick={() => actions.open(info, hero ? "home" : "guided")}>
              Open the sample project <ArrowRight />
            </button>
            {secondary}
          </div>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            {note(info.summary)}
            <button
              type="button"
              className={cn("inline-flex items-center gap-1 text-xs underline-offset-4 hover:underline", hero ? cn(BLOCK_FOCUS, "text-block-muted hover:text-block-foreground") : "focus-ring text-muted-foreground hover:text-foreground")}
              onClick={() => actions.setHidden(info, true)}
            >
              <EyeOff className="size-3.5" aria-hidden /> Hide it
            </button>
          </div>
        </>
      );
      break;
    case "failed":
      body = (
        <>
          <p role="alert" className={cn("text-sm font-semibold", hero ? "text-block-foreground" : "text-foreground")}>
            {info.problem?.title ?? "The sample fit did not finish."}
          </p>
          {info.problem?.detail ? note(info.problem.detail) : null}
          <div className="flex flex-wrap items-center gap-3">
            {info.can_retry ? (
              <button type="button" className={main} disabled={actions.pending} onClick={() => actions.start(info)}>
                <RotateCcw /> Try again
              </button>
            ) : (
              note("An editor in your team can try again.")
            )}
            {secondary}
          </div>
        </>
      );
      break;
  }

  return (
    <div data-testid="sample-card" data-tour="sample.progress" data-state={view satisfies SampleView} data-variant={variant} className={cn("grid gap-3", className)}>
      {!hero ? <p className="text-sm font-semibold">{info.title}</p> : null}
      {body}
      {view === "working" && secondary ? <div className="flex flex-wrap gap-3">{secondary}</div> : null}
    </div>
  );
}

/** "Upload your own data", as a link styled for the block or a card. */
export function UploadLink({ onBlock, to = "/datasets" }: { onBlock: boolean; to?: "/datasets" | "/guided" }) {
  return (
    <Link to={to} className={onBlock ? GHOST_ON_BLOCK : QUIET}>
      Upload your own data
    </Link>
  );
}
