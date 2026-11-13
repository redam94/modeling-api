/**
 * The global search (v0.21): the command palette finds pages, actions and the user's own things
 * by name, in groups, with recent items, jumps by id and documentation pages. It offers only
 * what the sidebar shows and what the caller can read.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/env";
import { useRecents } from "@/features/search/recents";
import { MMM_DASHBOARD_ID } from "@/mocks/dashboards";
import * as fx from "@/mocks/fixtures";
import { createHandlers, createState } from "@/mocks/handlers";
import { useScope } from "@/stores/scope";
import { useUi } from "@/stores/ui";
import { renderApp } from "@/test/render-app";

// The index is generated from the docs (`scripts/gen_docs_search_index.py`): a fixed copy here.
vi.mock("@/features/search/docs-index.gen.json", () => ({
  default: [
    { t: "Build a client dashboard", s: "Guides", p: "guides/client-dashboards/", h: [["Publish the dashboard", "publish-the-dashboard"]] },
    { t: "Error codes", s: "Reference", p: "reference/error-codes/", h: [["Dashboard errors", "dashboard-errors"]] },
    { t: "Home", s: "Home", p: "", h: [] },
  ],
}));

const API = "http://127.0.0.1:8000";
const T = { timeout: 5000 };
const server = setupServer(...createHandlers());
const seen: URL[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(new URL(request.url));
  });
  window.scrollTo = () => undefined;
  Element.prototype.scrollIntoView = vi.fn();
});
beforeEach(() => {
  seen.length = 0;
  localStorage.setItem("mapi.noTour", "1");
  useUi.setState({ persona: "expert", personas: {}, navMoreOpen: false, paletteOpen: false, shortcutsOpen: false, theme: "system" });
  useRecents.setState({ owner: null, byScope: {} });
  (env as { docsUrl: string | null }).docsUrl = null;
});
afterEach(() => {
  server.resetHandlers();
  (env as { docsUrl: string | null }).docsUrl = null;
});
afterAll(() => server.close());

/** The app as `persona`, with the search open through the keyboard. */
async function openSearch(persona: Parameters<typeof renderApp>[1] = "ana", url = "/settings") {
  const user = userEvent.setup();
  const app = renderApp(url, persona);
  await screen.findByRole("navigation", { name: "Primary" }, T);
  await user.keyboard("{Control>}k{/Control}");
  const box = await screen.findByRole("combobox", { name: "Search" });
  return { user, box, ...app };
}

const listbox = () => screen.getByRole("listbox");
const groupNames = () =>
  within(listbox())
    .getAllByRole("group")
    .map((g) => document.getElementById(g.getAttribute("aria-labelledby") ?? "")?.textContent);
const group = (name: string) => within(listbox()).getByRole("group", { name });
const options = (name: string) => within(group(name)).getAllByRole("option");
const titleOf = (option: Element) => option.querySelector("[data-title]")?.textContent;
/** The row with `title` in the group `name` (a mark splits the accessible name of a row). */
const row = (name: string, title: string) => options(name).find((o) => titleOf(o) === title);
const activeOption = () => within(listbox()).getByRole("option", { selected: true });
/** The titles of all the rows (a mark splits the accessible name of a row: do not look for a row by its name). */
const allTitles = () => within(listbox()).queryAllByRole("option").map(titleOf);
/** Wait until no remote group is loading. */
const settled = () => waitFor(() => expect(listbox().querySelector("[aria-busy='true']")).toBeNull(), T);
const path = (router: { state: { location: { pathname: string } } }) => router.state.location.pathname;

async function type(user: UserEvent, text: string) {
  await user.type(screen.getByRole("combobox", { name: "Search" }), text);
}

const dataset = (n: number) => ({ ...fx.dataset, id: `ds_01jaf3cap${n}aaaaaaaaaaaaaaaaaa`, name: `cap_test_${n}`, updated_at: `2026-09-0${n}T08:00:00Z` });

describe("opening the search", () => {
  it("opens with Ctrl+K and with the / key, as a named combobox that controls a listbox", async () => {
    const { user, box } = await openSearch();
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute("aria-autocomplete", "list");
    expect(box).toHaveAttribute("aria-expanded", "true");
    expect(box.getAttribute("aria-controls")).toBe(listbox().id);
    const active = activeOption();
    expect(box.getAttribute("aria-activedescendant")).toBe(active.id);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.keyboard("/");
    expect(await screen.findByRole("combobox", { name: "Search" })).toHaveValue("");
  });

  it("shows the key hints", async () => {
    await openSearch();
    const hints = screen.getByTestId("search-hints");
    expect(hints).toHaveTextContent("Move");
    expect(hints).toHaveTextContent("Open");
    expect(hints).toHaveTextContent("Close");
  });
});

