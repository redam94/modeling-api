import { setupServer } from "msw/node";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { ShareSession } from "@/api/types";
import { env } from "@/env";
import type { ChatAvailability, ChatResponse, ChatSettingsView, ChatTranscript, ReadToken, ReadTokenCreated } from "@/features/dashboards/chat/types";

import { createChatHandlers, MOCK_CHAT_BUDGET_KEY, MOCK_CHAT_HOSTILE_REPLY, MOCK_CHAT_REPLY_KEY, mockTurnSig } from "./chat";
import { MMM_DASHBOARD_ID, MOCK_SHARE_TOKENS, V2_DASHBOARD_IDS, V2_PROJECT } from "./dashboards";
import { createHandlers, createState } from "./handlers";
import { isClientRoute, MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "./tenancy";

let state = createState();
let server = setupServer(...createHandlers(state));
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterAll(() => server.close());
beforeEach(() => {
  // A fresh mock state for each test (settings, budgets, tokens).
  server.close();
  state = createState();
  server = setupServer(...createHandlers(state));
  server.listen({ onUnhandledRequest: "error" });
});

type Who = keyof typeof PERSONAS;
const FORECAST = V2_DASHBOARD_IDS.forecast;
const D = (id: string = FORECAST) => `/v1/dashboards/${id}`;

async function call<T>(who: Who, method: string, path: string, body?: unknown, headers: Record<string, string> = {}, project = V2_PROJECT): Promise<{ status: number; data: T; headers: Headers }> {
  localStorage.setItem(MOCK_SESSION_KEY, who);
  const res = await fetch(`${env.apiBase}${path}`, {
    method,
    headers: { "X-Mapi-Project": project, "X-CSRF-Token": mockCsrfToken(PERSONAS[who]!), ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, headers: res.headers, data: (text ? JSON.parse(text) : null) as T };
}

async function shared<T>(session: string, method: string, body?: unknown): Promise<{ status: number; data: T; headers: Headers }> {
  const res = await fetch(`${env.apiBase}/v1/shared/chat`, {
    method,
    headers: { "X-Mapi-Share": session, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body === undefined ? null : JSON.stringify(body),
  });
  const text = await res.text();
  return { status: res.status, headers: res.headers, data: (text ? JSON.parse(text) : null) as T };
}

async function openLink(token: string): Promise<ShareSession> {
  const res = await fetch(`${env.apiBase}/v1/share-sessions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
  expect(res.status).toBe(200);
  return (await res.json()) as ShareSession;
}

/** Turn chat on (as Ana, an admin of acme/retail) with `patch` over the defaults. */
async function chatOn(patch: Record<string, unknown> = {}, id: string = FORECAST): Promise<ChatSettingsView> {
  const cur = await call<ChatSettingsView>("ana", "GET", `${D(id)}/chat/settings`);
  expect(cur.status).toBe(200);
  const body = { enabled: true, audiences: { members: true, clients: true, share_links: true }, keep_transcripts: false, tokens_per_day: 200_000, share_tokens_per_day: 50_000, ...patch };
  const res = await call<ChatSettingsView>("ana", "PUT", `${D(id)}/chat/settings`, body, { "If-Match": cur.data.etag });
  expect(res.status).toBe(200);
  return res.data;
}

const ask = (text: string, extra: Record<string, unknown> = {}) => ({ messages: [{ role: "user", text }], ...extra });

describe("chat availability", () => {
  it("is off by default, needs layout 2 and follows the audiences", async () => {
    const off = await call<ChatAvailability>("ana", "GET", `${D()}/chat`);
    expect(off.status).toBe(200);
    expect(off.data).toMatchObject({ enabled_for_me: false, reason: "off", transcript_days: 30 });
    expect(off.data.suggestions).toHaveLength(4);
    const layout1 = await call<ChatAvailability>("ana", "GET", `${D(MMM_DASHBOARD_ID)}/chat`, undefined, {}, "acme/growth");
    expect(layout1.data.reason).toBe("layout");
    await chatOn({ audiences: { members: false, clients: true, share_links: false } });
    expect((await call<ChatAvailability>("ana", "GET", `${D()}/chat`)).data.reason).toBe("audience");
    await chatOn();
    const on = await call<ChatAvailability>("ana", "GET", `${D()}/chat`);
    expect(on.data).toMatchObject({ enabled_for_me: true, reason: null, model: "fake-chat", keeps_transcripts: false });
  });

  it("is off for everyone when the operator's kill switch is off", async () => {
    await chatOn();
    state.tenancy.killSwitches = { ...state.tenancy.killSwitches, chat_enabled: false };
    expect((await call<ChatAvailability>("ana", "GET", `${D()}/chat`)).data.reason).toBe("killswitch");
    expect((await call<{ code: string }>("ana", "POST", `${D()}/chat`, ask("hello"))).data.code).toBe("CHAT_DISABLED");
    state.tenancy.killSwitches = { ...state.tenancy.killSwitches, chat_enabled: true };
    expect((await call<ChatAvailability>("ana", "GET", `${D()}/chat`)).data.enabled_for_me).toBe(true);
  });
});

describe("chat turns", () => {
  it("answers with numbers from the latest readout and signs the answer for its position", async () => {
    await chatOn();
    const r1 = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("What changed since the last readout?"));
    expect(r1.status).toBe(200);
    expect(r1.data.conversation_id).toMatch(/^cnv_[0-9a-z]{26}$/);
    expect(r1.data.reply.role).toBe("assistant");
    expect(r1.data.reply.sig).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(r1.data.reply.text).toMatch(/readout \d+/);
    expect(r1.data.tool_calls?.map((t) => t.name)).toContain("get_latest_readout");
    expect(r1.data.notices).toEqual([]);
    expect(r1.data.usage.total_tokens).toBeGreaterThan(0);
    expect(r1.data.reply.sig).toBe(mockTurnSig("member", PERSONAS.ana!.id, FORECAST, r1.data.conversation_id, 1, r1.data.reply.text));

    const history = [{ role: "user", text: "What changed since the last readout?" }, r1.data.reply];
    const r2 = await call<ChatResponse>("ana", "POST", `${D()}/chat`, { conversation_id: r1.data.conversation_id, messages: [...history, { role: "user", text: "And the accuracy?" }] });
    expect(r2.status).toBe(200);
    expect(r2.data.conversation_id).toBe(r1.data.conversation_id);

    // An edited, moved or foreign answer is refused.
    const edited = { ...r1.data.reply, text: `${r1.data.reply.text} (edited)` };
    const bad = await call<{ code: string }>("ana", "POST", `${D()}/chat`, { conversation_id: r1.data.conversation_id, messages: [history[0], edited, { role: "user", text: "?" }] });
    expect(bad.status).toBe(400);
    expect(bad.data.code).toBe("CHAT_HISTORY_INVALID");
    const moved = await call<{ code: string }>("ana", "POST", `${D()}/chat`, {
      conversation_id: r1.data.conversation_id,
      messages: [{ role: "user", text: "a" }, r2.data.reply, { role: "user", text: "b" }, r1.data.reply, { role: "user", text: "c" }],
    });
    expect(moved.data.code).toBe("CHAT_HISTORY_INVALID");
    const other = await call<{ code: string }>("ana", "POST", `${D(V2_DASHBOARD_IDS.regression)}/chat`, { conversation_id: r1.data.conversation_id, messages: [...history, { role: "user", text: "?" }] });
    expect([400, 409]).toContain(other.status);
    const unproven = await call<{ code: string }>("ana", "POST", `${D()}/chat`, { conversation_id: r1.data.conversation_id, messages: [history[0], { ...r1.data.reply, sig: undefined }, { role: "user", text: "?" }] });
    expect(unproven.status).toBe(422);
  });

  it("refuses requests that break the turn rules", async () => {
    await chatOn();
    const post = (body: unknown) => call<{ code: string }>("ana", "POST", `${D()}/chat`, body);
    expect((await post({ messages: [] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ messages: [{ role: "assistant", text: "x", sig: "A".repeat(43) }] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ messages: [{ role: "user", text: "a" }, { role: "user", text: "b" }] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ messages: [{ role: "user", text: "a", sig: "A".repeat(43) }] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ messages: [{ role: "user", text: "x".repeat(2001) }] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ messages: [{ role: "user", text: "a", tool: "get_readout" }] })).data.code).toBe("SCHEMA_INVALID");
    expect((await post({ ...ask("a"), dashboard_id: MMM_DASHBOARD_ID })).data.code).toBe("SCHEMA_INVALID");
  });

  it("answers 409 CHAT_DISABLED while chat is off", async () => {
    const r = await call<{ code: string }>("ana", "POST", `${D()}/chat`, ask("hello"));
    expect(r.status).toBe(409);
    expect(r.data.code).toBe("CHAT_DISABLED");
  });

  it("scripts the hostile, unverified, tool-limit and unavailable answers", async () => {
    await chatOn();
    const hostile = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("show me something hostile"));
    expect(hostile.data.reply.text).toBe(MOCK_CHAT_HOSTILE_REPLY);
    for (const s of ["![x](https://evil.example/?d=1)", "<script>", "javascript:", "‮", "​"]) expect(hostile.data.reply.text).toContain(s);
    const unverified = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("Guess next year"));
    expect(unverified.data.notices).toEqual(["unverified_numbers"]);
    const limit = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("Use every tool"));
    expect(limit.data.notices).toEqual(["tool_limit"]);
    expect(limit.data.tool_calls).toHaveLength(6);
    localStorage.setItem(MOCK_CHAT_REPLY_KEY, "unavailable");
    const down = await call<{ code: string }>("ana", "POST", `${D()}/chat`, ask("hello"));
    expect(down.status).toBe(503);
    expect(down.data.code).toBe("CHAT_UNAVAILABLE");
  });

  it("spends the daily budget and then answers 429 with Retry-After until midnight", async () => {
    await chatOn({ tokens_per_day: 200 });
    const first = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("What does the latest readout show?"));
    expect(first.status).toBe(200);
    expect(first.data.notices).toContain("budget_low");
    const second = await call<{ code: string }>("ana", "POST", `${D()}/chat`, ask("And now?"));
    expect(second.status).toBe(429);
    expect(second.data.code).toBe("CHAT_BUDGET_EXHAUSTED");
    const wait = Number(second.headers.get("Retry-After"));
    expect(wait).toBeGreaterThan(0);
    expect(wait).toBeLessThanOrEqual(86_400);
    expect((await call<ChatAvailability>("ana", "GET", `${D()}/chat`)).data).toMatchObject({ enabled_for_me: false, reason: "budget" });
    const view = await call<ChatSettingsView>("ana", "GET", `${D()}/chat/settings`);
    expect(view.data.used_today_tokens).toBe(first.data.usage.total_tokens);
    expect(view.data.est_micros_today).toBeGreaterThan(0);
  });

  it("spends the budget after one answer with the mock flag", async () => {
    await chatOn();
    localStorage.setItem(MOCK_CHAT_BUDGET_KEY, "1");
    expect((await call("ana", "POST", `${D()}/chat`, ask("one"))).status).toBe(200);
    expect((await call<{ code: string }>("ana", "POST", `${D()}/chat`, ask("two"))).data.code).toBe("CHAT_BUDGET_EXHAUSTED");
  });
});

describe("chat settings", () => {
  it("need If-Match with the current etag", async () => {
    const cur = await call<ChatSettingsView>("ana", "GET", `${D()}/chat/settings`);
    expect(cur.data).toMatchObject({ enabled: false, audiences: { members: true, clients: true, share_links: false }, keep_transcripts: false, tokens_per_day: 200_000, share_tokens_per_day: 50_000, etag: '"c1"' });
    expect(cur.headers.get("ETag")).toBe('"c1"');
    const body = { enabled: true, audiences: { members: true, clients: true, share_links: false }, keep_transcripts: false, tokens_per_day: 1000, share_tokens_per_day: 10 };
    expect((await call("ana", "PUT", `${D()}/chat/settings`, body)).status).toBe(412);
    const ok = await call<ChatSettingsView>("ana", "PUT", `${D()}/chat/settings`, body, { "If-Match": '"c1"' });
    expect(ok.data).toMatchObject({ enabled: true, etag: '"c2"', updated_by: PERSONAS.ana!.id, tokens_per_day: 1000 });
    expect((await call<{ code: string }>("ana", "PUT", `${D()}/chat/settings`, body, { "If-Match": '"c1"' })).data.code).toBe("PRECONDITION_FAILED");
    expect((await call<{ code: string }>("ana", "PUT", `${D()}/chat/settings`, { ...body, tokens_per_day: -1 }, { "If-Match": '"c2"' })).data.code).toBe("SCHEMA_INVALID");
  });

  it("are for editors only", async () => {
    expect((await call("vic", "GET", `${D(MMM_DASHBOARD_ID)}/chat/settings`, undefined, {}, "acme/growth")).status).toBe(403);
    expect((await call("rui", "GET", `${D(MMM_DASHBOARD_ID)}/chat/settings`, undefined, {}, "acme/growth")).status).toBe(200);
  });
});

describe("kept conversations", () => {
  it("are kept only when the dashboard keeps them, and editors read and delete them", async () => {
    const seeded = await call<{ items: { conversation_id: string; principal_label: string }[] }>("ana", "GET", `${D()}/chat/transcripts`);
    expect(seeded.data.items).toHaveLength(1);
    expect(seeded.data.items[0]!.principal_label).toMatch(/^Share link /);

    await chatOn();
    const notKept = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("not kept"));
    expect(notKept.data.transcript_kept).toBe(false);
    expect((await call<{ items: unknown[] }>("ana", "GET", `${D()}/chat/transcripts`)).data.items).toHaveLength(1);

    await chatOn({ keep_transcripts: true });
    expect((await call<ChatAvailability>("ana", "GET", `${D()}/chat`)).data.keeps_transcripts).toBe(true);
    const kept = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("kept"));
    expect(kept.data.transcript_kept).toBe(true);
    expect(kept.data.notices).toContain("transcript_kept");
    const list = await call<{ items: { conversation_id: string; principal_kind: string; principal_label: string; turns: number }[] }>("ana", "GET", `${D()}/chat/transcripts`);
    expect(list.data.items[0]).toMatchObject({ conversation_id: kept.data.conversation_id, principal_kind: "member", principal_label: "Ana Analyst", turns: 2 });
    const one = await call<ChatTranscript>("ana", "GET", `${D()}/chat/transcripts/${kept.data.conversation_id}`);
    expect(one.data.turns.map((t) => [t.index, t.role, t.text])).toEqual([
      [0, "user", "kept"],
      [1, "assistant", kept.data.reply.text],
    ]);
    expect(one.data.turns[1]!.tool_calls).toEqual(["get_latest_readout"]);
    expect((await call("ana", "DELETE", `${D()}/chat/transcripts/${kept.data.conversation_id}`)).status).toBe(204);
    expect((await call("ana", "GET", `${D()}/chat/transcripts/${kept.data.conversation_id}`)).status).toBe(404);
  });
});

describe("share-link chat", () => {
  it("needs the share_links audience, never takes a project header and ends with the link", async () => {
    const s = await openLink(MOCK_SHARE_TOKENS.forecastV2);
    expect((await shared<ChatAvailability>(s.session, "GET")).data.reason).toBe("off");
    await chatOn({ audiences: { members: true, clients: true, share_links: false } });
    const closed = await shared<ChatAvailability>(s.session, "GET");
    expect(closed.data.reason).toBe("audience");
    expect(closed.headers.get("Cache-Control")).toBe("no-store");
    expect((await shared<{ code: string }>(s.session, "POST", ask("hi"))).data.code).toBe("CHAT_DISABLED");
    await chatOn();
    const r = await shared<ChatResponse>(s.session, "POST", ask("What does the latest readout show?"));
    expect(r.status).toBe(200);
    expect(r.headers.get("Cache-Control")).toBe("no-store");
    expect(r.data.reply.sig).toBe(mockTurnSig("share", "dss_01jaf3w2fcst2share3x4y5z6", FORECAST, r.data.conversation_id, 1, r.data.reply.text));
    // A member's signature is not valid on the link (another principal).
    const member = await call<ChatResponse>("ana", "POST", `${D()}/chat`, ask("q"));
    const foreign = await shared<{ code: string }>(s.session, "POST", { conversation_id: member.data.conversation_id, messages: [{ role: "user", text: "q" }, member.data.reply, { role: "user", text: "?" }] });
    expect(foreign.data.code).toBe("CHAT_HISTORY_INVALID");
    expect((await shared<{ code: string }>("mps_unknown", "GET")).status).toBe(401);

    const withHeader = await fetch(`${env.apiBase}/v1/shared/chat`, { headers: { "X-Mapi-Share": s.session, "X-Mapi-Project": V2_PROJECT } });
    expect(withHeader.status).toBe(400);

    const shares = await call<{ items: { id: string }[] }>("ana", "GET", `${D()}/shares`);
    const revoke = await call("ana", "POST", `${D()}/shares/${shares.data.items[0]!.id}:revoke`);
    expect(revoke.status).toBeLessThan(300);
    const ended = await shared<{ code: string }>(s.session, "POST", ask("still there?"));
    expect(ended.status).toBe(410);
    expect(ended.data.code).toBe("LINK_EXPIRED");
  });
});

describe("read tokens", () => {
  it("lists your own, every owner's for editors, mints with the secret once and revokes", async () => {
    const mine = await call<ReadToken[]>("ana", "GET", `${D()}/read-tokens`);
    expect(mine.data.map((t) => t.owner_id)).toEqual([PERSONAS.ana!.id]);
    const all = await call<ReadToken[]>("ana", "GET", `${D()}/read-tokens?all=true`);
    expect(all.data.map((t) => t.owner_id).sort()).toEqual([PERSONAS.ana!.id, PERSONAS.cleo!.id].sort());
    expect(all.data.every((t) => !("secret" in t))).toBe(true);

    const made = await call<ReadTokenCreated>("ana", "POST", `${D()}/read-tokens`, { name: "Claude Code", expires_in_days: 7 });
    expect(made.status).toBe(201);
    expect(made.data.secret).toMatch(/^mapi_[0-9a-hjkmnp-tv-z]{8}_[0-9a-hjkmnp-tv-z]{39}$/);
    expect(made.data.secret.startsWith(`mapi_${made.data.prefix}_`)).toBe(true);
    expect(made.data).toMatchObject({ dashboard_id: FORECAST, owner_id: PERSONAS.ana!.id, owner_label: "Ana Analyst", revoked_at: null });
    expect(Date.parse(made.data.expires_at) - Date.parse(made.data.created_at)).toBe(7 * 86_400_000);
    const listed = await call<ReadToken[]>("ana", "GET", `${D()}/read-tokens`);
    expect(listed.data[0]!.id).toBe(made.data.id);
    expect(JSON.stringify(listed.data)).not.toContain(made.data.secret);

    expect((await call<{ code: string }>("ana", "POST", `${D()}/read-tokens`, { name: "x", expires_in_days: 91 })).data.code).toBe("SCHEMA_INVALID");
    expect((await call<{ code: string }>("ana", "POST", `${D()}/read-tokens`, { name: "" })).data.code).toBe("SCHEMA_INVALID");
    for (let i = 0; i < 3; i++) expect((await call("ana", "POST", `${D()}/read-tokens`, { name: `t${i}` })).status).toBe(201);
    const sixth = await call<{ code: string }>("ana", "POST", `${D()}/read-tokens`, { name: "one too many" });
    expect(sixth.status).toBe(409);
    expect(sixth.data.code).toBe("READ_TOKEN_LIMIT");

    expect((await call("ana", "DELETE", `${D()}/read-tokens/${made.data.id}`)).status).toBe(204);
    expect((await call("ana", "DELETE", `${D()}/read-tokens/${made.data.id}`)).status).toBe(204);
    expect((await call<ReadToken[]>("ana", "GET", `${D()}/read-tokens`)).data.find((t) => t.id === made.data.id)?.revoked_at).toBeTruthy();
    expect((await call("ana", "POST", `${D()}/read-tokens`, { name: "room again" })).status).toBe(201);
    expect((await call("ana", "DELETE", `${D()}/read-tokens/tok_01jaf3r1s2t3v4w5x6y7z8zzzz`)).status).toBe(404);
  });
});

/**
 * Client users reach chat and their read tokens through the tenancy guard once
 * `mocks/tenancy.ts` lists these routes among the client routes (`CLIENT_OPERATIONS`). The
 * chat routes are put in front of that guard here, so the chat rules for a client are tested
 * either way; the last test runs only once the guard lets them through.
 */
describe("a client user", () => {
  it("chats on a granted dashboard and manages only their own read tokens", async () => {
    await chatOn();
    server.use(...createChatHandlers(state.chat, { dashboards: state.dashboards, killSwitches: () => state.tenancy.killSwitches }));
    expect((await call<ChatAvailability>("cleo", "GET", `${D()}/chat`)).data.enabled_for_me).toBe(true);
    const r = await call<ChatResponse>("cleo", "POST", `${D()}/chat`, ask("What does the latest readout show?"));
    expect(r.status).toBe(200);
    expect(r.data.reply.sig).toBe(mockTurnSig("client", PERSONAS.cleo!.id, FORECAST, r.data.conversation_id, 1, r.data.reply.text));
    await chatOn({ audiences: { members: true, clients: false, share_links: false } });
    expect((await call<ChatAvailability>("cleo", "GET", `${D()}/chat`)).data.reason).toBe("audience");
    const mine = await call<ReadToken[]>("cleo", "GET", `${D()}/read-tokens`);
    expect(mine.data.map((t) => t.owner_id)).toEqual([PERSONAS.cleo!.id]);
    expect((await call("cleo", "GET", `${D()}/read-tokens?all=true`)).status).toBe(403);
    const anas = (await call<ReadToken[]>("ana", "GET", `${D()}/read-tokens`)).data[0]!;
    expect((await call("cleo", "DELETE", `${D()}/read-tokens/${anas.id}`)).status).toBe(404);
    expect((await call("cleo", "POST", `${D()}/read-tokens`, { name: "Cleo's agent" })).status).toBe(201);
    expect((await call("cleo", "GET", `${D()}/chat/settings`)).status).toBe(404);
    expect((await call("cleo", "GET", `${D()}/chat/transcripts`)).status).toBe(404);
    // A dashboard the client cannot see: 404, never 403.
    expect((await call("cleo", "GET", `${D("dsh_01jaf3w2none2none3x4y5z6a")}/chat`)).status).toBe(404);
  });

  it.skipIf(!isClientRoute("POST", `${D()}/chat`) || !isClientRoute("POST", `${D()}/read-tokens`))("passes the tenancy guard on the client routes", async () => {
    await chatOn();
    expect((await call("cleo", "POST", `${D()}/chat`, ask("hello"))).status).toBe(200);
    expect((await call("cleo", "GET", `${D()}/read-tokens`)).status).toBe(200);
  });
});
