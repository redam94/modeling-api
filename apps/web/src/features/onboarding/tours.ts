/**
 * The guided tours (v0.18 D18, I-8) as data: each step points at a `data-tour` anchor
 * (`anchors.ts`), says where that anchor lives (a route the tour opens for you), and holds
 * the coach-mark copy. The tour host (`tour-host.tsx`) plays them; progress goes to the
 * server (`use-onboarding.ts`).
 *
 * Copy follows the relaxed voice of `docs/project/writing-style.md` (onboarding and tours):
 * warm, second person, short. No step promises an instant result from the server (plan
 * probe P11: a live chart preview can take about ten seconds).
 *
 * Pure data and pure helpers: no React, no runtime imports.
 */
import type { SampleInfo, TourId } from "./api";
import type { TourAnchor } from "./anchors";

export type { TourId };

/** What a step's route and visibility may depend on. */
export interface TourContext {
  persona: "expert" | "guided";
  /** The tenant's sample project, when it is installed and visible to the user. */
  sample: SampleInfo | null;
  /** The Start-here hub (Home) and strip (Guided) are open; `false` once they fold away
   * (their steps then skip: the anchors are gone). Unknown counts as open. */
  hub?: boolean;
}

export type TourRoute = string | ((ctx: TourContext) => string | null);

export interface TourStep {
  /** Stable within its tour (tests and analytics use it; never shown). */
  id: string;
  anchor: TourAnchor;
  title: string;
  body: string;
  /**
   * Where the anchor lives. The tour opens this page when the step starts and the current
   * page does not match. `null` from a function: the page cannot be named yet (the sample
   * is not ready), so the step shows the "not on screen" card at once.
   * Leave it out for an anchor the previous step makes appear (a dialog, a builder).
   */
  route?: TourRoute;
  /** Preferred side of the coach-mark (it flips when there is no room). */
  side?: "top" | "right" | "bottom" | "left";
  /**
   * `"dialog"`: the anchor sits in an open dialog whose fields the step talks about. The
   * coach-mark goes beside the dialog (its wider margin), or, when neither margin has room,
   * docks in the window's bottom-left corner, never over the dialog's top fields.
   */
  beside?: "dialog";
  /** "Next" first clicks the anchored element (it opens what the next step points into). */
  clickOnNext?: boolean;
  /** Show the step only in this context (e.g. one persona). */
  when?: (ctx: TourContext) => boolean;
  /** A short extra line under the body (a pointer to the docs). */
  note?: string;
}

export interface TourDef {
  id: TourId;
  title: string;
  /** One line for the Start-here hub and the checklist. */
  summary: string;
  minutes: number;
  /** The tour walks through the sample project, so it needs one that is ready. */
  needsSample: boolean;
  steps: readonly TourStep[];
}

// ------------------------------------------------------------------------------ routes

const expert = (ctx: TourContext) => ctx.persona === "expert";
const guided = (ctx: TourContext) => ctx.persona === "guided";
/** The step points into the open Start-here hub or strip. */
const onHub = (ctx: TourContext) => ctx.hub !== false;
const expertHub = (ctx: TourContext) => expert(ctx) && onHub(ctx);

/** The page of a Guided step of the sample's session, or `null` while the sample is not ready. */
export function sampleStepPath(ctx: TourContext, step: "data" | "run" | "report" | "explore"): string | null {
  const s = ctx.sample;
  if (!s || s.state !== "ready" || !s.spec_id) return null;
  return `/guided/${encodeURIComponent(s.spec_id)}/${step}`;
}

const sampleStep = (step: "data" | "run" | "report" | "explore") => (ctx: TourContext) => sampleStepPath(ctx, step);

/** The start page of the current persona (Guided mode has no Expert Home). */
const startPage = (ctx: TourContext) => (ctx.persona === "guided" ? "/guided" : "/");

/** The new-dashboard dialog, prefilled with the sample's model (`/dashboards?new=1&model=`). */
function newDashboardPath(ctx: TourContext): string | null {
  const s = ctx.sample;
  if (!s || s.state !== "ready" || !s.model_id) return null;
  return `/dashboards?new=1&model=${encodeURIComponent(s.model_id)}`;
}

// ------------------------------------------------------------------------------ tours

