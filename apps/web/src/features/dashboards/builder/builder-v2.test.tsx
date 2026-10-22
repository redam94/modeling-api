import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getResponse, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { DRAFT_DASHBOARD_ID, V2_DASHBOARD_IDS } from "@/mocks/dashboards";
import { MODEL_ID } from "@/mocks/fixtures";
import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";

import type { DashboardCreate, DashboardSpecV2 } from "../types";
import { resetUpgradeOutcomes } from "./upgrade-banner";

const server = setupServer(...createHandlers());
const seen: Request[] = [];
const RETAIL = { tenantId: "acme", projectId: "retail" };
const T = { timeout: 10_000 };

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  localStorage.setItem("mapi.mock.readoutMs", "40");
  server.resetHandlers(...createHandlers());
  resetUpgradeOutcomes();
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const panelTitles = () =>
  within(screen.getByRole("list", { name: "Panels" }))
    .getAllByRole("listitem")
    .map((li) => li.getAttribute("data-panel"));

describe("builder › layout 2", () => {
  it("opens the automatically upgraded MMM dashboard on the planner layout", async () => {
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.mmm}`, "ana", RETAIL);
    const panels = await screen.findByRole("list", { name: "Panels" }, T);
    expect(within(panels).getAllByRole("listitem").length).toBeGreaterThan(0);
    expect(screen.getByLabelText<HTMLSelectElement>("Change the layout").value).toBe("mmm_planner");
    expect(screen.getByRole("table", { name: "Channels" })).toBeTruthy();
    expect(screen.queryByTestId("upgrade-banner")).toBeNull();
    expect(screen.queryByText("Unsaved changes")).toBeNull();
    // the upgraded mock is consistent: the planner's KPI and its grid are found
    await waitFor(() => expect(screen.getByRole("table", { name: "Channels" })).toBeTruthy(), T);
    await new Promise((r) => setTimeout(r, 300));
    const said = [...document.querySelectorAll("p,div,li,span")].map((e) => e.textContent ?? "").filter((t) => /has no output named|no response grid/.test(t));
    expect(said.at(-1) ?? null).toBeNull();
  });

  it("keeps a panel's editor open while its id is typed", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.classifier}`, "ana", RETAIL);
    await screen.findByRole("list", { name: "Panels" }, T);
    await user.click(screen.getByRole("button", { name: /^Note$/ }));
    const id = await screen.findByLabelText<HTMLInputElement>("Id");
    await user.type(id, "abc");
    expect(screen.getByLabelText<HTMLInputElement>("Id").value).toBe("noteabc");
    expect(screen.getByLabelText<HTMLInputElement>("Id")).toBe(document.activeElement);
    expect(panelTitles().at(-1)).toBe("noteabc");
  });

  it("moves, removes and adds panels from the keyboard, says where a panel went, and saves layout 2", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.classifier}`, "ana", RETAIL);
    await screen.findByRole("list", { name: "Panels" }, T);
    const before = panelTitles();
    expect(before.length).toBeGreaterThan(2);
    expect(screen.queryByText("Unsaved changes")).toBeNull();

    const firstTitle = within(screen.getByRole("list", { name: "Panels" })).getAllByRole("listitem")[0]!;
    const down = within(firstTitle).getByRole("button", { name: /^Move .* down$/ });
    down.focus();
    await user.keyboard("{Enter}");
    expect(panelTitles()).toEqual([before[1], before[0], ...before.slice(2)]);
    expect(screen.getByTestId("panels-live").textContent).toMatch(/moved to place 2 of \d+\./);
    // the focus stays with the moved panel
    const moved = screen.getByRole("list", { name: "Panels" }).querySelector(`[data-panel="${before[0]}"]`)!;
    expect(moved.contains(document.activeElement)).toBe(true);

    const last = within(screen.getByRole("list", { name: "Panels" })).getAllByRole("listitem").at(-1)!;
    await user.click(within(last).getByRole("button", { name: /^Remove / }));
    expect(panelTitles().length).toBe(before.length - 1);
    // R7: the focus goes to the Remove button of the panel before, never the page body
    const nowLast = within(screen.getByRole("list", { name: "Panels" })).getAllByRole("listitem").at(-1)!;
    expect(document.activeElement).toBe(within(nowLast).getByRole("button", { name: /^Remove / }));

    await user.click(screen.getByRole("button", { name: /^Note$/ }));
    expect(panelTitles().at(-1)).toBe("note");
    expect(await screen.findByLabelText("Text")).toBeTruthy();
    expect(screen.getByText("Unsaved changes")).toBeTruthy();

    await user.clear(screen.getByLabelText("Title", { selector: "#ds-title" }));
    await user.type(screen.getByLabelText("Title", { selector: "#ds-title" }), "High demand days");
    await user.click(screen.getByRole("button", { name: /Save draft/ }));
    expect(await screen.findByText("Draft saved", {}, T)).toBeTruthy();
    const put = seen.find((r) => r.method === "PUT" && r.url.endsWith("/draft"));
    const body = (await put!.json()) as { spec: DashboardSpecV2 };
    expect(body.spec.layout_version).toBe(2);
    expect(body.spec.title).toBe("High demand days");
    expect(body.spec.panels.map((p) => p.id).at(-1)).toBe("note");
  });

  it("asks before a layout change and lists what it removes", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.classifier}`, "ana", RETAIL);
    await screen.findByRole("list", { name: "Panels" }, T);
    const select = await screen.findByLabelText<HTMLSelectElement>("Change the layout");
    await waitFor(() => expect(select.querySelector<HTMLOptionElement>('option[value="regression"]')?.disabled).toBe(false), T);
    // a layout the model does not fit is disabled, with the reason in its name
    expect(select.querySelector<HTMLOptionElement>('option[value="image"]')?.disabled).toBe(true);
    await user.selectOptions(select, "regression");
    const dialog = await screen.findByRole("dialog", { name: /Use the predictions layout/ });
    const removed = within(dialog).getByRole("list", { name: "What the change removes" });
    expect(within(removed).getAllByRole("listitem").length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: /Keep the classifier layout/ }));
    expect(screen.getByLabelText<HTMLSelectElement>("Change the layout").value).toBe("classifier");
    await user.selectOptions(screen.getByLabelText("Change the layout"), "regression");
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Change the layout" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getAllByText("Predictions").length).toBeGreaterThan(0);
    expect(screen.getByText("Unsaved changes")).toBeTruthy();
  });

  it("shows the layout 2 inputs and warns when a limit refuses every run", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.classifier}?tab=inputs`, "ana", RETAIL);
    expect(await screen.findByLabelText("Let the client try cases", {}, T)).toBeTruthy();
    expect(screen.getByLabelText("Let the client score a file")).toBeTruthy();
    // no channels and no planner what-ifs outside the media planner
    expect(screen.queryByRole("table", { name: "Channels" })).toBeNull();
    expect(screen.queryByLabelText("Let the client try other spend (what-ifs)")).toBeNull();
    const runs = screen.getByLabelText("Runs each day");
    await user.clear(runs);
    await user.type(runs, "0");
    expect(await screen.findByText(/With 0 runs each day, every one of the/)).toBeTruthy();
  });
});

describe("builder › history › tried cases", () => {
  it("lists kept tried cases for review with their inputs, and keeps a client's private one", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${V2_DASHBOARD_IDS.classifier}?tab=history`, "ana", RETAIL);
    const pending = await screen.findByRole("list", { name: "Readouts waiting for review" }, T);
    const kept = within(pending).getByText("Kept: store s0 on promotion").closest("li")!;
    expect(within(kept as HTMLElement).getByText(/Tried case · kept/)).toBeTruthy();
    expect(within(kept as HTMLElement).getByRole("button", { name: "Publish readout 5" })).toBeTruthy();
    expect(within(kept as HTMLElement).getByRole("button", { name: "Hide readout 5" })).toBeTruthy();
    await user.click(within(kept as HTMLElement).getByRole("button", { name: /Show the case/ }));
    expect(await within(kept as HTMLElement).findByText((_, el) => el?.getAttribute("data-readout") === "prediction", {}, T)).toBeTruthy();

    const all = screen.getByRole("table", { name: "All readouts" });
    await user.click(within(all).getByRole("button", { name: "Keep tried case 3" }));
    await waitFor(() => expect(seen.some((r) => r.method === "POST" && r.url.endsWith("/readouts/3:keep"))).toBe(true), T);
    expect(await within(screen.getByRole("list", { name: "Readouts waiting for review" })).findByText("Two prices for store s0", {}, T)).toBeTruthy();
  });
});

