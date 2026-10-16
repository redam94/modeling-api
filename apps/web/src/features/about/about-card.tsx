/**
 * The plain-language About card (`mapi_spec.about.About`) every selectable catalog item
 * carries: what it does in plain words first, when to use it and when not, what it needs and
 * gives, a tiny example; the expert detail (assumptions, maths, references, related items)
 * stays one click away. All text is rendered as text.
 */
import { BookOpen, Clock, GraduationCap } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { About, Difficulty, TypicalRuntime } from "@/features/workflows/types";

export const DIFFICULTY_META: Record<Difficulty, { label: string; variant: "success" | "info" | "warning" }> = {
  beginner: { label: "Beginner", variant: "success" },
  intermediate: { label: "Intermediate", variant: "info" },
  expert: { label: "Expert", variant: "warning" },
};

const HEADING_TAG = { 2: "h2", 3: "h3", 4: "h4" } as const;

export const RUNTIME_LABEL: Record<TypicalRuntime, string> = {
  instant: "Instant",
  seconds: "Seconds",
  minutes: "Minutes",
  long: "Takes a while",
};

export function AboutBadges({ about, className }: { about: Pick<About, "difficulty" | "typical_runtime">; className?: string }) {
  const d = DIFFICULTY_META[about.difficulty];
  return (
    <span className={cn("inline-flex flex-wrap items-center gap-1", className)}>
      <Badge variant={d.variant} title="How much statistics it asks of you">
        <GraduationCap aria-hidden /> {d.label}
      </Badge>
      <Badge variant="outline" title="Typical runtime">
        <Clock aria-hidden /> {RUNTIME_LABEL[about.typical_runtime]}
      </Badge>
    </span>
  );
}

function List({ title, items }: { title: string; items: readonly string[] | undefined }) {
  if (!items?.length) return null;
  return (
    <div className="grid gap-1">
      <p className="text-xs font-medium text-muted-foreground">{title}</p>
      <ul className="grid list-disc gap-0.5 pl-4 text-[13px]">
        {items.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function safeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

/** The expert detail: assumptions, notes, references and related items. */
export function AboutExpert({ about, onRelated }: { about: About; onRelated?: (id: string) => void }) {
  const hasExpert = Boolean(about.assumptions?.length || about.expert_notes || about.references?.length || about.related?.length);
  if (!hasExpert) return null;
  return (
    <details className="group rounded-md border bg-muted/30 px-3 py-2 text-[13px]">
      <summary className="cursor-pointer text-xs font-medium text-muted-foreground group-open:mb-2">
        <BookOpen className="mr-1 inline size-3.5" aria-hidden />
        Expert detail
      </summary>
      <div className="grid gap-3">
        <List title="It assumes" items={about.assumptions} />
        {about.expert_notes ? <p className="whitespace-pre-line text-muted-foreground">{about.expert_notes}</p> : null}
        {about.references?.length ? (
          <div className="grid gap-1">
            <p className="text-xs font-medium text-muted-foreground">References</p>
            <ul className="grid gap-0.5 pl-4 text-[13px]">
              {about.references.map((r) => {
                const href = safeUrl(r.url);
                return (
                  <li key={r.title} className="list-disc">
                    {href ? (
                      <a href={href} target="_blank" rel="noreferrer noopener" className="text-primary hover:underline">
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}
        {about.related?.length ? (
          <div className="flex flex-wrap items-center gap-1">
            <span className="text-xs text-muted-foreground">Related:</span>
            {about.related.map((id) =>
              onRelated ? (
                <button key={id} type="button" onClick={() => onRelated(id)} className="rounded border px-1.5 font-mono text-[11px] hover:bg-accent">
                  {id}
                </button>
              ) : (
                <code key={id} className="rounded border px-1.5 font-mono text-[11px]">
                  {id}
                </code>
              ),
            )}
          </div>
        ) : null}
      </div>
    </details>
  );
}

/**
 * The full card. `heading` replaces the title (e.g. with an id beside it); `actions` sit in the
 * header (a "Use this" button).
 */
export function AboutCard({
  about,
  heading,
  actions,
  className,
  onRelated,
  headingLevel = 3,
  compact = false,
}: {
  about: About;
  heading?: ReactNode;
  actions?: ReactNode;
  className?: string;
  onRelated?: (id: string) => void;
  headingLevel?: 2 | 3 | 4;
  compact?: boolean;
}) {
  const titleId = useId();
  const H = HEADING_TAG[headingLevel];
  return (
    <article aria-labelledby={titleId} data-testid="about-card" className={cn("grid gap-3 rounded-lg border bg-card p-4", className)}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="grid min-w-0 gap-1">
          <H id={titleId} className="text-[15px] leading-6 font-semibold tracking-tight">
            {heading ?? about.title}
          </H>
          <AboutBadges about={about} />
        </div>
        {actions ? <div className="flex shrink-0 flex-wrap gap-2">{actions}</div> : null}
      </header>
      <p className="text-[13px] leading-5">{about.summary_plain}</p>
      {compact ? null : (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <List title="Use it when" items={about.use_when} />
            <List title="Pick something else when" items={about.avoid_when} />
            <List title="What it needs" items={about.data_needs} />
            <List title="What you get" items={about.outputs} />
          </div>
          {about.example ? (
            <p className="rounded-md border-l-2 border-primary/50 bg-muted/40 px-3 py-2 text-[13px]">
              <span className="font-medium">Example: </span>
              {about.example}
            </p>
          ) : null}
        </>
      )}
      <AboutExpert about={about} {...(onRelated ? { onRelated } : {})} />
    </article>
  );
}
