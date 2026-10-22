/**
 * Start here (v0.18 D22, §3.5): the top of Expert Home, and its slim cousin on the Guided
 * start page. A lazy chunk (`start-here-*`, ≤ 48 KiB): Home and Guided load it with
 * `import()`, so the entry, share links and the client portal never carry it.
 *
 * Expert Home, open: the page's one ink block (a greeting, the headline, the sample card with
 * the one lime action, and the hero art that assembles once), then the first-run checklist
 * beside the what-if playground (its own lazy `playground-*` chunk). Once the checklist is
 * done or hidden, or tips are off, the hub folds into a one-line banner that opens again on
 * a click. Without a signed-in tenant (the v0.2 profile) there is no progress to show, so
 * the hub shows the hero and the playground only.
 *
 * Guided (`StartHereStrip`): the ring, the next step and the sample card in one plain card,
 * hidden once the checklist is done, hidden or tips are off.
 */
import { ChevronDown, Compass, X } from "lucide-react";
import { lazy, Suspense, useId, useState } from "react";

import { HeroArt } from "@/components/illustrations/hero";
import { ProgressRing } from "@/components/ods/progress-ring";
import { useWorkspace } from "@/features/auth/workspace";
import { cn } from "@/lib/utils";

import { CHECKLIST_STEPS, checklistCount, checklistDone, useOnboarding, useOnboardingTenant, usePatchOnboarding, type OnboardingState } from "./api";
import { Checklist, CHECKLIST_COPY, nextStep, StepAction } from "./checklist";
import { BLOCK_FOCUS, LIME, PILL_SM, QUIET, SampleCard, UploadLink } from "./sample-card";
import { startTour } from "./use-onboarding";
import { useSample } from "./use-samples";

const Playground = lazy(() => import("./playground"));

/** The playground card's shape while its chunk loads (no motion). */
function PlaygroundSkeleton() {
  return (
    <div aria-hidden data-slot="playground-skeleton" className="grid min-h-96 content-start gap-4 rounded-2xl border bg-card p-7">
      <div className="h-7 w-3/5 rounded-full bg-muted" />
      <div className="h-4 w-4/5 rounded-full bg-muted" />
      <div className="mt-4 grid gap-5">
        <div className="h-2 w-full rounded-full bg-muted" />
        <div className="h-2 w-full rounded-full bg-muted" />
        <div className="h-2 w-full rounded-full bg-muted" />
      </div>
      <div className="mt-2 aspect-[8/3] w-full rounded-lg bg-muted/60" />
    </div>
  );
}

// ------------------------------------------------------------------------------ pure helpers

