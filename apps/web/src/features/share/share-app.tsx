/**
 * The share link app (plan D13/D15, §3.6.4): `/s#mpd_…` for anyone who holds the link, without
 * signing in. The entry loads this chunk before (and instead of) the workspace gate.
 *
 * Flow: read the token from the fragment → strip it from the address bar → trade it for a
 * session (`POST /v1/share-sessions`; every link goes through this exchange) → ask for the
 * passcode when the link needs one → show the readout page with the share client. The session is
 * refreshed at 80 % of its lifetime and lives in memory only (component state and a ref; never a
 * store, `localStorage` or a cookie). A link that was revoked or expired (410) shows "This link
 * has ended or was removed".
 *
 * Import discipline (checked by the chunk budget in `vite.config.ts`): only `components/ui`,
 * `components/ods`, the readout page and the dashboards data client, never `api/client.ts`,
 * `api/queries.ts`, the shell, the router or the expert app's heavy libraries.
 */
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { TooltipProvider } from "@/components/ui/overlays";
import { shareChatTransport } from "@/features/dashboards/chat/transport-share";
import type { ChatTransport } from "@/features/dashboards/chat/types";
import { dashboardErrorMessage, openShareSession, problemOf, shareClient } from "@/features/dashboards/client";
import { dk } from "@/features/dashboards/keys";
import { ReadoutPage } from "@/features/dashboards/readout/readout-page";
import type { DashboardClient, ShareSessionHandle } from "@/features/dashboards/types";
import { useApplyTheme } from "@/hooks/use-theme";

import { LinkEnded, LinkProblem } from "./link-ended";
import { PasscodeForm, retryWords } from "./passcode-form";
import { ShareFrame } from "./share-frame";
import { applyNoReferrer, stripFragment, tokenFromHash } from "./share-paths";
import { createShareSession, type ShareSessionController } from "./share-session";

export type SharePhase =
  | { kind: "opening" }
  | { kind: "passcode"; busy: boolean; error: string | null; locked: boolean; retryAfter?: number | null }
  | { kind: "open"; client: DashboardClient; chat: ChatTransport; endsAt: string | null }
  | { kind: "ended"; reason: "ended" | "incomplete" | "timeout" }
  | { kind: "problem"; message: string; retry: boolean };

/** What the server's answer to `POST /v1/share-sessions` means for the page. */
export function phaseOfOpenError(err: unknown, sentPasscode: boolean): SharePhase {
  const p = problemOf(err);
  if (!p) return { kind: "problem", message: dashboardErrorMessage(err), retry: true };
  if (p.status === 410 || p.status === 404) return { kind: "ended", reason: "ended" };
  if (p.status === 422) return { kind: "ended", reason: "incomplete" };
  if (p.code === "SHARE_PASSCODE_REQUIRED") {
    return { kind: "passcode", busy: false, locked: false, error: sentPasscode ? "Type the passcode that you got with the link." : null };
  }
  if (p.code === "SHARE_PASSCODE_INVALID") {
    const left = /(\d+)\s+tr(?:y|ies)\s+left/i.exec(p.problem?.detail ?? "");
    const n = left ? Number(left[1]) : null;
    const tail = n === null ? "" : ` You can try ${n} more ${n === 1 ? "time" : "times"} before the link locks.`;
    return { kind: "passcode", busy: false, locked: false, error: `The passcode is not correct.${tail}` };
  }
  if (p.code === "SHARE_LOCKED") {
    return { kind: "passcode", busy: false, locked: true, retryAfter: p.retryAfter, error: `The link is locked after too many wrong passcodes. ${retryWords(p.retryAfter)}` };
  }
  if (p.status === 429) {
    return { kind: "problem", message: `Many people opened this link in a short time. ${retryWords(p.retryAfter)}`, retry: true };
  }
  return { kind: "problem", message: dashboardErrorMessage(err), retry: p.status >= 500 };
}

