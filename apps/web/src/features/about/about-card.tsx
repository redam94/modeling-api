/**
 * The About card of a catalog item (M3.5): plain language first (what it does, when to use it
 * and when not, what it needs and gives, how hard and how long), the expert detail (maths and
 * references) one click away. Every field is untrusted catalog text, rendered as text only.
 *
 * v0.19 (D19): an uploaded entry (`t_<code>_<short>` id) always shows an "Uploaded by" chip,
 * and its references are links only when they pass the upload URL rules (https, a DNS host,
 * no `user@`), with the punycode host beside the link, an external-link marker and
 * `rel="noopener noreferrer nofollow ugc"`. The checks are inline string tests: this module
 * never imports `features/plugins` (the catalog, workflows and guided pages use it).
 */
import { Link } from "@tanstack/react-router";
import { BookOpen, CheckCircle2, Clock, Database, ExternalLink, Info, Lightbulb, PackageOpen, Scale, Upload, XCircle } from "lucide-react";
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
      <ul className="grid gap-1 text-sm">
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

/**
 * `mapi_spec.plugins.is_uploaded_id`: models/steps/plots `t_<code>_<short>.x`, workflow nodes
 * `<kind>.t_<code>_<short>_x`, templates `t_<code>_<short>_x`.
 */
export function isUploadedCatalogId(id: string | null | undefined): boolean {
  return typeof id === "string" && /^(?:[a-z]+\.)?t_[a-z2-7]{6}_[a-z][a-z0-9]{1,15}(?:[._][a-z0-9_.]*)?$/.test(id) && id.length <= 128;
}

