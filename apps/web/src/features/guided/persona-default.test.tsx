/**
 * Where a first-time user lands: a self-service (public tier) workspace starts in Guided mode,
 * Expert navigation always offers Guided, and the sign-up's "Sign in" returns to Guided.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { PublicApp } from "@/features/trust/public-pages";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_GUIDED_LOCK_KEY, PERSONAS } from "@/mocks/tenancy";
import { MOCK_OUTBOX_KEY } from "@/mocks/trust";
import { renderApp } from "@/test/render-app";
import { personaOf, useUi } from "@/stores/ui";

import { defaultPersona } from "./persona";

const signIn = vi.hoisted(() => vi.fn());
vi.mock("@/api/session", async (orig) => ({ ...(await orig<typeof import("@/api/session")>()), signIn }));

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  useUi.setState({ persona: null, personas: {} });
  signIn.mockReset();
  sessionStorage.removeItem(MOCK_OUTBOX_KEY);
  window.history.replaceState(null, "", "/");
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("the default persona", () => {
  it("is Guided in a self-service workspace and Expert elsewhere or for operators", () => {
    expect(defaultPersona("public", false)).toBe("guided");
    expect(defaultPersona("verified", false)).toBe("expert");
    expect(defaultPersona("internal", false)).toBe("expert");
    expect(defaultPersona(undefined, false)).toBe("expert");
    expect(defaultPersona("public", true)).toBe("expert");
  });

  it("applies only while neither the user nor this browser has chosen", () => {
    expect(personaOf(useUi.getState(), "usr_new", "guided")).toBe("guided");
    useUi.getState().setPersona("expert", "usr_a");
    expect(personaOf(useUi.getState(), "usr_a", "guided")).toBe("expert");
    // the browser's last choice still carries over to a user without their own
    expect(personaOf(useUi.getState(), "usr_new", "guided")).toBe("expert");
  });

  it("forgets the browser-wide default stored before it could be told apart from a choice", () => {
    const migrate = useUi.persist.getOptions().migrate!;
    const v2 = { theme: "dark", persona: "expert", personas: { usr_a: "guided" }, sidebarCollapsed: false };
    expect(migrate(v2, 2)).toMatchObject({ theme: "dark", persona: null, personas: { usr_a: "guided" } });
  });
});

describe("a first visit", () => {
  it("opens Guided mode in a self-service workspace", async () => {
    // Ana, without her operator flag, in Globex's public-tier Sandbox.
    const ana = PERSONAS.ana!;
    ana.operator = false;
    try {
      const { router } = renderApp("/", "ana", { tenantId: "globex", projectId: "sandbox" });
      expect(await screen.findByRole("heading", { name: "What do you want to learn?" })).toBeInTheDocument();
      await waitFor(() => expect(router.state.location.pathname).toBe("/guided"));
      expect(screen.getByRole("radiogroup", { name: "Mode" })).toBeInTheDocument();
    } finally {
      ana.operator = true;
    }
  });

  it("opens Expert Home elsewhere, whose navigation and start card lead to Guided", async () => {
    const { router } = renderApp("/", "ana");
    const nav = await screen.findByRole("navigation", { name: "Primary" });
    expect(router.state.location.pathname).toBe("/");
    expect(within(nav).getByRole("link", { name: "Guided" })).toHaveAttribute("href", "/guided");
    expect(within(nav).getByRole("link", { name: "Home" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /Start a guided analysis/ })).toHaveAttribute("href", "/guided");
  });
});

describe("the Guided lock", () => {
  it("is switched on and off by a project admin in Settings", async () => {
    const user = userEvent.setup();
    const patches: unknown[] = [];
    server.events.on("request:start", ({ request }) => {
      if (request.method === "PATCH" && new URL(request.url).pathname === "/v1/tenants/acme/projects/growth") {
        void request.clone().json().then((b) => patches.push(b));
      }
    });
    try {
      renderApp("/settings", "ana");
      const lock = await screen.findByRole("switch", { name: "Lock Growth to Guided mode" });
      expect(lock).not.toBeChecked();
      await user.click(lock);
      await waitFor(() => expect(lock).toBeChecked());
      expect(patches).toEqual([{ guided_locked: true }]);
      expect(screen.getByText(/Members below admin see Guided screens only/)).toBeInTheDocument();
      await user.click(lock);
      await waitFor(() => expect(lock).not.toBeChecked());
      expect(patches).toEqual([{ guided_locked: true }, { guided_locked: false }]);
    } finally {
      server.events.removeAllListeners();
      localStorage.removeItem(MOCK_GUIDED_LOCK_KEY);
    }
  });

  it("is not offered below the project admin role", async () => {
    renderApp("/settings", "rui");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Mode" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: /to Guided mode/ })).toBeNull();
  });
});

describe("after sign-up", () => {
  it("signs in to Guided mode", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<PublicApp path="/signup" />);
    await user.type(screen.getByLabelText("Work e-mail"), "nia@northwind.example");
    await user.type(screen.getByLabelText("Your name"), "Nia Newcomer");
    await user.click(screen.getByRole("checkbox", { name: /I accept the terms/ }));
    await user.click(screen.getByRole("button", { name: "Create workspace" }));
    const href = (await screen.findByRole("link", { name: "Open the verification link" })).getAttribute("href") ?? "";
    unmount();
    window.history.replaceState(null, "", href);
    render(<PublicApp path="/verify-email" />);
    await user.click(screen.getByRole("button", { name: "Confirm and create my workspace" }));
    await user.click(await screen.findByRole("button", { name: "Sign in" }));
    expect(signIn).toHaveBeenCalledWith("/guided", expect.anything());
  });
});
