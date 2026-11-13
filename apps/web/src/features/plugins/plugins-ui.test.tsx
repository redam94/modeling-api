/**
 * The uploaded-code pages (v0.19 WP10) against the mock API (MSW): the reviewer panel (facts
 * first, URLs with punycode and never a `javascript:` link, the page's digests in the decision,
 * the uploader's disabled buttons, a stale version, the reauth round trip), untrusted strings
 * as text, the Uploaded section of Catalog › Yours, the settings card, the dev banner, the run
 * badge and the browser upload through the checks.
 */
import { File as NodeFile } from "node:buffer";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, getResponse, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, onTestFinished } from "vitest";

import { env } from "@/env";
import { AboutCard, isUploadedCatalogId, uploadedReferenceLink } from "@/features/about/about-card";
import * as fx from "@/mocks/fixtures";
import { createHandlers } from "@/mocks/handlers";
import { BEA_ID, MOCK_PLUGIN_MS_KEY, MOCK_PLUGIN_REAUTH_KEY, MOCK_PLUGINS_PAUSED_KEY, PLUGIN_ACME, PLUGIN_GEO, V } from "@/mocks/plugins";
import { renderApp } from "@/test/render-app";

import { exportsDiff } from "./exports-list";
import { shortNameProblem } from "./upload-page";

const API = env.apiBase;
const calls: { method: string; path: string; body: unknown }[] = [];
const server = setupServer(...createHandlers());
server.events.on("request:start", ({ request }) => {
  const url = new URL(request.url);
  if (!url.pathname.includes("/v1/")) return;
  void request
    .clone()
    .text()
    .then((t) => {
      let body: unknown = null;
      try {
        body = t ? (JSON.parse(t) as unknown) : null;
      } catch {
        body = t;
      }
      calls.push({ method: request.method, path: url.pathname, body });
    })
    .catch(() => calls.push({ method: request.method, path: url.pathname, body: null }));
});

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
  Element.prototype.scrollIntoView ??= () => undefined;
});
// a fresh mock state per test (the pages change versions)
beforeEach(() => server.resetHandlers(...createHandlers()));
afterEach(() => {
  calls.length = 0;
});
afterAll(() => server.close());

const versionUrl = (id: string) => `/plugins/${PLUGIN_ACME}/versions/${id}`;