describe("builder › the move to the new layout", () => {
  it("offers “Use the new layout” on a layout 1 draft and opens the layout 2 form after it", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${DRAFT_DASHBOARD_ID}`);
    const banner = await screen.findByTestId("upgrade-banner", {}, T);
    expect(within(banner).getByText(/This dashboard uses the old layout/)).toBeTruthy();
    await user.click(within(banner).getByRole("button", { name: /Use the new layout/ }));
    expect(await screen.findByRole("list", { name: "Panels" }, T)).toBeTruthy();
    expect(await screen.findByTestId("upgrade-result")).toBeTruthy();
    expect(screen.queryByTestId("upgrade-banner")).toBeNull();
    const post = seen.find((r) => r.method === "POST" && r.url.endsWith(":upgrade"));
    expect(post?.headers.get("If-Match")).toMatch(/^"/);
    expect(screen.queryByText("Unsaved changes")).toBeNull();
  });

  it("does not move a draft with unsaved changes", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${DRAFT_DASHBOARD_ID}`);
    const banner = await screen.findByTestId("upgrade-banner", {}, T);
    await user.type(screen.getByLabelText("Title", { selector: "#ds-title" }), " 2");
    const button = within(banner).getByRole<HTMLButtonElement>("button", { name: /Use the new layout/ });
    expect(button.disabled).toBe(true);
    expect(within(banner).getByText("Save or discard your changes first.")).toBeTruthy();
  });

  it("lists in plain words what stopped the automatic move", async () => {
    const handlers = createHandlers();
    server.resetHandlers(
      http.get(`*/v1/dashboards/${DRAFT_DASHBOARD_ID}`, async ({ request }) => {
        const res = await getResponse(handlers, request);
        if (!res || !res.ok) return res;
        const d = (await res.json()) as Record<string, unknown>;
        return HttpResponse.json(
          { ...d, problem: "upgrade_needs_review", upgrade_issues: [{ code: "BINDING_INVALID", severity: "error", pointer: "/channels/2/name", message: "The response grid has no channel named radio." }] },
          { headers: { ETag: String(d.draft_etag) } },
        );
      }),
      ...handlers,
    );
    renderApp(`/dashboards/${DRAFT_DASHBOARD_ID}`);
    const banner = await screen.findByTestId("upgrade-banner", {}, T);
    expect(within(banner).getByText(/could not move to the new layout by itself/)).toBeTruthy();
    const list = within(banner).getByRole("list", { name: "What stopped the move to the new layout" });
    expect(list.textContent).toBe("Channels: The response grid has no channel named radio.");
    expect(list.textContent).not.toContain("BINDING_INVALID");
  });
});