export const TOURS: Readonly<Record<TourId, TourDef>> = {
  welcome: {
    id: "welcome",
    title: "A two-minute look around",
    summary: "Where things live, and the quickest way to a first result.",
    minutes: 2,
    needsSample: false,
    steps: [
      {
        id: "start-here",
        anchor: "home.start-here",
        route: "/",
        when: expert,
        title: "Welcome! Start right here",
        body: "This is your starting point. The sample project, your checklist and a little playground all live on this page.",
      },
      {
        id: "checklist",
        anchor: "home.checklist",
        route: "/",
        when: expertHub,
        side: "left",
        title: "Your first-week checklist",
        body: "Each box ticks itself as you go. No need to do them in order, and you can hide the list whenever you like.",
      },
      {
        id: "playground",
        anchor: "home.playground",
        route: "/",
        when: expertHub,
        title: "Have a play",
        body: "Drag a slider to move a channel's spend and watch the result change. It runs on example data, so nothing you do here can break a thing.",
      },
      {
        id: "guided-start",
        anchor: "guided.templates",
        route: "/guided",
        when: guided,
        title: "Welcome! Pick a question to answer",
        body: "Each card is a ready-made analysis. Choose one, add your data and we'll walk you through the rest, step by step.",
      },
      {
        id: "nav",
        anchor: "nav.primary",
        side: "right",
        title: "Everything else is one click away",
        body: "Datasets, models, dashboards and more sit in this menu. Press g then a letter to jump around from the keyboard.",
      },
      {
        id: "make-your-own",
        anchor: "nav.make-your-own",
        side: "right",
        title: "Make it yours",
        body: "Save your own model recipes, prep steps and chart styles here, then reuse them with your team. No code needed.",
      },
      {
        id: "help",
        anchor: "shell.help-menu",
        side: "bottom",
        title: "Come back any time",
        body: "Replay this tour, or pick a longer one, from your menu up here. That's it, enjoy exploring!",
      },
    ],
  },

  data: {
    id: "data",
    title: "Bring in data",
    summary: "How a table becomes something a model can learn from.",
    minutes: 2,
    needsSample: true,
    steps: [
      {
        id: "templates",
        anchor: "guided.templates",
        route: "/guided",
        title: "Every analysis starts with a question",
        body: "These cards are the questions we can help with. The sample project used the marketing-mix one.",
      },
      {
        id: "upload",
        anchor: "guided.data.upload",
        route: sampleStep("data"),
        title: "Your table goes here",
        body: "Drop in a CSV or a spreadsheet export. Weekly rows with one column per channel work best.",
      },
      {
        id: "mapping",
        anchor: "guided.data.mapping",
        route: sampleStep("data"),
        title: "Tell us what each column means",
        body: "We guess which column is the date, the outcome and the spend. Check the guesses and fix any that look off.",
      },
    ],
  },

  fit: {
    id: "fit",
    title: "Fit a model",
    summary: "What happens while the model learns, and how to tell it went well.",
    minutes: 2,
    needsSample: true,
    steps: [
      {
        id: "sample",
        anchor: "sample.progress",
        route: startPage,
        when: onHub,
        title: "The sample is a real fit",
        body: "Your sample project was fitted on our servers, just like your own models will be. This card shows how it went.",
      },
      {
        id: "progress",
        anchor: "guided.run.progress",
        route: sampleStep("run"),
        title: "Watch it learn",
        body: "A fit takes a few minutes. You can leave this page and come back; the run keeps going without you.",
      },
      {
        id: "chains",
        anchor: "guided.run.chains",
        route: sampleStep("run"),
        title: "Four chains, one answer",
        body: "The model explores the answer four times on its own. When the four agree, you can trust what it found.",
      },
    ],
  },

  results: {
    id: "results",
    title: "Read the results",
    summary: "The headline, the health check and the charts behind them.",
    minutes: 2,
    needsSample: true,
    steps: [
      {
        id: "headline",
        anchor: "guided.report.headline",
        route: sampleStep("report"),
        title: "The headline first",
        body: "This is the short answer to your question, in plain words, with the range we're confident about.",
      },
      {
        id: "scorecard",
        anchor: "guided.report.scorecard",
        route: sampleStep("report"),
        title: "A quick health check",
        body: "Green means good to go. Amber means read the note before you share. Each line says what to do about it.",
      },
      {
        id: "plots",
        anchor: "guided.report.plots",
        route: sampleStep("report"),
        title: "The charts behind the answer",
        body: "Open any chart to see how well the model fits the past, and what each channel added week by week.",
      },
    ],
  },

  whatif: {
    id: "whatif",
    title: "Ask what if",
    summary: "Move spend between channels and see what the model expects.",
    minutes: 2,
    needsSample: true,
    steps: [
      {
        id: "sliders",
        anchor: "guided.explore.sliders",
        route: sampleStep("explore"),
        title: "Move the sliders",
        body: "Each slider scales one channel's spend. Try doubling TV, or switching social off, and see what happens.",
      },
      {
        id: "result",
        anchor: "guided.explore.result",
        route: sampleStep("explore"),
        title: "The answer, with its range",
        body: "This number updates as you drag. The band shows how sure the model is, so wider means less certain.",
      },
      {
        id: "exact",
        anchor: "guided.explore.exact",
        route: sampleStep("explore"),
        title: "Want the exact figure?",
        body: "The sliders use a quick sketch of the model. Press this to run your scenario properly; it takes a little while.",
      },
    ],
  },

  dashboard: {
    id: "dashboard",
    title: "Share a dashboard",
    summary: "Turn the sample model into a page your team can read.",
    minutes: 3,
    needsSample: true,
    steps: [
      {
        id: "presets",
        anchor: "dashboards.new.presets",
        route: newDashboardPath,
        // the card would cover the dialog's Title and Source fields above the layouts
        beside: "dialog",
        title: "Start from a preset",
        body: "Pick the layout closest to what your readers need, then press Create. You can change every panel later.",
      },
      {
        id: "panels",
        anchor: "dashboards.builder.panels",
        title: "Arrange your panels",
        body: "Add, remove and reorder panels here. Each one reads straight from the model, so the numbers stay in sync.",
      },
      {
        id: "publish",
        anchor: "dashboards.publish",
        title: "Publish when it's ready",
        body: "Publishing freezes this version for your readers. Keep editing afterwards; they only see a new version when you publish again.",
      },
      {
        id: "share",
        anchor: "dashboards.share",
        title: "Share it",
        body: "Send a link to your team, or invite a client to read it in their own portal.",
      },
    ],
  },

  make_your_own: {
    id: "make_your_own",
    title: "Make your own",
    summary: "Try a chart type, then save your own model recipe. No code needed.",
    minutes: 4,
    needsSample: false,
    steps: [
      {
        id: "catalog",
        anchor: "catalog.browse",
        route: "/catalog",
        title: "Everything we can do, in one place",
        body: "The catalog lists every model, prep step and chart type available to you, with a card that says what each is good for.",
      },
      {
        id: "plots-tab",
        anchor: "catalog.tab.plots",
        route: "/catalog?tab=plots",
        title: "Chart types",
        body: "This tab lists the charts a model can draw. Each one has an example you can try right away.",
      },
      {
        id: "try-it",
        anchor: "catalog.recipe.try",
        route: "/catalog?tab=plots",
        clickOnNext: true,
        title: "Try one",
        body: "Press Next and we'll open the playground for this chart.",
      },
      {
        id: "settings",
        anchor: "catalog.recipe.settings",
        title: "Change a setting",
        body: "Titles and sizes change as you type. Other settings redraw the chart on our servers, which can take a few seconds.",
      },
      {
        id: "yaml",
        anchor: "catalog.recipe.yaml",
        title: "Take it with you",
        body: "Copy these settings as YAML and paste them into a model spec or a workflow.",
      },
      {
        id: "add-own",
        anchor: "extend-add-own",
        title: "Add your own",
        body: "Want something that isn't here? There's a no-code way and a with-code way, side by side.",
      },
      {
        id: "no-code",
        anchor: "extend-no-code",
        title: "No code: the library",
        body: "Build model recipes, prep presets and chart styles from forms, then share them with your project or your whole team.",
      },
      {
        id: "with-code",
        anchor: "extend-with-code",
        title: "With code: a plugin",
        body: "Developers can scaffold a plugin with one command and install it on your own stack.",
      },
      {
        id: "library",
        anchor: "library.new-model",
        route: "/library",
        clickOnNext: true,
        title: "Make your first model recipe",
        body: "Press Next and we'll start a new model recipe for you.",
      },
      {
        id: "prior",
        anchor: "library.prior",
        title: "Say what you already believe",
        body: "Pick a prior for each driver. The little curve shows which values you think are likely before the model sees any data.",
      },
      {
        id: "use",
        anchor: "library.use",
        title: "Save it and use it",
        body: "Save the recipe, then use it in Build or fit it right away. Your team can reuse it too.",
        note: "Want to go further? The docs walk you through your first custom model, prep step, chart and workflow (Extend › Your first extensions).",
      },
    ],
  },
};

