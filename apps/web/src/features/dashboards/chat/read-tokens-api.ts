/**
 * TanStack Query hooks of dashboard read tokens (v0.20, signed-in only): the tokens a member or
 * a client user makes to connect a chat tool over MCP (`…/read-tokens`). A read token reads
 * one dashboard and works only on the MCP endpoint.
 *
 * The secret is in the answer of {@link useCreateReadToken} only, once: it never goes into the
 * query cache (the list is read again instead) or into browser storage. Show it, offer to copy
 * it, and forget it when the dialog closes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, unwrap, unwrapVoid } from "@/api/client";
import { useProjectScope } from "@/api/queries";

import { ck } from "./keys";
import { READ_TOKEN_DEFAULT_DAYS, READ_TOKEN_MAX_DAYS, READ_TOKEN_NAME_MAX, type ReadToken, type ReadTokenCreate, type ReadTokenCreated } from "./types";

/** `true` for the 409 of a new token when you already have the most active tokens allowed. */
export function isReadTokenLimit(err: unknown): boolean {
  return err instanceof ApiError && err.code === "READ_TOKEN_LIMIT";
}

/** Whether a listed token can still be used now (not revoked, not expired). */
export function isActiveToken(t: ReadToken, now: number = Date.now()): boolean {
  return !t.revoked_at && Date.parse(t.expires_at) > now;
}

/** A problem with the fields of a new token (plain words), or `null` when the server will take them. */
export function readTokenInputProblem(body: ReadTokenCreate): string | null {
  const name = body.name.trim();
  if (!name) return "Give the token a name.";
  if (name.length > READ_TOKEN_NAME_MAX) return `Use at most ${READ_TOKEN_NAME_MAX} characters for the name.`;
  if (/[\u0000-\u001f\u007f]/.test(name)) return "Use letters, numbers and punctuation only in the name.";
  const days = body.expires_in_days ?? READ_TOKEN_DEFAULT_DAYS;
  if (!Number.isInteger(days) || days < 1 || days > READ_TOKEN_MAX_DAYS) return `Choose from 1 to ${READ_TOKEN_MAX_DAYS} days.`;
  return null;
}

/**
 * Read tokens of dashboard `id`: yours, newest first; `all` (editors): every owner's. Never a
 * secret.
 */
export function useReadTokens(id: string, opts: { all?: boolean; enabled?: boolean } = {}) {
  const s = useProjectScope();
  const all = opts.all ?? false;
  return useQuery({
    queryKey: ck.readTokenList(s.key, id, all),
    queryFn: ({ signal }): Promise<ReadToken[]> =>
      unwrap(api.GET("/v1/dashboards/{dashboard_id}/read-tokens", { params: { path: { dashboard_id: id }, query: all ? { all: true } : {} }, headers: s.headers, signal })),
    enabled: opts.enabled ?? true,
  });
}

/** Make a read token for dashboard `id`. The answer holds the secret: show it once, never store it. */
export function useCreateReadToken(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ReadTokenCreate): Promise<ReadTokenCreated> =>
      unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/read-tokens", {
          params: { path: { dashboard_id: id } },
          headers: s.headers,
          body: { name: body.name.trim(), expires_in_days: body.expires_in_days ?? READ_TOKEN_DEFAULT_DAYS },
        }),
      ),
    // The list is read again (never seeded from this answer, which holds the secret).
    onSuccess: () => qc.invalidateQueries({ queryKey: ck.readTokens(s.key, id) }),
  });
}

/** Revoke a read token (yours, or any of the dashboard's as an editor). Idempotent. */
export function useRevokeReadToken(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tokenId: string) =>
      unwrapVoid(api.DELETE("/v1/dashboards/{dashboard_id}/read-tokens/{token_id}", { params: { path: { dashboard_id: id, token_id: tokenId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ck.readTokens(s.key, id) }),
  });
}