describe("the reviewer panel", () => {
  it("shows the file facts first, every URL safely, and untrusted text as text", async () => {
    const { container } = renderApp(versionUrl(V.awaiting));
    const panel = await screen.findByTestId("plugin-reviewer-panel", {}, { timeout: 5000 });
    // web+cli+docs-6: the checklist the review docs list, including the "What changed" item
    const checklist = within(within(panel).getByRole("list", { name: "Review checklist" })).getAllByRole("listitem");
    expect(checklist).toHaveLength(6);
    expect(checklist[5]).toHaveTextContent("What changed matches what the uploader told you");
    // inspect facts come before the SBOM, the URLs and the test report
    const order = ["plugin-inspect-facts", "plugin-sbom", "plugin-urls", "plugin-kit-report", "plugin-exports-diff"].map((id) => within(panel).getByTestId(id));
    for (let i = 1; i < order.length; i++) expect(order[i - 1]!.compareDocumentPosition(order[i]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const urls = within(panel).getByTestId("plugin-urls");
    const links = within(urls).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.org/acme", "https://xn--bcher-kva.example/docs"]);
    for (const a of links) expect(a).toHaveAttribute("rel", "noopener noreferrer nofollow ugc");
    expect(urls).toHaveTextContent("xn--bcher-kva.example");
    expect(urls).toHaveTextContent("javascript:alert(1)");
    expect(urls).toHaveTextContent("Not a link: Links must start with https://.");
    expect(urls).toHaveTextContent("Not a link: Links must not contain a user name");

    // the hostile step title is text: no <img> anywhere, no bidi override left
    expect(container.querySelector("img")).toBeNull();
    // (the diff shows once the last approved version has loaded)
    await waitFor(() => expect(panel).toHaveTextContent("<img src=x onerror=alert(1)>Winsorize"));
    expect(panel.textContent ?? "").not.toContain("‮");
  });

  it("what changed waits for the last approved version, and says when it cannot load it", async () => {
    // web+cli+docs-5: never "every entry is new" because the comparison is missing
    let fail = false;
    server.use(
      http.get(new RegExp(`${V.approved}$`), async () => {
        if (fail) return HttpResponse.json({ type: "urn:x", title: "Down", status: 503, code: "UNAVAILABLE", detail: "down", errors: [] }, { status: 503, headers: { "content-type": "application/problem+json" } });
        await delay("infinite");
        return undefined;
      }),
    );
    const first = renderApp(versionUrl(V.awaiting));
    let diff = await screen.findByTestId("plugin-exports-diff", {}, { timeout: 5000 });
    expect(diff).toHaveAttribute("data-loading", "true");
    expect(diff).not.toHaveTextContent(/Compared with|everything is new/);
    first.unmount();
    fail = true;
    renderApp(versionUrl(V.awaiting));
    await waitFor(() => expect(screen.getByTestId("plugin-exports-diff")).toHaveTextContent("could not load the last approved version"), { timeout: 8000 });
    diff = screen.getByTestId("plugin-exports-diff");
    expect(diff).not.toHaveTextContent(/Compared with|everything is new/);
  }, 15_000);

  it("approve sends the digests the page showed", async () => {
    const user = userEvent.setup();
    renderApp(versionUrl(V.awaiting));
    const approve = await screen.findByTestId("plugin-approve", {}, { timeout: 5000 });
    expect(approve).toBeEnabled();
    await user.click(approve);
    await waitFor(() => expect(screen.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "approved"));
    const post = calls.find((c) => c.method === "POST" && c.path.endsWith(`${V.awaiting}:approve`));
    const body = post?.body as Record<string, string> | undefined;
    for (const k of ["overlay_sha256", "describe_digest", "base_image_digest"]) expect([k, body?.[k]?.startsWith("sha256:")]).toEqual([k, true]);
  });

  it("reject needs a note", async () => {
    const user = userEvent.setup();
    renderApp(versionUrl(V.awaiting));
    await user.click(await screen.findByTestId("plugin-reject", {}, { timeout: 5000 }));
    expect(await screen.findByText("Write a note for the uploader: say what to change.")).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith(":reject"))).toBe(false);
  });

  it("the uploader sees why the buttons are off", async () => {
    renderApp(versionUrl(V.awaitingOwn));
    const approve = await screen.findByTestId("plugin-approve", {}, { timeout: 5000 });
    expect(approve).toBeDisabled();
    expect(screen.getByTestId("plugin-decision-blocked")).toHaveTextContent("You uploaded this version, so another admin needs to review it.");
    expect(approve).toHaveAccessibleDescription(/You uploaded this version/);
  });

  it("a stale version says so and reloads", async () => {
    server.use(
      http.post(new RegExp(`${V.awaiting}:approve$`), () =>
        HttpResponse.json({ type: "urn:x", title: "The version changed", status: 409, code: "PLUGIN_STATE_CONFLICT", detail: "stale", errors: [] }, { status: 409, headers: { "content-type": "application/problem+json" } }),
      ),
    );
    const user = userEvent.setup();
    renderApp(versionUrl(V.awaiting));
    await user.click(await screen.findByTestId("plugin-approve", {}, { timeout: 5000 }));
    expect(await screen.findByText(/This version changed while you were looking at it/)).toBeInTheDocument();
    const before = calls.filter((c) => c.method === "GET" && c.path.endsWith(V.awaiting)).length;
    await user.click(screen.getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(calls.filter((c) => c.method === "GET" && c.path.endsWith(V.awaiting)).length).toBeGreaterThan(before));
  });

  it("an admin the uploader brought in sees the server's reason, not 'ask for a higher role'", async () => {
    // v0.19 live: the 403 showed the generic role advice and "See project members" to an admin
    const detail = "The person who uploaded this version made you a tenant admin less than 7 days ago, so you cannot review it. Ask another tenant admin.";
    server.use(
      http.post(new RegExp(`${V.awaiting}:approve$`), () =>
        HttpResponse.json({ type: "urn:x", title: "Not allowed", status: 403, code: "FORBIDDEN", detail, errors: [] }, { status: 403, headers: { "content-type": "application/problem+json" } }),
      ),
    );
    const user = userEvent.setup();
    renderApp(versionUrl(V.awaiting));
    await user.click(await screen.findByTestId("plugin-approve", {}, { timeout: 5000 }));
    expect(await screen.findByTestId("plugin-review-refused")).toHaveTextContent(detail);
    expect(screen.queryByText(/higher role/)).toBeNull();
    expect(screen.queryByRole("link", { name: "See project members" })).toBeNull();
  });

  it("a stale sign-in sends the reviewer through sign-in and back, keeping the note", async () => {
    localStorage.setItem(MOCK_PLUGIN_REAUTH_KEY, "1");
    const user = userEvent.setup();
    renderApp(versionUrl(V.awaiting));
    await user.type(await screen.findByLabelText("Note", {}, { timeout: 5000 }), "Looks fine");
    await user.click(screen.getByTestId("plugin-approve"));
    const link = await screen.findByTestId("plugin-reauth");
    expect(link.getAttribute("href")).toContain("/auth/login?return_to=");
    // review-flow-3: the server asks the identity provider to authenticate again
    expect(link.getAttribute("href")).toMatch(/[?&]reauth=1$/);
    expect(sessionStorage.getItem(`mapi.plugins.reviewNote.${V.awaiting}`)).toBe("Looks fine");
  });
});

