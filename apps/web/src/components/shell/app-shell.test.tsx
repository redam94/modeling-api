/**
 * The v0.21 shell: the Search trigger and its key hint, "g x" chords that follow the nav mode,
 * and the Help group of the account menu.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { useRecents } from "@/features/search/recents";
import { createHandlers } from "@/mocks/handlers";
import { useUi } from "@/stores/ui";
import { renderApp } from "@/test/render-app";

const docs = vi.hoisted(() => ({ url: null as string | null }));
vi.mock("@/env", async (orig) => {
  const m = await orig<typeof import("@/env")>();
  return {
    ...m,
    env: new Proxy(m.env, { get: (t, k) => (k === "docsUrl" ? docs.url : t[k as keyof typeof t]) }),
  };
});

const server = setupServer(...createHandlers());
const T = { timeout: 5000 };
const platform = Object.getOwnPropertyDescriptor(Navigator.prototype, "platform");
function setPlatform(value: string) {
  Object.defineProperty(navigator, "platform", { value, configurable: true });
}
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  localStorage.setItem("mapi.noTour", "1");
  useUi.setState({ persona: null, personas: {}, navMoreOpen: false, paletteOpen: false, shortcutsOpen: false });
  docs.url = null;
});
afterEach(() => {
  localStorage.removeItem("mapi.noTour");
  localStorage.removeItem("mapi.mock.guidedLocked");
  // back to the prototype's getter
  delete (navigator as unknown as Record<string, unknown>).platform;
  if (platform) Object.defineProperty(Navigator.prototype, "platform", platform);
  server.resetHandlers();
});
afterAll(() => server.close());

describe("the Search trigger", () => {
  it("is one button named Search that opens the palette, with Ctrl K as its hint", async () => {
    setPlatform("Win32");
    renderApp("/", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    const triggers = screen.getAllByRole("button", { name: "Search" });
    expect(triggers).toHaveLength(1);
    const trigger = triggers[0]!;
    expect(trigger).toHaveTextContent("Search");
    expect(within(trigger).getByText("Ctrl K").tagName).toBe("KBD");
    expect(screen.queryByRole("button", { name: /Jump to/ })).toBeNull();
    expect(useUi.getState().paletteOpen).toBe(false);
    await userEvent.setup().click(trigger);
    expect(useUi.getState().paletteOpen).toBe(true);
  });

  it("shows the Command symbol on an Apple platform", async () => {
    setPlatform("MacIntel");
    renderApp("/", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    const trigger = screen.getByRole("button", { name: "Search" });
    expect(within(trigger).getByText("⌘K").tagName).toBe("KBD");
    expect(within(trigger).queryByText("Ctrl K")).toBeNull();
  });
});

describe("the g chords", () => {
  it("go to an Expert page in Expert mode", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    await user.keyboard("gr");
    await waitFor(() => expect(router.state.location.pathname).toBe("/runs"), T);
    await user.keyboard("g,");
    await waitFor(() => expect(router.state.location.pathname).toBe("/settings"), T);
  });

  it("do not go to a page that a Guided-locked sidebar hides", async () => {
    localStorage.setItem("mapi.mock.guidedLocked", "acme/growth");
    server.resetHandlers(...createHandlers());
    const user = userEvent.setup();
    const { router } = renderApp("/guided", "rui");
    const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
    await waitFor(() => expect(within(nav).queryByRole("link", { name: "Datasets" })).toBeNull());
    for (const chord of ["gr", "gd", "gm", "gb", "gk"]) {
      await user.keyboard(chord);
      await new Promise((r) => setTimeout(r, 30));
      expect(router.state.location.pathname).toBe("/guided");
    }
    // a page the locked sidebar shows still answers its chord
    await user.keyboard("gi");
    await waitFor(() => expect(router.state.location.pathname).toBe("/integrations"), T);
  });

  it("go to every page of Guided mode, More included", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    const user = userEvent.setup();
    const { router } = renderApp("/guided", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    await user.keyboard("gd");
    await waitFor(() => expect(router.state.location.pathname).toBe("/datasets"), T);
    await user.keyboard("gr");
    await waitFor(() => expect(router.state.location.pathname).toBe("/runs"), T);
  });
});

describe("the account menu", () => {
  async function openMenu() {
    const user = userEvent.setup();
    renderApp("/", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    const trigger = screen.getByRole("button", { name: /^Account:/ });
    expect(trigger).toHaveAttribute("data-tour", "shell.help-menu");
    await user.click(trigger);
    return { user, menu: await screen.findByRole("menu") };
  }

  it("has a Help group: the keyboard shortcuts, then the tours", async () => {
    const { user, menu } = await openMenu();
    const help = within(menu).getByRole("group", { name: "Help" });
    expect(within(help).getAllByRole("menuitem").map((i) => i.textContent?.trim())).toEqual([
      "Keyboard shortcuts",
      "Take the 2-minute tour",
      "Tour: bring in data",
      "Tour: fit a model",
      "Tour: read the results",
      "Tour: try a what-if",
      "Tour: make a dashboard",
      "Tour: make your own",
    ]);
    // no docs address is set: no docs link
    expect(within(menu).queryByRole("menuitem", { name: "Read the docs" })).toBeNull();
    // the entries that were there stay
    for (const name of ["API tokens", "Settings", "Sign out"]) expect(within(menu).getByRole("menuitem", { name })).toBeInTheDocument();
    expect(useUi.getState().shortcutsOpen).toBe(false);
    await user.click(within(help).getByRole("menuitem", { name: "Keyboard shortcuts" }));
    expect(useUi.getState().shortcutsOpen).toBe(true);
  });

  it("removes the recent items of the search from this browser on sign-out", async () => {
    // the lists hold names and descriptions: the next person on this browser must not get them
    useRecents.setState({ owner: null, byScope: {} });
    useRecents.getState().add("acme/growth", { group: "datasets", title: "ACQUISITION TARGET due diligence", secondary: "Confidential: Project Falcon", href: "/datasets/ds_01jaf3a1b2c3d4e5f6g7h8j9k0" });
    expect(localStorage.getItem("mapi.search.recent")).toContain("ACQUISITION TARGET");
    // jsdom has no navigation: the reload at "/" is a call that the test only counts
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    try {
      const { user, menu } = await openMenu();
      await user.click(within(menu).getByRole("menuitem", { name: "Sign out" }));
      await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
      expect(localStorage.getItem("mapi.search.recent")).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("links to the docs in a new tab when the docs address is set", async () => {
    docs.url = "https://docs.example.test/";
    const { menu } = await openMenu();
    const help = within(menu).getByRole("group", { name: "Help" });
    const link = within(help).getByRole("menuitem", { name: "Read the docs" });
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", "https://docs.example.test/");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(within(help).getAllByRole("menuitem")[0]).toBe(link);
  });
});
