import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { EXPERT_NAV, GUIDED_MODE_NAV, LOCKED_NAV } from "@/components/shell/nav";
import { DRAFT_DASHBOARD_ID, MMM_DASHBOARD_ID } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const seen: Request[] = [];

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
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const T = { timeout: 10_000 };

describe("navigation", () => {
  it("lists Dashboards for Expert and Guided users, not in a Guided-locked project", () => {
    expect(EXPERT_NAV.map((n) => n.to)).toContain("/dashboards");
    expect(GUIDED_MODE_NAV.map((n) => n.to)).toContain("/dashboards");
    expect(LOCKED_NAV.map((n) => n.to)).not.toContain("/dashboards");
  });
});

describe("dashboards list", () => {
  it("shows the project's dashboards with their status", async () => {
    renderApp("/dashboards");
    const table = await screen.findByRole("table", { name: "Dashboards" }, T);
    expect(within(table).getByText("Weekly media readout")).toBeTruthy();
    expect(within(table).getByText("Spring promotion plan")).toBeTruthy();
    expect(within(table).getAllByText("Published").length).toBeGreaterThan(0);
    expect(within(table).getByText("Draft")).toBeTruthy();
  });
});

describe("builder › access", () => {
  it("lists the clients with access", async () => {
    renderApp(`/dashboards/${MMM_DASHBOARD_ID}?tab=access`);
    const grants = await screen.findByRole("table", { name: "Clients with access" }, T);
    expect(within(grants).getByText("Cleo Client")).toBeTruthy();
    expect(within(grants).getByText("cleo@northwind.example")).toBeTruthy();
  });

  it("creates a read-only link by default, shows the token one time, then only its redacted form", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${MMM_DASHBOARD_ID}?tab=access`);
    await screen.findByRole("table", { name: "Share links" }, T);
    await user.click(screen.getByRole("button", { name: /Create link/ }));
    const dialog = await screen.findByRole("dialog");
    const whatif = within(dialog).getByRole("checkbox", { name: "Let them try plan scenarios" });
    const uploads = within(dialog).getByRole("checkbox", { name: "Let them upload numbers" });
    expect(whatif.getAttribute("aria-checked")).toBe("false");
    expect(uploads.getAttribute("aria-checked")).toBe("false");
    expect(within(dialog).getByTestId("share-access-summary").textContent).toBe("This link: Read only.");

    await user.type(within(dialog).getByLabelText("Label (optional)"), "Finance director");
    await user.type(within(dialog).getByLabelText("Passcode (optional)"), "short");
    await user.click(within(dialog).getByRole("button", { name: /Create link/ }));
    expect(await within(dialog).findByText("Use at least 8 characters for the passcode.")).toBeTruthy();
    await user.type(within(dialog).getByLabelText("Passcode (optional)"), "-enough");
    await user.click(within(dialog).getByRole("button", { name: /Create link/ }));

    const shown = await screen.findByRole("dialog", { name: "Copy the link now" }, T);
    const field = within(shown).getByLabelText<HTMLInputElement>("Share link");
    expect(field.value).toMatch(/\/s#mpd_[0-9a-hjkmnp-tv-z]{40}$/);
    const token = field.value.split("#")[1] ?? "";
    expect(within(shown).getByText(/is read-only/)).toBeTruthy();
    const post = seen.find((r) => r.method === "POST" && r.url.endsWith(`/v1/dashboards/${MMM_DASHBOARD_ID}/shares`));
    expect(await post?.json()).toEqual({ expires_in_days: 30, allowed_inputs: [], label: "Finance director", passcode: "short-enough" });

    const done = within(shown).getByRole("button", { name: "Done" });
    expect((done as HTMLButtonElement).disabled).toBe(true);
    await user.click(within(shown).getByText("I have copied the link"));
    await user.click(done);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const table = screen.getByRole("table", { name: "Share links" });
    const row = await within(table).findByText("Finance director", {}, T);
    const tr = row.closest("tr")!;
    expect(within(tr).getByText(`mpd_…${token.slice(-4)}`)).toBeTruthy();
    expect(within(tr).getByText("Read only")).toBeTruthy();
    expect(within(tr).getByText("With a passcode")).toBeTruthy();
    expect(document.body.textContent).not.toContain(token);
  });
});

describe("builder › history", () => {
  it("lists the pending upload and its refit request next to it, and approves the refit", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${MMM_DASHBOARD_ID}?tab=history`);
    const pending = await screen.findByRole("list", { name: "Readouts waiting for review" }, T);
    expect(within(pending).getByText("Numbers sent on 20/10/2026")).toBeTruthy();
    expect(within(pending).getByRole("button", { name: "Publish readout 13" })).toBeTruthy();
    const refits = await screen.findByRole("list", { name: "Refit requests" }, T);
    expect(within(refits).getByText("Refit with the numbers of readout 13")).toBeTruthy();
    expect(within(refits).getByText(/Asked by a client user/)).toBeTruthy();

    await user.click(within(refits).getByRole("button", { name: /Approve/ }));
    const dialog = await screen.findByRole("dialog", { name: "Approve the refit?" });
    expect(within(dialog).getByText(/starts .* as you/)).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: /Approve and start the refit/ }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Refit requests" })).getByText("Approved")).toBeTruthy(), T);
    const post = seen.find((r) => r.method === "POST" && r.url.includes(":approve"));
    expect(post?.headers.get("Idempotency-Key")).toBeTruthy();
  });
});

describe("builder › publish", () => {
  it("checks the draft, says what publishing costs, and publishes", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${DRAFT_DASHBOARD_ID}`);
    await screen.findByRole("heading", { name: "Spring promotion plan" }, T);
    await user.click(screen.getByRole("button", { name: /^Publish$/ }));
    const dialog = await screen.findByRole("dialog", { name: /Publish “Spring promotion plan”/ });
    expect(within(dialog).getByTestId("publish-cost").textContent).toContain("Live inputs run as you and use your project's compute.");
    expect(await within(dialog).findByText("The draft is ready to publish.", {}, T)).toBeTruthy();
    await user.click(within(dialog).getByRole("button", { name: /^Publish$/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull(), T);
    expect(await screen.findByText(/Revision 1 published/, {}, T)).toBeTruthy();
  });

  it("asks to save unsaved changes before publishing", async () => {
    const user = userEvent.setup();
    renderApp(`/dashboards/${DRAFT_DASHBOARD_ID}`);
    const title = await screen.findByLabelText("Title", {}, T);
    await user.clear(title);
    expect(await screen.findByText("Type a title.")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: /Save draft/ }).disabled).toBe(true);
    await user.type(title, "Spring plan");
    expect(screen.getByText("Unsaved changes")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Publish$/ }).getAttribute("title")).toBe("Save the draft first.");
    await user.click(screen.getByRole("button", { name: /Save draft/ }));
    expect(await screen.findByText("Draft saved", {}, T)).toBeTruthy();
    await waitFor(() => expect(screen.queryByText("Unsaved changes")).toBeNull());
    const put = seen.find((r) => r.method === "PUT" && r.url.endsWith("/draft"));
    expect(put?.headers.get("If-Match")).toMatch(/^"/);
  });
});
