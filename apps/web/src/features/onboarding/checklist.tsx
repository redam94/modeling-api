/**
 * The first-run checklist (v0.18 D19, D22, I-3, I-8): eight small steps with a progress ring.
 * Steps tick themselves from onboarding events (the shell's tour host listens and PATCHes);
 * a completed step never comes back. The current step (the first one not done, or the one
 * the user opened) sits on the lime tint with its "go" action; the others open on a click.
 *
 * Studio look: a white card, the ring in the primary ink, done steps struck through with a
 * tick that pops in (CSS keyframes; still under reduced motion). Copy is the relaxed voice.
 */
import { Link } from "@tanstack/react-router";
import { Check, X } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { ProgressRing } from "@/components/ods/progress-ring";
import { cn } from "@/lib/utils";

import { CHECKLIST_STEPS, checklistCount, isStepDone, type ChecklistStep, type OnboardingState, type SampleInfo, type TourId } from "./api";
import { PILL_SM, PRIMARY, QUIET } from "./sample-card";
import { startTour } from "./use-onboarding";
import { sampleView, useSampleActions } from "./use-samples";

export interface StepCopy {
  title: string;
  desc: string;
}

/** What each step says (relaxed voice: onboarding copy, plan D28). */
export const CHECKLIST_COPY: Readonly<Record<ChecklistStep, StepCopy>> = {
  took_tour: { title: "Take the 2-minute tour", desc: "A quick look at where everything lives." },
  open_sample: { title: "Open the sample project", desc: "A real marketing mix model, fitted on example data and ready to explore." },
  fit_model: { title: "Fit a model", desc: "Watch a model learn from data. A fit of your own counts, and so does the sample's." },
  read_results: { title: "See what drove sales", desc: "Which channels earned their budget, and which did not." },
  try_whatif: { title: "Move a budget slider", desc: "Push a channel past its sweet spot and watch sales respond." },
  make_dashboard: { title: "Make a dashboard", desc: "Turn a model into a readout your stakeholders can read and play with." },
  own_data: { title: "Bring your own data", desc: "Upload a CSV with weekly sales and spend. We help you map the columns." },
  make_your_own: { title: "Make your first model recipe", desc: "Build a model your way, then reuse it with your team. No code needed." },
};

/** The step to open by default: the first one not done (`null` when all are). */
export function nextStep(state: OnboardingState | undefined): ChecklistStep | null {
  return CHECKLIST_STEPS.find((s) => !isStepDone(state, s)) ?? null;
}

/** "3 of 8 done. Nice going!" */
export function summaryWords(done: number, total: number): string {
  if (done >= total) return "All done. You're off and running!";
  if (done === 0) return `0 of ${total} done. Start anywhere, they tick themselves.`;
  if (done >= total - 2) return `${done} of ${total} done. Nearly there!`;
  return `${done} of ${total} done. Nice going!`;
}

export interface ChecklistProps {
  state: OnboardingState | undefined;
  sample: SampleInfo | null;
  /** Where the step actions point: Expert Home (with the playground) or the Guided start page. */
  surface: "home" | "guided";
  onDismiss: () => void;
  className?: string;
}

export function Checklist({ state, sample, surface, onDismiss, className }: ChecklistProps) {
  const titleId = useId();
  const total = CHECKLIST_STEPS.length;
  const done = checklistCount(state);
  const [picked, setPicked] = useState<ChecklistStep | null>(null);
  const open = picked && !isStepDone(state, picked) ? picked : nextStep(state);
  return (
    <section data-testid="checklist" data-tour="home.checklist" aria-labelledby={titleId} className={cn("grid content-start gap-5 rounded-2xl border bg-card p-7 text-card-foreground max-sm:p-5", className)}>
      <header className="flex items-center gap-4">
        <ProgressRing value={done} max={total} label="Your first steps" className={cn(done >= total && "motion-safe:animate-[mapi-pop_360ms_cubic-bezier(0.3,1.6,0.5,1)]")} />
        <div className="grid min-w-0 flex-1 gap-1">
          <h2 id={titleId} className="text-xl leading-tight font-bold">
            Your first week here
          </h2>
          <p className="text-xs text-muted-foreground">{summaryWords(done, total)}</p>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Hide the checklist"
          title="Hide the checklist"
          className="focus-ring grid size-9 shrink-0 place-items-center self-start rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </header>
      <ol className="grid gap-1.5" aria-label="Checklist">
        {CHECKLIST_STEPS.map((step) => (
          <ChecklistItem
            key={step}
            step={step}
            done={isStepDone(state, step)}
            current={open === step}
            onOpen={() => setPicked(step)}
            action={<StepAction step={step} sample={sample} surface={surface} />}
          />
        ))}
      </ol>
      {done >= total ? (
        <button type="button" className={cn(QUIET, PILL_SM, "w-fit")} onClick={onDismiss}>
          Tidy this away
        </button>
      ) : null}
    </section>
  );
}

