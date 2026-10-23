/**
 * One chat conversation as the panel holds it (v0.20, plan D5): a pure reducer, no React and
 * no network, so the share app's lazy chat chunk and the tests use it alike.
 *
 * The client holds the conversation as text. Each request sends every earlier turn plus the
 * new question ({@link requestFor}): user turns as typed, assistant turns exactly as the server
 * returned them, `sig` unchanged. The server signs each answer for its **position** in the
 * conversation, so turns are never dropped or re-ordered: once the next request would exceed
 * `CHAT_MAX_TURNS` or `CHAT_MAX_HISTORY_CHARS` the conversation is full and the panel offers a
 * new one ({@link sendBlock} answers `"full"`).
 *
 * A question in flight is `pending`; when its request fails it becomes `failed` (the words are
 * kept, so "Try again" sends the same question). {@link chatErrorOf} reads any error a
 * {@link ChatTransport} throws (`ApiError` signed in, `ShareApiError` on a share link) without
 * importing either.
 */
import {
  CHAT_MAX_HISTORY_CHARS,
  CHAT_MAX_TURNS,
  CHAT_MAX_USER_CHARS,
  type ChatNoticeKind,
  type ChatRequest,
  type ChatResponse,
  type ChatTurn,
  type ToolCallSummary,
} from "./types";

/** A question as it was sent. */
export interface UserEntry {
  role: "user";
  text: string;
}

/** An answer as the server returned it (`sig` verbatim) and what the panel shows under it. */
export interface AssistantEntry {
  role: "assistant";
  text: string;
  sig: string;
  toolCalls: ToolCallSummary[];
  notices: ChatNoticeKind[];
}

export type ChatEntry = UserEntry | AssistantEntry;

/** What went wrong with a request, in the panel's terms. */
export type ChatErrorKind =
  /** `CHAT_BUDGET_EXHAUSTED`: the daily budget is spent (`retryAfter`: until it resets). */
  | "budget"
  /** `RATE_LIMITED`: too many questions in a minute. */
  | "rate"
  /** `CHAT_DISABLED`: chat was turned off (or closed to this viewer) since the panel opened. */
  | "disabled"
  /** `CHAT_HISTORY_INVALID`: the server refused the conversation; start a new one. */
  | "history"
  /** `CHAT_UNAVAILABLE` or a 5xx: the assistant did not answer; try again. */
  | "unavailable"
  /** `LINK_EXPIRED` (410): the share link ended. */
  | "link_ended"
  /** 401: the sign-in or the share session ended. */
  | "signed_out"
  /** 404: the dashboard is gone or no longer shared with this viewer. */
  | "gone"
  /** `DASHBOARD_NOT_PUBLISHED`: the dashboard was unpublished. */
  | "unpublished"
  /** `UPLOADED_CODE_NOT_SHAREABLE`: the dashboard uses uploaded code, which this viewer cannot reach. */
  | "not_shareable"
  /** No answer from the network. */
  | "network"
  | "other";

export interface ChatError {
  kind: ChatErrorKind;
  status: number | null;
  code: string | null;
  /** Seconds to wait before asking again (`Retry-After`), when the server said. */
  retryAfter: number | null;
}

export interface ChatState {
  /** `null` until the first answer (the server makes the id). */
  conversationId: string | null;
  /** Answered turns: user and assistant alternate, starting with a user turn. */
  turns: ChatEntry[];
  /** The question in flight. */
  pending: UserEntry | null;
  /** The question whose request failed, and why (kept so it can be sent again). */
  failed: { text: string; error: ChatError } | null;
  /** Whether the server keeps this conversation (from the last answer). */
  kept: boolean;
}

export type ChatAction =
  | { type: "send"; text: string }
  | { type: "replied"; response: ChatResponse }
  | { type: "failed"; error: unknown }
  | { type: "retry" }
  | { type: "dismiss" }
  | { type: "reset" };

export const initialChatState: ChatState = { conversationId: null, turns: [], pending: null, failed: null, kept: false };

/** Why `text` cannot be sent now, or `null` when it can. */
export type SendBlock = "pending" | "empty" | "too_long" | "full";

function chars(turns: readonly { text: string }[]): number {
  return turns.reduce((n, t) => n + t.text.length, 0);
}

/** Why {@link chatReducer} would refuse to send `text` now (`null`: it would send it). */
export function sendBlock(state: ChatState, text: string): SendBlock | null {
  if (state.pending) return "pending";
  const q = text.trim();
  if (!q) return "empty";
  if (q.length > CHAT_MAX_USER_CHARS) return "too_long";
  if (state.turns.length + 1 > CHAT_MAX_TURNS || chars(state.turns) + q.length > CHAT_MAX_HISTORY_CHARS) return "full";
  return null;
}