/** The tours in the order the hub lists them. */
export const TOUR_ORDER: readonly TourId[] = ["welcome", "data", "fit", "results", "whatif", "dashboard", "make_your_own"];

export function isTourId(value: unknown): value is TourId {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(TOURS, value);
}

/** The steps of `tour` that apply in `ctx`. */
export function tourSteps(tour: TourDef, ctx: TourContext): TourStep[] {
  return tour.steps.filter((s) => !s.when || s.when(ctx));
}

/** Every anchor any tour can point at (for the static anchor test). */
export function tourAnchors(): TourAnchor[] {
  return [...new Set(Object.values(TOURS).flatMap((t) => t.steps.map((s) => s.anchor)))];
}

/** The page a step opens: a path with an optional query, `null` when it cannot be named yet, `undefined` for "stay". */
export function stepRoute(step: TourStep, ctx: TourContext): string | null | undefined {
  if (step.route === undefined) return undefined;
  return typeof step.route === "function" ? step.route(ctx) : step.route;
}

/**
 * True when the current location is the step's page: the same path, and every query
 * parameter the route names has the same value (other parameters may differ).
 */
export function onRoute(route: string, pathname: string, search: string): boolean {
  const [path = "", query = ""] = route.split("?", 2);
  if (trimSlash(path) !== trimSlash(pathname)) return false;
  const want = new URLSearchParams(query);
  const have = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  for (const [k, v] of want) if (have.get(k) !== v) return false;
  return true;
}

function trimSlash(p: string): string {
  return p.length > 1 ? p.replace(/\/+$/, "") : p;
}

/** True when the tour can run now (a sample tour needs a ready sample). */
export function tourAvailable(tour: TourDef, ctx: TourContext): boolean {
  return !tour.needsSample || (ctx.sample?.state === "ready" && Boolean(ctx.sample.spec_id));
}
