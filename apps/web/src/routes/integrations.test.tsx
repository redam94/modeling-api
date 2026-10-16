import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import { ENDPOINT_ALIAS, SAVED_PLOT_ID, WEBHOOK_DISABLED_ID, WEBHOOK_ID } from "@/mocks/integrations";
import generated from "@/mocks/integrations.gen.json";
import { SNIPPET_LANGUAGES } from "@/lib/integrations";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const seen: { method: string; path: string; body: unknown; headers: Headers }[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    const clone = request.clone();
    void clone.text().then((text) => {
      let body: unknown = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
      seen.push({ method: request.method, path: new URL(request.url).pathname, body, headers: request.headers });
    });
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  // A fresh mock state for every test.
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("Integrations hub", () => {
  it("shows the Guided cards with the Apps Script for an endpoint", async () => {
    renderApp("/integrations");
    expect(await screen.findByRole("heading", { name: "Integrations" })).toBeInTheDocument();
    expect(screen.getByText("Google Sheets")).toBeInTheDocument();
    expect(screen.getByText("BI dashboard")).toBeInTheDocument();
    expect(await screen.findByLabelText("Apps Script")).toHaveTextContent("UrlFetchApp.fetch");
    const tokenLink = screen.getByRole("link", { name: "Create a token" });
    expect(tokenLink).toHaveAttribute("href", `/tokens?preset=score%3A${ENDPOINT_ALIAS}`);
    expect(screen.getByRole("link", { name: "Interactive docs" })).toHaveAttribute("href", `${env.apiBase}/docs`);
  });

  it("promotes through the gate with an audited admin override", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/integrations?tab=endpoints");
    expect(await screen.findByRole("link", { name: ENDPOINT_ALIAS })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Promote a model" }));
    const dialog = await screen.findByRole("dialog", { name: "Promote a model to an endpoint" });
    await user.type(within(dialog).getByLabelText("Endpoint name"), "geo-mmm-staging");
    // The version's scorecard warns: the preview says an override will be needed.
    expect(await within(dialog).findByText("Forecasts were off by 14% on the last 8 weeks.")).toBeInTheDocument();
    expect(within(dialog).getByText("Expected gate")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Promote" }));
    expect(await within(dialog).findByText("The gate needs an admin override")).toBeInTheDocument();
    expect(within(dialog).getByRole("list", { name: "Gate reasons" })).toHaveTextContent("holdout.mape");
    // The override needs a reason.
    await user.click(within(dialog).getByRole("button", { name: "Override and promote" }));
    expect(within(dialog).getByText(/Say why this version should serve/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Override reason"), "Accepted for the Q4 plan");
    await user.click(within(dialog).getByRole("button", { name: "Override and promote" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/integrations/endpoints/geo-mmm-staging"));
    const puts = seen.filter((r) => r.method === "PUT" && r.path === "/v1/endpoints/geo-mmm-staging");
    expect(puts).toHaveLength(2);
    const [first, second] = puts.map((r) => r.body as Record<string, unknown>);
    expect(first).toMatchObject({ expected_revision: 0 });
    expect(String(first?.model_version_id)).toMatch(/^mv_/);
    expect(first?.override).toBeUndefined();
    expect(second).toMatchObject({ override: true, reason: "Accepted for the Q4 plan" });
    expect(puts[0]?.headers.get("X-Mapi-Project")).toBe("acme/growth");
  });

  it("does not offer promotion or overrides below the admin role", async () => {
    renderApp("/integrations?tab=endpoints", "rui");
    expect(await screen.findByRole("link", { name: ENDPOINT_ALIAS })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Promote a model" })).toBeDisabled();
  });

  it("rolls back with a reason and shows the new revision", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/endpoints/${ENDPOINT_ALIAS}`);
    await user.click(await screen.findByRole("button", { name: "Roll back to r1" }));
    const dialog = await screen.findByRole("dialog", { name: `Roll ${ENDPOINT_ALIAS} back?` });
    await user.type(within(dialog).getByLabelText("Reason"), "r2 over-forecasts the holidays");
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    expect(await screen.findByText("r3")).toBeInTheDocument();
    expect(seen.find((r) => r.path.endsWith("/rollback"))?.body).toEqual({ reason: "r2 over-forecasts the holidays", to_revision: 1 });
  });

  it("validates and scores the example call, and copies snippets", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/endpoints/${ENDPOINT_ALIAS}?tab=schema`);
    expect(await screen.findByRole("table", { name: "Variables" })).toHaveTextContent("spend_local");
    await user.click(await screen.findByRole("button", { name: "Validate" }));
    expect(await screen.findByText("The call is valid")).toBeInTheDocument();
    const body = screen.getByLabelText("Call body");
    await user.clear(body);
    await user.click(body);
    await user.paste('{"mode": "simulate"}');
    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("The call would be refused")).toBeInTheDocument();
    expect(screen.getByText(/does not support mode 'simulate'/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset to the example" }));
    await user.click(screen.getByRole("button", { name: "Score" }));
    expect(await screen.findByText("Scored")).toBeInTheDocument();
    expect(screen.getByText("ui/plotdata/ppc_fan.arrow")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Snippets" }));
    // Every tab shows, and copies, exactly the code the server generates (mapi_spec.snippets).
    // (The mock app's API base is the one the fixture was generated with, unless overridden.)
    const server = generated.snippets.items.map((s) => ({ ...s, code: s.code.replaceAll(generated.base_url, env.apiBase) }));
    expect(server.map((s) => s.language)).toEqual(SNIPPET_LANGUAGES.map((l) => l.id));
    for (const snippet of server) {
      const label = SNIPPET_LANGUAGES.find((l) => l.id === snippet.language)!.label;
      await user.click(await screen.findByRole("tab", { name: label }));
      expect((await screen.findByLabelText(`${snippet.title} snippet`)).textContent).toBe(snippet.code);
    }
    await user.click(screen.getByRole("tab", { name: "Python" }));
    await user.click(screen.getByRole("button", { name: "Copy Python (mapi-client) snippet" }));
    await waitFor(async () => expect(await navigator.clipboard.readText()).toBe(server[0]!.code));
    await user.click(screen.getByRole("tab", { name: "Notebook" }));
    expect(await screen.findByLabelText("Notebook (mapi-client, xarray) snippet")).toHaveTextContent("endpoint = api.endpoint('geo-mmm-prod')");
  });
});

describe("webhooks", () => {
  it("creates a webhook and shows its secret once", async () => {
    const user = userEvent.setup();
    renderApp("/integrations?tab=webhooks");
    await user.click(await screen.findByRole("button", { name: "New webhook" }));
    const dialog = await screen.findByRole("dialog", { name: "New webhook" });
    await user.type(within(dialog).getByLabelText("URL"), "https://127.0.0.1/hook");
    await user.click(within(dialog).getByRole("checkbox", { name: /Endpoint promoted/ }));
    await user.click(within(dialog).getByRole("button", { name: "Create webhook" }));
    // The SSRF policy refuses it: the server's reason is shown at the URL.
    expect(await within(dialog).findByText(/never to internal or private addresses/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText("URL"));
    await user.type(within(dialog).getByLabelText("URL"), "https://hooks.example.com/mapi");
    await user.click(within(dialog).getByRole("button", { name: "Create webhook" }));
    const secret = await screen.findByRole("dialog", { name: "Copy the signing secret now" });
    const value = within(secret).getByLabelText<HTMLInputElement>("Signing secret").value;
    expect(value).toMatch(/^whsec_[A-Za-z0-9_-]{43}$/);
    expect(within(secret).getByRole("button", { name: "Done" })).toBeDisabled();
    await user.click(within(secret).getByRole("button", { name: "Copy signing secret" }));
    await waitFor(() => expect(within(secret).getByRole("button", { name: "Done" })).toBeEnabled());
    expect(await navigator.clipboard.readText()).toBe(value);
    const created = seen.find((r) => r.method === "POST" && r.path === "/v1/webhooks" && JSON.stringify(r.body).includes("hooks.example.com"));
    expect(created?.body).toEqual({ url: "https://hooks.example.com/mapi", events: ["run.succeeded", "run.failed", "model.version.ready", "endpoint.promoted"] });
  });

  it("logs deliveries, replays one and sends a test", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/webhooks/${WEBHOOK_ID}`);
    const table = await screen.findByRole("table", { name: "Deliveries" });
    expect(within(table).getByText("Retrying")).toBeInTheDocument();
    expect(within(table).getByText("503")).toBeInTheDocument();
    const failedRow = within(table).getByText("Failed").closest("tr")!;
    await user.click(within(failedRow).getByRole("button", { name: "Details" }));
    expect(await screen.findByText(/Traceback \(most recent call last\)/)).toBeInTheDocument();
    await user.click(within(failedRow).getByRole("button", { name: /^Replay delivery/ }));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Deliveries" })).getAllByText("replay").length).toBe(1));
    await user.click(screen.getByRole("button", { name: "Send test" }));
    await waitFor(() => expect(seen.filter((r) => r.path.endsWith(":replay")).length).toBe(2));
  });

  it("explains an automatically disabled webhook and re-enables it", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/webhooks/${WEBHOOK_DISABLED_ID}`);
    expect(await screen.findByText("This webhook was disabled automatically")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send test" })).toBeDisabled();
    await user.click(screen.getByRole("switch", { name: "Webhook enabled" }));
    await waitFor(() => expect(screen.queryByText("This webhook was disabled automatically")).toBeNull());
    expect(seen.find((r) => r.method === "PATCH")?.body).toEqual({ active: true });
  });

  it("is explained, not listed, to non-admins", async () => {
    renderApp("/integrations?tab=webhooks", "rui");
    expect(await screen.findByText("Webhooks are managed by project admins")).toBeInTheDocument();
  });
});

describe("plots and links", () => {
  it("creates an embed link with an expiry and allowed sites", async () => {
    const user = userEvent.setup();
    renderApp("/integrations?tab=sharing");
    const cards = await screen.findAllByTestId("saved-plot");
    const vega = cards.find((c) => within(c).queryByText("EU posterior predictive fan (v1)"))!;
    await user.click(within(vega).getByRole("button", { name: "Create link" }));
    const dialog = await screen.findByRole("dialog", { name: /Share “EU posterior predictive fan/ });
    await user.type(within(dialog).getByLabelText("Label (optional)"), "Weekly review");
    await user.selectOptions(within(dialog).getByLabelText("Expires after"), "90");
    await user.type(within(dialog).getByLabelText(/Sites that may frame it/), "http://bad.example");
    await user.click(within(dialog).getByRole("button", { name: "Create link" }));
    expect(within(dialog).getByText(/is not an https origin/)).toBeInTheDocument();
    await user.clear(within(dialog).getByLabelText(/Sites that may frame it/));
    await user.type(within(dialog).getByLabelText(/Sites that may frame it/), "https://lookerstudio.google.com");
    await user.click(within(dialog).getByRole("button", { name: "Create link" }));
    const created = await screen.findByRole("dialog", { name: "Copy the link now" });
    expect(within(created).getByLabelText<HTMLInputElement>("Embed link").value).toMatch(new RegExp(`^${env.plotsOrigin}/embed/mpe_`));
    expect(within(created).getByLabelText("Embed code")).toHaveTextContent("<iframe");
    expect(seen.find((r) => r.path === `/v1/plots/${SAVED_PLOT_ID}/embeds` && r.method === "POST")?.body).toEqual({
      expires_in_days: 90,
      allowed_origins: ["https://lookerstudio.google.com"],
      label: "Weekly review",
    });
  });

  it("disables Plotly downloads with the server's reason", async () => {
    const user = userEvent.setup();
    renderApp("/integrations?tab=sharing");
    const cards = await screen.findAllByTestId("saved-plot");
    const plotly = cards.find((c) => within(c).queryByText("Spend by market (3D)"))!;
    expect(within(plotly).getByRole("button", { name: "Create link" })).toBeDisabled();
    await user.click(within(plotly).getByRole("button", { name: "Download PNG" }));
    expect(await within(plotly).findByTestId("plotly-refused")).toHaveTextContent("MAPI_RENDER_PLOTLY_ENABLED");
    expect(within(plotly).getByRole("button", { name: "Download SVG" })).toBeDisabled();
  });

  it("downloads a render after polling a slow run", async () => {
    const created = vi.fn((_: Blob) => "blob:mock");
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: vi.fn() });
    const user = userEvent.setup();
    renderApp("/integrations?tab=sharing");
    const cards = await screen.findAllByTestId("saved-plot");
    const vega = cards.find((c) => within(c).queryByText("EU posterior predictive fan (v1)"))!;
    await user.click(within(vega).getByRole("button", { name: "Download PDF" }));
    await waitFor(() => expect(created).toHaveBeenCalledTimes(1), { timeout: 5000 });
    expect(seen.some((r) => r.path.startsWith("/v1/plots/renders/"))).toBe(true);
    expect(seen.find((r) => r.path === "/v1/plots:render")?.headers.get("Idempotency-Key")).toBeTruthy();
  });

  it("saves a model plot from the gallery link and goes on to share it", async () => {
    const user = userEvent.setup();
    renderApp("/integrations?tab=sharing&model=mdl_01jaf3c1d2e3f4g5h6j7k8m9n0&version=1&plot=core.ppc_fan");
    const dialog = await screen.findByRole("dialog", { name: "Save a model plot" });
    await waitFor(() => expect(within(dialog).getByLabelText("Plot")).toHaveValue("core.ppc_fan"));
    await user.click(within(dialog).getByRole("button", { name: "Save plot" }));
    expect(await screen.findByRole("dialog", { name: /Share “Posterior predictive fan \(v1\)”/ })).toBeInTheDocument();
    expect(seen.find((r) => r.method === "POST" && r.path === "/v1/plots")?.body).toEqual({
      title: "Posterior predictive fan (v1)",
      source: { model_id: "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0", version: 1, plot_id: "core.ppc_fan" },
    });
  });
});

describe("tokens", () => {
  it("opens the New token dialog for one endpoint", async () => {
    renderApp(`/tokens?preset=score:${ENDPOINT_ALIAS}`);
    const dialog = await screen.findByRole("dialog", { name: "New API token" });
    expect(within(dialog).getByLabelText("Score models (optional)")).toHaveValue(ENDPOINT_ALIAS);
    expect(within(dialog).getByLabelText("Name")).toHaveValue(`${ENDPOINT_ALIAS}-integration`);
    expect(within(dialog).getByRole("checkbox", { name: /^Read models:read/ })).not.toBeChecked();
    // The budget period keeps its full label.
    const period = within(dialog).getByLabelText("Budget period");
    expect(period.className).toContain("shrink-0");
    expect(within(period).getByRole("option", { name: "per month" })).toBeInTheDocument();
  });

  it("ignores a malformed preset", async () => {
    server.use(http.get(`${env.apiBase}/v1/tokens`, () => HttpResponse.json({ items: [], next_cursor: null })));
    renderApp("/tokens?preset=score:BAD alias");
    expect(await screen.findByText("No tokens yet")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
