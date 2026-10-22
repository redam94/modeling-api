/**
 * Start here (v0.18 D22, WP-T6): the Expert Home hub (hero, sample card, checklist,
 * playground), its folded banner, the Guided strip, Settings › Reset tips and the Members
 * "Sample" badge, against the MSW onboarding and sample mocks.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { MOCK_FRESH_USER_KEY, MOCK_ONBOARDING_KEY, MOCK_SAMPLE_MS_KEY, MOCK_SAMPLES_KEY, MOCK_SAMPLES_OFF_KEY, SAMPLE_PROJECT } from "@/mocks/onboarding";
import { renderApp } from "@/test/render-app";
import { useUi } from "@/stores/ui";

import { emptyOnboarding, type OnboardingState, type SampleInfo } from "./api";
import { CHECKLIST_COPY, nextStep, summaryWords } from "./checklist";
import { firstName, greeting, hubCollapsed } from "./start-here";
import { useTourStore } from "./use-onboarding";
import { currentSample, minutesWords, sampleReportPath, sampleView, stageWords } from "./use-samples";

const server = setupServer(...createHandlers());
const T = { timeout: 5000 };

function resetMocks() {
  for (const k of [MOCK_FRESH_USER_KEY, MOCK_ONBOARDING_KEY, MOCK_SAMPLES_KEY, MOCK_SAMPLE_MS_KEY, MOCK_SAMPLES_OFF_KEY, "mapi.noTour", "mapi.tour"]) localStorage.removeItem(k);
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  resetMocks();
  useUi.setState({ persona: null, personas: {}, navMoreOpen: false });
  useTourStore.setState({ active: null, suppressed: false });
});
afterEach(() => {
  resetMocks();
  server.resetHandlers();
});
afterAll(() => server.close());

/** Fresh handlers (the mocks read localStorage when they are made). */
function useFreshMocks(opts: { fresh?: boolean; sampleMs?: number } = {}) {
  if (opts.fresh) localStorage.setItem(MOCK_FRESH_USER_KEY, "1");
  if (opts.sampleMs) localStorage.setItem(MOCK_SAMPLE_MS_KEY, String(opts.sampleMs));
  // the onboarding mock reads the dismissed welcome only through these keys; keep tours quiet
  localStorage.setItem("mapi.noTour", "1");
  server.resetHandlers(...createHandlers());
}

const sample = (patch: Partial<SampleInfo> = {}): SampleInfo => ({ id: "geo_mmm", title: "Marketing mix", summary: "Two years of weekly sales.", minutes: 1, attempts: 0, ...patch });

// ------------------------------------------------------------------------------ pure rules

