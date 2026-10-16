import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { MOCK_OUTBOX_KEY } from "@/mocks/trust";
import { renderApp } from "@/test/render-app";

import { PublicApp } from "./public-pages";

const server = setupServer(...createHandlers());
const seen: { method: string; path: string; body: unknown }[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    void request
      .clone()
      .text()
      .then((t) => seen.push({ method: request.method, path: new URL(request.url).pathname, body: t ? (JSON.parse(t) as unknown) : null }));
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  sessionStorage.removeItem(MOCK_OUTBOX_KEY);
  server.resetHandlers(...createHandlers());
  window.history.replaceState(null, "", "/");
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

async function fillSignup(user: ReturnType<typeof userEvent.setup>, email: string) {
  await user.type(screen.getByLabelText("Work e-mail"), email);
  await user.type(screen.getByLabelText("Your name"), "Nia Newcomer");
  await user.type(screen.getByLabelText("Organisation (optional)"), "Northwind Trading");
}

describe("sign-up", () => {
  it("asks for the terms and explains a disposable address at the field", async () => {
    const user = userEvent.setup();
    render(<PublicApp path="/signup" />);
    await fillSignup(user, "nia@mailinator.com");
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(screen.getByText(/Accept the terms of service/)).toBeInTheDocument();
    expect(seen.some((r) => r.path === "/auth/signup")).toBe(false);
    await user.click(screen.getByRole("checkbox", { name: /I accept the terms/ }));
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    const email = screen.getByLabelText("Work e-mail");
    await waitFor(() => expect(email).toHaveAttribute("aria-invalid", "true"));
    expect(email).toHaveAccessibleDescription(/Disposable e-mail addresses cannot sign up/);
    expect(email).toHaveFocus();
  });

  it("goes from sign-up to the inbox to a verified workspace", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<PublicApp path="/signup" />);
    await fillSignup(user, "nia@northwind.example");
    await user.click(screen.getByRole("checkbox", { name: /I accept the terms/ }));
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    expect(await screen.findByRole("heading", { name: "Check your inbox" })).toBeInTheDocument();
    expect(seen.find((r) => r.path === "/auth/signup")?.body).toEqual({
      email: "nia@northwind.example",
      name: "Nia Newcomer",
      organisation: "Northwind Trading",
      accepted_terms: true,
      terms_version: "2026-10",
    });
    // The development outbox (dev builds only) links straight to the e-mail's link.
    const link = await screen.findByRole("link", { name: "Open the verification link" });
    const href = link.getAttribute("href") ?? "";
    expect(href).toMatch(/^\/verify-email\?token=mev_[0-9a-z]{40}$/);
    unmount();

    window.history.replaceState(null, "", href);
    const second = render(<PublicApp path="/verify-email" />);
    // The token leaves the address bar at once.
    await waitFor(() => expect(window.location.search).toBe(""));
    await user.click(screen.getByRole("button", { name: "Confirm and create my workspace" }));
    expect(await screen.findByRole("heading", { name: "Your workspace is ready" })).toBeInTheDocument();
    expect(screen.getByText("northwind-trading")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    second.unmount();

    // The link works once.
    window.history.replaceState(null, "", href);
    render(<PublicApp path="/verify-email" />);
    await user.click(screen.getByRole("button", { name: "Confirm and create my workspace" }));
    expect(await screen.findByRole("heading", { name: "This link has expired" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Get a new link" })).toHaveAttribute("href", "/signup");
  });

  it("says when a link is incomplete", () => {
    window.history.replaceState(null, "", "/verify-email?token=nope");
    render(<PublicApp path="/verify-email" />);
    expect(screen.getByRole("heading", { name: "This link is incomplete" })).toBeInTheDocument();
  });

  it("is reachable from the sign-in screen", async () => {
    renderApp("/", "signed-out");
    expect(await screen.findByRole("link", { name: "Create a workspace" })).toHaveAttribute("href", "/signup");
  });
});

describe("Settings > Data", () => {
  it("exports the organisation's data and offers the download", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    expect(await screen.findByRole("heading", { name: "Data" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: /Audit log/ }));
    await user.click(screen.getByRole("button", { name: "Request export" }));
    const status = await screen.findByTestId("export-status");
    const download = await within(status).findByRole("link", { name: /Download archive/ }, { timeout: 5000 });
    expect(download.getAttribute("href")).toMatch(/^http:\/\/127\.0\.0\.1:4443\/mock-artifacts\/exports%2Fexp_/);
    expect(seen.find((r) => r.method === "POST" && r.path === "/v1/tenants/acme/exports")?.body).toEqual({ include: ["metadata", "datasets", "models", "usage"] });
  });

  it("finds the latest export from another browser (the server lists them)", async () => {
    const user = userEvent.setup();
    const first = renderApp("/settings");
    await user.click(await screen.findByRole("button", { name: "Request export" }));
    const status = await screen.findByTestId("export-status");
    await within(status).findByRole("link", { name: /Download archive/ }, { timeout: 5000 });
    const id = within(status).getByText(/^exp_/).textContent;
    first.unmount();
    // another browser: nothing remembered locally
    for (const key of Object.keys(localStorage)) if (key.startsWith("mapi.export.")) localStorage.removeItem(key);
    renderApp("/settings");
    const again = await screen.findByTestId("export-status");
    expect(within(again).getByText(/^exp_/).textContent).toBe(id);
    expect(await within(again).findByRole("link", { name: /Download archive/ })).toBeInTheDocument();
    expect(seen.some((r) => r.method === "GET" && r.path === "/v1/tenants/acme/exports")).toBe(true);
  });

  it("schedules the deletion with a typed confirmation, then cancels it", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    await user.click(await screen.findByRole("button", { name: "Delete Acme Retail…" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete Acme Retail?" });
    const schedule = within(dialog).getByRole("button", { name: "Schedule deletion" });
    await user.type(within(dialog).getByLabelText("Type acme to confirm"), "acm");
    await user.click(within(dialog).getByRole("checkbox", { name: /cannot be undone/ }));
    expect(schedule).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Type acme to confirm"), "e");
    expect(schedule).toBeEnabled();
    await user.click(schedule);
    expect(await screen.findByRole("list", { name: "Deletion progress" })).toBeInTheDocument();
    expect(seen.find((r) => r.method === "POST" && r.path === "/v1/tenants/acme/deletion")?.body).toEqual({ confirm: "acme" });
    // The tenant is suspended meanwhile, and the banner says until when and how to cancel.
    const banner = await screen.findByText(/is scheduled for deletion on/);
    expect(banner.closest("[data-tenant-status]")).toHaveAttribute("data-tenant-status", "suspended");
    await user.click(screen.getByRole("button", { name: "Cancel the deletion" }));
    await waitFor(() => expect(screen.queryByText(/is scheduled for deletion on/)).toBeNull());
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });

  it("is only for organisation admins", async () => {
    renderApp("/settings", "rui");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Data" })).toBeNull();
  });
});

describe("admin console", () => {
  it("reviews a flagged sign-up", async () => {
    const user = userEvent.setup();
    renderApp("/admin?tab=signups");
    const table = await screen.findByRole("table", { name: "Sign-ups" });
    expect(within(table).getByText("same_device")).toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "Reject promo@burst.example" }));
    const dialog = await screen.findByRole("dialog", { name: "Reject promo@burst.example?" });
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(within(dialog).getByText(/audited with the decision/)).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Reason"), "Promotion farm");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Sign-ups" })).queryByText("promo@burst.example")).toBeNull());
  });

  it("marks a tenant's identity verified and shows its deletion status", async () => {
    const user = userEvent.setup();
    renderApp("/admin");
    await user.click(await screen.findByRole("button", { name: "Trust and deletion of Globex Labs" }));
    const dialog = await screen.findByRole("dialog", { name: "Trust: Globex Labs" });
    expect(await within(dialog).findByText("The tenant never asked to be deleted.")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("How was the identity checked?"), "manual");
    await user.type(within(dialog).getByLabelText("Reason (audited)"), "Contract signed, ticket T-42");
    await user.click(within(dialog).getByRole("button", { name: "Mark identity verified" }));
    expect(await within(dialog).findByRole("button", { name: "Withdraw verification" })).toBeInTheDocument();
    expect(seen.find((r) => r.method === "PUT" && r.path === "/v1/admin/tenants/globex/verification")?.body).toEqual({
      identity_verified: true,
      method: "manual",
      reason: "Contract signed, ticket T-42",
    });
  });
});