/** Whether the conversation has room for no further question (offer a new conversation). */
export function isFull(state: ChatState): boolean {
  return sendBlock({ ...state, pending: null }, "?") === "full";
}

/** The request that asks `question` after the turns so far (assistant turns with their `sig`, unchanged). */
export function requestFor(state: ChatState, question: string, readoutSeq: number | null = null): ChatRequest {
  const messages: ChatTurn[] = state.turns.map((t) => (t.role === "user" ? { role: "user", text: t.text } : { role: "assistant", text: t.text, sig: t.sig }));
  messages.push({ role: "user", text: question });
  return {
    ...(state.conversationId ? { conversation_id: state.conversationId } : {}),
    messages,
    ...(readoutSeq !== null && readoutSeq >= 1 ? { readout_seq: readoutSeq } : {}),
  };
}

/** The request of the question in flight (`null` when none is). */
export function pendingRequest(state: ChatState, readoutSeq: number | null = null): ChatRequest | null {
  return state.pending ? requestFor(state, state.pending.text, readoutSeq) : null;
}

/** Read the status, code and `Retry-After` of anything a {@link ChatTransport} throws. */
export function chatErrorOf(err: unknown): ChatError {
  if (err instanceof TypeError) return { kind: "network", status: null, code: null, retryAfter: null };
  const e = (typeof err === "object" && err !== null ? err : {}) as { status?: unknown; problem?: { code?: unknown } | null; retryAfter?: unknown };
  const status = typeof e.status === "number" ? e.status : null;
  const code = typeof e.problem?.code === "string" ? e.problem.code : null;
  const retryAfter = typeof e.retryAfter === "number" ? e.retryAfter : null;
  const kind: ChatErrorKind =
    code === "CHAT_BUDGET_EXHAUSTED"
      ? "budget"
      : code === "RATE_LIMITED"
        ? "rate"
        : code === "CHAT_DISABLED"
          ? "disabled"
          : code === "CHAT_HISTORY_INVALID"
            ? "history"
            : code === "LINK_EXPIRED" || status === 410
              ? "link_ended"
              : code === "DASHBOARD_NOT_PUBLISHED"
                ? "unpublished"
                : code === "UPLOADED_CODE_NOT_SHAREABLE"
                  ? "not_shareable"
                  : code === "CHAT_UNAVAILABLE" || (status !== null && status >= 500)
                    ? "unavailable"
                    : status === 401
                      ? "signed_out"
                      : status === 404
                        ? "gone"
                        : status === 429
                          ? "rate"
                          : "other";
  return { kind, status, code, retryAfter };
}

/** Whether asking the same question again may work (the panel shows "Try again"). */
export function canRetry(error: ChatError): boolean {
  return error.kind === "unavailable" || error.kind === "network" || error.kind === "rate" || error.kind === "other";
}

function assistantOf(r: ChatResponse): AssistantEntry {
  return { role: "assistant", text: r.reply.text, sig: r.reply.sig ?? "", toolCalls: r.tool_calls ?? [], notices: r.notices ?? [] };
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "send": {
      if (sendBlock(state, action.text) !== null) return state;
      return { ...state, pending: { role: "user", text: action.text.trim() }, failed: null };
    }
    case "replied": {
      if (!state.pending) return state;
      const r = action.response;
      // A reply that is not a signed assistant turn cannot be sent back: treat it as a failure.
      if (r.reply.role !== "assistant" || !r.reply.sig) {
        return { ...state, pending: null, failed: { text: state.pending.text, error: { kind: "other", status: null, code: null, retryAfter: null } } };
      }
      // A different conversation id restarts the server's numbering: never mix the two.
      const turns = state.conversationId === null || state.conversationId === r.conversation_id ? state.turns : [];
      return {
        conversationId: r.conversation_id,
        turns: [...turns, state.pending, assistantOf(r)],
        pending: null,
        failed: null,
        kept: Boolean(r.transcript_kept),
      };
    }
    case "failed": {
      if (!state.pending) return state;
      return { ...state, pending: null, failed: { text: state.pending.text, error: chatErrorOf(action.error) } };
    }
    case "retry": {
      if (!state.failed || state.pending) return state;
      return { ...state, pending: { role: "user", text: state.failed.text }, failed: null };
    }
    case "dismiss":
      return state.failed ? { ...state, failed: null } : state;
    case "reset":
      return initialChatState;
  }
}