describe("version pages", () => {
  it("a refused file lists its problems in plain words", async () => {
    renderApp(versionUrl(V.refused));
    const problems = await screen.findByTestId("plugin-problems", {}, { timeout: 5000 });
    expect(within(problems).getAllByRole("listitem")).toHaveLength(5);
    expect(problems).toHaveTextContent("Compiled code");
    expect(problems).toHaveTextContent("Path configuration file (.pth)");
    expect(screen.getByTestId("plugin-state-chip")).toHaveTextContent("Refused");
  });

  it("each description issue says where it is and shows the uploaded text as text", async () => {
    // web+cli+docs-4: the message is generic by design; the pointer and the text locate it
    const base = createHandlers();
    server.use(
      http.get(new RegExp(`${V.kitFailed}$`), async ({ request }) => {
        const res = await getResponse(base, request.clone());
        const v = (await res!.json()) as Record<string, unknown>;
        const issue = { code: "SCHEMA_INVALID", severity: "error", message: "The text holds hidden characters. Remove them.", pointer: "/models/0/about/title", untrusted_text: "<b>Store‮ mean</b>" };
        return HttpResponse.json({ ...v, issues: [issue] });
      }),
    );
    const { container } = renderApp(versionUrl(V.kitFailed));
    const [item] = await screen.findAllByTestId("plugin-issue", {}, { timeout: 5000 });
    expect(item).toHaveTextContent("The text holds hidden characters. Remove them.");
    expect(item).toHaveTextContent("/models/0/about/title");
    expect(item).toHaveTextContent("<b>Store");
    expect(item!.textContent ?? "").not.toContain("‮");
    expect(container.querySelector("li b")).toBeNull();
  });

  it("a failed test run groups the report and shows the SBOM", async () => {
    renderApp(versionUrl(V.kitFailed));
    const kit = await screen.findByTestId("plugin-kit-report", {}, { timeout: 5000 });
    expect(within(kit).getAllByRole("heading", { level: 4 }).map((h) => h.textContent)).toEqual(["Failed (2)", "Passed (3)", "Skipped (1)"]);
    expect(kit.textContent ?? "").not.toContain("‮");
    const sbom = screen.getByTestId("plugin-sbom");
    expect(within(sbom).getByTestId("plugin-sbom-files")).toHaveTextContent("40");
    expect(sbom).toHaveTextContent("2.1.3");
  });

  it("withdraw shows what uses the version first", async () => {
    const user = userEvent.setup();
    renderApp(versionUrl(V.approved));
    await user.click(await screen.findByTestId("plugin-yank", {}, { timeout: 5000 }));
    const usage = await screen.findByTestId("plugin-usage");
    await waitFor(() => expect(within(usage).getByTestId("plugin-usage-runs")).toHaveTextContent("14"));
    await user.click(screen.getByTestId("plugin-yank-confirm"));
    await waitFor(() => expect(screen.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "yanked"));
  });

  it("the uploader asks for a review of a private version", async () => {
    const user = userEvent.setup();
    renderApp(versionUrl(V.privateOwn));
    expect(await screen.findByTestId("plugin-only-you", {}, { timeout: 5000 })).toHaveTextContent("Only you can use this");
    await user.click(screen.getByTestId("plugin-request-approval"));
    await waitFor(() => expect(screen.getByTestId("plugin-version-page")).toHaveAttribute("data-state", "awaiting_approval"));
  });
});

