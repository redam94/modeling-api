/** Shared building blocks used across pages. */
import type { UseQueryResult } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, Check, Clock, Copy, Gauge, Loader2, Lock, LogIn, PauseCircle, RefreshCw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { ApiError, errorMessage } from "@/api/client";
import { signIn } from "@/api/session";
import type { Issue, Problem, RunState } from "@/api/types";
import { useWorkspace } from "@/features/auth/workspace";
import { stripAnsi } from "@/lib/ansi";
import { adviseProblem, type Advice, type AdviceKind } from "@/lib/problems";
import { STATE_META, TONE_CLASSES } from "@/lib/run-state";
import { cn } from "@/lib/utils";

import { Button } from "./ui/button";
import { Skeleton } from "./ui/primitives";

// ----------------------------------------------------------------------- page chrome

export function PageHeader({
  title,
  description,
  actions,
  meta,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
  /** A small label above the title (the Studio list-page eyebrow). */
  eyebrow?: ReactNode;
}) {
  return (
    <header className="flex min-w-0 flex-wrap items-end justify-between gap-x-6 gap-y-3 pb-6">
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow mb-3">{eyebrow}</p> : null}
        {/* the Studio page title (direction §4): 48px display, as on the list pages */}
        <h1 className="display-title text-[clamp(2.25rem,5vw,3rem)] leading-[1.02] text-balance">{title}</h1>
        {description ? <p className="mt-2 max-w-[68ch] text-lg text-muted-foreground">{description}</p> : null}
        {meta ? <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">{meta}</div> : null}
      </div>
      {actions ? <div className="flex max-w-full min-w-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/**
 * What a list or page shows before it has anything in it. Without `illustration` it is the plain
 * dashed card (icon, title, text, action). With one (an art component from
 * `components/illustrations`, passed in so this module never imports it) it becomes the Studio
 * empty state: a large dashed card with the words and the calls to action on the left and the art
 * on the right (above the words on a phone). `primaryAction` comes first in the action row, before
 * `action`.
 */
export function EmptyState({
  icon,
  title,
  children,
  action,
  primaryAction,
  illustration,
  className,
}: {
  icon?: ReactNode;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  /** The main call to action (a filled button); `action` then holds the quieter ones. */
  primaryAction?: ReactNode;
  /** Decorative art (`aria-hidden`) or labelled art; switches to the two-column layout. */
  illustration?: ReactNode;
  className?: string;
}) {
  const actions =
    primaryAction || action ? (
      <div className={cn("flex flex-wrap items-center gap-2", illustration ? "pt-4" : "pt-2")}>
        {primaryAction}
        {action}
      </div>
    ) : null;
  if (illustration) {
    return (
      <div
        data-slot="empty-state"
        className={cn(
          "grid items-center gap-8 rounded-3xl border-[1.5px] border-dashed border-border-strong bg-card p-[clamp(1.75rem,5vw,4rem)] md:grid-cols-2 md:gap-10",
          className,
        )}
      >
        <div className="grid min-w-0 justify-items-start gap-3 max-md:order-2">
          {icon ? <div className="text-muted-foreground [&_svg]:size-5">{icon}</div> : null}
          <h3 className="display-title text-[clamp(1.75rem,4vw,2.25rem)] leading-[1.05] text-balance">{title}</h3>
          {children ? <div className="max-w-[46ch] text-base text-muted-foreground">{children}</div> : null}
          {actions}
        </div>
        <div className="mx-auto w-full max-w-[420px] min-w-0 max-md:order-1 max-md:max-w-[260px]">{illustration}</div>
      </div>
    );
  }
  return (
    <div className={cn("flex flex-col items-start gap-2 rounded-xl border border-dashed border-border-strong bg-card p-8", className)}>
      {icon ? <div className="text-muted-foreground [&_svg]:size-5">{icon}</div> : null}
      <h3 className="font-semibold">{title}</h3>
      {children ? <div className="max-w-[60ch] text-muted-foreground">{children}</div> : null}
      {primaryAction ? actions : action ? <div className="pt-2">{action}</div> : null}
    </div>
  );
}

// ----------------------------------------------------------------------- run state

export function StateBadge({ state, className }: { state: RunState; className?: string }) {
  const meta = STATE_META[state];
  const active = meta.tone === "active";
  return (
    <span
      title={meta.description}
      data-state={state}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE_CLASSES[meta.tone],
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full bg-current",
          state === "stalled" && "animate-stall",
          active && "shadow-[0_0_0_3px_color-mix(in_oklab,currentColor_22%,transparent)]",
        )}
      />
      {meta.label}
    </span>
  );
}

// ----------------------------------------------------------------------- errors

/** Untrusted text (tracebacks, plugin messages, log lines): text only, ANSI stripped. */
export function UntrustedText({ text, className, maxHeight = "16rem" }: { text: string; className?: string; maxHeight?: string }) {
  return (
    <pre
      className={cn(
        "overflow-auto rounded-md border bg-muted/60 p-3 font-mono text-xs leading-5 whitespace-pre-wrap text-foreground/90",
        className,
      )}
      style={{ maxHeight }}
    >
      {stripAnsi(text)}
    </pre>
  );
}

/** Seconds left of a countdown that starts at `seconds` (re-armed when it changes). */
export function useCountdown(seconds: number | null): number {
  const [left, setLeft] = useState(seconds ?? 0);
  useEffect(() => {
    setLeft(seconds ?? 0);
    if (!seconds) return undefined;
    const started = Date.now();
    const t = setInterval(() => {
      const rest = Math.max(0, Math.ceil(seconds - (Date.now() - started) / 1000));
      setLeft(rest);
      if (rest === 0) clearInterval(t);
    }, 250);
    return () => clearInterval(t);
  }, [seconds]);
  return left;
}

const ADVICE_ICON: Record<AdviceKind, ReactNode> = {
  rate: <Clock className="mt-0.5 size-4 shrink-0 text-warning" />,
  quota: <Gauge className="mt-0.5 size-4 shrink-0 text-warning" />,
  paused: <PauseCircle className="mt-0.5 size-4 shrink-0 text-warning" />,
  unauthenticated: <LogIn className="mt-0.5 size-4 shrink-0 text-info" />,
  forbidden: <Lock className="mt-0.5 size-4 shrink-0 text-warning" />,
  suspended: <Lock className="mt-0.5 size-4 shrink-0 text-warning" />,
};

function AdviceActions({ advice, onRetry }: { advice: Advice; onRetry?: (() => void) | undefined }) {
  const left = useCountdown(advice.retryAfter != null && advice.actions.some((a) => a.kind === "retry") ? advice.retryAfter : null);
  const actions = advice.actions.filter((a) => a.kind !== "retry" || onRetry);
  if (!actions.length) return null;
  return (
    <div className="flex flex-wrap gap-2 pt-1">
      {actions.map((a) =>
        a.kind === "retry" ? (
          <Button key="retry" size="sm" variant="outline" disabled={left > 0} onClick={onRetry}>
            <RefreshCw /> {left > 0 ? `Try again in ${left} s` : a.label}
          </Button>
        ) : a.kind === "signin" ? (
          <Button key="signin" size="sm" onClick={() => signIn()}>
            <LogIn /> {a.label}
          </Button>
        ) : (
          <Button key={a.label} size="sm" variant="outline" asChild>
            <Link to={a.to ?? "/"} {...(a.search ? { search: a.search } : {})}>
              {a.label}
            </Link>
          </Button>
        ),
      )}
    </div>
  );
}

/**
 * A problem the user can act on (sign-in, permissions, rate limits, quotas): what happened in
 * plain words, what to do next, and the server's code and detail one click away.
 */
export function AdviceNotice({ advice, problem, onRetry }: { advice: Advice; problem: Problem; onRetry?: (() => void) | undefined }) {
  const tone = advice.kind === "unauthenticated" ? "border-info/35 bg-info/6" : "border-warning/40 bg-warning/8";
  return (
    <div role="alert" data-advice={advice.kind} className={cn("grid gap-2 rounded-xl border p-4", tone)}>
      <div className="flex items-start gap-2">
        {ADVICE_ICON[advice.kind]}
        <div className="grid min-w-0 gap-1">
          <p className="font-semibold">{advice.title}</p>
          <p className="text-muted-foreground">{advice.message}</p>
          {advice.steps.length ? (
            <ul className="list-disc pl-5 text-sm text-muted-foreground">
              {advice.steps.map((s) => (
                <li key={s}>{s}</li>
              ))}
            </ul>
          ) : null}
          <AdviceActions advice={advice} onRetry={onRetry} />
          <details className="pt-1">
            <summary className="cursor-pointer text-xs text-muted-foreground">Details</summary>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {problem.code}
              {problem.detail ? ` · ${problem.detail}` : ""}
            </p>
          </details>
        </div>
      </div>
    </div>
  );
}

export function ProblemNotice({
  problem,
  title,
  retryAfter = null,
  onRetry,
}: {
  problem: Problem;
  title?: string;
  retryAfter?: number | null;
  onRetry?: (() => void) | undefined;
}) {
  const ws = useWorkspace();
  const advice = adviseProblem(problem, {
    retryAfter,
    role: ws.mode === "session" ? (ws.current?.role ?? null) : null,
    projectKey: ws.current?.key ?? null,
    tenantSuspended: ws.current?.tenant.status === "suspended",
  });
  if (advice) return <AdviceNotice advice={advice} problem={problem} onRetry={onRetry} />;
  return (
    <div role="alert" className="grid gap-2 rounded-xl border border-destructive/35 bg-destructive/6 p-4">
      <div className="flex items-start gap-2">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
        <div className="min-w-0">
          <p className="font-semibold">{title ?? problem.title}</p>
          {problem.detail ? <p className="text-muted-foreground">{problem.detail}</p> : null}
          <p className="mt-1 font-mono text-xs text-muted-foreground">{problem.code}</p>
        </div>
      </div>
      {problem.errors?.length ? <IssueList issues={problem.errors} /> : null}
      {problem.untrusted_text ? (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">Output from the run (untrusted)</summary>
          <UntrustedText text={problem.untrusted_text} className="mt-2" />
        </details>
      ) : null}
    </div>
  );
}

export function ErrorNotice({ error, title, onRetry }: { error: unknown; title?: string; onRetry?: () => void }) {
  if (error instanceof ApiError && error.problem) {
    return <ProblemNotice problem={error.problem} retryAfter={error.retryAfter} onRetry={onRetry} {...(title ? { title } : {})} />;
  }
  return (
    <div role="alert" className="flex items-start gap-2 rounded-xl border border-destructive/35 bg-destructive/6 p-4">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div>
        {title ? <p className="font-semibold">{title}</p> : null}
        <p className="text-muted-foreground">{errorMessage(error)}</p>
      </div>
    </div>
  );
}

const SEVERITY_CLASS = {
  error: "text-destructive",
  warning: "text-warning",
  info: "text-info",
} as const;

export function IssueList({ issues, onSelect }: { issues: readonly Issue[]; onSelect?: (issue: Issue) => void }) {
  return (
    <ul className="grid gap-1.5">
      {issues.map((issue, i) => {
        const sev = issue.severity ?? "error";
        const body = (
          <>
            <span className={cn("font-mono text-xs font-semibold", SEVERITY_CLASS[sev])}>{issue.code}</span>
            <span className="min-w-0 flex-1">
              {issue.message}
              {issue.pointer ? <span className="ml-2 font-mono text-xs text-muted-foreground">{issue.pointer}</span> : null}
              {issue.suggested_action ? <span className="block text-xs text-muted-foreground">{issue.suggested_action}</span> : null}
            </span>
          </>
        );
        return (
          <li key={`${issue.code}-${issue.pointer ?? ""}-${i}`}>
            {onSelect ? (
              <button
                type="button"
                onClick={() => onSelect(issue)}
                className="focus-ring flex w-full items-start gap-2 rounded-md px-2 py-1 text-left text-sm hover:bg-surface-hover"
              >
                {body}
              </button>
            ) : (
              <div className="flex items-start gap-2 px-2 py-1 text-sm">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

// ----------------------------------------------------------------------- query wrapper

export function QueryView<T>({
  query,
  children,
  loading,
  errorTitle,
}: {
  query: Pick<UseQueryResult<T>, "data" | "error" | "isPending" | "isError">;
  children: (data: T) => ReactNode;
  loading?: ReactNode;
  errorTitle?: string;
}) {
  if (query.isPending) return <>{loading ?? <LoadingRows />}</>;
  if (query.isError) return <ErrorNotice error={query.error} {...(errorTitle ? { title: errorTitle } : {})} />;
  return <>{children(query.data as T)}</>;
}

export function LoadingRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="grid gap-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-9" />
      ))}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 aria-hidden className={cn("size-4 animate-spin", className)} />;
}

// ----------------------------------------------------------------------- small widgets

export function CopyButton({ value, label = "Copy", onCopied }: { value: string; label?: string; onCopied?: () => void }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      aria-label={label}
      title={label}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => {
          setDone(true);
          onCopied?.();
          setTimeout(() => setDone(false), 1200);
        });
      }}
    >
      {done ? <Check className="text-success" /> : <Copy />}
    </Button>
  );
}

export function Mono({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span className={cn("font-mono text-xs tracking-tight", className)} title={title}>
      {children}
    </span>
  );
}

export function KeyValues({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(8rem,auto)_1fr] gap-x-6 gap-y-2 text-sm", className)}>
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Pretty JSON shown as text (never interpreted). */
export function JsonBlock({ value, className, maxHeight = "28rem" }: { value: unknown; className?: string; maxHeight?: string }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    text = "(cannot show as JSON)";
  }
  return (
    <pre
      className={cn("overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-5", className)}
      style={{ maxHeight }}
    >
      {text}
    </pre>
  );
}