/** "Good morning" by the local clock. */
export function greeting(now: Date = new Date()): string {
  const h = now.getHours();
  return h < 5 ? "Hello" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** The first name to greet ("Ana Analyst" → "Ana"); `null` for an e-mail or an id. */
export function firstName(name: string | null | undefined): string | null {
  const first = (name ?? "").trim().split(/\s+/)[0] ?? "";
  if (!first || first.includes("@") || /\d/.test(first) || first.length > 24) return null;
  return first;
}

/** Fold the hub into a banner? (checklist done or hidden, or tips off). */
export function hubCollapsed(state: OnboardingState | undefined): boolean {
  return Boolean(state && (checklistDone(state) || state.tips_off));
}

// ------------------------------------------------------------------------------ Expert Home

export default function StartHere() {
  const tenant = useOnboardingTenant();
  const progress = useOnboarding();
  const patch = usePatchOnboarding();
  const [opened, setOpened] = useState(false);
  const state = progress.data;
  // wait for the progress before drawing (no flash of the full hub for someone who hid it)
  if (tenant !== null && progress.isPending) return null;
  const collapsed = hubCollapsed(state) && !opened;
  if (collapsed) return <Banner state={state} onOpen={() => setOpened(true)} />;

  const dismiss = () => {
    setOpened(false);
    if (state && !state.checklist_dismissed) patch.mutate({ patch: { checklist_dismissed: true } });
  };
  return <Hub state={tenant !== null ? state : undefined} onDismiss={dismiss} />;
}

function Hub({ state, onDismiss }: { state: OnboardingState | undefined; onDismiss: () => void }) {
  const { info } = useSample();
  const titleId = useId();
  return (
    <section data-testid="start-here" data-state="open" data-tour="home.start-here" aria-labelledby={titleId} className="grid gap-6">
      <Hero titleId={titleId} state={state} />
      <div className={cn("grid gap-6", state && "xl:grid-cols-[minmax(0,1fr)_minmax(0,1.55fr)]")}>
        {state ? <Checklist state={state} sample={info} surface="home" onDismiss={onDismiss} /> : null}
        <Suspense fallback={<PlaygroundSkeleton />}>
          <Playground />
        </Suspense>
      </div>
    </section>
  );
}

function Hero({ titleId, state }: { titleId: string; state: OnboardingState | undefined }) {
  const ws = useWorkspace();
  const { info, view } = useSample();
  const name = ws.mode === "session" ? firstName(ws.user.name) : null;
  const tourDone = Boolean(state?.checklist?.took_tour);
  return (
    <div className="grid items-center gap-6 overflow-hidden rounded-3xl bg-block p-[clamp(1.75rem,5vw,3.5rem)] text-block-foreground md:grid-cols-[minmax(0,1.25fr)_minmax(0,1fr)]">
      <div className="grid min-w-0 justify-items-start gap-5">
        <p className="font-medium text-block-muted">{name ? `${greeting()}, ${name}` : `${greeting()}!`}</p>
        <h2 id={titleId} className="display-title max-w-[14ch] text-[clamp(2.5rem,6vw,4.5rem)] leading-[0.98] text-balance">
          Find out what's really driving your numbers.
        </h2>
        <p className="max-w-[46ch] text-lg leading-relaxed text-block-muted">
          Open a real model on example data, move a few budgets and watch sales respond. No setup, and nothing you do here can break a thing.
        </p>
        {info && view !== "none" ? (
          <SampleCard info={info} variant="hero" secondary={<UploadLink onBlock />} className="mt-3 w-full" />
        ) : (
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button type="button" className={LIME} onClick={() => startTour("welcome")}>
              <Compass /> Take the 2-minute tour
            </button>
            <UploadLink onBlock />
          </div>
        )}
        {info && view !== "none" && !tourDone ? (
          <button type="button" onClick={() => startTour("welcome")} className={cn(BLOCK_FOCUS, "text-xs text-block-muted underline underline-offset-4 hover:text-block-foreground")}>
            New here? Take the 2-minute tour
          </button>
        ) : null}
      </div>
      <div className="order-first w-full max-w-[260px] justify-self-start md:order-none md:max-w-[380px] md:justify-self-end">
        <HeroArt className="w-full" />
      </div>
    </div>
  );
}

/** The folded hub: one line, the ring, and a way back in. */
function Banner({ state, onOpen }: { state: OnboardingState | undefined; onOpen: () => void }) {
  const total = CHECKLIST_STEPS.length;
  const done = checklistCount(state);
  const all = done >= total;
  return (
    <section
      data-testid="start-here"
      data-state="collapsed"
      data-tour="home.start-here"
      aria-label="Start here"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border bg-card px-4 py-3 text-card-foreground"
    >
      <ProgressRing value={done} max={total} label="Your first steps" showText={false} className="size-9" />
      <p className="min-w-0 flex-1 text-sm">
        <span className="font-semibold">{all ? "You've finished the checklist." : `Start here: ${done} of ${total} first steps done.`}</span>{" "}
        <span className="text-muted-foreground">The sample project, the tours and the playground are one click away.</span>
      </p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={cn(QUIET, PILL_SM)} onClick={() => startTour("welcome")}>
          <Compass /> Take a tour
        </button>
        <button type="button" className={cn(QUIET, PILL_SM)} onClick={onOpen} aria-expanded={false}>
          <ChevronDown /> Show
        </button>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------------------ Guided

export function StartHereStrip() {
  const tenant = useOnboardingTenant();
  const progress = useOnboarding();
  const patch = usePatchOnboarding();
  const { info, view } = useSample();
  const titleId = useId();
  const state = progress.data;
  if (tenant === null || !state || hubCollapsed(state)) return null;
  const total = CHECKLIST_STEPS.length;
  const done = checklistCount(state);
  const next = nextStep(state);
  const showSample = info !== null && view !== "none" && view !== "hidden" && !(view === "ready" && state.checklist?.open_sample);
  return (
    <section
      data-testid="start-here-strip"
      aria-labelledby={titleId}
      className="grid gap-5 rounded-2xl border bg-card p-5 text-card-foreground md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] md:p-6"
    >
      <div className="grid content-start gap-3">
        <div className="flex items-center gap-3">
          <ProgressRing value={done} max={total} label="Your first steps" className="size-12" />
          <div className="grid min-w-0 flex-1 gap-0.5">
            <h2 id={titleId} className="text-lg leading-tight font-bold">
              Start here
            </h2>
            <p className="text-xs text-muted-foreground">
              {done} of {total} first steps done
            </p>
          </div>
          <button
            type="button"
            onClick={() => patch.mutate({ patch: { checklist_dismissed: true } })}
            aria-label="Hide the first steps"
            title="Hide the first steps"
            className="focus-ring grid size-9 shrink-0 place-items-center self-start rounded-full text-muted-foreground hover:bg-muted hover:text-foreground md:hidden"
          >
            <X className="size-4" />
          </button>
        </div>
        {next ? (
          <div className="grid gap-2 rounded-lg bg-highlight-soft p-3">
            <p className="text-xs font-bold tracking-wide text-highlight-ink uppercase">Next up</p>
            <p className="font-semibold">{CHECKLIST_COPY[next].title}</p>
            <p className="text-xs text-muted-foreground">{CHECKLIST_COPY[next].desc}</p>
            <div className="flex flex-wrap gap-2 pt-1">
              <StepAction step={next} sample={info} surface="guided" />
            </div>
          </div>
        ) : null}
      </div>
      <div className="flex items-start gap-3">
        {showSample && info ? <SampleCard info={info} variant="strip" className="min-w-0 flex-1" /> : <div className="flex-1" />}
        <button
          type="button"
          onClick={() => patch.mutate({ patch: { checklist_dismissed: true } })}
          aria-label="Hide the first steps"
          title="Hide the first steps"
          className="focus-ring hidden size-9 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground md:grid"
        >
          <X className="size-4" />
        </button>
      </div>
    </section>
  );
}
