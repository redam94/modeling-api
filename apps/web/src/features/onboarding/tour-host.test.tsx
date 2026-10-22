import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onOnboarding } from "@/lib/onboarding-events";

import { emptyOnboarding, type OnboardingState, type SampleInfo } from "./api";
import { ANCHOR_WAIT_MS } from "./spotlight";
import TourHost, { pickSample, TourView, type TourViewProps } from "./tour-host";
import { TOURS, type TourDef } from "./tours";
import { startTour, TOUR_POSITION_KEY, useTourStore } from "./use-onboarding";
import { WelcomeDialog } from "./welcome-dialog";

// ------------------------------------------------------------------------------ mocks

const nav = vi.hoisted(() => {
  const push = vi.fn();
  const replace = vi.fn();
  return { pathname: "/", searchStr: "", push, replace, router: { history: { push, replace } } };
});
const data = vi.hoisted(() => ({
  state: undefined as OnboardingState | undefined,
  samples: [] as SampleInfo[],
  mutate: vi.fn(),
  sampleMutate: vi.fn(),
}));

vi.mock("@tanstack/react-router", () => ({
  useRouterState: ({ select }: { select: (s: { location: { pathname: string; searchStr: string } }) => unknown }) =>
    select({ location: { pathname: nav.pathname, searchStr: nav.searchStr } }),
  useRouter: () => nav.router,
}));

vi.mock("@/features/guided/persona", () => ({ useEffectivePersona: () => "expert" }));

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    useOnboardingTenant: () => "acme",
    useOnboarding: () => ({ data: data.state, isPending: data.state === undefined, isError: false }),
    useOnboardingEventSync: () => undefined,
    usePatchOnboarding: () => ({ mutate: data.mutate }),
    useSamples: () => ({ data: data.samples }),
    useSampleAction: () => ({ mutate: data.sampleMutate }),
  };
});

// ------------------------------------------------------------------------------ helpers