describe("the hub's rules", () => {
  it("greets by the clock and by first name only", () => {
    expect(greeting(new Date(2026, 0, 1, 9))).toBe("Good morning");
    expect(greeting(new Date(2026, 0, 1, 14))).toBe("Good afternoon");
    expect(greeting(new Date(2026, 0, 1, 20))).toBe("Good evening");
    expect(firstName("Ana Analyst")).toBe("Ana");
    expect(firstName("ana@acme.example")).toBeNull();
    expect(firstName("usr_01jaf3a9b8c7d6e5f4g3h2j1k0")).toBeNull();
    expect(firstName("")).toBeNull();
  });

  it("folds when the checklist is done, hidden or tips are off", () => {
    const s: OnboardingState = emptyOnboarding();
    expect(hubCollapsed(undefined)).toBe(false);
    expect(hubCollapsed(s)).toBe(false);
    expect(hubCollapsed({ ...s, tips_off: true })).toBe(true);
    expect(hubCollapsed({ ...s, checklist_dismissed: true })).toBe(true);
    const all = Object.fromEntries(Object.keys(CHECKLIST_COPY).map((k) => [k, "2026-10-01T00:00:00Z"]));
    expect(hubCollapsed({ ...s, checklist: all })).toBe(true);
  });

  it("opens the first step that is not done and counts in plain words", () => {
    expect(nextStep(emptyOnboarding())).toBe("took_tour");
    expect(nextStep({ ...emptyOnboarding(), checklist: { took_tour: "x", open_sample: "x" } })).toBe("fit_model");
    expect(summaryWords(0, 8)).toMatch(/^0 of 8 done/);
    expect(summaryWords(3, 8)).toBe("3 of 8 done. Nice going!");
    expect(summaryWords(7, 8)).toBe("7 of 8 done. Nearly there!");
    expect(summaryWords(8, 8)).toMatch(/All done/);
  });

  it("picks one sample card per state", () => {
    expect(sampleView(null)).toBe("none");
    expect(sampleView(sample({ state: "retired" }))).toBe("none");
    expect(sampleView(sample({ state: null, can_install: true }))).toBe("offer");
    expect(sampleView(sample({ state: null, can_install: false }))).toBe("ask");
    expect(sampleView(sample({ state: "provisioning" }))).toBe("working");
    expect(sampleView(sample({ state: "fitting", progress: 0.4 }))).toBe("working");
    expect(sampleView(sample({ state: "ready" }))).toBe("ready");
    expect(sampleView(sample({ state: "failed" }))).toBe("failed");
    expect(sampleView(sample({ state: "ready", hidden: true }))).toBe("hidden");
    expect(currentSample([sample({ state: "retired" }), sample({ id: "b", state: "ready" })])?.id).toBe("b");
  });

  it("says each stage without promising a time", () => {
    expect(stageWords({ state: "provisioning" })).toBe("Setting up the project");
    expect(stageWords({ state: "fitting", progress: 0.456 })).toBe("Fitting the model, 46%");
    expect(stageWords({ state: "fitting", progress: null })).toBe("Fitting the model");
    expect(minutesWords(1)).toBe("about a minute");
    expect(minutesWords(2.6)).toBe("about 3 minutes");
    expect(sampleReportPath(sample({ state: "ready", spec_id: "spec_x" }))).toBe("/guided/spec_x/report");
    expect(sampleReportPath(sample({ state: "fitting", spec_id: "spec_x" }))).toBeNull();
  });
});

// ------------------------------------------------------------------------------ Expert Home