describe("paused by the platform", () => {
  it("a version waiting for its checks says the platform paused uploaded code", async () => {
    // web+cli+docs-3: GET /v1/me plugins_paused (kill switch "Allow uploaded code to run" off)
    localStorage.setItem(MOCK_PLUGINS_PAUSED_KEY, "1");
    onTestFinished(() => localStorage.removeItem(MOCK_PLUGINS_PAUSED_KEY));
    renderApp(`/plugins/${PLUGIN_GEO}/versions/${V.inspecting}`);
    const paused = await screen.findByTestId("plugin-checks-paused", {}, { timeout: 5000 });
    expect(paused).toHaveTextContent("Paused by the platform");
    expect(screen.getByTestId("plugin-check-steps")).toHaveAttribute("data-paused", "true");
  });

  it("says nothing about a pause while uploaded code runs", async () => {
    renderApp(`/plugins/${PLUGIN_GEO}/versions/${V.inspecting}`);
    await screen.findByTestId("plugin-check-steps", {}, { timeout: 5000 });
    expect(screen.queryByTestId("plugin-checks-paused")).toBeNull();
  });
});

describe("Catalog › Yours › Uploaded", () => {
  it("lists the plugins with state chips and who uploaded them", async () => {
    renderApp("/catalog?tab=yours");
    const section = await screen.findByTestId("uploaded-section", {}, { timeout: 8000 });
    const cards = await within(section).findAllByTestId("uploaded-card");
    expect(cards.length).toBe(2);
    const acme = cards.find((c) => c.getAttribute("data-plugin") === "acme")!;
    expect(within(acme).getByTestId("plugin-state-chip")).toHaveAttribute("data-state", "kit_failed");
    expect(within(acme).getByTestId("uploaded-by-chip")).toHaveTextContent("Uploaded by you");
  });
});

describe("people on uploaded code", () => {
  it("names an attested uploader who is only a project member (admins read the uploader list)", async () => {
    // v0.19 live: an editor attested to upload showed as "user usr_…" to the tenant admins
    server.use(
      http.get(`${API}/v1/tenants/acme/members`, () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`${API}/v1/tenants/acme/plugin-uploaders`, () =>
        HttpResponse.json([{ user_id: BEA_ID, display_name: "Bea Builder", email: "bea@acme.example", method: "admin_attested", verified_by: "usr_x", verified_at: "2026-10-01T00:00:00Z" }]),
      ),
    );
    renderApp(`/plugins/${PLUGIN_ACME}`);
    expect(await screen.findByText(/Started by Bea Builder/, {}, { timeout: 8000 })).toBeInTheDocument();
  });
});

describe("dev banner and run badge", () => {
  it("the banner shows when the server runs uploads without a sandbox", async () => {
    // the mock's own /v1/me answer, plus the flag
    const base = createHandlers();
    server.use(
      http.get(`${API}/v1/me`, async ({ request }) => {
        const res = await getResponse(base, request.clone());
        const me = (await res!.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...me, dev_untrusted_code: true });
      }),
    );
    renderApp(versionUrl(V.approved));
    expect(await screen.findByTestId("dev-untrusted-banner", {}, { timeout: 5000 })).toHaveTextContent("Development only");
  });
});

describe("run detail", () => {
  it("an upload check run carries the Uploaded code badge; an ordinary run does not", async () => {
    const run = { ...fx.runFailed, kind: "plugin_inspect" as const };
    server.use(http.get(`${API}/v1/runs/${run.id}`, () => HttpResponse.json(run)));
    renderApp(`/runs/${run.id}`);
    expect(await screen.findByTestId("run-uploaded-badge", {}, { timeout: 5000 })).toHaveTextContent("Uploaded code");
  });

  it("a plain fit has no badge", async () => {
    renderApp(`/runs/${fx.runFailed.id}`);
    await screen.findByText(fx.runFailed.id, {}, { timeout: 5000 });
    expect(screen.queryByTestId("run-uploaded-badge")).toBeNull();
  });
});