type Media = { reduced?: boolean; phone?: boolean };
const realMatchMedia = window.matchMedia;
function setMedia({ reduced = false, phone = false }: Media) {
  window.matchMedia = (query: string) =>
    ({
      matches: (query.includes("reduce") && reduced) || (query.includes("max-width") && phone),
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

/** An anchor with a layout box (jsdom draws none). */
function placeAnchor(name: string, top = 120): HTMLElement {
  const el = document.createElement("button");
  el.textContent = name;
  el.setAttribute("data-tour", name);
  el.getBoundingClientRect = () => ({ top, left: 40, width: 200, height: 40, right: 240, bottom: top + 40, x: 40, y: top, toJSON: () => ({}) });
  el.scrollIntoView = vi.fn();
  document.body.append(el);
  return el;
}

const ready = {
  id: "geo_mmm",
  title: "Geo MMM",
  summary: "",
  minutes: 2,
  state: "ready",
  project_id: "sample",
  spec_id: "spec_1",
  run_id: "r",
  model_id: "eu-geo-mmm",
  model_version: 1,
  progress: 1,
  problem: null,
  attempts: 1,
  can_install: false,
  can_retry: false,
  can_retire: false,
  hidden: false,
} as SampleInfo;

function view(over: Partial<TourViewProps> = {}) {
  const props: TourViewProps = {
    tour: TOURS.welcome,
    step: 0,
    ctx: { persona: "expert", sample: null },
    pathname: "/",
    search: "",
    navigate: vi.fn(),
    onStep: vi.fn(),
    onFinish: vi.fn(),
    onSkip: vi.fn(),
    onLeave: vi.fn(),
    ...over,
  };
  return { props, ...render(<TourView {...props} />) };
}

/** The PATCH a tour step, finish or skip sends. */
function progress(tour: string, status: string, step: number) {
  return { patch: { tours: { [tour]: expect.objectContaining({ status, step }) as unknown } } };
}

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  setMedia({});
  nav.pathname = "/";
  nav.searchStr = "";
  nav.push.mockClear();
  nav.replace.mockClear();
  data.state = undefined;
  data.samples = [];
  data.mutate.mockClear();
  data.sampleMutate.mockClear();
});

afterEach(() => {
  cleanup();
  window.matchMedia = realMatchMedia;
  vi.useRealTimers();
  useTourStore.setState({ active: null, suppressed: false, returnFocus: null });
  document.body.replaceChildren();
});

// ------------------------------------------------------------------------------ the view

describe("TourView", () => {
  it("points the coach-mark at the anchor, moves focus to it and takes the keys", () => {
    placeAnchor("home.start-here");
    const { props } = view();
    const pop = screen.getByTestId("tour-popover");
    expect(pop).toHaveTextContent("Welcome! Start right here");
    expect(pop).toHaveTextContent("step 1 of 6");
    expect(screen.getByTestId("tour-spotlight")).toBeInTheDocument();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Welcome! Start right here" }));
    // the Studio coach-mark: the ink block with the lime action (direction §7.1)
    expect(pop.className).toContain("bg-block");
    expect(screen.getByTestId("tour-next").className).toContain("bg-highlight");

    fireEvent.keyDown(document.activeElement!, { key: "ArrowRight" });
    expect(props.onStep).toHaveBeenLastCalledWith(1);
    fireEvent.keyDown(document.activeElement!, { key: "Enter" });
    expect(props.onStep).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(props.onSkip).toHaveBeenCalledWith(0);
  });

  it("goes back with ← and finishes on the last step", () => {
    placeAnchor("shell.help-menu");
    const { props } = view({ step: 5 });
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(props.onStep).toHaveBeenLastCalledWith(4);
    fireEvent.click(screen.getByTestId("tour-next"));
    expect(screen.getByTestId("tour-next")).toHaveTextContent("Finish");
    expect(props.onFinish).toHaveBeenCalledWith(5);
  });

  it("leaves keys alone while the user types elsewhere", () => {
    placeAnchor("home.start-here");
    const input = document.createElement("input");
    document.body.append(input);
    const { props } = view();
    input.focus();
    fireEvent.keyDown(input, { key: "ArrowRight" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onStep).not.toHaveBeenCalled();
  });

  it("never blocks the page: clicks outside reach their target and the tour stays", () => {
    placeAnchor("home.start-here");
    const outside = document.createElement("button");
    const clicked = vi.fn();
    outside.addEventListener("click", clicked);
    document.body.append(outside);
    const { props } = view();
    fireEvent.pointerDown(outside);
    fireEvent.click(outside);
    expect(clicked).toHaveBeenCalled();
    expect(props.onSkip).not.toHaveBeenCalled();
    expect(screen.getByTestId("tour-popover")).toBeInTheDocument();
    expect(screen.getByTestId("tour-spotlight").className).toContain("pointer-events-none");
  });

  it("opens the step's page when the step starts", () => {
    const tour = TOURS.make_your_own;
    const { props } = view({ tour, step: 1, pathname: "/catalog", search: "" });
    expect(props.navigate).toHaveBeenCalledTimes(1);
    expect(props.navigate).toHaveBeenCalledWith("/catalog?tab=plots");
  });

  it("stays put when the step is already on screen's page", () => {
    placeAnchor("catalog.browse");
    const { props } = view({ tour: TOURS.make_your_own, step: 0, pathname: "/catalog" });
    expect(props.navigate).not.toHaveBeenCalled();
  });

  it("shows the 'not on screen' card after 8 s without the anchor, with Take me there and Skip", () => {
    vi.useFakeTimers();
    const tour = TOURS.make_your_own;
    const { props } = view({ tour, step: 0, pathname: "/runs" });
    expect(props.navigate).toHaveBeenCalledWith("/catalog");
    expect(screen.queryByTestId("tour-missing")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Finding the next part of the tour");
    act(() => {
      vi.advanceTimersByTime(ANCHOR_WAIT_MS - 1);
    });
    expect(screen.queryByTestId("tour-missing")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    const card = screen.getByTestId("tour-missing");
    expect(card).toHaveTextContent("This part isn't on screen right now");
    expect(card).toHaveAttribute("role", "dialog");
    expect(card).not.toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByTestId("tour-take-me-there"));
    expect(props.navigate).toHaveBeenLastCalledWith("/catalog");
    fireEvent.click(screen.getByTestId("tour-skip-step"));
    expect(props.onStep).toHaveBeenCalledWith(1);
  });

  it("picks up an anchor that renders late, and drops the card when it arrives", async () => {
    vi.useFakeTimers();
    view({ tour: TOURS.make_your_own, step: 0, pathname: "/catalog" });
    act(() => {
      vi.advanceTimersByTime(ANCHOR_WAIT_MS);
    });
    expect(screen.getByTestId("tour-missing")).toBeInTheDocument();
    // no page to open: the card offers no "Take me there"
    expect(screen.queryByTestId("tour-take-me-there")).toBeNull();
    await act(async () => {
      placeAnchor("catalog.browse");
      await Promise.resolve();
    });
    expect(screen.queryByTestId("tour-missing")).toBeNull();
    expect(screen.getByTestId("tour-popover")).toHaveTextContent("Everything we can do");
  });

  it("clicks the anchor on Next when the step opens something (Try it)", () => {
    const el = placeAnchor("catalog.recipe.try");
    const clicked = vi.fn();
    el.addEventListener("click", clicked);
    const { props } = view({ tour: TOURS.make_your_own, step: 2, pathname: "/catalog", search: "?tab=plots" });
    fireEvent.click(screen.getByTestId("tour-next"));
    expect(clicked).toHaveBeenCalledTimes(1);
    expect(props.onStep).toHaveBeenCalledWith(3);
  });

  it("asks for the sample when a sample tour has none ready", () => {
    const { props } = view({ tour: TOURS.whatif, ctx: { persona: "expert", sample: null } });
    expect(screen.getByTestId("tour-needs-sample")).toHaveTextContent("This tour uses the sample project");
    fireEvent.click(screen.getByRole("button", { name: "Go to the sample" }));
    expect(props.onLeave).toHaveBeenCalled();
    expect(props.navigate).toHaveBeenCalledWith("/");
  });

  it("under reduced motion: no entrance animation, no glide, an instant scroll", () => {
    setMedia({ reduced: true });
    const el = placeAnchor("home.start-here", -300);
    view();
    expect(el.scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "auto" });
    expect(screen.getByTestId("tour-popover").className).not.toContain("animate-coach-in");
    expect(screen.getByTestId("tour-spotlight")).not.toHaveAttribute("data-glide");
  });

  it("animates in and scrolls smoothly otherwise", () => {
    const el = placeAnchor("home.start-here", -300);
    view();
    expect(el.scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
    expect(screen.getByTestId("tour-popover").className).toContain("animate-coach-in");
  });

  it("is a bottom sheet on a phone, with Esc ending the tour", () => {
    setMedia({ phone: true });
    placeAnchor("home.start-here");
    const { props } = view();
    const sheet = screen.getByTestId("tour-popover");
    expect(sheet).toHaveAttribute("data-variant", "sheet");
    expect(sheet).toHaveAttribute("role", "dialog");
    expect(sheet.className).toContain("rounded-t-3xl");
    expect(document.activeElement?.tagName).toBe("H2");
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(props.onSkip).toHaveBeenCalledWith(0);
  });

  it("renders tour copy as text (no markup)", () => {
    placeAnchor("home.start-here");
    const tour: TourDef = { ...TOURS.welcome, steps: [{ ...TOURS.welcome.steps[0]!, title: "<img src=x onerror=alert(1)>" }] };
    view({ tour });
    expect(screen.getByRole("heading").textContent).toBe("<img src=x onerror=alert(1)>");
    expect(document.querySelector("img")).toBeNull();
  });
});

// ------------------------------------------------------------------------------ welcome

describe("WelcomeDialog", () => {
  it("is non-modal: the page stays clickable and visible to screen readers", () => {
    const page = document.createElement("button");
    page.textContent = "A page button";
    document.body.append(page);
    const onChoose = vi.fn();
    render(<WelcomeDialog sampleOffered sampleStarted={false} onChoose={onChoose} />);
    const dlg = screen.getByTestId("welcome-dialog");
    expect(dlg).toHaveAttribute("role", "dialog");
    expect(dlg).not.toHaveAttribute("aria-modal", "true");
    expect(page).not.toHaveAttribute("aria-hidden");
    expect(document.body.style.pointerEvents).not.toBe("none");
    fireEvent.pointerDown(page);
    expect(onChoose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Take the 2-minute tour" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Explore the sample project" })).toBeInTheDocument();
    fireEvent.keyDown(dlg, { key: "Escape" });
    expect(onChoose).toHaveBeenCalledWith("later");
  });

  it("hides the sample offer when samples are off", () => {
    render(<WelcomeDialog sampleOffered={false} sampleStarted={false} onChoose={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /sample/ })).toBeNull();
  });
});

