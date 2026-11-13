/**
 * TanStack Query hooks of the analyst's chat routes (v0.20, signed-in only): the chat settings
 * of a dashboard (`GET` / `PUT …/chat/settings`, editors; the `etag` goes back in `If-Match`)
 * and its kept conversations (`…/chat/transcripts`, editors).
 *
 * Every request names the selected project (`X-Mapi-Project`); keys sit under the dashboard's
 * own key (`keys.ts`). The share app never loads this module (it imports `api/client.ts`).
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { api, ApiError, unwrap, unwrapVoid } from "@/api/client";
import { useProjectScope } from "@/api/queries";

import { dk } from "../keys";

import { ck } from "./keys";
import { CHAT_DEFAULT_SHARE_TOKENS_PER_DAY, CHAT_DEFAULT_TOKENS_PER_DAY, type ChatSettingsUpdate, type ChatSettingsView, type ChatTranscript } from "./types";

const TRANSCRIPT_PAGE = 50;

/** The editable part of a settings view, with the server's defaults for absent fields. */
export function settingsUpdateOf(v: ChatSettingsView): ChatSettingsUpdate {
  return {
    enabled: v.enabled ?? false,
    audiences: { members: v.audiences?.members ?? true, clients: v.audiences?.clients ?? true, share_links: v.audiences?.share_links ?? false },
    keep_transcripts: v.keep_transcripts ?? false,
    tokens_per_day: v.tokens_per_day ?? CHAT_DEFAULT_TOKENS_PER_DAY,
    share_tokens_per_day: v.share_tokens_per_day ?? CHAT_DEFAULT_SHARE_TOKENS_PER_DAY,
  };
}

/** `true` for the 412 of a settings save: someone changed the settings since you read them. */
export function isSettingsConflict(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 412 || err.code === "PRECONDITION_FAILED");
}

/** Drop every cached chat availability of dashboard `id` (any viewer kind) so the launcher asks again. */
export function invalidateChatAvailability(qc: QueryClient, scopeKey: string, id: string): Promise<void> {
  const prefix = dk.one(scopeKey, id);
  return qc.invalidateQueries({
    predicate: (q) => {
      const k = q.queryKey;
      return prefix.every((part, i) => k[i] === part) && k.at(-2) === "chat" && k.at(-1) === "availability";
    },
  });
}

/** The chat settings of dashboard `id` and the tokens used today (editors; 403 for others). */
export function useChatSettings(id: string, opts: { enabled?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ck.settings(s.key, id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/dashboards/{dashboard_id}/chat/settings", { params: { path: { dashboard_id: id } }, headers: s.headers, signal })),
    enabled: opts.enabled ?? true,
    retry: (n, err) => !(err instanceof ApiError && (err.status === 403 || err.status === 404)) && n < 2,
  });
}

/**
 * Save the chat settings. `etag` is the `etag` of the settings you read: a 412
 * ({@link isSettingsConflict}) means they changed since; read them again and decide.
 */
export function useSaveChatSettings(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ body, etag }: { body: ChatSettingsUpdate; etag: string }) =>
      unwrap(api.PUT("/v1/dashboards/{dashboard_id}/chat/settings", { params: { path: { dashboard_id: id } }, headers: { ...s.headers, "If-Match": etag }, body })),
    onSuccess: async (view) => {
      qc.setQueryData(ck.settings(s.key, id), view);
      await invalidateChatAvailability(qc, s.key, id);
    },
    onError: (err) => {
      if (isSettingsConflict(err)) void qc.invalidateQueries({ queryKey: ck.settings(s.key, id), exact: true });
    },
  });
}

/** The kept conversations of dashboard `id`, newest first, a page at a time (editors). */
export function useChatTranscripts(id: string, opts: { enabled?: boolean } = {}) {
  const s = useProjectScope();
  return useInfiniteQuery({
    queryKey: ck.transcripts(s.key, id),
    queryFn: ({ signal, pageParam }) =>
      unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/chat/transcripts", {
          params: { path: { dashboard_id: id }, query: { limit: TRANSCRIPT_PAGE, ...(pageParam ? { cursor: pageParam } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? null,
    enabled: opts.enabled ?? true,
  });
}

/** One kept conversation, oldest turn first (`conversationId` `null`: none open). */
export function useChatTranscript(id: string, conversationId: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ck.transcript(s.key, id, conversationId ?? ""),
    queryFn: ({ signal }): Promise<ChatTranscript> =>
      unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/chat/transcripts/{conversation_id}", {
          params: { path: { dashboard_id: id, conversation_id: conversationId ?? "" } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: conversationId !== null,
  });
}

/** Delete a kept conversation (every turn of it; audited). */
export function useDeleteChatTranscript(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (conversationId: string) =>
      unwrapVoid(
        api.DELETE("/v1/dashboards/{dashboard_id}/chat/transcripts/{conversation_id}", {
          params: { path: { dashboard_id: id, conversation_id: conversationId } },
          headers: s.headers,
        }),
      ),
    onSuccess: (_v, conversationId) => {
      qc.removeQueries({ queryKey: ck.transcript(s.key, id, conversationId), exact: true });
      return qc.invalidateQueries({ queryKey: ck.transcripts(s.key, id), exact: true });
    },
  });
}