describe("the upload page", () => {
  it("uploads a wheel and follows the checks to the test report", async () => {
    localStorage.setItem(MOCK_PLUGIN_MS_KEY, "200");
    const user = userEvent.setup();
    renderApp(`/plugins/new?plugin=${PLUGIN_ACME}`);
    const drop = await screen.findByTestId("plugin-upload-drop", {}, { timeout: 5000 });
    await screen.findByText(/Namespace t_k3vq7m_acme/, {}, { timeout: 5000 });
    expect(screen.getByTestId("plugin-upload-cli")).toHaveTextContent("mapi plugin push --plugin acme");
    const input = drop.querySelector("input[type=file]") as HTMLInputElement;
    // jsdom's File and FormData do not stream through Node's fetch: Node's own classes here
    // (the browser has one set of both), as in states.test.ts.
    const wheel = new NodeFile([new Uint8Array(2048)], "t_k3vq7m_acme-0.4.0-py3-none-any.whl", { type: "application/zip" });
    Object.defineProperty(input, "files", { configurable: true, get: () => [wheel] });
    fireEvent.change(input);
    const jsdomFormData = globalThis.FormData;
    globalThis.FormData = (await new Response("a=1", { headers: { "content-type": "application/x-www-form-urlencoded" } }).formData()).constructor as typeof FormData;
    onTestFinished(() => {
      globalThis.FormData = jsdomFormData;
    });
    await user.click(screen.getByTestId("plugin-upload-start"));
    const result = await screen.findByTestId("plugin-upload-result", {}, { timeout: 5000 });
    await waitFor(() => expect(result).toHaveAttribute("data-state", "kit_passed"), { timeout: 8000 });
    expect(await screen.findByTestId("plugin-kit-report")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-sbom")).toBeInTheDocument();
    expect(calls.map((c) => `${c.method} ${c.path.replace(/^.*\/v1/, "/v1")}`)).toEqual(
      expect.arrayContaining([`POST /v1/plugins/${PLUGIN_ACME}/uploads`, expect.stringMatching(/^POST \/v1\/plugins\/.+:complete$/)]),
    );
  }, 15_000);

  it("after the first claim the picker shows the new plugin and the claim form closes", async () => {
    // web+cli+docs-1: no plugin yet; the list has the new one only after the claim
    let claimed = false;
    server.use(
      http.post(`${API}/v1/plugins`, () => {
        claimed = true;
      }),
      http.get(`${API}/v1/plugins`, () => (claimed ? undefined : HttpResponse.json({ items: [], next_cursor: null }))),
    );
    const user = userEvent.setup();
    renderApp("/plugins/new");
    const form = await screen.findByRole("form", { name: "Start a new plugin" }, { timeout: 5000 });
    await user.type(within(form).getByLabelText("Short name"), "zeta");
    await user.type(within(form).getByLabelText("Title"), "Zeta tools");
    await user.click(within(form).getByRole("button", { name: "Create the plugin" }));
    await screen.findByText(/Namespace t_[a-z2-7]{6}_zeta/, {}, { timeout: 5000 });
    expect(screen.getByLabelText("Plugin")).not.toHaveValue("__new");
    expect(screen.queryByRole("form", { name: "Start a new plugin" })).toBeNull();
  });

  it("names the wheel file the way build tools write it (underscores), and warns on another name", async () => {
    // v0.19 live: the hint said `t-k3vq7m-acme-<version>-…whl`, but `uv build` writes `t_k3vq7m_acme-…`
    const user = userEvent.setup({ applyAccept: false });
    renderApp(`/plugins/new?plugin=${PLUGIN_ACME}`);
    const drop = await screen.findByTestId("plugin-upload-drop", {}, { timeout: 5000 });
    expect(await screen.findByText(/Namespace t_k3vq7m_acme/, {}, { timeout: 5000 })).toHaveTextContent(
      "The package must be named t-k3vq7m-acme, so its wheel is t_k3vq7m_acme-<version>-py3-none-any.whl.",
    );
    const input = drop.querySelector("input[type=file]") as HTMLInputElement;
    await user.upload(input, new File(["x"], "t_k3vq7m_acme-0.4.0-py3-none-any.whl"));
    expect(screen.queryByText(/doesn't start with/)).toBeNull();
    await user.upload(input, new File(["x"], "other_pkg-0.4.0-py3-none-any.whl"));
    expect(await screen.findByText(/doesn't start with/)).toHaveTextContent("This wheel's name doesn't start with t_k3vq7m_acme");
  });

  it("refuses a file that is not a pure-Python wheel before asking for a ticket", async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderApp(`/plugins/new?plugin=${PLUGIN_ACME}`);
    const drop = await screen.findByTestId("plugin-upload-drop", {}, { timeout: 5000 });
    await screen.findByText(/Namespace t_k3vq7m_acme/, {}, { timeout: 5000 });
    const input = drop.querySelector("input[type=file]") as HTMLInputElement;
    await user.upload(input, new File(["x"], "t_k3vq7m_acme-0.4.0-cp312-cp312-linux_x86_64.whl"));
    expect(await screen.findByText("Only pure-Python wheels (py3-none-any) can be uploaded.")).toBeInTheDocument();
    expect(screen.getByTestId("plugin-upload-start")).toBeDisabled();
  });
});

describe("settings card", () => {
  it("tenant admins turn private use off and confirm an uploader", async () => {
    const user = userEvent.setup();
    renderApp("/settings");
    const card = await screen.findByTestId("plugin-settings-card", {}, { timeout: 8000 });
    const toggle = await within(card).findByTestId("plugin-private-use");
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path.endsWith("/plugin-settings"))).toBe(true));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
    const uploaders = within(card).getByTestId("plugin-uploaders");
    expect(await within(uploaders).findByText("Rui Runner")).toBeInTheDocument();
    await act(async () => {
      await user.click(within(uploaders).getByRole("button", { name: /Revoke Rui Runner/ }));
    });
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path.includes("/plugin-uploaders/"))).toBe(true));
  });
});

