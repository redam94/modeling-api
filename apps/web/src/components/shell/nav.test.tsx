/**
 * The v0.18 navigation (D23, WP-T6): Guided mode's seven items and "More" (remembered, opens
 * by itself on one of its pages), Expert mode's three labelled groups with the v0.17 labels
 * and chords, Make your own, the locked nav unchanged, and the tour anchors on the shell.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { useTourStore } from "@/features/onboarding/use-onboarding";
import { useUi } from "@/stores/ui";
import { renderApp } from "@/test/render-app";

import { EXPERT_NAV, EXPERT_NAV_GROUPS, GUIDED_MODE_NAV, GUIDED_MORE_NAV, GUIDED_PRIMARY_NAV, inGuidedMore, LIBRARY_NAV, LOCKED_NAV } from "./nav";
import { MENU_TOURS, withTourParam } from "./user-menu";

const server = setupServer(...createHandlers());
const T = { timeout: 5000 };
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  localStorage.setItem("mapi.noTour", "1");
  useUi.setState({ persona: null, personas: {}, navMoreOpen: false });
});
afterEach(() => {
  localStorage.removeItem("mapi.noTour");
  server.resetHandlers();
});
afterAll(() => server.close());

describe("the nav lists", () => {
  it("gives Guided mode seven items, then the rest behind More", () => {
    expect(GUIDED_PRIMARY_NAV.map((n) => n.label)).toEqual(["Guided", "Help me choose", "Recipes", "Dashboards", "Datasets", "Models", "Make your own"]);
    expect(GUIDED_MORE_NAV.map((n) => n.label)).toEqual(["Build", "Runs", "Experiments", "Score", "Governance", "Integrations", "Catalog"]);
    expect(GUIDED_MODE_NAV).toEqual([...GUIDED_PRIMARY_NAV, ...GUIDED_MORE_NAV]);
    expect(inGuidedMore("/runs")).toBe(true);
    expect(inGuidedMore("/runs/run_1")).toBe(true);
    expect(inGuidedMore("/models")).toBe(false);
    expect(inGuidedMore("/guided")).toBe(false);
  });

  it("keeps Expert labels and chords, in three groups, plus Make your own", () => {
    expect(EXPERT_NAV_GROUPS.map((g) => g.label)).toEqual(["Work", "Automate & share", "Learn"]);
    expect(EXPERT_NAV_GROUPS[2]?.items.map((n) => n.label)).toEqual(["Catalog", "Make your own", "Help me choose"]);
    const v017 = ["Home", "Guided", "Datasets", "Build", "Runs", "Experiments", "Models", "Score", "Workflows", "Governance", "Integrations", "Dashboards", "Catalog", "Help me choose"];
    expect(EXPERT_NAV.map((n) => n.label).filter((l) => l !== LIBRARY_NAV.label)).toEqual(v017);
    expect(EXPERT_NAV.map((n) => `${n.label}:${n.chord}`).filter((c) => !c.startsWith("Make"))).toEqual([
      "Home:h",
      "Guided:g",
      "Datasets:d",
      "Build:b",
      "Runs:r",
      "Experiments:e",
      "Models:m",
      "Score:s",
      "Workflows:w",
      "Governance:v",
      "Integrations:i",
      "Dashboards:k",
      "Catalog:c",
      "Help me choose:o",
    ]);
    expect(LIBRARY_NAV).toMatchObject({ to: "/library", label: "Make your own", chord: "y" });
    expect(LOCKED_NAV.map((n) => n.to)).toEqual(["/guided", "/choose", "/workflows", "/integrations"]);
  });

  it("adds ?tour= to the page for a tour from the account menu", () => {
    expect(MENU_TOURS.map((t) => t.id)).toEqual(["welcome", "data", "fit", "results", "whatif", "dashboard", "make_your_own"]);
    expect(withTourParam("/runs", "?state=failed", "fit")).toBe("/runs?state=failed&tour=fit");
    expect(withTourParam("/", "", "welcome")).toBe("/?tour=welcome");
  });
});

describe("the sidebar", () => {
  it("shows Expert groups and the tour anchors", async () => {
    renderApp("/", "ana");
    const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
    expect(nav).toHaveAttribute("data-tour", "nav.primary");
    for (const g of ["Work", "Automate & share", "Learn"]) expect(within(nav).getByRole("group", { name: g })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Make your own" })).toHaveAttribute("data-tour", "nav.make-your-own");
    expect(within(nav).getByRole("link", { name: "Catalog" })).toHaveAttribute("data-tour", "nav.catalog");
    expect(within(nav).getByRole("link", { name: "Home" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).queryByTestId("nav-more")).toBeNull();
    expect(screen.getByRole("button", { name: /^Account:/ })).toHaveAttribute("data-tour", "shell.help-menu");
  });

  it("puts Guided mode's other areas behind More, and remembers it open", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    const user = userEvent.setup();
    renderApp("/guided", "ana");
    const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
    const more = within(nav).getByTestId("nav-more");
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(more).toHaveAttribute("data-tour", "nav.more");
    expect(within(nav).getByRole("link", { name: "Make your own" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Build" })).toBeNull();
    await user.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(within(nav).getByRole("link", { name: "Build" })).toBeInTheDocument();
    expect(useUi.getState().navMoreOpen).toBe(true);
    await user.click(more);
    expect(within(nav).queryByRole("link", { name: "Build" })).toBeNull();
  });

  it("keeps a ?tour link when Guided mode moves Home to /guided (v0.18 live)", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    const { router } = renderApp("/?tour=results&from=docs", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    await waitFor(() => expect(router.state.location.pathname).toBe("/guided"), T);
    // the tour host may already have taken `tour` off; the rest of the query must survive
    await waitFor(() => expect(router.state.location.searchStr).toContain("from=docs"), T);
    const kept = router.state.location.searchStr.includes("tour=results") || useTourStore.getState().active?.id === "results";
    expect(kept).toBe(true);
  });

  it("opens More by itself on one of its pages", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    renderApp("/runs", "ana");
    const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
    await waitFor(() => expect(within(nav).getByTestId("nav-more")).toHaveAttribute("aria-expanded", "true"));
    expect(within(nav).getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
    // v0.18 review: it can still be folded there (the button was dead on More pages)
    await userEvent.setup().click(within(nav).getByTestId("nav-more"));
    expect(within(nav).getByTestId("nav-more")).toHaveAttribute("aria-expanded", "false");
    expect(within(nav).queryByRole("link", { name: "Runs" })).toBeNull();
  });

  it("keeps a Guided-locked project's nav as it was", async () => {
    localStorage.setItem("mapi.mock.guidedLocked", "acme/growth");
    server.resetHandlers(...createHandlers());
    try {
      renderApp("/guided", "rui");
      const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
      await waitFor(() => expect(within(nav).queryByRole("link", { name: "Datasets" })).toBeNull());
      const project = new Set(["Usage", "Members", "Tokens"]);
      expect(within(nav).getAllByRole("link").map((a) => a.textContent).filter((l) => !project.has(l ?? ""))).toEqual(["Guided", "Help me choose", "Recipes", "Integrations"]);
      expect(within(nav).queryByTestId("nav-more")).toBeNull();
    } finally {
      localStorage.removeItem("mapi.mock.guidedLocked");
    }
  });
});

describe("the UI store", () => {
  it("migrates v3 to v4 with More closed", () => {
    const migrate = useUi.persist.getOptions().migrate!;
    expect(migrate({ theme: "dark", persona: "guided", personas: {}, sidebarCollapsed: true }, 3)).toMatchObject({ persona: "guided", navMoreOpen: false, sidebarCollapsed: true });
    expect(useUi.persist.getOptions().version).toBe(4);
  });
});
