/**
 * InlineMessage (ODS): a short status line in the page flow. Size s is bare text with an icon;
 * size m sits on its tone's tint (18px corners). The tone is said in words too (a visually
 * hidden "Warning:"), so it never depends on the icon colour alone. On a phone the action goes
 * under the text, so a wide button does not squeeze the words.
 */
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import type * as React from "react";

import { cn } from "@/lib/utils";

export type MessageTone = "info" | "success" | "warning" | "error";

const TONE: Record<MessageTone, { word: string; icon: typeof Info; ink: string; fill: string }> = {
  info: { word: "Note", icon: Info, ink: "text-muted-foreground", fill: "bg-muted" },
  success: { word: "Done", icon: CheckCircle2, ink: "text-success", fill: "bg-success/12" },
  warning: { word: "Warning", icon: AlertTriangle, ink: "text-warning", fill: "bg-warning/14" },
  error: { word: "Error", icon: XCircle, ink: "text-destructive", fill: "bg-destructive/12" },
};

export function InlineMessage({
  tone = "info",
  size = "m",
  role,
  action,
  className,
  children,
}: {
  tone?: MessageTone;
  size?: "s" | "m";
  /** Defaults to "alert" for errors and "status" otherwise; pass "none" for a message on page load. */
  role?: "alert" | "status" | "none";
  action?: React.ReactNode;
  className?: string;
  children: React.ReactNode;
}) {
  const t = TONE[tone];
  const Icon = t.icon;
  const r = role ?? (tone === "error" ? "alert" : "status");
  return (
    <div
      data-slot="inline-message"
      data-tone={tone}
      role={r === "none" ? undefined : r}
      className={cn(
        "flex min-w-0 items-start gap-2 text-foreground max-sm:flex-wrap",
        size === "m" ? cn("min-h-8 items-center rounded-xl px-3 py-1.5 text-sm", t.fill) : "text-xs font-medium",
        className,
      )}
    >
      <Icon aria-hidden className={cn("size-4 flex-none", size === "s" && "mt-0.5", t.ink)} />
      <div className="min-w-0 flex-1">
        <span className="sr-only">{t.word}: </span>
        {children}
      </div>
      {action ? <div className="flex-none max-sm:basis-full max-sm:pl-6">{action}</div> : null}
    </div>
  );
}
