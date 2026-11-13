/**
 * Query keys of dashboard chat and read tokens (v0.20).
 *
 * The analyst's keys (settings, kept conversations, read tokens) sit under the dashboard's own
 * key (`dk.one(p, id)`), so a project switch never mixes caches and invalidating a dashboard
 * also drops them. Availability is keyed by the readout page's {@link DashboardClient.cacheKey}
 * (`["p", …, id, "member" | "client"]` or `["share", id]`): a member preview, a client user and
 * a share link never share an answer. Like `../keys.ts`, this module imports nothing at runtime
 * except that module's key builders (the share app may use it).
 */
import { dk } from "../keys";

export const ck = {
  /** Whether the viewer of a {@link DashboardClient} can chat now (`GET …/chat`, `GET /v1/shared/chat`). */
  availability: (cacheKey: readonly string[]) => [...cacheKey, "chat", "availability"] as const,
  /** Every chat query of one dashboard (settings and kept conversations). */
  all: (p: string, id: string) => [...dk.one(p, id), "chat"] as const,
  settings: (p: string, id: string) => [...dk.one(p, id), "chat", "settings"] as const,
  transcripts: (p: string, id: string) => [...dk.one(p, id), "chat", "transcripts"] as const,
  transcript: (p: string, id: string, conversationId: string) => [...dk.one(p, id), "chat", "transcripts", conversationId] as const,
  /** Every read-token list of one dashboard (yours and, for editors, everyone's). */
  readTokens: (p: string, id: string) => [...dk.one(p, id), "read-tokens"] as const,
  readTokenList: (p: string, id: string, all: boolean) => [...dk.one(p, id), "read-tokens", { all }] as const,
};
