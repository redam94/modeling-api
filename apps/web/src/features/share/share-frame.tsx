/**
 * The frame of a share link page: a generic mark, the words "Shared dashboard", the page and,
 * once the dashboard is open, the footer that says when the link ends. No navigation, no
 * account menu: the viewer is not signed in.
 */
import type * as React from "react";

import { dmy } from "@/components/ods/format";
import { cn } from "@/lib/utils";

function Mark({ className = "size-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={cn("shrink-0", className)} aria-hidden>
      <rect x="2" y="4" width="20" height="4" rx="2" className="fill-primary/25" />
      <rect x="5" y="10" width="14" height="4" rx="2" className="fill-primary/55" />
      <rect x="9" y="16" width="6" height="4" rx="2" className="fill-primary" />
    </svg>
  );
}

/** "Shared view. The link ends on DD/MM/YYYY." (just "Shared view." without an end date). */
export function footerText(endsAt: string | null | undefined): string {
  return endsAt ? `Shared view. The link ends on ${dmy(endsAt)}.` : "Shared view.";
}

export function ShareFrame({
  children,
  endsAt,
  narrow = false,
}: {
  children: React.ReactNode;
  /** When the link ends (the footer is shown only with an open dashboard). */
  endsAt?: string | null | undefined;
  /** A centred card (passcode, ended link) instead of the full-width readout. */
  narrow?: boolean;
}) {
  return (
    <div className="flex min-h-svh flex-col bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex h-14 w-full max-w-7xl items-center gap-2.5 px-4 sm:px-6">
          <Mark />
          <span className="text-sm font-semibold tracking-tight">Shared dashboard</span>
        </div>
      </header>
      <main id="main" className={cn("mx-auto w-full flex-1 px-4 py-6 sm:px-6 sm:py-8", narrow ? "grid max-w-md place-items-start content-start" : "max-w-7xl")}>
        {narrow ? <div className="grid w-full gap-5 rounded-xl border bg-card p-6 shadow-sm">{children}</div> : children}
      </main>
      {endsAt !== undefined ? (
        <footer className="border-t">
          <p className="mx-auto w-full max-w-7xl px-4 py-4 text-sm text-hint sm:px-6" data-share="footer">
            {footerText(endsAt)}
          </p>
        </footer>
      ) : null}
    </div>
  );
}
