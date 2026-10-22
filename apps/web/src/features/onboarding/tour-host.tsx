/**
 * The tour host (v0.18 D18, D24): a lazy chunk (`tour-host-*`, ≤ 48 KiB)
 * that only the signed-in shell mounts. It
 *
 * - plays the tour in `useTourStore` (started by the hub, the user menu, a `?tour=<id>` deep
 *   link, or resumed after a reload): opens each step's page, waits up to 8 s for its
 *   `data-tour` anchor, spotlights it and shows the coach-mark (a Radix Popover on a virtual
 *   anchor; a bottom sheet on a phone);
 * - offers the welcome dialog on a first visit (polite rules in `use-onboarding.ts`);
 * - ticks checklist steps from onboarding events and sends tour progress to the server.
 *
 * Motion: the coach-mark rises in with the Studio `coach-in` keyframes and the spotlight
 * glides on the same curve (CSS only). The `motion` library's smallest set (`m` +
 * `domAnimation`) is about 70 KiB minified on its own, more than this chunk's whole 48 KiB
 * budget, so the tour host does not load it (plan D27 allows, never requires, it here).
 *
 * Keys while the coach-mark (or nothing) has focus: → or Enter next, ← back, Esc ends the
 * tour. Focus moves to the coach-mark's title on each step and returns to where it was when
 * the tour ends. Nothing here blocks the page: the scrim lets clicks through and both cards
 * are non-modal.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useRouterState } from "@tanstack/react-router";
import { Popover as PopoverPrimitive } from "radix-ui";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";

import { useEffectivePersona } from "@/features/guided/persona";
import { emitOnboarding } from "@/lib/onboarding-events";
import { cn } from "@/lib/utils";
import { useScope } from "@/stores/scope";

import { hubFolded, useOnboarding, useOnboardingEventSync, useOnboardingTenant, usePatchOnboarding, useSampleAction, useSamples, type SampleInfo } from "./api";
import { findAnchor } from "./anchors";
import { PHONE, REDUCED_MOTION, revealAnchor, Spotlight, stickyTop, useAnchor, useAnchorBox, useMedia } from "./spotlight";
import { onRoute, stepRoute, tourAvailable, TOURS, tourSteps, type TourContext, type TourDef, type TourStep } from "./tours";
import {
  noTourFlag,
  readPosition,
  resumablePosition,
  shouldWelcome,
  startTour,
  tourParam,
  useTourReporter,
  useTourStore,
  withoutTourParam,
} from "./use-onboarding";
import { WelcomeDialog, type WelcomeChoice } from "./welcome-dialog";

// ------------------------------------------------------------------------------ styles

const pill = "inline-flex min-h-10 items-center justify-center rounded-full px-4 text-sm font-semibold outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-highlight";
const primary = cn(pill, "bg-highlight text-highlight-foreground hover:shadow-[inset_0_0_0_2px_var(--highlight-foreground)]");
const ghost = cn(pill, "shadow-[inset_0_0_0_1.5px_currentColor] hover:bg-block-raised");
const quiet =
  "min-h-10 rounded-md px-1 text-xs text-block-muted underline underline-offset-4 outline-none hover:text-block-foreground focus-visible:outline-2 focus-visible:outline-highlight";

const INTERACTIVE = "button, a, input, textarea, select, [role='slider'], [role='tab'], [role='menuitem'], [contenteditable='true']";

// ------------------------------------------------------------------------------ the view

export interface TourViewProps {
  tour: TourDef;
  /** Index into the steps that apply in `ctx`. */
  step: number;
  ctx: TourContext;
  pathname: string;
  search: string;
  navigate: (to: string) => void;
  onStep: (step: number) => void;
  onFinish: (step: number) => void;
  onSkip: (step: number) => void;
  /** Close without recording anything (the sample is not ready yet). */
  onLeave: () => void;
  /** The samples list is still loading: a sample tour waits instead of saying "set it up". */
  samplesPending?: boolean;
}