describe("Start here on Expert Home", () => {
  it("opens for a first visit: hero, sample card, checklist and playground, above the activity", async () => {
    useFreshMocks({ fresh: true });
    renderApp("/", "ana");
    const hub = await screen.findByTestId("start-here", {}, T);
    expect(hub).toHaveAttribute("data-state", "open");
    // the welcome tour's first step points at the hero's actions, not the whole hub
    expect(hub).not.toHaveAttribute("data-tour");
    expect(hub.querySelector('[data-tour="home.start-here"]')).not.toBeNull();
    expect(within(hub).getByRole("heading", { name: /really driving your numbers/ })).toBeInTheDocument();
    expect(within(hub).getByText(/, Ana$/)).toBeInTheDocument();

    const card = await within(hub).findByTestId("sample-card", {}, T);
    expect(card).toHaveAttribute("data-state", "offer");
    expect(card).toHaveAttribute("data-tour", "sample.progress");
    expect(within(card).getByRole("button", { name: /Make the sample project/ })).toBeInTheDocument();

    const list = within(hub).getByTestId("checklist");
    expect(list).toHaveAttribute("data-tour", "home.checklist");
    expect(within(list).getAllByRole("listitem")).toHaveLength(8);
    expect(within(list).getByTestId("checklist-item-took_tour")).toHaveAttribute("aria-current", "step");
    expect(within(list).getByRole("progressbar", { name: "Your first steps" })).toHaveAttribute("aria-valuenow", "0");

    expect(await screen.findByTestId("playground", {}, T)).toHaveAttribute("data-tour", "home.playground");
    // the activity section and the guided link stay (e2e selects them)
    expect(await screen.findByRole("heading", { name: /run(s)? in flight|Nothing running/ }, T)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Start a guided analysis/ })).toHaveAttribute("href", "/guided");
  });

  it("folds into one line for someone who turned tips off, and opens again on a click", async () => {
    useFreshMocks();
    const user = userEvent.setup();
    renderApp("/", "ana");
    const banner = await screen.findByTestId("start-here", {}, T);
    expect(banner).toHaveAttribute("data-state", "collapsed");
    expect(within(banner).getByText(/Start here: 0 of 8 first steps done/)).toBeInTheDocument();
    expect(screen.queryByTestId("checklist")).toBeNull();
    await user.click(within(banner).getByRole("button", { name: /Show/ }));
    expect(await screen.findByTestId("checklist", {}, T)).toBeInTheDocument();
    expect(screen.getByTestId("start-here")).toHaveAttribute("data-state", "open");
  });

  it("hides the checklist on the server when it is dismissed", async () => {
    useFreshMocks({ fresh: true });
    const patches: unknown[] = [];
    server.events.on("request:start", ({ request }) => {
      if (request.method === "PATCH" && new URL(request.url).pathname.endsWith("/me/onboarding")) void request.clone().json().then((b) => patches.push(b));
    });
    try {
      const user = userEvent.setup();
      renderApp("/", "ana");
      const list = await screen.findByTestId("checklist", {}, T);
      await user.click(within(list).getByRole("button", { name: "Hide the checklist" }));
      await waitFor(() => expect(screen.getByTestId("start-here")).toHaveAttribute("data-state", "collapsed"));
      await waitFor(() => expect(patches).toContainEqual({ checklist_dismissed: true }));
    } finally {
      server.events.removeAllListeners();
    }
  });

  it("opens a step on a click and offers its action", async () => {
    useFreshMocks({ fresh: true });
    const user = userEvent.setup();
    renderApp("/", "ana");
    const list = await screen.findByTestId("checklist", {}, T);
    await user.click(within(list).getByRole("button", { name: /Make your first model recipe/ }));
    const item = within(list).getByTestId("checklist-item-make_your_own");
    expect(item).toHaveAttribute("aria-current", "step");
    expect(within(item).getByRole("link", { name: "Make a model recipe" })).toHaveAttribute("href", "/library?new=model");
    await user.click(within(list).getByRole("button", { name: /Make a dashboard/ }));
    expect(within(list).getByRole("link", { name: "Make a dashboard" })).toHaveAttribute("href", "/dashboards?new=true");
  });

  it("starts the tour from the first step", async () => {
    useFreshMocks({ fresh: true });
    const user = userEvent.setup();
    renderApp("/", "ana");
    const list = await screen.findByTestId("checklist", {}, T);
    await user.click(within(list).getByRole("button", { name: "Start the tour" }));
    expect(useTourStore.getState().active?.id).toBe("welcome");
  });

  it("makes the sample, shows its progress and then opens it", async () => {
    useFreshMocks({ fresh: true, sampleMs: 400 });
    const user = userEvent.setup();
    renderApp("/", "ana");
    const card = await screen.findByTestId("sample-card", {}, T);
    await user.click(within(card).getByRole("button", { name: /Make the sample project/ }));
    expect(await screen.findByTestId("sample-progress", {}, T)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Sample project progress" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("sample-card")).toHaveAttribute("data-state", "ready"), { timeout: 8000 });
    expect(within(screen.getByTestId("sample-card")).getByRole("button", { name: /Open the sample project/ })).toBeInTheDocument();
  }, 15_000);

  it("hides the sample card when samples are off", async () => {
    localStorage.setItem(MOCK_SAMPLES_OFF_KEY, "1");
    useFreshMocks({ fresh: true });
    renderApp("/", "ana");
    const hub = await screen.findByTestId("start-here", {}, T);
    await screen.findByTestId("checklist", {}, T);
    expect(within(hub).queryByTestId("sample-card")).toBeNull();
    expect(within(hub).getByRole("button", { name: /Take the 2-minute tour/ })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------ Guided, Settings, Members

describe("the Guided strip", () => {
  it("shows the next step and the sample card on the Guided start page", async () => {
    useFreshMocks({ fresh: true });
    useUi.setState({ persona: "guided", personas: {} });
    renderApp("/guided", "ana");
    const strip = await screen.findByTestId("start-here-strip", {}, T);
    expect(within(strip).getByText("Next up")).toBeInTheDocument();
    expect(within(strip).getByText(CHECKLIST_COPY.took_tour.title)).toBeInTheDocument();
    expect(await within(strip).findByTestId("sample-card", {}, T)).toHaveAttribute("data-variant", "strip");
  });

  it("stays away once tips are off", async () => {
    useFreshMocks();
    useUi.setState({ persona: "guided", personas: {} });
    renderApp("/guided", "ana");
    expect(await screen.findByRole("heading", { name: "What do you want to learn?" }, T)).toBeInTheDocument();
    await act(async () => new Promise((r) => setTimeout(r, 300)));
    expect(screen.queryByTestId("start-here-strip")).toBeNull();
  });
});

describe("Settings › Tips and tours", () => {
  it("resets the tips and switches them on and off", async () => {
    useFreshMocks();
    const calls: string[] = [];
    server.events.on("request:start", ({ request }) => {
      const path = new URL(request.url).pathname;
      if (path.includes("/me/onboarding")) calls.push(`${request.method} ${path.split("/").pop()}`);
    });
    try {
      const user = userEvent.setup();
      renderApp("/settings", "ana");
      const tips = await screen.findByRole("switch", { name: "Show tips and the Start here checklist" }, T);
      await waitFor(() => expect(tips).not.toBeDisabled());
      expect(tips).not.toBeChecked();
      await user.click(tips);
      await waitFor(() => expect(tips).toBeChecked());
      await user.click(screen.getByRole("button", { name: /Reset tips/ }));
      await waitFor(() => expect(calls).toContain("POST onboarding:reset"));
      expect(calls).toContain("PATCH onboarding");
    } finally {
      server.events.removeAllListeners();
    }
  });
});

describe("Members of the sample project", () => {
  it("carry a Sample badge and the tenant admin can retire it", async () => {
    localStorage.setItem(MOCK_SAMPLES_KEY, JSON.stringify({ acme: { installedAt: Date.now() - 60_000, installer: "usr_01jaf3a9b8c7d6e5f4g3h2j1k0", attempts: 1, retired: false, seeded: true } }));
    useFreshMocks();
    renderApp("/members", "ana", { tenantId: "acme", projectId: SAMPLE_PROJECT });
    expect(await screen.findByTestId("sample-badge", {}, T)).toHaveTextContent("Sample");
    fireEvent.click(screen.getByRole("button", { name: "Retire the sample" }));
    expect(await screen.findByRole("dialog", { name: "Retire the sample project?" })).toBeInTheDocument();
  });

  it("carry no badge in an ordinary project", async () => {
    useFreshMocks();
    renderApp("/members", "ana");
    expect(await screen.findByRole("heading", { name: "Members" }, T)).toBeInTheDocument();
    await act(async () => new Promise((r) => setTimeout(r, 300)));
    expect(screen.queryByTestId("sample-badge")).toBeNull();
  });
});

describe("the dashboards prefill", () => {
  it("opens the New dashboard dialog from ?new=1, and closing it drops the flag", async () => {
    useFreshMocks();
    const user = userEvent.setup();
    const { router } = renderApp("/dashboards?new=1&model=eu-geo-mmm", "ana");
    const dialog = await screen.findByRole("dialog", { name: "New client dashboard" }, T);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    await waitFor(() => expect(router.state.location.search).not.toHaveProperty("new"));
  });
});