// ------------------------------------------------------------------------------ the host

describe("TourHost", () => {
  it("welcomes a first visit; Not now records it and closes", () => {
    data.state = emptyOnboarding();
    data.samples = [{ ...ready, state: null }];
    render(<TourHost />, { wrapper });
    expect(screen.getByTestId("welcome-dialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(data.mutate).toHaveBeenCalledWith({ patch: { welcomed: true } });
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
  });

  it("does not welcome with tips off, with ?tour=off, on /admin or with mapi.noTour", () => {
    data.state = { ...emptyOnboarding(), tips_off: true };
    const a = render(<TourHost />, { wrapper });
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
    a.unmount();

    data.state = emptyOnboarding();
    nav.searchStr = "?tour=off";
    const b = render(<TourHost />, { wrapper });
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
    expect(useTourStore.getState().suppressed).toBe(true);
    expect(nav.replace).toHaveBeenCalledWith("/");
    b.unmount();
    useTourStore.setState({ suppressed: false });

    nav.searchStr = "";
    nav.pathname = "/admin";
    const c = render(<TourHost />, { wrapper });
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
    c.unmount();

    nav.pathname = "/";
    localStorage.setItem("mapi.noTour", "1");
    render(<TourHost />, { wrapper });
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
  });

  it("Take the tour starts the welcome tour and records the first step", () => {
    data.state = emptyOnboarding();
    placeAnchor("home.start-here");
    render(<TourHost />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Take the 2-minute tour" }));
    expect(useTourStore.getState().active).toEqual({ id: "welcome", step: 0 });
    expect(screen.getByTestId("tour-popover")).toHaveTextContent("Welcome! Start right here");
    expect(data.mutate).toHaveBeenCalledWith(progress("welcome", "in_progress", 0));
  });

  it("PATCHes every step and the finish, tells the checklist, and gives focus back", () => {
    data.state = { ...emptyOnboarding(), welcomed_at: "2026-10-01T09:00:00Z" };
    placeAnchor("nav.make-your-own");
    placeAnchor("shell.help-menu");
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    const events: string[] = [];
    const stop = onOnboarding(({ event }) => events.push(event));
    // the Expert welcome tour has six steps: 4 = Make your own, 5 = the menu (last)
    act(() => {
      startTour("welcome", 4);
    });
    render(<TourHost />, { wrapper });
    expect(data.mutate).toHaveBeenLastCalledWith(progress("welcome", "in_progress", 4));
    fireEvent.click(screen.getByTestId("tour-next"));
    expect(data.mutate).toHaveBeenLastCalledWith(progress("welcome", "in_progress", 5));
    fireEvent.click(screen.getByTestId("tour-next"));
    expect(data.mutate).toHaveBeenLastCalledWith(progress("welcome", "completed", 5));
    expect(events).toContain("tour_finished");
    expect(useTourStore.getState().active).toBeNull();
    expect(localStorage.getItem(TOUR_POSITION_KEY)).toBeNull();
    expect(screen.queryByTestId("tour-popover")).toBeNull();
    expect(document.activeElement).toBe(opener);
    stop();
  });

  it("Skip records the tour as dismissed", () => {
    data.state = { ...emptyOnboarding(), welcomed_at: "2026-10-01T09:00:00Z" };
    placeAnchor("home.start-here");
    act(() => {
      startTour("welcome");
    });
    render(<TourHost />, { wrapper });
    fireEvent.click(screen.getByTestId("tour-skip"));
    expect(data.mutate).toHaveBeenLastCalledWith(progress("welcome", "dismissed", 0));
    expect(useTourStore.getState().active).toBeNull();
  });

  it("?tour=whatif opens the what-if tour on the sample's explore page", () => {
    data.state = { ...emptyOnboarding(), tips_off: true, welcomed_at: "2026-10-01T09:00:00Z" };
    data.samples = [ready];
    nav.pathname = "/runs";
    nav.searchStr = "?tour=whatif";
    render(<TourHost />, { wrapper });
    expect(useTourStore.getState().active).toEqual({ id: "whatif", step: 0 });
    expect(nav.replace).toHaveBeenCalledWith("/runs");
    expect(nav.push).toHaveBeenCalledWith("/guided/spec_1/explore");
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
  });

  it("resumes a tour after a reload unless the server finished it", () => {
    localStorage.setItem(TOUR_POSITION_KEY, JSON.stringify({ id: "make_your_own", step: 2 }));
    data.state = { ...emptyOnboarding(), welcomed_at: "2026-10-01T09:00:00Z" };
    const a = render(<TourHost />, { wrapper });
    expect(useTourStore.getState().active).toEqual({ id: "make_your_own", step: 2 });
    a.unmount();
    useTourStore.setState({ active: null });

    localStorage.setItem(TOUR_POSITION_KEY, JSON.stringify({ id: "make_your_own", step: 2 }));
    data.state = {
      ...emptyOnboarding(),
      welcomed_at: "2026-10-01T09:00:00Z",
      tours: { make_your_own: { status: "completed", step: 10, updated_at: "2026-10-01T09:00:00Z" } },
    };
    render(<TourHost />, { wrapper });
    expect(useTourStore.getState().active).toBeNull();
  });

  it("Explore the sample starts the install and goes to the start page", () => {
    data.state = emptyOnboarding();
    data.samples = [{ ...ready, state: null, can_install: true, spec_id: null, model_id: null }];
    render(<TourHost />, { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Explore the sample project" }));
    expect(data.sampleMutate).toHaveBeenCalledWith({ id: "geo_mmm", action: "install" });
    expect(nav.push).toHaveBeenCalledWith("/");
  });

  it("pickSample skips hidden and retired samples", () => {
    expect(pickSample([{ ...ready, hidden: true }, { ...ready, id: "b", state: "retired" }])).toBeNull();
    expect(pickSample([{ ...ready, hidden: true }, { ...ready, id: "b" }])?.id).toBe("b");
  });
});
