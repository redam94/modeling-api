import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { env } from "@/env";
import { V2_DASHBOARD_IDS, V2_PROJECT, MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";

import { openShareSession, refreshShareSession, ShareApiError } from "../client";
import type { ShareSessionHandle } from "../types";

import { chatErrorOf, chatReducer, initialChatState, pendingRequest, type ChatState } from "./conversation";
import { memberChatTransport } from "./transport-member";
import { shareChatTransport } from "./transport-share";
import type { ChatSettingsView, ChatTransport } from "./types";

let server = setupServer(...createHandlers());
const seen: Request[] = [];

function listen() {
  server.listen({ onUnhandledRequest: "error" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
}

beforeAll(listen);
beforeEach(() => {
  // A fresh mock state for each test (chat settings, budgets, sessions).
  server.close();
  server = setupServer(...createHandlers());
  listen();
  seen.length = 0;
});
afterEach(() => {
  useAuth.setState({ mode: "unknown", csrf: null });
  vi.restoreAllMocks();
});
afterAll(() => server.close());

const FORECAST = V2_DASHBOARD_IDS.forecast;

/** Turn chat on for every audience of the forecast dashboard (as Ana, outside any transport). */
async function chatOn(): Promise<void> {
  localStorage.setItem(MOCK_SESSION_KEY, "ana");
  const h = { "X-Mapi-Project": V2_PROJECT, "X-CSRF-Token": mockCsrfToken(PERSONAS.ana!), "Content-Type": "application/json" };
  const cur = (await (await fetch(`${env.apiBase}/v1/dashboards/${FORECAST}/chat/settings`, { headers: h })).json()) as ChatSettingsView;
  const body = { enabled: true, audiences: { members: true, clients: true, share_links: true }, keep_transcripts: false, tokens_per_day: 200_000, share_tokens_per_day: 50_000 };
  const res = await fetch(`${env.apiBase}/v1/dashboards/${FORECAST}/chat/settings`, { method: "PUT", headers: { ...h, "If-Match": cur.etag }, body: JSON.stringify(body) });
  expect(res.status).toBe(200);
  seen.length = 0;
}

async function handleFor(token: string): Promise<ShareSessionHandle & { session: string }> {
  const first = await openShareSession(token);
  const h = {
    session: first.session,
    current: () => h.session,
    expired: () => false,
    refresh: async () => {
      h.session = (await refreshShareSession(h.session)).session;
    },
  };
  return h;
}

/** Ask `questions` in order through `t`, as the panel would (reducer + transport). */
async function converse(t: ChatTransport, questions: string[]): Promise<ChatState> {
  let s = initialChatState;
  for (const q of questions) {
    s = chatReducer(s, { type: "send", text: q });
    try {
      s = chatReducer(s, { type: "replied", response: await t.send(pendingRequest(s, 3)!) });
    } catch (error) {
      s = chatReducer(s, { type: "failed", error });
    }
  }
  return s;
}

describe("shareChatTransport", () => {
  it("sends the share session only: never cookies, the CSRF header or the project header", async () => {
    await chatOn();
    // A signed-in analyst in the same browser: the share transport must still send none of it.
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
    const h = await handleFor(MOCK_SHARE_TOKENS.forecastV2);
    const spy = vi.spyOn(globalThis, "fetch");
    const t = shareChatTransport(h);
    expect((await t.availability()).enabled_for_me).toBe(true);
    const s = await converse(t, ["What does the latest readout show?", "And what changed?"]);
    expect(s.failed).toBeNull();
    expect(s.turns).toHaveLength(4);
    for (const [input, init] of spy.mock.calls) {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      expect(url).toBe(`${env.apiBase}/v1/shared/chat`);
      expect(init?.credentials).toBe("omit");
      expect(init?.referrerPolicy).toBe("no-referrer");
      expect(init?.cache).toBe("no-store");
      const headers = new Headers(init?.headers);
      expect(headers.get("X-Mapi-Share")).toBe(h.session);
      expect(headers.has("X-CSRF-Token")).toBe(false);
      expect(headers.has("X-Mapi-Project")).toBe(false);
      expect(headers.has("Cookie")).toBe(false);
      expect(headers.has("Authorization")).toBe(false);
    }
    const chat = seen.filter((r) => new URL(r.url).pathname === "/v1/shared/chat");
    expect(chat).toHaveLength(3);
    for (const r of chat) expect(r.credentials).toBe("omit");
    // The second request carried the first answer with its sig, unchanged, and the readout hint.
    const second = (await chat[2]!.json()) as { conversation_id: string; messages: { role: string; sig?: string }[]; readout_seq: number };
    expect(second.conversation_id).toBe(s.conversationId);
    expect(second.messages[1]!.sig).toBe(s.turns[1]!.role === "assistant" ? s.turns[1]!.sig : null);
    expect(second.readout_seq).toBe(3);
  });

  it("refreshes an ended session once and asks again", async () => {
    await chatOn();
    const h = await handleFor(MOCK_SHARE_TOKENS.forecastV2);
    const real = h.session;
    h.session = "mps_ended.session";
    const t = shareChatTransport({ ...h, current: () => h.session, refresh: async () => void (h.session = real) });
    expect((await t.availability()).enabled_for_me).toBe(true);
    expect(seen.filter((r) => new URL(r.url).pathname === "/v1/shared/chat")).toHaveLength(2);
  });

  it("reports a revoked link as 410 LINK_EXPIRED", async () => {
    await chatOn();
    const h = await handleFor(MOCK_SHARE_TOKENS.forecastV2);
    const t = shareChatTransport(h);
    const before = await converse(t, ["What does the latest readout show?"]);
    expect(before.turns).toHaveLength(2);
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    const res = await fetch(`${env.apiBase}/v1/dashboards/${FORECAST}/shares/dss_01jaf3w2fcst2share3x4y5z6:revoke`, {
      method: "POST",
      headers: { "X-Mapi-Project": V2_PROJECT, "X-CSRF-Token": mockCsrfToken(PERSONAS.ana!) },
    });
    expect(res.status).toBeLessThan(300);
    const err = await t.send({ messages: [{ role: "user", text: "Still there?" }] }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ShareApiError);
    expect(chatErrorOf(err)).toMatchObject({ kind: "link_ended", status: 410, code: "LINK_EXPIRED" });
  });

  it("reads Retry-After of a spent budget", async () => {
    await chatOn();
    localStorage.setItem("mapi.mock.chatBudget", "1");
    const s = await converse(shareChatTransport(await handleFor(MOCK_SHARE_TOKENS.forecastV2)), ["one", "two"]);
    expect(s.turns).toHaveLength(2);
    expect(s.failed?.text).toBe("two");
    expect(s.failed?.error.kind).toBe("budget");
    expect(s.failed?.error.retryAfter).toBeGreaterThan(0);
  });
});

describe("memberChatTransport", () => {
  it("asks through the signed-in API with the session's CSRF and project headers", async () => {
    await chatOn();
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    const t = memberChatTransport(FORECAST, { scopeKey: V2_PROJECT });
    expect((await t.availability()).enabled_for_me).toBe(true);
    const s = await converse(t, ["What does the latest readout show?", "And what changed?"]);
    expect(s.failed).toBeNull();
    expect(s.turns.map((x) => x.role)).toEqual(["user", "assistant", "user", "assistant"]);
    const chat = seen.filter((r) => new URL(r.url).pathname === `/v1/dashboards/${FORECAST}/chat`);
    expect(chat).toHaveLength(3);
    for (const r of chat) {
      expect(r.headers.get("X-Mapi-Project")).toBe(V2_PROJECT);
      expect(r.headers.has("X-Mapi-Share")).toBe(false);
      expect(r.credentials).toBe("include");
    }
    for (const r of chat.filter((x) => x.method === "POST")) expect(r.headers.get("X-CSRF-Token")).toBe(mockCsrfToken(PERSONAS.ana!));
  });

  it("throws the API error of a refused turn (chat off: 409 CHAT_DISABLED)", async () => {
    useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
    localStorage.setItem(MOCK_SESSION_KEY, "ana");
    const t = memberChatTransport(FORECAST, { scopeKey: V2_PROJECT });
    expect(await t.availability()).toMatchObject({ enabled_for_me: false, reason: "off" });
    const s = await converse(t, ["hello"]);
    expect(s.failed?.error).toMatchObject({ kind: "disabled", status: 409, code: "CHAT_DISABLED" });
  });
});