describe("the About card's upload rules (inline in about-card.tsx)", () => {
  it("recognises every uploaded id kind and no built-in one", () => {
    for (const id of ["t_k3vq7m_acme.store_mean", "transform.t_k3vq7m_acme_lag", "t_k3vq7m_acme_weekly"]) expect([id, isUploadedCatalogId(id)]).toEqual([id, true]);
    for (const id of ["ts.sarimax", "core.clip", "transform.lag", "t_short.x", "t_k3vq7m_a.x", null]) expect([id, isUploadedCatalogId(id)]).toEqual([id, false]);
  });

  it("links only safe references, with the punycode host", () => {
    expect(uploadedReferenceLink("https://xn--bcher-kva.example/docs")).toEqual({ href: "https://xn--bcher-kva.example/docs", host: "xn--bcher-kva.example" });
    for (const url of ["javascript:alert(1)", "http://example.org", "https://user@evil.example/x", "https://bücher.example/", "https://10.0.0.1/", "https://exa\u202emple.org/"]) {
      expect([url, uploadedReferenceLink(url)]).toEqual([url, null]);
    }
  });

  it("an uploaded About card shows the chip and marks outside links", () => {
    const about = {
      title: "Store mean",
      summary_plain: "A mean.",
      use_when: [],
      avoid_when: [],
      data_needs: [],
      outputs: [],
      assumptions: [],
      example: "x",
      difficulty: "beginner",
      typical_runtime: "seconds",
      references: [
        { title: "Docs", url: "https://example.org/a" },
        { title: "Bad", url: "javascript:alert(1)" },
      ],
    } as never;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AboutCard about={about} id="t_k3vq7m_acme.store_mean" expertOpen />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("uploaded-by-chip")).toHaveTextContent("Uploaded by your team");
    const link = screen.getByRole("link", { name: /Docs/ });
    expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow ugc");
    expect(screen.getByText("example.org")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Bad/ })).toBeNull();
  });
});

describe("pure helpers", () => {
  it("short names follow PLUGIN_SHORT_RE", () => {
    expect(shortNameProblem("acme")).toBeNull();
    expect(shortNameProblem("a")).toMatch(/2 to 16/);
    expect(shortNameProblem("1acme")).toMatch(/lowercase letter/);
    expect(shortNameProblem("ac_me")).toMatch(/no spaces, dashes or underscores/);
  });

  it("the exports diff marks new, changed, removed and same", () => {
    const step = (id: string, title: string) => ({ id, version: "0.1.0", about: { title } }) as never;
    const rows = exportsDiff({ steps: [step("a", "A"), step("b", "B2"), step("c", "C")] }, { steps: [step("b", "B"), step("c", "C"), step("d", "D")] });
    expect(rows.map((r) => `${r.id}:${r.mark}`)).toEqual(["a:added", "b:changed", "c:same", "d:removed"]);
  });
});
