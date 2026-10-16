/**
 * The About card of a catalog item (M3.5): plain language first (what it does, when to use it
 * and when not, what it needs and gives, how hard and how long), the expert detail (maths and
 * references) one click away. Every field is untrusted catalog text, rendered as text only.
 */
import { Link } from "@tanstack/react-router";
import { BookOpen, CheckCircle2, Clock, Database, Info, Lightbulb, PackageOpen, Scale, XCircle } from "lucide-react";
import { useId, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/overlays";
import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { useAbout, useAboutIndex } from "./api";
import { aboutKey, DIFFICULTY_META, KIND_META, RUNTIME_META } from "./labels";
import type { About, AboutKind } from "./types";

export function DifficultyBadge({ difficulty }: { difficulty: About["difficulty"] }) {
  const meta = DIFFICULTY_META[difficulty];
  return (
    <Badge variant={meta.variant} title={meta.description} data-difficulty={difficulty}>
      {meta.label}
    </Badge>
  );
}

export function RuntimeBadge({ runtime }: { runtime: About["typical_runtime"] }) {
  const meta = RUNTIME_META[runtime];
  return (
    <Badge variant="outline" title={`Typical runtime: ${meta.description}`}>
      <Clock aria-hidden /> {meta.label}
    </Badge>
  );
}

function Section({ icon, title, items, tone }: { icon: ReactNode; title: string; items: readonly string[]; tone?: "good" | "bad" }) {
  if (!items.length) return null;
  return (
    <section className="grid content-start gap-1.5">
      <h4 className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase [&_svg]:size-3.5">
        <span aria-hidden className={cn(tone === "good" && "text-success", tone === "bad" && "text-destructive")}>
          {icon}
        </span>
        {title}
      </h4>
      <ul className="grid gap-1 text-[13px]">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <span aria-hidden className="mt-[0.45rem] size-1 shrink-0 rounded-full bg-muted-foreground/60" />
            <span className="min-w-0">{item}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function safeHttps(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.toString() : null;
  } catch {
    return null;
  }
}

function RelatedChips({ ids, onRelated }: { ids: readonly string[]; onRelated?: ((id: string) => void) | undefined }) {
  const index = useAboutIndex();
  if (!ids.length) return null;
  return (
    <section className="grid gap-1.5">
      <h4 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Related</h4>
      <ul className="flex flex-wrap gap-1.5" aria-label="Related items">
        {ids.map((id) => {
          const entry = index.data?.byId.get(id);
          const label = entry ? entry.about.title : id;
          const cls = "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs hover:border-primary/40 hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring";
          return (
            <li key={id}>
              {onRelated ? (
                <button type="button" className={cls} onClick={() => onRelated(id)} title={id}>
                  {label}
                </button>
              ) : entry ? (
                <Link to="/catalog" search={{ about: aboutKey(entry.kind, entry.id) }} className={cls} title={id}>
                  {label}
                </Link>
              ) : (
                <span className={cn(cls, "font-mono text-muted-foreground")}>{id}</span>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export interface AboutCardProps {
  about: About;
  kind?: AboutKind | undefined;
  id?: string | undefined;
  /** Show the expert notes expanded (Expert screens); collapsed by default. */
  expertOpen?: boolean;
  /** Called for a related item instead of linking to the catalog. */
  onRelated?: ((id: string) => void) | undefined;
  /** Buttons under the card ("Start with this", "Use this model"). */
  actions?: ReactNode;
  /** Heading level of the title (cards inside a page section use h3). */
  as?: "h2" | "h3";
  className?: string;
}

/** The full card. */
export function AboutCard({ about, kind, id, expertOpen = false, onRelated, actions, as: Heading = "h3", className }: AboutCardProps) {
  const titleId = useId();
  const refs = (about.references ?? []).map((r) => ({ ...r, href: safeHttps(r.url) }));
  const hasExpert = Boolean(about.expert_notes) || refs.length > 0;
  return (
    <article aria-labelledby={titleId} className={cn("grid gap-4", className)} data-about={id}>
      <header className="grid gap-2">
        {kind ? <p className="text-xs font-medium text-muted-foreground">{KIND_META[kind].label}</p> : null}
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <Heading id={titleId} className="text-[17px] leading-snug font-semibold tracking-tight">
            {about.title}
          </Heading>
          <div className="flex flex-wrap items-center gap-1.5">
            <DifficultyBadge difficulty={about.difficulty} />
            <RuntimeBadge runtime={about.typical_runtime} />
          </div>
        </div>
        {id ? <p className="font-mono text-[11px] text-muted-foreground">{id}</p> : null}
        <p className="max-w-[72ch] text-[15px] leading-relaxed">{about.summary_plain}</p>
      </header>
      <div className="grid gap-x-8 gap-y-4 md:grid-cols-2">
        <Section icon={<CheckCircle2 />} title="Use it when" items={about.use_when} tone="good" />
        <Section icon={<XCircle />} title="Avoid it when" items={about.avoid_when} tone="bad" />
        <Section icon={<Database />} title="What it needs" items={about.data_needs} />
        <Section icon={<PackageOpen />} title="What you get" items={about.outputs} />
        <Section icon={<Scale />} title="It assumes" items={about.assumptions} />
        <section className="grid content-start gap-1.5">
          <h4 className="flex items-center gap-1.5 text-xs font-semibold tracking-wide text-muted-foreground uppercase [&_svg]:size-3.5">
            <Lightbulb aria-hidden /> For example
          </h4>
          <p className="rounded-md border-l-2 border-primary/50 bg-muted/40 px-3 py-2 text-[13px] italic">{about.example}</p>
        </section>
      </div>
      <RelatedChips ids={about.related ?? []} onRelated={onRelated} />
      {hasExpert ? (
        <details className="group rounded-lg border bg-muted/20 px-4 py-2" open={expertOpen}>
          <summary className="flex cursor-pointer items-center gap-2 py-1 text-[13px] font-medium select-none">
            <BookOpen aria-hidden className="size-4 text-muted-foreground" /> For experts: how it works
          </summary>
          <div className="grid gap-3 pt-2 pb-2">
            {about.expert_notes ? <p className="text-[13px] leading-relaxed whitespace-pre-line text-foreground/90">{about.expert_notes}</p> : null}
            {refs.length ? (
              <div className="grid gap-1">
                <p className="text-xs font-semibold text-muted-foreground">References</p>
                <ul className="grid gap-1 text-[13px]">
                  {refs.map((r) => (
                    <li key={r.title}>
                      {r.href ? (
                        <a href={r.href} target="_blank" rel="noopener noreferrer" className="text-primary underline-offset-2 hover:underline">
                          {r.title}
                        </a>
                      ) : (
                        r.title
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </details>
      ) : null}
      {actions ? <footer className="flex flex-wrap gap-2 border-t pt-4">{actions}</footer> : null}
    </article>
  );
}

/** A compact tile: title, one-line summary and the badges (lists, pickers). */
export function AboutTile({ about, kind, id, onOpen, selected, children }: { about: About; kind?: AboutKind; id?: string; onOpen?: () => void; selected?: boolean; children?: ReactNode }) {
  const body = (
    <>
      <span className="flex flex-wrap items-start justify-between gap-2">
        <span className="min-w-0">
          {kind ? <span className="block text-[11px] font-medium text-muted-foreground">{KIND_META[kind].label}</span> : null}
          <span className="block font-semibold tracking-tight">{about.title}</span>
        </span>
        <DifficultyBadge difficulty={about.difficulty} />
      </span>
      <span className="line-clamp-3 text-[13px] text-muted-foreground">{about.summary_plain}</span>
      <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
        <RuntimeBadge runtime={about.typical_runtime} />
        {id ? <span className="truncate font-mono text-[11px] text-muted-foreground">{id}</span> : null}
      </span>
      {children}
    </>
  );
  const cls = cn(
    "flex h-full flex-col gap-2 rounded-xl border bg-card p-4 text-left shadow-[0_1px_0_0_rgb(0_0_0/0.03)] transition-colors",
    onOpen && "hover:border-primary/40 hover:bg-accent/40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
    selected && "border-primary/60 bg-accent/50",
  );
  return onOpen ? (
    <button type="button" className={cls} onClick={onOpen} aria-pressed={selected} aria-label={`${about.title}: ${about.summary_plain}`}>
      {body}
    </button>
  ) : (
    <div className={cls}>{body}</div>
  );
}

/** The full card in a dialog. */
export function AboutDialog({
  about,
  kind,
  id,
  open,
  onOpenChange,
  actions,
}: {
  about: About;
  kind?: AboutKind | undefined;
  id?: string | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  actions?: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide>
        <DialogTitle className="sr-only">About {about.title}</DialogTitle>
        <DialogDescription className="sr-only">{about.summary_plain}</DialogDescription>
        <AboutCard about={about} kind={kind} id={id} actions={actions} as="h2" />
      </DialogContent>
    </Dialog>
  );
}

/**
 * A small "About" button for an item that opens its card; renders nothing when the catalog
 * has no card for it (custom plugins, unknown ids).
 */
export function AboutButton({ kind, id, label, className, size = "icon-sm" }: { kind: AboutKind | null; id: string | null | undefined; label?: string; className?: string; size?: "icon-sm" | "sm" }) {
  const { about, entry } = useAbout(kind, id);
  const [open, setOpen] = useState(false);
  if (!about || !entry) return null;
  return (
    <>
      <Button
        variant="ghost"
        size={size}
        className={className}
        aria-label={label ?? `About ${about.title}`}
        title={`About ${about.title}`}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        <Info /> {size === "sm" ? (label ?? "About") : null}
      </Button>
      <AboutDialog about={about} kind={entry.kind} id={entry.id} open={open} onOpenChange={setOpen} />
    </>
  );
}

/** One line of plain summary for an item in a picker (with its card one click away). */
export function AboutInline({ kind, id, className }: { kind: AboutKind | null; id: string | null | undefined; className?: string }) {
  const { about, entry } = useAbout(kind, id);
  if (!about || !entry) return null;
  return (
    <p className={cn("flex items-start gap-1 text-[13px] text-muted-foreground", className)}>
      <span className="min-w-0 flex-1">{about.summary_plain}</span>
      <AboutButton kind={entry.kind} id={entry.id} />
    </p>
  );
}