describe("with no text", () => {
  it("shows three examples to try, then the pages and the actions", async () => {
    const { user, box } = await openSearch();
    expect(groupNames()).toEqual(["Try", "Go to", "Actions"]);
    expect(options("Try")).toHaveLength(3);
    expect(within(group("Go to")).getByRole("option", { name: /^Datasets/ })).toBeInTheDocument();
    expect(within(group("Actions")).getByRole("option", { name: /^New spec/ })).toBeInTheDocument();
    expect(within(group("Actions")).getByRole("option", { name: /^Switch to Acme Retail \/ Retail demand/ })).toBeInTheDocument();
    // no request for a list before the user types
    expect(seen.some((u) => u.pathname === "/v1/library")).toBe(false);
    // an example fills the box
    const first = options("Try")[0]!;
    const example = first.querySelector("[data-title]")?.textContent ?? "";
    expect(example.length).toBeGreaterThan(1);
    await user.keyboard("{Enter}");
    expect(box).toHaveValue(example);
    expect(screen.getByRole("dialog", { name: "Search" })).toBeInTheDocument();
  });

  it("shows the recent items of this project first, newest first, and clears them", async () => {
    useRecents.getState().add("acme/growth", { group: "datasets", title: "eu_media_daily", secondary: "Daily spend", href: `/datasets/${fx.DS_ID}` });
    useRecents.getState().add("acme/growth", { group: "runs", title: "Nightly fit", href: "/runs/run_01jaf3aaaaaaaaaaaaaaaaaaaaaa" });
    useRecents.getState().add("acme/retail", { group: "models", title: "Other project model", href: "/models/mdl_01jaf3aaaaaaaaaaaaaaaaaaaaaa" });
    const { user, router } = await openSearch();
    expect(groupNames()).toEqual(["Recent", "Go to", "Actions"]);
    expect(options("Recent").map(titleOf)).toEqual(["Nightly fit", "eu_media_daily", "Clear recent items"]);
    await user.keyboard("{ArrowDown}{Enter}");
    await waitFor(() => expect(path(router)).toBe(`/datasets/${fx.DS_ID}`));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox", { name: "Search" });
    // the dataset is now the newest
    expect(options("Recent")[0]).toHaveTextContent("eu_media_daily");
    await user.click(within(group("Recent")).getByRole("option", { name: /^Clear recent items/ }));
    expect(groupNames()).toEqual(["Try", "Go to", "Actions"]);
    expect(useRecents.getState().list("acme/growth")).toEqual([]);
    expect(useRecents.getState().list("acme/retail")).toHaveLength(1);
  });
});