/** One step of a tour on screen: spotlight + coach-mark, or the "not on screen" card. */
export function TourView(props: TourViewProps) {
  const { tour, ctx } = props;
  if (!tourAvailable(tour, ctx)) {
    if (tour.needsSample && props.samplesPending) {
      return (
        <p role="status" className="sr-only">
          Finding the next part of the tour…
        </p>
      );
    }
    return <NeedsSampleCard {...props} />;
  }
  const steps = tourSteps(tour, ctx);
  if (steps.length === 0) return null;
  const i = Math.max(0, Math.min(props.step, steps.length - 1));
  const step = steps[i]!;
  return <StepView {...props} steps={steps} i={i} current={step} key={`${tour.id}:${i}`} />;
}

interface StepViewProps extends TourViewProps {
  steps: TourStep[];
  i: number;
  current: TourStep;
}

/** When a step's anchor is revealed a second time (after a page change has settled). */
export const REVEAL_AGAIN_MS = 450;

function StepView({ tour, ctx, steps, i, current, pathname, search, navigate, onStep, onFinish, onSkip }: Omit<StepViewProps, "samplesPending">) {
  const stepKey = `${tour.id}:${i}`;
  const route = stepRoute(current, ctx);
  const last = i === steps.length - 1;
  const reduced = useMedia(REDUCED_MOTION);
  const phone = useMedia(PHONE);
  const anchor = useAnchor(current.anchor, stepKey, route === null);
  const box = useAnchorBox(anchor.el);
  const coachRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const uid = useId();
  const ids = { title: `${uid}-title`, body: `${uid}-body` };

  // A step on another page opens that page once, when the step starts (the user can still
  // wander off; the "not on screen" card then offers the way back).
  const where = useRef({ pathname, search, navigate });
  where.current = { pathname, search, navigate };
  useEffect(() => {
    const w = where.current;
    if (typeof route === "string" && !onRoute(route, w.pathname, w.search)) w.navigate(route);
    // only on entering the step
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepKey]);

  useEffect(() => {
    const el = anchor.el;
    if (!el) return;
    revealAnchor(el, { reduced, phone });
    // v0.18 live: a step that opens a page (or follows a click that does) can be scrolled
    // back to the top by the router's scroll restoration just after it revealed its anchor,
    // leaving the coach-mark below the window. Look again once the page has settled
    // (revealAnchor does nothing when the anchor is already on screen).
    const again = window.setTimeout(() => revealAnchor(el, { reduced, phone }), REVEAL_AGAIN_MS);
    return () => window.clearTimeout(again);
    // reveal once per element, not on every media change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [anchor.el]);

  // v0.18 live: a step whose words ask for an action that takes its anchor away ("then press
  // Create" closes the dialog and opens the builder) moves on by itself when the next step's
  // anchor has appeared, instead of showing "not on screen". Only after the anchor was seen,
  // and only into a step that has no page of its own (it "shows up after the step before").
  // The next anchor must be new: one that was already there while this step showed (the
  // nav, say) does not mean the user did what this step asked.
  const following = last ? undefined : steps[i + 1];
  const seen = useRef(false);
  const followingWasThere = useRef(false);
  if (anchor.el) {
    seen.current = true;
    followingWasThere.current ||= Boolean(following && findAnchor(following.anchor));
  }
  useEffect(() => {
    if (anchor.el || !seen.current || followingWasThere.current || !following || following.route !== undefined) return;
    const advance = () => {
      if (findAnchor(following.anchor)) {
        observer.disconnect();
        onStep(i + 1);
      }
    };
    const observer = new MutationObserver(advance);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-tour", "hidden"] });
    advance();
    return () => observer.disconnect();
  }, [anchor.el, following, i, onStep]);

  const next = useCallback(() => {
    if (current.clickOnNext && anchor.el) anchor.el.click();
    if (last) onFinish(i);
    else onStep(i + 1);
  }, [current.clickOnNext, anchor.el, last, i, onFinish, onStep]);
  const back = useCallback(() => {
    if (i > 0) onStep(i - 1);
  }, [i, onStep]);
  const skip = useCallback(() => onSkip(i), [i, onSkip]);
  const skipStep = useCallback(() => (last ? onFinish(i) : onStep(i + 1)), [last, i, onFinish, onStep]);

  const shown = Boolean(anchor.el && box);
  const sheet = phone || !shown;
  useTourKeys(coachRef, { next, back, skip, escape: sheet });

  // The bottom sheet and the "not on screen" card take focus themselves; the popover does it in onOpenAutoFocus.
  useEffect(() => {
    if (sheet && (shown || anchor.missing)) titleRef.current?.focus({ preventScroll: true });
  }, [sheet, shown, anchor.missing]);

  if (!shown) {
    if (!anchor.missing) {
      return (
        <p role="status" className="sr-only">
          Finding the next part of the tour…
        </p>
      );
    }
    const canGo = typeof route === "string" && !onRoute(route, pathname, search);
    return (
      <Centered>
        <CoachCard
          refs={{ coach: coachRef, title: titleRef }}
          ids={ids}
          variant="card"
          reduced={reduced}
          tour={tour}
          i={i}
          total={steps.length}
          testId="tour-missing"
          title="This part isn't on screen right now"
          body={
            canGo
              ? "It lives on another page. We can take you there, or you can skip this step."
              : "It shows up after the step before, or it isn't available to you here. Go back and try again, or skip this one."
          }
        >
          <button type="button" data-testid="tour-skip" className={quiet} onClick={skip}>
            End the tour
          </button>
          <div className="flex flex-wrap justify-end gap-2">
            {!canGo && i > 0 ? (
              <button type="button" className={ghost} onClick={back}>
                Back
              </button>
            ) : null}
            <button type="button" data-testid="tour-skip-step" className={canGo ? ghost : primary} onClick={skipStep}>
              {last ? "Finish" : "Skip this step"}
            </button>
            {canGo ? (
              <button type="button" data-testid="tour-take-me-there" className={primary} onClick={() => navigate(route)}>
                Take me there
              </button>
            ) : null}
          </div>
        </CoachCard>
      </Centered>
    );
  }

  const actions = (
    <>
      <button type="button" data-testid="tour-skip" className={quiet} onClick={skip}>
        Skip the tour
      </button>
      <div className="flex gap-2">
        {i > 0 ? (
          <button type="button" className={ghost} onClick={back}>
            Back
          </button>
        ) : null}
        <button type="button" data-testid="tour-next" className={primary} onClick={next}>
          {last ? "Finish" : "Next"}
        </button>
      </div>
    </>
  );
  const card = (variant: "popover" | "sheet") => (
    <CoachCard
      refs={{ coach: coachRef, title: titleRef }}
      ids={ids}
      variant={variant}
      reduced={reduced}
      tour={tour}
      i={i}
      total={steps.length}
      testId="tour-popover"
      title={current.title}
      body={current.body}
      note={current.note}
    >
      {actions}
    </CoachCard>
  );

  return (
    <>
      <Spotlight box={box!} stepKey={stepKey} reduced={reduced} />
      {phone ? (
        <div className="fixed inset-x-0 bottom-0 z-[70]">{card("sheet")}</div>
      ) : (
        <PopoverPrimitive.Root open>
          <PopoverPrimitive.Anchor virtualRef={{ current: visiblePart(anchor.el!) }} />
          <PopoverPrimitive.Portal>
            <PopoverPrimitive.Content
              side={current.side ?? "bottom"}
              align="start"
              sideOffset={16}
              collisionPadding={16}
              avoidCollisions
              aria-labelledby={ids.title}
              aria-describedby={ids.body}
              className="z-[70] w-[min(360px,calc(100vw-32px))] outline-none"
              onOpenAutoFocus={(e) => {
                e.preventDefault();
                titleRef.current?.focus({ preventScroll: true });
              }}
              onCloseAutoFocus={(e) => e.preventDefault()}
              // the page stays usable while the tour plays: clicks elsewhere never close it
              onInteractOutside={(e) => e.preventDefault()}
              onEscapeKeyDown={(e) => {
                // Radix hears Esc for the top layer wherever focus is: end the tour only when
                // the coach-mark (or nothing) has it; elsewhere the key is the page's
                const t = e.target instanceof Element ? e.target : null;
                if (!t || t === document.body || t === document.documentElement || coachRef.current?.contains(t)) {
                  e.preventDefault();
                  skip();
                  return;
                }
                // an open dialog under the coach-mark is no longer the top layer: close it
                t.closest<HTMLElement>("[role='dialog']")?.querySelector<HTMLElement>("[data-dialog-close]")?.click();
              }}
            >
              {card("popover")}
              <PopoverPrimitive.Arrow className="fill-block" width={18} height={9} />
            </PopoverPrimitive.Content>
          </PopoverPrimitive.Portal>
        </PopoverPrimitive.Root>
      )}
    </>
  );
}

/**
 * What the coach-mark points at: the anchor's part inside the window. A tall anchor (a whole
 * section, the Start-here hub) gets a band at the top of its visible part, so the card sits
 * on screen next to that band instead of past the anchor's far edge, out of view.
 */
export function visiblePart(el: HTMLElement): { getBoundingClientRect: () => DOMRect; contextElement: HTMLElement } {
  return {
    contextElement: el,
    getBoundingClientRect: () => {
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight || document.documentElement.clientHeight;
      const top = Math.max(r.top, stickyTop()); // below the sticky header (taller on a phone)
      const bottom = Math.min(r.bottom, vh);
      if (bottom - top <= vh * 0.5) return r;
      return new DOMRect(r.left, top, r.width, Math.min(96, Math.max(0, bottom - top)));
    },
  };
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="pointer-events-none fixed inset-0 z-[70] grid place-items-center p-4">{children}</div>;
}

interface CoachCardProps {
  refs: { coach: RefObject<HTMLDivElement | null>; title: RefObject<HTMLHeadingElement | null> };
  ids: { title: string; body: string };
  variant: "popover" | "sheet" | "card";
  reduced: boolean;
  tour: TourDef;
  i: number;
  total: number;
  testId: string;
  title: string;
  body: string;
  note?: string | undefined;
  children: ReactNode;
}

/** The Studio coach-mark: the ink block, a lime progress dot per step, one lime action. */
function CoachCard({ refs, ids, variant, reduced, tour, i, total, testId, title, body, note, children }: CoachCardProps) {
  const titleId = ids.title;
  const bodyId = ids.body;
  // the popover's Radix Content is the dialog; the sheet and the card are their own
  const dialog = variant === "popover" ? {} : { role: "dialog", "aria-modal": false, "aria-labelledby": titleId, "aria-describedby": bodyId };
  return (
    <div
      ref={refs.coach}
      {...dialog}
      data-testid={testId}
      data-tour-coach=""
      data-variant={variant}
      // A step can point into an open dialog while the card sits outside it: a press or focus
      // here must not reach the dialog's outside-click listener on the document, or it closes
      // and takes the next step's anchor with it.
      onPointerDown={(e) => e.stopPropagation()}
      onFocus={(e) => e.stopPropagation()}
      data-tour-step={`${tour.id}:${i}`}
      className={cn(
        // the lime ring keeps the card apart from an ink block it sits on or beside
        "pointer-events-auto grid gap-3 bg-block p-5 text-block-foreground shadow-lg ring-2 ring-highlight",
        // the Studio entrance (app.css keyframes; ends at its last frame under reduced motion)
        !reduced && "animate-coach-in",
        variant === "sheet" ? "rounded-t-3xl pb-[max(1.25rem,env(safe-area-inset-bottom))]" : "rounded-[20px]",
        variant === "card" && "w-[min(380px,calc(100vw-32px))]",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs font-bold text-block-muted">
        <span className="inline-flex gap-1" aria-hidden>
          {Array.from({ length: total }, (_, k) => (
            <i key={k} className={cn("block h-1 w-[18px] rounded-full", k <= i ? "bg-highlight" : "bg-block-foreground/30")} />
          ))}
        </span>
        <span>
          {tour.title} · step {i + 1} of {total}
        </span>
      </div>
      <h2 ref={refs.title} id={titleId} tabIndex={-1} className="display-title text-xl leading-tight outline-none">
        {title}
      </h2>
      <p id={bodyId} className="text-sm">
        {body}
      </p>
      {note ? <p className="text-xs text-block-muted">{note}</p> : null}
      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">{children}</div>
    </div>
  );
}

/** → / Enter next, ← back, Esc (when Radix does not handle it) ends the tour; only while focus is in the card or nowhere. */
function useTourKeys(coach: RefObject<HTMLDivElement | null>, h: { next: () => void; back: () => void; skip: () => void; escape: boolean }) {
  const handlers = useRef(h);
  handlers.current = h;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target instanceof Element ? e.target : null;
      const inCoach = Boolean(t && coach.current?.contains(t));
      const nowhere = !t || t === document.body || t === document.documentElement;
      if (!inCoach && !nowhere) return;
      const onControl = Boolean(t && inCoach && t.closest(INTERACTIVE));
      if (e.key === "ArrowRight" || (e.key === "Enter" && !onControl)) {
        e.preventDefault();
        handlers.current.next();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        handlers.current.back();
      } else if (e.key === "Escape" && handlers.current.escape) {
        e.preventDefault();
        handlers.current.skip();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [coach]);
}

function NeedsSampleCard({ tour, ctx, navigate, onLeave }: TourViewProps) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const coachRef = useRef<HTMLDivElement>(null);
  const uid = useId();
  const reduced = useMedia(REDUCED_MOTION);
  useEffect(() => titleRef.current?.focus({ preventScroll: true }), []);
  const making = ctx.sample?.state === "provisioning" || ctx.sample?.state === "ingesting" || ctx.sample?.state === "fitting";
  return (
    <Centered>
      <CoachCard
        refs={{ coach: coachRef, title: titleRef }}
        ids={{ title: `${uid}-title`, body: `${uid}-body` }}
        variant="card"
        reduced={reduced}
        tour={tour}
        i={0}
        total={1}
        testId="tour-needs-sample"
        title="This tour uses the sample project"
        body={
          making
            ? "Your sample is still being made. It takes a couple of minutes; come back to this tour once it's ready."
            : "Set up the sample from your start page first. It takes a couple of minutes, and this tour will be right here when it's ready."
        }
      >
        <button type="button" className={quiet} onClick={onLeave}>
          Not now
        </button>
        <button
          type="button"
          className={primary}
          onClick={() => {
            onLeave();
            navigate(ctx.persona === "guided" ? "/guided" : "/");
          }}
        >
          Go to the sample
        </button>
      </CoachCard>
    </Centered>
  );
}

// ------------------------------------------------------------------------------ the host

/** The tenant's sample as tours use it: the first one the user has not hidden or retired. */
export function pickSample(samples: readonly SampleInfo[] | undefined): SampleInfo | null {
  return samples?.find((s) => !s.hidden && s.state !== "retired") ?? null;
}

function TourHostInner() {
  const tenant = useOnboardingTenant();
  const progress = useOnboarding();
  useOnboardingEventSync(tenant !== null);
  const persona = useEffectivePersona();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const search = useRouterState({ select: (s) => s.location.searchStr });
  const router = useRouter();
  const qc = useQueryClient();
  const active = useTourStore((s) => s.active);
  const suppressed = useTourStore((s) => s.suppressed);
  const [noTour] = useState(noTourFlag);
  const [welcomeClosed, setWelcomeClosed] = useState(false);
  const reporter = useTourReporter(progress.data);
  const patch = usePatchOnboarding();
  const sampleAction = useSampleAction();

  const tour = active ? TOURS[active.id] : null;
  const showWelcome = !welcomeClosed && shouldWelcome({ state: progress.data, pathname, search, noTour, suppressed, tourActive: active !== null });
  const samples = useSamples({ enabled: showWelcome || Boolean(tour?.needsSample) });
  const sample = pickSample(samples.data);
  const samplesPending = samples.isLoading;
  // the Home hub (Expert) and the Start-here strip (Guided) fold away: their steps then skip
  const hub = !hubFolded(progress.data);
  const ctx = useMemo<TourContext>(() => ({ persona, sample, hub }), [persona, sample, hub]);

  // `?tour=<id>` opens a tour; `?tour=off` keeps everything quiet for this page load.
  const handledLink = useRef<string | null>(null);
  useEffect(() => {
    const p = tourParam(search);
    const link = `${pathname}${search}`;
    if (p === null) {
      // the param was stripped: the menu may push the very same link again later
      handledLink.current = null;
      return;
    }
    if (handledLink.current === link) return;
    handledLink.current = link;
    if (p === "off") useTourStore.getState().suppress();
    else startTour(p, 0);
    router.history.replace(`${pathname}${withoutTourParam(search)}`);
  }, [search, pathname, router]);

  // After a reload, carry on where the tour was (once the server's progress is known).
  const resumed = useRef(false);
  useEffect(() => {
    if (resumed.current) return;
    if (tenant && progress.isPending) return;
    resumed.current = true;
    const store = useTourStore.getState();
    if (store.active) return;
    const pos = resumablePosition(readPosition(), progress.data, { noTour, suppressed: store.suppressed, pathname });
    if (pos) startTour(pos.id, pos.step);
  }, [tenant, progress.isPending, progress.data, noTour, pathname]);

  // Every step the user reaches is recorded (D19: "PATCH on step").
  const { step: reportStep, finish: reportFinish, skip: reportSkip } = reporter;
  useEffect(() => {
    if (active) reportStep(active.id, active.step);
  }, [active, reportStep]);

  // Sample tours work in the sample's project: switch to it before opening one of its pages.
  const navigate = useCallback(
    (to: string) => {
      const cur = useScope.getState().active;
      if (tour?.needsSample && sample?.project_id && cur && cur.projectId !== sample.project_id) {
        void qc.cancelQueries({ queryKey: ["p"] });
        useScope.getState().select({ tenantId: cur.tenantId, projectId: sample.project_id });
      }
      router.history.push(to);
    },
    [tour?.needsSample, sample?.project_id, qc, router],
  );

  const onStep = useCallback((step: number) => useTourStore.getState().go(step), []);
  const onFinish = useCallback(
    (step: number) => {
      if (active) reportFinish(active.id, step);
      useTourStore.getState().stop();
    },
    [active, reportFinish],
  );
  const onSkip = useCallback(
    (step: number) => {
      if (active) reportSkip(active.id, step);
      useTourStore.getState().stop();
    },
    [active, reportSkip],
  );
  const onLeave = useCallback(() => useTourStore.getState().stop(), []);

  const onWelcome = (choice: WelcomeChoice) => {
    setWelcomeClosed(true);
    if (tenant) patch.mutate({ patch: { welcomed: true } });
    if (choice === "tour") startTour("welcome");
    if (choice !== "sample") return;
    const home = persona === "guided" ? "/guided" : "/";
    if (sample?.state === "ready" && sample.spec_id && sample.project_id) {
      const cur = useScope.getState().active;
      if (cur && cur.projectId !== sample.project_id) {
        void qc.cancelQueries({ queryKey: ["p"] });
        useScope.getState().select({ tenantId: cur.tenantId, projectId: sample.project_id });
      }
      emitOnboarding("sample_opened", { sample: sample.id });
      router.history.push(`/guided/${encodeURIComponent(sample.spec_id)}/report`);
      return;
    }
    // not made yet: start it (or join it) and watch it on the start page's sample card
    if (sample && (sample.state == null || sample.state === "failed") && (sample.can_install || sample.can_retry)) {
      sampleAction.mutate({ id: sample.id, action: sample.state === "failed" ? "retry" : "install" });
    }
    router.history.push(home);
  };

  return (
    <>
      {showWelcome ? (
        <WelcomeDialog
          sampleOffered={Boolean(sample)}
          sampleStarted={Boolean(sample?.state && sample.state !== "failed")}
          onChoose={onWelcome}
        />
      ) : null}
      {tour && active ? (
        <TourView
          tour={tour}
          step={active.step}
          ctx={ctx}
          pathname={pathname}
          search={search}
          navigate={navigate}
          onStep={onStep}
          onFinish={onFinish}
          onSkip={onSkip}
          onLeave={onLeave}
          samplesPending={samplesPending}
        />
      ) : null}
    </>
  );
}

/** Mounted once by the signed-in shell (lazy): `<Suspense fallback={null}><TourHost /></Suspense>`. */
export default function TourHost() {
  return <TourHostInner />;
}
