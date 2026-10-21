import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { env } from "@/env";
import { ShareApiError } from "@/features/dashboards/client";
import { MOCK_SHARE_PASSCODE, MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";

import { LINK_ENDED_TEXT } from "./link-ended";
import { passcodeError, retryWords } from "./passcode-form";
import { phaseOfOpenError, ShareApp } from "./share-app";
import { footerText } from "./share-frame";

const server = setupServer(...createHandlers());
const seen: Request[] = [];
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
});
beforeEach(() => {
  seen.length = 0;
  localStorage.setItem("mapi.mock.readoutMs", "40");
});
afterEach(() => {
  server.resetHandlers();
  window.history.replaceState(null, "", "/");
});
afterAll(() => server.close());

function openAt(fragment: string) {
  window.history.replaceState(null, "", `/s${fragment}`);
  return render(<ShareApp />);
}

const sessionPosts = () => seen.filter((r) => r.method === "POST" && new URL(r.url).pathname === "/v1/share-sessions");

describe("ShareApp", () => {
  it("opens a read-only link: strips the token, trades it once, shows the readout and the footer", async () => {
    openAt(`#${MOCK_SHARE_TOKENS.readOnly}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Weekly media readout" })).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain(MOCK_SHARE_TOKENS.readOnly);
    expect(document.head.querySelector('meta[name="referrer"]')?.getAttribute("content")).toBe("no-referrer");
    expect(screen.getByText(/^Shared view\. The link ends on \d{2}\/\d{2}\/\d{4}\.$/)).toBeInTheDocument();

    await waitFor(() => expect(seen.some((r) => new URL(r.url).pathname === "/v1/shared/readouts")).toBe(true));
    const posts = sessionPosts();
    expect(posts).toHaveLength(1);
    expect(((await posts[0]!.json()) as { token: string }).token).toBe(MOCK_SHARE_TOKENS.readOnly);
    for (const r of seen) {
      expect(r.url).not.toContain(MOCK_SHARE_TOKENS.readOnly);
      expect(r.headers.get("Cookie")).toBeNull();
      expect(r.headers.get("X-CSRF-Token")).toBeNull();
      expect(r.headers.get("X-Mapi-Project")).toBeNull();
    }
    const shared = seen.filter((r) => new URL(r.url).pathname.startsWith("/v1/shared/"));
    expect(shared.length).toBeGreaterThan(0);
    for (const r of shared) expect(r.headers.get("X-Mapi-Share")).toMatch(/^mps_/);
  });

  it("keeps the token and the session out of persisted stores", async () => {
    openAt(`#${MOCK_SHARE_TOKENS.readOnly}`);
    await screen.findByRole("heading", { level: 1, name: "Weekly media readout" });
    await waitFor(() => expect(seen.some((r) => r.headers.get("X-Mapi-Share"))).toBe(true));
    const session = seen.find((r) => r.headers.get("X-Mapi-Share"))!.headers.get("X-Mapi-Share")!;
    const stored = [localStorage, sessionStorage].flatMap((s) => Object.keys(s).map((k) => `${k}=${s.getItem(k) ?? ""}`)).join("\n");
    expect(stored).not.toContain(session);
    expect(stored).not.toContain(MOCK_SHARE_TOKENS.readOnly);
    expect(stored).not.toMatch(/mps_|mpd_/);
    expect(document.cookie).not.toMatch(/mps_|mpd_/);
  });

  it("asks for the passcode, checks its length, says when it is wrong and opens with the right one", async () => {
    const user = userEvent.setup();
    openAt(`#${MOCK_SHARE_TOKENS.inputs}`);
    const field = await screen.findByLabelText("Passcode");
    expect(window.location.hash).toBe("");

    await user.type(field, "short");
    await user.click(screen.getByRole("button", { name: "Open the dashboard" }));
    expect(await screen.findByText(/at least 8 characters/)).toBeInTheDocument();
    expect(sessionPosts()).toHaveLength(1);

    await user.clear(field);
    await user.type(field, "not-the-passcode");
    await user.click(screen.getByRole("button", { name: "Open the dashboard" }));
    expect(await screen.findByText(/^The passcode is not correct\. You can try 4 more times before the link locks\.$/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Passcode"));
    await user.type(screen.getByLabelText("Passcode"), MOCK_SHARE_PASSCODE);
    await user.click(screen.getByRole("button", { name: "Open the dashboard" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Weekly media readout" })).toBeInTheDocument();
    const last = (await sessionPosts().at(-1)!.json()) as { token: string; passcode: string };
    expect(last).toEqual({ token: MOCK_SHARE_TOKENS.inputs, passcode: MOCK_SHARE_PASSCODE });
  });

  it("shows that a revoked link has ended", async () => {
    openAt(`#${MOCK_SHARE_TOKENS.revoked}`);
    expect(await screen.findByText(LINK_ENDED_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(/^Shared view/)).not.toBeInTheDocument();
  });

  it("ends the page when a later request answers 410 (revoked while open)", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/shared/readouts`, () =>
        HttpResponse.json({ type: "about:blank", code: "LINK_EXPIRED", title: "The link has expired or was removed", status: 410 }, { status: 410 }),
      ),
    );
    openAt(`#${MOCK_SHARE_TOKENS.readOnly}`);
    expect(await screen.findByText(LINK_ENDED_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Weekly media readout" })).not.toBeInTheDocument();
  });

  it("asks for the full link when the fragment has no valid token, without calling the server", async () => {
    openAt("#mpd_not-a-token");
    expect(await screen.findByText(/This page needs the full link/)).toBeInTheDocument();
    expect(sessionPosts()).toHaveLength(0);
  });
});

describe("share app helpers", () => {
  const problem = (status: number, code: string, detail?: string, retryAfter: number | null = null) =>
    new ShareApiError(status, { type: "about:blank", code, title: code, status, ...(detail ? { detail } : {}) } as never, undefined, retryAfter);

  it("maps the session answers to page phases", () => {
    expect(phaseOfOpenError(problem(410, "LINK_EXPIRED"), false)).toEqual({ kind: "ended", reason: "ended" });
    expect(phaseOfOpenError(problem(404, "NOT_FOUND"), false)).toEqual({ kind: "ended", reason: "ended" });
    expect(phaseOfOpenError(problem(401, "SHARE_PASSCODE_REQUIRED"), false)).toMatchObject({ kind: "passcode", error: null, locked: false });
    expect(phaseOfOpenError(problem(429, "SHARE_LOCKED", undefined, 900), true)).toMatchObject({
      kind: "passcode",
      locked: true,
      error: "The link is locked after too many wrong passcodes. Try again in 15 minutes.",
    });
    expect(phaseOfOpenError(problem(401, "SHARE_PASSCODE_INVALID", "1 tries left before the link locks"), true)).toMatchObject({
      error: "The passcode is not correct. You can try 1 more time before the link locks.",
    });
    expect(phaseOfOpenError(problem(429, "RATE_LIMITED", undefined, 30), false)).toMatchObject({ kind: "problem", retry: true });
    expect(phaseOfOpenError(new TypeError("Failed to fetch"), false)).toMatchObject({ kind: "problem", retry: true });
  });

  it("validates passcodes and words the wait", () => {
    expect(passcodeError("")).toMatch(/Type the passcode/);
    expect(passcodeError("1234567")).toMatch(/at least 8/);
    expect(passcodeError("12345678")).toBeNull();
    expect(passcodeError("x".repeat(129))).toMatch(/maximum of 128/);
    expect(retryWords(null)).toBe("Try again later.");
    expect(retryWords(30)).toBe("Try again in a minute.");
    expect(retryWords(61)).toBe("Try again in 2 minutes.");
  });

  it("words the footer", () => {
    expect(footerText("2026-11-19T09:00:00Z")).toBe("Shared view. The link ends on 19/11/2026.");
    expect(footerText(null)).toBe("Shared view.");
  });
});