function ChecklistItem({ step, done, current, onOpen, action }: { step: ChecklistStep; done: boolean; current: boolean; onOpen: () => void; action: ReactNode }) {
  const copy = CHECKLIST_COPY[step];
  const descId = useId();
  return (
    <li
      data-testid={`checklist-item-${step}`}
      data-done={done ? "true" : "false"}
      aria-current={current ? "step" : undefined}
      className={cn("grid grid-cols-[2rem_minmax(0,1fr)] items-start gap-3 rounded-lg p-3", current && "bg-highlight-soft")}
    >
      <span
        aria-hidden
        className={cn(
          "mt-px grid size-7 place-items-center rounded-full border-2",
          done ? "border-primary bg-primary text-primary-foreground" : current ? "border-highlight-ink bg-card" : "border-input bg-card",
        )}
      >
        {done ? <Check className="size-3.5 animate-[mapi-pop_360ms_cubic-bezier(0.3,1.6,0.5,1)]" strokeWidth={3} /> : null}
      </span>
      <div className="grid min-w-0 gap-0.5">
        {done || current ? (
          <p className={cn("font-semibold", done && "text-muted-foreground line-through decoration-[1.5px]")}>
            {copy.title}
            <span className="sr-only">{done ? ": done" : ": next up"}</span>
          </p>
        ) : (
          <button type="button" onClick={onOpen} aria-describedby={descId} className="focus-ring w-fit rounded-sm text-left font-semibold hover:underline hover:underline-offset-4">
            {copy.title}
            <span className="sr-only">: not done yet</span>
          </button>
        )}
        <p id={descId} className="text-xs text-muted-foreground">
          {copy.desc}
        </p>
        {current && !done ? <div className="mt-2.5 flex flex-wrap gap-2">{action}</div> : null}
      </div>
    </li>
  );
}

// ------------------------------------------------------------------------------ step actions

const GO = cn(PRIMARY, PILL_SM);
const NOTE = "text-xs text-muted-foreground";

function TourButton({ id, children }: { id: TourId; children: ReactNode }) {
  return (
    <button type="button" className={GO} onClick={() => startTour(id)}>
      {children}
    </button>
  );
}

/** Scroll to Home's playground and put the keyboard on its first slider. */
function goToPlayground() {
  const el = document.querySelector<HTMLElement>('[data-tour="home.playground"]');
  if (!el) return;
  const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
  el.scrollIntoView?.({ behavior: reduced ? "auto" : "smooth", block: "center" });
  el.querySelector<HTMLElement>("[role='slider'], input[type='range']")?.focus({ preventScroll: true });
}

export function StepAction({ step, sample, surface }: { step: ChecklistStep; sample: SampleInfo | null; surface: "home" | "guided" }) {
  const actions = useSampleActions();
  const view = sampleView(sample);
  const ready = view === "ready" && sample !== null;
  switch (step) {
    case "took_tour":
      return <TourButton id="welcome">Start the tour</TourButton>;
    case "open_sample":
      if (ready) {
        return (
          <button type="button" className={GO} onClick={() => actions.open(sample, "checklist")}>
            Open it
          </button>
        );
      }
      if (view === "offer" && sample) {
        return (
          <button type="button" className={GO} disabled={actions.pending} onClick={() => actions.start(sample)}>
            Make the sample project
          </button>
        );
      }
      if (view === "working") return <p className={NOTE}>It's being made right now. Hang tight!</p>;
      if (view === "hidden" && sample) {
        return (
          <button type="button" className={cn(QUIET, PILL_SM)} onClick={() => actions.setHidden(sample, false)}>
            Show the sample again
          </button>
        );
      }
      return <p className={NOTE}>The sample project isn't available here. Try the playground instead.</p>;
    case "fit_model":
      return ready ? (
        <TourButton id="fit">Show me the sample's fit</TourButton>
      ) : (
        <Link to="/guided" className={GO}>
          Start a guided analysis
        </Link>
      );
    case "read_results":
      return ready ? <TourButton id="results">Walk me through it</TourButton> : <p className={NOTE}>Open the sample project or fit a model first, then the results are one click away.</p>;
    case "try_whatif":
      if (surface === "home") {
        return (
          <button type="button" className={GO} onClick={goToPlayground}>
            Go to the playground
          </button>
        );
      }
      return ready ? <TourButton id="whatif">Show me the what-if</TourButton> : <p className={NOTE}>The playground on the Expert Home page works on example data.</p>;
    case "make_dashboard":
      return ready && sample.model_id ? (
        <Link to="/dashboards" search={{ new: true, model: sample.model_id }} className={GO}>
          Make one from the sample
        </Link>
      ) : (
        <Link to="/dashboards" search={{ new: true }} className={GO}>
          Make a dashboard
        </Link>
      );
    case "own_data":
      return surface === "guided" ? (
        <Link to="/guided" className={GO}>
          Pick a question, then upload
        </Link>
      ) : (
        <Link to="/datasets" className={GO}>
          Upload a dataset
        </Link>
      );
    case "make_your_own":
      return (
        <Link to="/library" search={{ new: "model" }} className={GO}>
          Make a model recipe
        </Link>
      );
  }
}