/** An uploaded entry's reference (D19): `{href, host}` when it may be a link, else `null`. */
export function uploadedReferenceLink(url: string | null | undefined): { href: string; host: string } | null {
  if (!url || url.length > 500 || !url.toLowerCase().startsWith("https://")) return null;
  for (const ch of url) {
    const c = ch.codePointAt(0) ?? 0;
    if (c <= 0x20 || (c >= 0x7f && c <= 0x9f) || (c >= 0x200b && c <= 0x200f) || (c >= 0x202a && c <= 0x202e) || (c >= 0x2066 && c <= 0x2069) || c === 0xfeff) return null;
  }
  const authority = url.slice(8).split(/[/?#\\]/, 1)[0] ?? "";
  if (authority.includes("@") || /[^\x21-\x7e]/.test(authority)) return null;
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    if (u.protocol !== "https:" || u.username || u.password || !host.includes(".") || /^[\d.]+$/.test(host) || host.startsWith("[")) return null;
    return { href: u.href, host };
  } catch {
    return null;
  }
}

/** The always-visible chip of an uploaded entry (D19). */
export function UploadedChip({ by }: { by?: string | null | undefined }) {
  return (
    <span data-testid="uploaded-by-chip" className="inline-flex items-center gap-1 rounded-full bg-accent px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap text-accent-foreground">
      <Upload aria-hidden className="size-3" />
      Uploaded by {by || "your team"}
    </span>
  );
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
  /** Shown under the summary: what the item looks like (a plot's example render). */
  media?: ReactNode;
  /** Heading level of the title (cards inside a page section use h3). */
  as?: "h2" | "h3" | "h4";
  /** The same as `as`, by number (`headingLevel={2}` is `as="h2"`). */
  headingLevel?: 2 | 3 | 4;
  /** Replaces the title text (e.g. the title with an id or tags beside it). */
  heading?: ReactNode;
  /** Only the plain summary, the badges and the expert detail (side panels, pickers). */
  compact?: boolean;
  /** Draw the card's own border and padding (lists of cards); off inside dialogs and panels. */
  framed?: boolean;
  className?: string;
  /** v0.19: who uploaded it (an uploaded entry shows "Uploaded by your team" without it). */
  uploadedBy?: string | null | undefined;
}

const HEADING_TAG = { 2: "h2", 3: "h3", 4: "h4" } as const;

/** The full card. */
export function AboutCard({
  about,
  kind,
  id,
  expertOpen = false,
  onRelated,
  actions,
  media,
  as,
  headingLevel,
  heading,
  compact = false,
  framed = false,
  className,
  uploadedBy,
}: AboutCardProps) {
  const titleId = useId();
  const Heading = as ?? (headingLevel ? HEADING_TAG[headingLevel] : "h3");
  const uploaded = isUploadedCatalogId(id) || Boolean(uploadedBy);
  const refs = (about.references ?? []).map((r) => {
    if (!uploaded) return { ...r, href: safeHttps(r.url), host: null as string | null };
    const link = uploadedReferenceLink(r.url);
    return { ...r, href: link?.href ?? null, host: link?.host ?? null };
  });
  const hasExpert = Boolean(about.expert_notes) || refs.length > 0 || (compact && about.assumptions.length > 0);
  return (
    <article
      aria-labelledby={titleId}
      data-testid="about-card"
      className={cn(compact ? "grid gap-3" : "grid gap-4", framed && "rounded-2xl border bg-card p-5", className)}
      data-about={id}
    >
      <header className="grid gap-2">
        {kind ? <p className="text-xs font-medium text-muted-foreground">{KIND_META[kind].label}</p> : null}
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <Heading id={titleId} className={cn("leading-snug font-semibold tracking-tight", compact ? "text-base" : "text-lg")}>
            {heading ?? about.title}
          </Heading>
          <div className="flex flex-wrap items-center gap-1.5">
            {uploaded ? <UploadedChip by={uploadedBy} /> : null}
            <DifficultyBadge difficulty={about.difficulty} />
            <RuntimeBadge runtime={about.typical_runtime} />
          </div>
        </div>
        {id ? <p className="font-mono text-xs text-muted-foreground">{id}</p> : null}
        <p className={cn("max-w-[72ch] leading-relaxed", compact ? "text-sm" : "text-base")}>{about.summary_plain}</p>
      </header>
      {media}
      {compact ? null : (
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
          <p className="rounded-md border-l-2 border-primary/50 bg-muted/40 px-3 py-2 text-sm italic">{about.example}</p>
        </section>
      </div>
      )}
      {compact ? null : <RelatedChips ids={about.related ?? []} onRelated={onRelated} />}
      {hasExpert ? (
        <details className="group rounded-lg border bg-muted/20 px-4 py-2" open={expertOpen}>
          <summary className="flex cursor-pointer items-center gap-2 py-1 text-sm font-medium select-none">
            <BookOpen aria-hidden className="size-4 text-muted-foreground" /> For experts: how it works
          </summary>
          <div className="grid gap-3 pt-2 pb-2">
            {compact && about.assumptions.length ? (
              <div className="grid gap-1">
                <p className="text-xs font-semibold text-muted-foreground">It assumes</p>
                <ul className="grid list-disc gap-0.5 pl-4 text-sm">
                  {about.assumptions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {about.expert_notes ? <p className="text-sm leading-relaxed whitespace-pre-line text-foreground/90">{about.expert_notes}</p> : null}
            {refs.length ? (
              <div className="grid gap-1">
                <p className="text-xs font-semibold text-muted-foreground">References</p>
                <ul className="grid gap-1 text-sm">
                  {refs.map((r) => (
                    <li key={r.title}>
                      {r.href && uploaded ? (
                        <>
                          <a href={r.href} target="_blank" rel="noopener noreferrer nofollow ugc" referrerPolicy="no-referrer" className="inline-flex items-center gap-1 text-primary underline-offset-2 hover:underline">
                            {r.title}
                            <ExternalLink aria-label="(opens an outside site)" className="size-3" />
                          </a>{" "}
                          <span className="font-mono text-xs text-muted-foreground">{r.host}</span>
                        </>
                      ) : r.href ? (
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

/** "Beginner" (lime), "Some experience", "For experts": the Studio chip of a difficulty. */
export function DifficultyChip({ difficulty, onBlock = false }: { difficulty: About["difficulty"]; onBlock?: boolean }) {
  const meta = DIFFICULTY_META[difficulty];
  return (
    <span
      title={meta.description}
      data-difficulty={difficulty}
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap",
        onBlock ? "bg-block-raised text-block-foreground" : difficulty === "beginner" ? "bg-highlight-soft text-highlight-ink" : difficulty === "expert" ? "bg-attention-soft text-attention" : "bg-muted text-muted-foreground",
      )}
    >
      {meta.label}
    </span>
  );
}

/** How long it usually takes, as a quiet chip. */
export function RuntimeChip({ runtime, onBlock = false }: { runtime: About["typical_runtime"]; onBlock?: boolean }) {
  const meta = RUNTIME_META[runtime];
  return (
    <span title={`Typical runtime: ${meta.description}`} className={cn("inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap", onBlock ? "bg-block-raised text-block-foreground" : "bg-muted text-muted-foreground")}>
      <Clock aria-hidden className="size-3" /> {meta.label}
    </span>
  );
}

/**
 * A compact tile (the Catalog grid, lists, pickers): kind, title, the plain summary, what it is
 * good for and the difficulty and runtime chips. `glyph` is the kind's drawing (passed in, so
 * this module never imports the illustrations).
 */
export function AboutTile({
  about,
  kind,
  id,
  onOpen,
  selected,
  glyph,
  children,
}: {
  about: About;
  kind?: AboutKind;
  id?: string;
  onOpen?: () => void;
  selected?: boolean;
  glyph?: ReactNode;
  children?: ReactNode;
}) {
  const goodFor = about.use_when[0];
  const body = (
    <>
      {kind || glyph ? (
        <span className="flex items-center gap-2.5 text-xs font-semibold text-muted-foreground">
          {glyph}
          {kind ? KIND_META[kind].label : null}
        </span>
      ) : null}
      <span className="block font-display text-xl leading-tight font-bold tracking-[-0.02em] [overflow-wrap:anywhere]">{about.title}</span>
      <span className="line-clamp-3 text-sm text-muted-foreground">{about.summary_plain}</span>
      {goodFor ? (
        <span className="line-clamp-2 text-sm">
          <span className="font-semibold">Good for: </span>
          {goodFor}
        </span>
      ) : null}
      <span className="mt-auto flex flex-wrap items-center gap-1.5 pt-1">
        {isUploadedCatalogId(id) ? <UploadedChip /> : null}
        <DifficultyChip difficulty={about.difficulty} />
        <RuntimeChip runtime={about.typical_runtime} />
        {id ? <span className="min-w-0 truncate font-mono text-xs text-hint">{id}</span> : null}
      </span>
      {children}
    </>
  );
  const cls = cn(
    "flex h-full w-full flex-col gap-3 rounded-2xl border bg-card p-5 text-left transition-colors sm:p-[22px]",
    onOpen && "focus-ring hover:border-primary",
    selected && "border-primary bg-accent/50",
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
  media,
}: {
  about: About;
  kind?: AboutKind | undefined;
  id?: string | undefined;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  actions?: ReactNode;
  media?: ReactNode;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide>
        <DialogTitle className="sr-only">About {about.title}</DialogTitle>
        <DialogDescription className="sr-only">{about.summary_plain}</DialogDescription>
        <AboutCard about={about} kind={kind} id={id} actions={actions} media={media} as="h2" />
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
    <p className={cn("flex items-start gap-1 text-sm text-muted-foreground", className)}>
      <span className="min-w-0 flex-1">{about.summary_plain}</span>
      <AboutButton kind={entry.kind} id={entry.id} />
    </p>
  );
}
