import { describe, expect, it } from "vitest";

import {
  canRetry,
  chatErrorOf,
  chatReducer,
  initialChatState,
  isFull,
  pendingRequest,
  requestFor,
  sendBlock,
  type ChatState,
} from "./conversation";
import { CHAT_MAX_HISTORY_CHARS, CHAT_MAX_TURNS, CHAT_MAX_USER_CHARS, type ChatResponse } from "./types";

const CNV = "cnv_01jaf3t1v2w3x4y5z6a7b8c9d0";
const sigOf = (n: number) => `${"A".repeat(40)}${String(n).padStart(3, "0")}`;

function reply(text: string, n: number, patch: Partial<ChatResponse> = {}): ChatResponse {
  return {
    conversation_id: CNV,
    reply: { role: "assistant", text, sig: sigOf(n) },
    tool_calls: [{ name: "get_latest_readout", args: {}, ok: true, ms: 12 }],
    usage: { input_tokens: 10, output_tokens: 5, thought_tokens: 0, total_tokens: 15, model: "fake-chat" },
    notices: [],
    transcript_kept: false,
    ...patch,
  };
}

/** Ask `n` questions, each answered. */
function talk(n: number, state: ChatState = initialChatState): ChatState {
  let s = state;
  for (let i = 0; i < n; i++) {
    s = chatReducer(s, { type: "send", text: `question ${i}` });
    s = chatReducer(s, { type: "replied", response: reply(`answer ${i}`, i) });
  }
  return s;
}

/** An error as `ApiError` / `ShareApiError` carry it. */
function httpError(status: number, code: string | null, retryAfter: number | null = null): Error {
  return Object.assign(new Error(code ?? `HTTP ${status}`), { status, problem: code ? { code, title: code, status } : null, retryAfter });
}

describe("chatReducer", () => {
  it("sends the first question without a conversation id and keeps the server's", () => {
    const s1 = chatReducer(initialChatState, { type: "send", text: "  What changed?  " });
    expect(s1.pending).toEqual({ role: "user", text: "What changed?" });
    expect(pendingRequest(s1)).toEqual({ messages: [{ role: "user", text: "What changed?" }] });
    const s2 = chatReducer(s1, { type: "replied", response: reply("Revenue went up by 4%.", 1, { transcript_kept: true }) });
    expect(s2.pending).toBeNull();
    expect(s2.conversationId).toBe(CNV);
    expect(s2.kept).toBe(true);
    expect(s2.turns.map((t) => t.role)).toEqual(["user", "assistant"]);
  });

  it("sends every earlier turn, alternating, with each answer's sig unchanged", () => {
    const s = talk(3);
    const req = requestFor(s, "And next week?", 12);
    expect(req.conversation_id).toBe(CNV);
    expect(req.readout_seq).toBe(12);
    expect(req.messages.map((m) => m.role)).toEqual(["user", "assistant", "user", "assistant", "user", "assistant", "user"]);
    expect(req.messages.filter((m) => m.role === "assistant").map((m) => m.sig)).toEqual([sigOf(0), sigOf(1), sigOf(2)]);
    expect(req.messages.filter((m) => m.role === "user").every((m) => !("sig" in m))).toBe(true);
    expect(req.messages.at(-1)).toEqual({ role: "user", text: "And next week?" });
    // The tool chips and notices stay on the panel's side: the request carries text only.
    expect(req.messages.every((m) => Object.keys(m).every((k) => ["role", "text", "sig"].includes(k)))).toBe(true);
  });

  it("never drops or moves a turn: it is full once the next request would pass the limits", () => {
    const max = Math.floor((CHAT_MAX_TURNS - 1) / 2); // answered pairs that still leave room for a question
    const s = talk(max);
    expect(s.turns).toHaveLength(max * 2);
    expect(requestFor(s, "one more").messages.length).toBeLessThanOrEqual(CHAT_MAX_TURNS);
    expect(isFull(s)).toBe(false);
    const full = talk(1, s);
    expect(full.turns).toHaveLength(max * 2 + 2);
    expect(isFull(full)).toBe(true);
    expect(sendBlock(full, "too many")).toBe("full");
    expect(chatReducer(full, { type: "send", text: "too many" })).toBe(full);
    // The first turns are still there, in order.
    expect(full.turns[0]).toEqual({ role: "user", text: "question 0" });

    const long = "x".repeat(CHAT_MAX_USER_CHARS);
    let big = initialChatState;
    const pair = (s: ChatState, q: string, a: string, n: number) => chatReducer(chatReducer(s, { type: "send", text: q }), { type: "replied", response: reply(a, n) });
    for (let i = 0; i < 5; i++) big = pair(big, long, long, i);
    big = pair(big, "y".repeat(1000), "z".repeat(1500), 5);
    expect(CHAT_MAX_HISTORY_CHARS - (5 * 2 * CHAT_MAX_USER_CHARS + 2500)).toBeLessThan(CHAT_MAX_USER_CHARS);
    expect(sendBlock(big, long)).toBe("full");
    expect(sendBlock(big, "short")).toBeNull();
  });

  it("refuses an empty, a too long or a second question while one is in flight", () => {
    expect(sendBlock(initialChatState, "   ")).toBe("empty");
    expect(sendBlock(initialChatState, "x".repeat(CHAT_MAX_USER_CHARS + 1))).toBe("too_long");
    const s = chatReducer(initialChatState, { type: "send", text: "first" });
    expect(sendBlock(s, "second")).toBe("pending");
    expect(chatReducer(s, { type: "send", text: "second" })).toBe(s);
  });

  it("keeps the question of a failed request and sends it again on retry", () => {
    const before = talk(1);
    const s1 = chatReducer(before, { type: "send", text: "Why?" });
    const s2 = chatReducer(s1, { type: "failed", error: httpError(503, "CHAT_UNAVAILABLE", 5) });
    expect(s2.pending).toBeNull();
    expect(s2.failed).toEqual({ text: "Why?", error: { kind: "unavailable", status: 503, code: "CHAT_UNAVAILABLE", retryAfter: 5 } });
    expect(s2.turns).toEqual(before.turns);
    expect(canRetry(s2.failed!.error)).toBe(true);
    const s3 = chatReducer(s2, { type: "retry" });
    expect(s3.failed).toBeNull();
    expect(s3.pending).toEqual({ role: "user", text: "Why?" });
    expect(pendingRequest(s3)?.messages.at(-1)).toEqual({ role: "user", text: "Why?" });
    const s4 = chatReducer(s3, { type: "replied", response: reply("Because.", 9) });
    expect(s4.turns.slice(-2).map((t) => t.text)).toEqual(["Why?", "Because."]);
    // A new question replaces a failed one; dismiss forgets it.
    const s5 = chatReducer(s2, { type: "dismiss" });
    expect(s5.failed).toBeNull();
    expect(chatReducer(s2, { type: "send", text: "Other" }).failed).toBeNull();
  });

  it("keeps the tool chips and notices with each answer", () => {
    const s = chatReducer(chatReducer(initialChatState, { type: "send", text: "q" }), {
      type: "replied",
      response: reply("a", 1, { notices: ["unverified_numbers"], tool_calls: [{ name: "get_channel", args: { channel: "tv" }, ok: true, ms: 3 }] }),
    });
    const a = s.turns[1]!;
    expect(a.role === "assistant" && a.notices).toEqual(["unverified_numbers"]);
    expect(a.role === "assistant" && a.toolCalls.map((t) => t.name)).toEqual(["get_channel"]);
  });

  it("treats an unsigned reply as a failure and never mixes two conversations", () => {
    const s1 = chatReducer(initialChatState, { type: "send", text: "q" });
    const bad = chatReducer(s1, { type: "replied", response: { ...reply("a", 1), reply: { role: "assistant", text: "a", sig: null } } });
    expect(bad.failed?.text).toBe("q");
    expect(bad.turns).toEqual([]);
    const s2 = chatReducer(talk(2), { type: "send", text: "q" });
    const moved = chatReducer(s2, { type: "replied", response: { ...reply("a", 1), conversation_id: "cnv_01jaf3t1v2w3x4y5z6a7b8c9zz" } });
    expect(moved.turns).toHaveLength(2);
    expect(moved.conversationId).toBe("cnv_01jaf3t1v2w3x4y5z6a7b8c9zz");
    expect(chatReducer(moved, { type: "reset" })).toEqual(initialChatState);
  });

  it("ignores replies and failures with nothing in flight", () => {
    expect(chatReducer(initialChatState, { type: "replied", response: reply("a", 1) })).toBe(initialChatState);
    expect(chatReducer(initialChatState, { type: "failed", error: new Error("x") })).toBe(initialChatState);
    expect(chatReducer(initialChatState, { type: "retry" })).toBe(initialChatState);
  });
});