/** The share app's own query client: no retries on 4xx; a 410 anywhere ends the page. */
export function createShareQueryClient(onAccessLost: (reason: "ended" | "timeout") => void): QueryClient {
  const onError = (err: unknown) => {
    const p = problemOf(err);
    if (p?.status === 410) onAccessLost("ended");
    // A 401 that survived the share client's refresh: the session ended while the tab slept.
    else if (p?.status === 401) onAccessLost("timeout");
  };
  return new QueryClient({
    queryCache: new QueryCache({ onError }),
    mutationCache: new MutationCache({ onError }),
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: false,
        retry: (count, err) => {
          const status = problemOf(err)?.status;
          return !(status !== undefined && status < 500) && count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

const TIMEOUT_TEXT = "This view closed after a long time without use. Open the link that you got again.";

export function ShareApp() {
  useApplyTheme();
  // Read once (StrictMode may call this twice; the fragment is stripped later, in an effect).
  const [initialToken] = useState(() => (typeof window === "undefined" ? null : tokenFromHash(window.location.hash)));
  const token = useRef<string | null>(initialToken);
  const controller = useRef<ShareSessionController | null>(null);
  const started = useRef(false);
  const [phase, setPhase] = useState<SharePhase>(() => (initialToken ? { kind: "opening" } : { kind: "ended", reason: "incomplete" }));

  const lose = useRef<(reason: "ended" | "timeout") => void>(() => undefined);
  const [queryClient] = useState(() => createShareQueryClient((reason) => lose.current(reason)));
  useEffect(() => {
    lose.current = (reason) => {
      controller.current?.stop();
      controller.current = null;
      queryClient.clear();
      setPhase({ kind: "ended", reason });
    };
  }, [queryClient]);

  // Before any request: no Referer from this page, and the token out of the address bar.
  useEffect(() => {
    applyNoReferrer();
    stripFragment();
    // Another link pasted into this tab only changes the fragment: start again with it.
    const onHash = () => {
      if (tokenFromHash(window.location.hash)) window.location.reload();
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = useCallback(
    async (passcode: string | null) => {
      const t = token.current;
      if (!t) return;
      try {
        const s = await openShareSession(t, passcode);
        controller.current?.stop();
        const handle: ShareSessionHandle = {
          current: () => controller.current?.handle.current() ?? s.session,
          expired: () => controller.current?.handle.expired() ?? true,
          refresh: () => controller.current?.handle.refresh() ?? Promise.reject(new Error("the share session was closed")),
        };
        const client = shareClient(handle, s.dashboard.id);
        controller.current = createShareSession(s, {
          onEnded: () => lose.current("ended"),
          onDashboard: (d) => queryClient.setQueryData(dk.viewDashboard(client.cacheKey), d),
        });
        // The token was sent once; the session replaces it.
        token.current = null;
        queryClient.setQueryData(dk.viewDashboard(client.cacheKey), s.dashboard);
        // v0.20: chat rides on the same session (`X-Mapi-Share`); the launcher shows only when
        // the analyst opened chat to share links.
        setPhase({ kind: "open", client, chat: shareChatTransport(handle), endsAt: s.dashboard.ends_at ?? null });
      } catch (err) {
        setPhase(phaseOfOpenError(err, passcode !== null));
      }
    },
    [queryClient],
  );

  useEffect(() => {
    if (started.current || !token.current) return;
    started.current = true;
    void open(null);
  }, [open]);

  // A locked link unlocks after Retry-After; the form then takes a passcode again.
  const lockedFor = phase.kind === "passcode" && phase.locked ? phase : null;
  useEffect(() => {
    if (!lockedFor) return;
    const ms = Math.min(Math.max(lockedFor.retryAfter ?? 60, 1), 3600) * 1000;
    const t = globalThis.setTimeout(() => setPhase((p) => (p.kind === "passcode" ? { ...p, locked: false, error: null } : p)), ms);
    return () => globalThis.clearTimeout(t);
  }, [lockedFor]);

  useEffect(
    () => () => {
      controller.current?.stop();
    },
    [],
  );

  const submitPasscode = (passcode: string) => {
    setPhase((p) => (p.kind === "passcode" ? { ...p, busy: true } : p));
    void open(passcode);
  };

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={300}>
        {phase.kind === "open" ? (
          <ShareFrame endsAt={phase.endsAt}>
            <ReadoutPage client={phase.client} chat={phase.chat} />
          </ShareFrame>
        ) : (
          <ShareFrame narrow>
            {phase.kind === "opening" ? (
              <div className="flex items-center gap-2.5 text-muted-foreground" role="status">
                <Loader2 className="size-5 animate-spin" aria-hidden />
                <span>Opening the dashboard…</span>
              </div>
            ) : phase.kind === "passcode" ? (
              <PasscodeForm busy={phase.busy} error={phase.error} locked={phase.locked} onSubmit={submitPasscode} />
            ) : phase.kind === "ended" ? (
              phase.reason === "timeout" ? (
                <LinkProblem message={TIMEOUT_TEXT} />
              ) : (
                <LinkEnded reason={phase.reason} />
              )
            ) : (
              <LinkProblem
                message={phase.message}
                onRetry={
                  phase.retry
                    ? () => {
                        setPhase({ kind: "opening" });
                        void open(null);
                      }
                    : undefined
                }
              />
            )}
          </ShareFrame>
        )}
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default ShareApp;
