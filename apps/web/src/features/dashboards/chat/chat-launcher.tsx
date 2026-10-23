/**
 * The "Ask about this dashboard" button (v0.20, plan D21): a small floating button on the
 * readout page of every surface (signed in, the client portal, a share link). It asks the
 * transport whether this viewer can chat now and shows nothing when not (chat off, not for this
 * audience, a layout 1 dashboard, …). The panel itself is a lazy chunk (`chat-panel`) that
 * loads on the first open (and is fetched early when the pointer or focus reaches the button).
 *
 * This module is in the share app's static closure, so it stays tiny: no panel code, no
 * session client, one icon.
 */
import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { lazy, Suspense, useId, useRef, useState } from "react";

import { ck } from "./keys";
import type { ChatTransport } from "./types";

const loadPanel = () => import("./chat-panel");
const ChatPanel = lazy(loadPanel);

export interface ChatLauncherProps {
  transport: ChatTransport;
  /**
   * The cache key of the readout's {@link DashboardClient} (`client.cacheKey`), so a member, a
   * client user and a share link never share an availability answer. Default: one per launcher.
   */
  cacheKey?: readonly string[] | undefined;
  /** The readout the viewer is looking at (a hint the question carries). */
  readoutSeq?: number | null | undefined;
}

export function ChatLauncher({ transport, cacheKey, readoutSeq = null }: ChatLauncherProps) {
  const own = useId();
  const availability = useQuery({
    queryKey: ck.availability(cacheKey ?? ["chat", own]),
    queryFn: ({ signal }) => transport.availability(signal),
    staleTime: 60_000,
    retry: false,
  });
  const [open, setOpen] = useState(false);
  const [started, setStarted] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const a = availability.data;
  const enabled = Boolean(a?.enabled_for_me);
  if (!a || (!enabled && !started)) return null;

  return (
    <>
      {enabled ? (
        <button
          ref={button}
          type="button"
          data-testid="chat-launcher"
          aria-haspopup="dialog"
          aria-expanded={open}
          onPointerEnter={() => void loadPanel()}
          onFocus={() => void loadPanel()}
          onClick={() => {
            setStarted(true);
            setOpen(true);
          }}
          className="focus-ring fixed right-4 bottom-4 z-30 inline-flex h-11 items-center gap-2 rounded-full bg-primary px-5 text-sm font-semibold text-primary-foreground shadow-lg transition-colors hover:bg-primary-hover active:bg-primary-active print:hidden md:right-8 md:bottom-8 [&_svg]:size-4"
        >
          <Sparkles aria-hidden />
          Ask about this dashboard
        </button>
      ) : null}
      {started ? (
        <Suspense fallback={null}>
          <ChatPanel transport={transport} availability={a} open={open} onOpenChange={setOpen} returnFocus={button} readoutSeq={readoutSeq} />
        </Suspense>
      ) : null}
    </>
  );
}
