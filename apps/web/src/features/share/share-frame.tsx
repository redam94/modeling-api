/**
 * The frame of a share link page: a generic mark, the words "Shared dashboard", the page and,
 * once the dashboard is open, the footer that says when the link ends. No navigation, no
 * account menu: the viewer is not signed in.
 */
import type * as React from "react";

import { Logo } from "@/components/brand/logo";
import { dmy } from "@/components/ods/format";
import { cn } from "@/lib/utils";

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
      <header className="frost sticky top-0 z-20 border-b [--frost-surface:var(--background)]">
        <div className="mx-auto flex h-12 w-full max-w-7xl items-center gap-2.5 px-4 sm:px-6">
          <Logo />
          <span className="text-sm font-semibold tracking-tight">Shared dashboard</span>
        </div>
      </header>
      <main id="main" className={cn("mx-auto w-full flex-1 px-4 py-6 sm:px-6 sm:py-8", narrow ? "grid max-w-md place-items-start content-start" : "max-w-7xl")}>
        {narrow ? <div className="grid w-full gap-5 rounded-3xl bg-card p-8 max-sm:p-6">{children}</div> : children}
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
