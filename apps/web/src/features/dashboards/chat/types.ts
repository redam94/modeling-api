/**
 * Types and limits of "Ask about your dashboard" (v0.20, `mapi_spec.chat`).
 *
 * The DTOs are read straight from the generated schema (`api/schema.d.ts`); the limits mirror
 * the constants of `mapi_spec.chat` (the server checks them again: these only let the UI say
 * so before it sends). {@link ChatTransport} is the one interface the chat panel talks to, so
 * the panel never knows whether a member, a client user or a share link is asking (the
 * signed-in transport is `transport-member.ts`, the share-link one `transport-share.ts`).
 *
 * This module has no runtime imports beyond constants, so the share app, the readout page and
 * the lazy chat chunk can use it freely.
 */
import type { components } from "@/api/schema";

type S = components["schemas"];

export type ChatAudiences = S["ChatAudiences"];
export type ChatAvailability = S["ChatAvailability"];
export type ChatRequest = S["ChatRequest"];
export type ChatResponse = S["ChatResponse"];
export type ChatSettingsUpdate = S["ChatSettingsUpdate"];
export type ChatSettingsView = S["ChatSettingsView"];
export type ChatTranscript = S["ChatTranscript"];
export type ChatTranscriptPage = S["Page_ChatTranscriptSummary_"];
export type ChatTranscriptSummary = S["ChatTranscriptSummary"];
export type ChatTranscriptTurn = S["ChatTranscriptTurn"];
export type ChatTurn = S["ChatTurn"];
export type ChatUsage = S["ChatUsage"];
export type ToolCallSummary = S["ToolCallSummary"];
export type ReadToken = S["ReadToken"];
export type ReadTokenCreate = S["ReadTokenCreate"];
export type ReadTokenCreated = S["ReadTokenCreated"];

export type ChatRole = ChatTurn["role"];
/** `mapi_spec.chat.ChatNoticeKind`: the notices shown under an answer. */
export type ChatNoticeKind = NonNullable<ChatResponse["notices"]>[number];
/** `mapi_spec.chat.ChatUnavailableReason`: why chat is not available now. */
export type ChatUnavailableReason = NonNullable<ChatAvailability["reason"]>;

/** `CHAT_MAX_TURNS`: the turns of one request, the new question included. */
export const CHAT_MAX_TURNS = 20;
/** `CHAT_MAX_USER_CHARS`: the characters of one question. */
export const CHAT_MAX_USER_CHARS = 2000;
/** `CHAT_MAX_HISTORY_CHARS`: the characters of every turn of one request together. */
export const CHAT_MAX_HISTORY_CHARS = 24_000;
/** `CHAT_MAX_REPLY_CHARS`: the characters of one answer. */
export const CHAT_MAX_REPLY_CHARS = 4000;
/** `CHAT_MAX_TOOL_CALLS`: the tool calls of one answer. */
export const CHAT_MAX_TOOL_CALLS = 6;
/** `CHAT_TRANSCRIPT_DAYS`: how long a dashboard that keeps chats keeps them. */
export const CHAT_TRANSCRIPT_DAYS = 30;
/** `CHAT_DEFAULT_TOKENS_PER_DAY` / `CHAT_MAX_TOKENS_PER_DAY`: the dashboard's daily budget. */
export const CHAT_DEFAULT_TOKENS_PER_DAY = 200_000;
export const CHAT_MAX_TOKENS_PER_DAY = 5_000_000;
/** `CHAT_DEFAULT_SHARE_TOKENS_PER_DAY` / `CHAT_MAX_SHARE_TOKENS_PER_DAY`: each share link's daily budget. */
export const CHAT_DEFAULT_SHARE_TOKENS_PER_DAY = 50_000;
export const CHAT_MAX_SHARE_TOKENS_PER_DAY = 1_000_000;
/** `READ_TOKEN_DEFAULT_DAYS` / `READ_TOKEN_MAX_DAYS`: the lifetime of a dashboard read token. */
export const READ_TOKEN_DEFAULT_DAYS = 30;
export const READ_TOKEN_MAX_DAYS = 90;
/** `dashboard_read_tokens_per_user` (server default): active read tokens per person and dashboard. */
export const READ_TOKENS_PER_USER = 5;
/** `ReadTokenCreate.name`: 1-100 characters, no control characters. */
export const READ_TOKEN_NAME_MAX = 100;

/** `CONVERSATION_ID_RE`: a conversation id (`cnv_` and 26 base32 characters). */
export const CONVERSATION_ID_RE = /^cnv_[0-9a-z]{26}$/;
/** `CHAT_SIG_RE`: the signature of an assistant turn (base64url, 43 characters). */
export const CHAT_SIG_RE = /^[A-Za-z0-9_-]{43}$/;

/** `mapi_spec.chat.CHAT_SUGGESTIONS`: the fixed questions offered before the first message. */
export const CHAT_SUGGESTIONS: readonly string[] = [
  "What does the latest readout show?",
  "What changed since the last readout?",
  "How accurate is the model?",
  "Explain the main numbers in plain words.",
];

/**
 * How the chat panel asks: one transport per surface (plan I-9). `availability` answers
 * whether this viewer can chat now; `send` asks one question (the whole conversation so far,
 * see `conversation.ts`) and answers one reply. Both throw the surface's HTTP error (an
 * `ApiError` signed in, a `ShareApiError` on a share link); `chatErrorOf` reads either.
 */
export interface ChatTransport {
  availability(signal?: AbortSignal): Promise<ChatAvailability>;
  send(req: ChatRequest, signal?: AbortSignal): Promise<ChatResponse>;
}