describe("chatErrorOf", () => {
  it("reads the problems a chat request can answer", () => {
    expect(chatErrorOf(httpError(429, "CHAT_BUDGET_EXHAUSTED", 3600))).toEqual({ kind: "budget", status: 429, code: "CHAT_BUDGET_EXHAUSTED", retryAfter: 3600 });
    expect(chatErrorOf(httpError(429, "RATE_LIMITED", 20)).kind).toBe("rate");
    expect(chatErrorOf(httpError(409, "CHAT_DISABLED")).kind).toBe("disabled");
    expect(chatErrorOf(httpError(400, "CHAT_HISTORY_INVALID")).kind).toBe("history");
    expect(chatErrorOf(httpError(410, "LINK_EXPIRED")).kind).toBe("link_ended");
    expect(chatErrorOf(httpError(410, null)).kind).toBe("link_ended");
    expect(chatErrorOf(httpError(409, "DASHBOARD_NOT_PUBLISHED")).kind).toBe("unpublished");
    expect(chatErrorOf(httpError(403, "UPLOADED_CODE_NOT_SHAREABLE")).kind).toBe("not_shareable");
    expect(chatErrorOf(httpError(502, null)).kind).toBe("unavailable");
    expect(chatErrorOf(httpError(401, "UNAUTHENTICATED")).kind).toBe("signed_out");
    expect(chatErrorOf(httpError(404, "NOT_FOUND")).kind).toBe("gone");
    expect(chatErrorOf(new TypeError("Failed to fetch")).kind).toBe("network");
    expect(chatErrorOf("boom")).toEqual({ kind: "other", status: null, code: null, retryAfter: null });
    expect(canRetry(chatErrorOf(httpError(429, "CHAT_BUDGET_EXHAUSTED")))).toBe(false);
    expect(canRetry(chatErrorOf(httpError(400, "CHAT_HISTORY_INVALID")))).toBe(false);
  });
});