describe("new dashboard", () => {
  it("opened prefilled for a model (the dashboard tour) starts with a title", async () => {
    // v0.18 live: the tour's coach-mark sits over the Title field and says "then press
    // Create"; with an empty title Create only answered "Title: Type a title."
    renderApp(`/dashboards?new=1&model=${MODEL_ID}`);
    const dialog = await screen.findByRole("dialog", { name: "New client dashboard" }, T);
    expect(within(dialog).getByLabelText<HTMLInputElement>("Title").value).toBe("My first dashboard");
    expect(within(dialog).getByLabelText<HTMLInputElement>("Name").value).toBe("my-first-dashboard");
  });

  it("opened prefilled again (a second dashboard tour) picks a title whose name is free", async () => {
    // v0.18 live: the second run of the dashboard tour got a plain 409 for `my-first-dashboard`
    server.use(
      http.get("*/v1/dashboards", async ({ request }) => {
        const real = await getResponse(createHandlers(), request.clone());
        const page = (await real!.json()) as { items: { name: string }[] };
        return HttpResponse.json({ ...page, items: [...page.items, { ...page.items[0]!, id: "dsh_taken1", name: "my-first-dashboard" }, { ...page.items[0]!, id: "dsh_taken2", name: "my-first-dashboard-2" }] });
      }),
    );
    renderApp(`/dashboards?new=1&model=${MODEL_ID}`);
    const dialog = await screen.findByRole("dialog", { name: "New client dashboard" }, T);
    await waitFor(() => expect(within(dialog).getByLabelText<HTMLInputElement>("Title").value).toBe("My first dashboard 3"), T);
    expect(within(dialog).getByLabelText<HTMLInputElement>("Name").value).toBe("my-first-dashboard-3");
  });

  it("starts from the suggested layout and never asks for channels outside the planner", async () => {
    const user = userEvent.setup();
    renderApp("/dashboards");
    await user.click(await screen.findByRole("button", { name: /New dashboard/ }, T));
    const dialog = await screen.findByRole("dialog", { name: "New client dashboard" });
    await user.type(within(dialog).getByLabelText("Title"), "Monthly sales");
    await user.selectOptions(within(dialog).getByLabelText("Endpoint"), "sales-forecast");
    const layouts = await within(dialog).findByRole("radiogroup", { name: "Layout" }, T);
    await waitFor(() => expect(within(layouts).getByLabelText<HTMLInputElement>(/Forecast/).checked).toBe(true), T);
    expect(within(layouts).getByText("Suggested")).toBeTruthy();
    expect(within(dialog).queryByLabelText("Channels")).toBeNull();
    expect(within(dialog).queryByText(/channel/i)).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: /Create dashboard/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull(), T);
    const post = seen.find((r) => r.method === "POST" && /\/v1\/dashboards$/.test(r.url));
    const body = (await post!.json()) as DashboardCreate;
    const spec = body.spec as DashboardSpecV2;
    expect(spec.layout_version).toBe(2);
    expect(spec.preset).toBe("forecast");
    expect(spec.title).toBe("Monthly sales");
    expect(spec.mmm ?? null).toBeNull();
    expect(body.name).toBe("monthly-sales");
  });
});