describe("with text", () => {
  it("shows the results in labelled groups, in order, with the match marked", async () => {
    const { user } = await openSearch();
    await type(user, "media");
    await settled();
    const names = groupNames();
    // the pages, then the names, then the actions: an action never goes before a name
    const ORDER = ["Go to", "Datasets", "Models", "Specs", "Runs", "Experiments", "Dashboards", "Workflows", "Endpoints", "Catalog", "Make your own", "Actions", "Docs"];
    expect(names).toEqual(ORDER.filter((n) => names.includes(n)));
    expect(names).toContain("Datasets");
    expect(names).toContain("Dashboards");
    const found = row("Datasets", "eu_media_daily")!;
    const mark = found.querySelector("mark");
    expect(mark).toHaveTextContent("media");
    expect(mark?.className).toContain("decoration-chart-now");
    expect(mark?.className).not.toMatch(/\bbg-(?!transparent)/);
    expect(found).toHaveTextContent("Daily spend and revenue per market");
    expect(found).toHaveTextContent(/Dataset$/);
    expect(row("Dashboards", "Weekly media readout")).toBeDefined();
    expect(await screen.findByRole("status")).toHaveTextContent(/^\d+ results$/);
  });

  it("shows at most five rows of a group, the newest first, and a row that opens the list page with the text", async () => {
    server.use(http.get(`${API}/v1/datasets`, () => HttpResponse.json({ items: [1, 2, 3, 4, 5, 6, 7, 8].map(dataset), next_cursor: null })));
    const { user, router } = await openSearch();
    await type(user, "cap_test");
    await settled();
    const rows = options("Datasets");
    expect(rows.map(titleOf)).toEqual(["cap_test_8", "cap_test_7", "cap_test_6", "cap_test_5", "cap_test_4", "See all in Datasets"]);
    await user.click(rows[5]!);
    await waitFor(() => expect(path(router)).toBe("/datasets"));
    expect(router.state.location.search).toMatchObject({ q: "cap_test" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("asks the server after a pause, with two characters or more, by name and with a limit", async () => {
    const { user } = await openSearch();
    await type(user, "g");
    await new Promise((r) => setTimeout(r, 400));
    expect(seen.some((u) => u.pathname === "/v1/runs" && u.searchParams.has("name"))).toBe(false);
    expect(within(listbox()).queryByRole("group", { name: "Runs" })).toBeNull();
    await type(user, "eo");
    await settled();
    const runs = seen.filter((u) => u.pathname === "/v1/runs" && u.searchParams.has("name"));
    expect(runs.map((u) => [u.searchParams.get("name"), u.searchParams.get("limit")])).toEqual([["geo", "5"]]);
    expect(seen.some((u) => u.pathname === "/v1/experiments" && u.searchParams.get("name") === "geo")).toBe(true);
    expect(seen.some((u) => u.pathname === "/v1/library" && u.searchParams.get("q") === "geo" && u.searchParams.get("limit") === "5")).toBe(true);
    expect(seen.some((u) => u.pathname === "/v1/datasets" && u.searchParams.get("limit") === "200")).toBe(true);
    expect(within(group("Experiments")).getByRole("option", { name: /Geo adstock study/ })).toBeInTheDocument();
  });

  it("shows a loading line for a slow group and still shows the other groups; a failed group shows nothing", async () => {
    server.use(
      http.get(`${API}/v1/datasets`, async () => {
        await delay(600);
        return HttpResponse.json({ items: [dataset(1)], next_cursor: null });
      }),
      http.get(`${API}/v1/dashboards`, () => HttpResponse.json({ type: "about:blank", title: "Server error", status: 500, code: "INTERNAL" }, { status: 500 })),
    );
    const { user } = await openSearch();
    await type(user, "model");
    // the local group is there at once
    expect(groupNames()[0]).toBe("Go to");
    expect(options("Go to").map(titleOf)).toEqual(["Models"]);
    await waitFor(() => expect(within(listbox()).getByRole("group", { name: "Datasets" })).toHaveAttribute("aria-busy", "true"));
    expect(within(group("Datasets")).queryByRole("option")).toBeNull();
    await settled();
    expect(within(listbox()).queryByRole("group", { name: "Datasets" })).toBeNull();
    expect(within(listbox()).queryByRole("group", { name: "Dashboards" })).toBeNull();
    expect(within(group("Catalog")).getAllByRole("option").length).toBeGreaterThan(0);
  });

  it("opens a thing by its pasted id", async () => {
    const { user, router } = await openSearch();
    await user.click(screen.getByRole("combobox", { name: "Search" }));
    await user.paste(MMM_DASHBOARD_ID);
    const jump = within(group("Go to")).getByRole("option", { name: new RegExp(`^Open dashboard ${MMM_DASHBOARD_ID}`) });
    expect(jump).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(path(router)).toBe(`/dashboards/${MMM_DASHBOARD_ID}`));
    expect(useRecents.getState().list("acme/growth")[0]).toMatchObject({ group: "dashboards", href: `/dashboards/${MMM_DASHBOARD_ID}` });
  });

  it("says that there are no results and offers Help me choose", async () => {
    const { user, router } = await openSearch();
    await type(user, "zzqqxx");
    await settled();
    expect(await screen.findByText("No page, action or item matches this text.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("No results"));
    expect(within(listbox()).queryByRole("option", { name: /Search the docs/ })).toBeNull();
    await user.click(within(listbox()).getByRole("option", { name: /^Open Help me choose/ }));
    await waitFor(() => expect(path(router)).toBe("/choose"));
  });

  it("still opens a page and runs an action by name", async () => {
    const { user, router } = await openSearch();
    await type(user, "runs");
    expect(activeOption()).toHaveTextContent("Runs");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(path(router)).toBe("/runs"));
    await user.keyboard("{Control>}k{/Control}");
    await type(user, "dark theme");
    expect(activeOption()).toHaveTextContent("Use dark theme");
    await user.keyboard("{Enter}");
    expect(useUi.getState().theme).toBe("dark");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("the documentation group", () => {
  it("is not there without a documentation URL", async () => {
    const { user } = await openSearch();
    await type(user, "dashboard");
    await settled();
    expect(groupNames()).not.toContain("Docs");
  });

  it("finds pages and headings and opens them in a new tab", async () => {
    (env as { docsUrl: string | null }).docsUrl = "https://docs.example.test/";
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { user } = await openSearch();
    await type(user, "dashboard");
    await settled();
    const rows = await waitFor(() => options("Docs"));
    // the last row goes on to the search of the docs site
    expect(rows.map(titleOf)).toEqual(["Dashboard errors", "Build a client dashboard", "Publish the dashboard", "Search the docs for “dashboard”"]);
    expect(rows[0]).toHaveTextContent("Reference · Error codes");
    expect(rows[1]).toHaveTextContent("Guides");
    expect(rows[0]?.querySelector("mark")).toHaveTextContent("Dashboard");
    expect(groupNames().at(-1)).toBe("Docs");
    await user.click(rows[0]!);
    expect(open).toHaveBeenCalledWith("https://docs.example.test/reference/error-codes/#dashboard-errors", "_blank", "noopener,noreferrer");
  });

  it("offers a search of the docs when there are no results", async () => {
    (env as { docsUrl: string | null }).docsUrl = "https://docs.example.test/";
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { user } = await openSearch();
    await type(user, "zz qq");
    await settled();
    await user.click(await within(listbox()).findByRole("option", { name: /^Search the docs/ }));
    expect(open).toHaveBeenCalledWith("https://docs.example.test/?q=zz%20qq", "_blank", "noopener,noreferrer");
  });
});

describe("the keyboard", () => {
  it("moves across the groups, to the ends, and scrolls the active row into view", async () => {
    const { user, box } = await openSearch();
    const all = within(listbox()).getAllByRole("option");
    expect(all[0]).toHaveAttribute("aria-selected", "true");
    // three examples, then the first page: the fourth row is in the next group
    await user.keyboard("{ArrowDown}{ArrowDown}{ArrowDown}");
    expect(activeOption()).toBe(options("Go to")[0]);
    expect(box.getAttribute("aria-activedescendant")).toBe(options("Go to")[0]!.id);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    await user.keyboard("{ArrowUp}");
    expect(activeOption()).toBe(options("Try")[2]);
    await user.keyboard("{End}");
    expect(activeOption()).toBe(all.at(-1));
    await user.keyboard("{ArrowDown}");
    expect(activeOption()).toBe(all.at(-1));
    await user.keyboard("{Home}");
    expect(activeOption()).toBe(all[0]);
    await user.keyboard("{ArrowUp}");
    expect(activeOption()).toBe(all[0]);
    expect(within(listbox()).getAllByRole("option", { selected: true })).toHaveLength(1);
  });

  it("opens a link result in a new tab with Ctrl+Enter", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { user, router } = await openSearch();
    await type(user, "runs");
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(open).toHaveBeenCalledWith("/runs", "_blank", "noopener");
    expect(path(router)).toBe("/settings");
  });

  it("closes with Esc and gives the focus back to the opener", async () => {
    const user = userEvent.setup();
    renderApp("/settings", "ana");
    const nav = await screen.findByRole("navigation", { name: "Primary" }, T);
    const opener = within(nav).getAllByRole("link")[0]!;
    opener.focus();
    await user.keyboard("{Control>}k{/Control}");
    expect(await screen.findByRole("combobox", { name: "Search" })).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});

describe("what a caller may find", () => {
  it("offers a Guided-locked member only the pages of the sidebar, and no hidden group or id", async () => {
    localStorage.setItem("mapi.mock.guidedLocked", "acme/growth");
    server.resetHandlers(...createHandlers());
    try {
      const { user } = await openSearch("rui", "/guided");
      await waitFor(() => expect(within(screen.getByRole("navigation", { name: "Primary", hidden: true })).queryByRole("link", { name: "Datasets", hidden: true })).toBeNull());
      const pages = options("Go to").map(titleOf);
      expect(pages).toEqual(["Guided", "Help me choose", "Recipes", "Integrations", "Usage", "Members", "Tokens", "Settings"]);
      expect(within(group("Actions")).queryByRole("option", { name: /^New spec/ })).toBeNull();
      seen.length = 0;
      await type(user, "eu_media");
      await settled();
      expect(within(listbox()).queryByRole("group", { name: "Datasets" })).toBeNull();
      expect(within(listbox()).queryByRole("option", { name: /^Datasets/ })).toBeNull();
      expect(seen.some((u) => u.pathname === "/v1/datasets" || u.pathname === "/v1/runs" || u.pathname === "/v1/models")).toBe(false);
      await user.clear(screen.getByRole("combobox", { name: "Search" }));
      await user.paste(fx.DS_ID);
      expect(within(listbox()).queryByRole("option", { name: /^Open dataset/ })).toBeNull();
    } finally {
      localStorage.removeItem("mapi.mock.guidedLocked");
    }
  });

  it("does not open by id what the role cannot read", async () => {
    const id = "whk_01jaf3w1x2y3z4a5b6c7d8e9f0";
    const { user } = await openSearch("vic");
    await user.paste(id);
    expect(within(listbox()).queryByRole("option", { name: /^Open webhook/ })).toBeNull();
    // a viewer has no admin page and no project of another tenant
    await user.clear(screen.getByRole("combobox", { name: "Search" }));
    expect(within(group("Go to")).queryByRole("option", { name: /^Admin/ })).toBeNull();
  });

  it("opens a webhook by id for an admin", async () => {
    const id = "whk_01jaf3w1x2y3z4a5b6c7d8e9f0";
    const { user } = await openSearch("ana");
    await user.paste(id);
    expect(within(group("Go to")).getByRole("option", { name: new RegExp(`^Open webhook ${id}`) })).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ the fixes of the v0.21 review

/** Datasets with these names, in place of the fixture list. */
const datasetsNamed = (...names: string[]) =>
  http.get(`${API}/v1/datasets`, () => HttpResponse.json({ items: names.map((name, i) => ({ ...dataset(i + 1), name })), next_cursor: null }));

describe("the mouse", () => {
  it("leaves the keyboard focus in the box: the keys still go there after a click on a row, a heading, the clear button or the footer", async () => {
    const { user, box } = await openSearch();
    // an example fills the box and the overlay stays open
    await user.click(within(group("Try")).getByRole("option", { name: /^fit/ }));
    expect(box).toHaveValue("fit");
    expect(box).toHaveFocus();
    await user.keyboard("ness");
    expect(box).toHaveValue("fitness");
    await user.click(screen.getByRole("button", { name: "Clear the text" }));
    expect(box).toHaveFocus();
    await user.keyboard("runs");
    expect(box).toHaveValue("runs");
    await user.click(document.getElementById(group("Go to").getAttribute("aria-labelledby") ?? "")!);
    expect(box).toHaveFocus();
    await user.click(screen.getByTestId("search-hints"));
    expect(box).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("gives the focus back to the box when the keyboard presses the clear button", async () => {
    const { user, box } = await openSearch();
    await type(user, "runs");
    screen.getByRole("button", { name: "Clear the text" }).focus();
    await user.keyboard("{Enter}");
    expect(box).toHaveValue("");
    expect(box).toHaveFocus();
  });
});

describe("the order of pages, names and actions", () => {
  /** The live case: a second project "Default / Sample: marketing mix" and names that start with "eu". */
  function sampleProject() {
    const state = createState();
    const tenant = state.tenancy.tenants.find((t) => t.id === "acme");
    const project = state.tenancy.projects.get("acme/retail");
    if (!tenant || !project) throw new Error("the fixture has no acme/retail");
    tenant.name = "Default";
    project.name = "Sample: marketing mix";
    server.use(...createHandlers(state), datasetsNamed("eu_media_daily", "eu_media_future", "holidays_eu"));
  }

  it("does not find \"Switch to Default / Sample: marketing mix\" for \"eu\", and Enter opens a name that starts with the text", async () => {
    sampleProject();
    const { user, router } = await openSearch();
    expect(within(group("Actions")).getByRole("option", { name: /^Switch to Default \/ Sample: marketing mix/ })).toBeInTheDocument();
    await type(user, "eu");
    await settled();
    expect(allTitles().filter((t) => t?.startsWith("Switch to"))).toEqual([]);
    expect(groupNames()).not.toContain("Actions");
    expect(groupNames()[0]).toBe("Datasets");
    expect(titleOf(activeOption())).toMatch(/^eu_media_/);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(path(router)).toMatch(/^\/datasets\/ds_/));
    expect(useScope.getState().active).toMatchObject({ tenantId: "acme", projectId: "growth" });
  });

  it("puts an action that has the text as a word after the pages and the names", async () => {
    sampleProject();
    server.use(datasetsNamed("sample_sales", "eu_media_daily"));
    const { user, router } = await openSearch();
    await type(user, "sample");
    await settled();
    const names = groupNames();
    expect(names.indexOf("Datasets")).toBeGreaterThanOrEqual(0);
    expect(names.indexOf("Actions")).toBeGreaterThan(names.indexOf("Datasets"));
    expect(options("Actions").map(titleOf)).toEqual(["Switch to Default / Sample: marketing mix"]);
    expect(titleOf(activeOption())).toBe("sample_sales");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(path(router)).toMatch(/^\/datasets\/ds_/));
    expect(useScope.getState().active).toMatchObject({ projectId: "growth" });
  });

  it("does not find an action by letters in order, but still finds a page that way", async () => {
    const { user } = await openSearch();
    await type(user, "dkth");
    await settled();
    expect(allTitles()).not.toContain("Use dark theme");
    expect(groupNames()).not.toContain("Actions");
    await user.clear(screen.getByRole("combobox", { name: "Search" }));
    await type(user, "dts");
    expect(options("Go to").map(titleOf)).toContain("Datasets");
  });
});

describe("a list that answers late", () => {
  it("does not move the row the user is on, and Enter opens that row", async () => {
    server.use(
      http.get(`${API}/v1/datasets`, async () => {
        await delay(900);
        return HttpResponse.json({ items: [{ ...dataset(1), name: "media_plan" }, { ...dataset(2), name: "media_costs" }], next_cursor: null });
      }),
    );
    const { user, box, router } = await openSearch();
    await type(user, "media");
    await waitFor(() => expect(row("Dashboards", "Weekly media readout")).toBeDefined(), T);
    expect(group("Datasets")).toHaveAttribute("aria-busy", "true");
    // the user moves to a row while the datasets load
    await user.keyboard("{ArrowDown}{ArrowDown}");
    const chosen = activeOption();
    const title = titleOf(chosen);
    const key = (o: Element) => `${o.closest("[role=group]")?.getAttribute("aria-labelledby")}|${titleOf(o)}`;
    const chosenKey = key(chosen);
    const before = within(listbox()).getAllByRole("option").indexOf(chosen);
    await settled();
    // the datasets came in above the chosen row: it has a new place, and it is still the active row
    expect(options("Datasets").map(titleOf)).toEqual(["media_costs", "media_plan", "See all in Datasets"]);
    expect(key(activeOption())).toBe(chosenKey);
    expect(within(listbox()).getAllByRole("option").indexOf(activeOption())).toBeGreaterThan(before);
    expect(box.getAttribute("aria-activedescendant")).toBe(activeOption().id);
    expect(within(listbox()).getAllByRole("option", { selected: true })).toHaveLength(1);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    if (title === "Weekly media readout") await waitFor(() => expect(path(router)).toBe(`/dashboards/${MMM_DASHBOARD_ID}`));
    else expect(path(router)).not.toMatch(/^\/datasets/);
  });

  it("puts the active row on the first row again when the text changes", async () => {
    const { user } = await openSearch();
    await type(user, "media");
    await settled();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(activeOption()).not.toBe(within(listbox()).getAllByRole("option")[0]);
    await type(user, " ");
    await type(user, "r");
    await settled();
    expect(activeOption()).toBe(within(listbox()).getAllByRole("option")[0]);
  });
});

describe("\"See all\" and the list page", () => {
  it("shows an item only when each word of the text is in its name, as the list page does", async () => {
    const { user, router } = await openSearch();
    await type(user, "media zzz");
    await settled();
    // one word of two is not a match: the list page would be empty
    expect(within(listbox()).queryByRole("group", { name: "Datasets" })).toBeNull();
    expect(screen.getByText("No page, action or item matches this text.")).toBeInTheDocument();
    await user.clear(screen.getByRole("combobox", { name: "Search" }));
    await type(user, "eu daily");
    await settled();
    expect(options("Datasets").map(titleOf)).toEqual(["eu_media_daily", "See all in Datasets"]);
    for (const name of groupNames()) {
      if (name === "Go to" || name === "Actions" || name === "Docs") continue;
      for (const o of options(name ?? "").filter((x) => !/^See all in /.test(titleOf(x) ?? ""))) expect(`${titleOf(o)} ${o.textContent}`.toLowerCase()).toMatch(/eu/);
    }
    await user.click(within(group("Datasets")).getByRole("option", { name: /^See all in Datasets/ }));
    await waitFor(() => expect(path(router)).toBe("/datasets"));
    expect(router.state.location.search).toMatchObject({ q: "eu daily" });
    // the list page shows the row the search showed
    const table = await screen.findByRole("table", { name: "Datasets" }, T);
    expect(within(table).getByText("eu_media_daily")).toBeInTheDocument();
    expect(screen.getByTestId("list-count")).toHaveTextContent(/^1 of \d+ datasets$/);
  });

  it.each(["1.0", "null", "1e3", "[1]"])("keeps the text %s as text in the link", async (text) => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    server.use(datasetsNamed(`sales_${text}_weekly`, "other"));
    const { user, router } = await openSearch();
    await type(user, text.replace(/[[{]/g, "$&$&"));
    await settled();
    const all = within(group("Datasets")).getByRole("option", { name: /^See all in Datasets/ });
    // a new tab gets the same link
    await user.keyboard("{Control>}");
    await user.click(all);
    await user.keyboard("{/Control}");
    const href = String(open.mock.calls[0]?.[0]);
    expect(href.startsWith("/datasets?q=")).toBe(true);
    // the router reads the link back as the same text (a plain `?q=1.0` would be the number 1)
    expect(router.options.parseSearch(href.slice("/datasets".length))).toEqual({ q: text });
    open.mockRestore();
    await user.keyboard("{Control>}k{/Control}");
    await type(user, text.replace(/[[{]/g, "$&$&"));
    await settled();
    await user.click(within(group("Datasets")).getByRole("option", { name: /^See all in Datasets/ }));
    await waitFor(() => expect(path(router)).toBe("/datasets"));
    expect(router.state.location.search).toEqual({ q: text });
    expect(await screen.findByRole("searchbox", { name: "Search datasets" }, T)).toHaveValue(text);
  });

  it("takes at most 200 characters, and never asks the server with a longer text than it accepts", async () => {
    const { user, box } = await openSearch();
    const long = "eu media daily ".repeat(20);
    await user.click(box);
    await user.paste(long);
    expect((box as HTMLInputElement).value).toHaveLength(200);
    expect(box).toHaveAttribute("maxlength", "200");
    await settled();
    const library = seen.filter((u) => u.pathname === "/v1/library" && u.searchParams.has("q"));
    expect(library.length).toBeGreaterThan(0);
    for (const u of library) expect((u.searchParams.get("q") ?? "").length).toBeLessThanOrEqual(120);
    for (const u of seen.filter((x) => (x.pathname === "/v1/runs" || x.pathname === "/v1/experiments") && x.searchParams.has("name"))) {
      expect((u.searchParams.get("name") ?? "").length).toBeLessThanOrEqual(200);
    }
  });
});

describe("dashboards with the same title", () => {
  it("shows the name of each on its second line, with the match marked there", async () => {
    const dash = (n: number, name: string) => ({ id: `dsh_01jaf3same${n}aaaaaaaaaaaaaaaa`, name, title: "eu-geo-mmm", status: "draft", client_name: null, updated_at: `2026-10-0${n}T08:00:00Z` });
    server.use(http.get(`${API}/v1/dashboards`, () => HttpResponse.json({ items: [dash(1, "eu-revenue-planner"), dash(2, "weekly-forecast-draft")], next_cursor: null })));
    const { user } = await openSearch();
    await type(user, "eu-geo");
    await settled();
    const two = options("Dashboards").filter((o) => titleOf(o) === "eu-geo-mmm");
    expect(two).toHaveLength(2);
    expect(two.map((o) => o.textContent)).toEqual([expect.stringContaining("weekly-forecast-draft · Draft"), expect.stringContaining("eu-revenue-planner · Draft")]);
    await user.clear(screen.getByRole("combobox", { name: "Search" }));
    await type(user, "revenue");
    await settled();
    const one = options("Dashboards").filter((o) => titleOf(o) === "eu-geo-mmm");
    expect(one).toHaveLength(1);
    expect(one[0]?.querySelector("mark")).toHaveTextContent("revenue");
  });
});

describe("the count and the short text", () => {
  it("counts the things found, without the rows that lead to more results", async () => {
    const { user } = await openSearch();
    await type(user, "media");
    await settled();
    const all = within(listbox()).getAllByRole("option");
    const ways = all.filter((o) => /^See all in /.test(titleOf(o) ?? ""));
    expect(ways.length).toBeGreaterThan(0);
    const things = all.length - ways.length;
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(new RegExp(`^${things} results?$`)));
  });

  it("says to type one more letter when one letter finds no page and no action", async () => {
    const { user } = await openSearch();
    await type(user, "q");
    const hint = "No page or action matches this text. Type one more letter to find items by name.";
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(hint));
    // the sentence is on the screen too (the status is for a screen reader)
    expect(screen.getAllByText(hint).some((el) => el.tagName === "P")).toBe(true);
    expect(screen.queryByText("No page, action or item matches this text.")).toBeNull();
    expect(within(listbox()).queryByRole("group", { name: "Datasets" })).toBeNull();
  });
});

describe("the overlay on a phone", () => {
  it("has a Close button (shown below the sm width) and hides the key hints there", async () => {
    const { user } = await openSearch();
    const close = screen.getByRole("button", { name: "Close" });
    expect(close.className).toContain("sm:hidden");
    expect(screen.getByTestId("search-hints").className).toContain("max-sm:hidden");
    await user.click(close);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("the search of the docs site", () => {
  it("is the last row of the Docs group, also when no docs page has the text in its name", async () => {
    (env as { docsUrl: string | null }).docsUrl = "https://docs.example.test/";
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { user } = await openSearch();
    await type(user, "dashboard");
    await settled();
    const rows = await waitFor(() => options("Docs"));
    expect(rows.map(titleOf)).toEqual(["Dashboard errors", "Build a client dashboard", "Publish the dashboard", "Search the docs for “dashboard”"]);
    await user.clear(screen.getByRole("combobox", { name: "Search" }));
    // "media" is in no docs title of the fixture index, but datasets have it
    await type(user, "media");
    await settled();
    expect(options("Docs").map(titleOf)).toEqual(["Search the docs for “media”"]);
    expect(groupNames().at(-1)).toBe("Docs");
    await user.click(options("Docs")[0]!);
    expect(open).toHaveBeenCalledWith("https://docs.example.test/?q=media", "_blank", "noopener,noreferrer");
  });
});

describe("the recent items of another person", () => {
  it("are not shown, and go out of the storage, when a different user id is in the session", async () => {
    useRecents.setState({ owner: "usr_somebody_else", byScope: { "acme/growth": [{ group: "datasets", title: "ACQUISITION TARGET due diligence", secondary: "Confidential: Project Falcon", href: `/datasets/${fx.DS_ID}` }] } });
    await openSearch("vic");
    expect(groupNames()).toEqual(["Try", "Go to", "Actions"]);
    expect(screen.queryByText("ACQUISITION TARGET due diligence")).toBeNull();
    expect(useRecents.getState().byScope).toEqual({});
    expect(localStorage.getItem("mapi.search.recent") ?? "").not.toContain("ACQUISITION");
    expect(useRecents.getState().owner).toMatch(/^usr_/);
  });

  it("stay for the same person", async () => {
    await openSearch("ana");
    const me = useRecents.getState().owner;
    expect(me).toMatch(/^usr_/);
    useRecents.getState().add("acme/growth", { group: "datasets", title: "eu_media_daily", href: `/datasets/${fx.DS_ID}` });
    useRecents.getState().claim(me ?? "");
    expect(useRecents.getState().list("acme/growth")).toHaveLength(1);
  });
});

describe("the list of keyboard shortcuts", () => {
  const shortcuts = async (user: UserEvent) => {
    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    return within(dialog)
      .getAllByRole("definition")
      .map((dd) => `${dd.previousElementSibling?.textContent} ${dd.textContent}`);
  };

  it("lists the g shortcuts of the Expert sidebar, with Project, Admin and Settings", async () => {
    const user = userEvent.setup();
    renderApp("/settings", "ana");
    await screen.findByRole("navigation", { name: "Primary" }, T);
    const lines = await shortcuts(user);
    for (const line of ["gd Go to Datasets", "gk Go to Dashboards", "gy Go to Make your own", "gu Go to Usage", "gp Go to Members", "gt Go to Tokens", "ga Go to Admin", "g, Go to Settings"]) expect(lines).toContain(line);
  });

  it("lists only the g shortcuts that work for a member of a Guided-locked project", async () => {
    localStorage.setItem("mapi.mock.guidedLocked", "acme/growth");
    server.resetHandlers(...createHandlers());
    try {
      const user = userEvent.setup();
      const { router } = renderApp("/guided", "rui");
      await screen.findByRole("navigation", { name: "Primary" }, T);
      await waitFor(() => expect(within(screen.getByRole("navigation", { name: "Primary", hidden: true })).queryByRole("link", { name: "Datasets", hidden: true })).toBeNull());
      const lines = (await shortcuts(user)).filter((l) => l.startsWith("g"));
      expect(lines).toEqual(["gg Go to Guided", "go Go to Help me choose", "gw Go to Recipes", "gi Go to Integrations", "gu Go to Usage", "gp Go to Members", "gt Go to Tokens", "g, Go to Settings"]);
      // each listed shortcut works; one that is not listed does nothing
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await user.keyboard("gd");
      expect(path(router)).toBe("/guided");
      await user.keyboard("gu");
      await waitFor(() => expect(path(router)).toBe("/usage"));
    } finally {
      localStorage.removeItem("mapi.mock.guidedLocked");
    }
  });
});
