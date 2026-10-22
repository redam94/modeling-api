/**
 * Small building blocks of "Make your own": Studio chips and pills, numbered builder sections,
 * field problems from `:validate` pointers, the metadata fields every item has, the scope
 * choice, and a debounce hook. Token classes only (Studio, `plans/v018-direction.md`).
 */
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useRef, useState, type ComponentProps, type ReactNode } from "react";

import type { Issue } from "@/api/types";
import { plainIssue } from "@/lib/plain-issue";
import { cn } from "@/lib/utils";
import { Field, Input, Textarea } from "@/components/ui/primitives";

import { slugify } from "./doc";
import type { LibrarySearch } from "./search";
import { issuesAt, type LibraryMetadata, type LibraryScope } from "./types";

// ------------------------------------------------------------------------------ hooks

/** `value` after it has stopped changing for `ms` (compared by its JSON, so new objects with the same content do not restart the wait). */
export function useDebounced<T>(value: T, ms: number): T {
  const [out, setOut] = useState(value);
  const key = JSON.stringify(value);
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    const t = setTimeout(() => setOut(latest.current), ms);
    return () => clearTimeout(t);
  }, [key, ms]);
  return out;
}

/** Move focus to the field a JSON pointer names (the closest one that exists). */
export function focusPointer(root: HTMLElement | null, pointer: string | null | undefined): boolean {
  if (!root || pointer == null) return false;
  let p = pointer;
  for (;;) {
    const el = [...root.querySelectorAll<HTMLElement>("[data-pointer]")].find((e) => e.dataset.pointer === p);
    if (el) {
      const target = el.matches("input,select,textarea,button") ? el : (el.querySelector<HTMLElement>("input,select,textarea,button") ?? el);
      target.scrollIntoView?.({ block: "center" });
      target.focus();
      return true;
    }
    const cut = p.lastIndexOf("/");
    if (cut <= 0) return false;
    p = p.slice(0, cut);
  }
}

// ------------------------------------------------------------------------------ chips and pills

export type ChipTone = "plain" | "lime" | "attention" | "ok" | "warn" | "block";

const CHIP: Record<ChipTone, string> = {
  plain: "bg-muted text-muted-foreground",
  lime: "bg-highlight-soft text-highlight-ink",
  attention: "bg-attention-soft text-attention",
  ok: "bg-[color-mix(in_srgb,var(--success)_12%,var(--card))] text-success",
  warn: "bg-[color-mix(in_srgb,var(--warning)_14%,var(--card))] text-warning",
  block: "bg-block-raised text-block-foreground",
};

export function Chip({ tone = "plain", className, children }: { tone?: ChipTone; className?: string; children: ReactNode }) {
  return <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap", CHIP[tone], className)}>{children}</span>;
}

/** Studio pill shapes for `Button`'s `className` (min 44 px, 36 px small). */
export const PILL = "h-auto min-h-11 rounded-full px-5";
export const PILL_SM = "h-auto min-h-9 rounded-full px-4 text-xs";
/** The lime call to action on an ink block (one per view). */
export const LIME = "bg-highlight text-highlight-foreground hover:bg-highlight hover:shadow-[inset_0_0_0_2px_var(--highlight-foreground)] active:bg-highlight";
/** A quiet pill (muted fill). */
export const QUIET = "bg-muted text-foreground ring-0 hover:bg-surface-hover";

/** A toggle chip (aria-pressed): pressed = foreground fill, like the Catalog filters. */
export function ToggleChip({ pressed, onClick, children, title, disabled, className }: { pressed: boolean; onClick: () => void; children: ReactNode; title?: string; disabled?: boolean; className?: string }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "focus-ring inline-flex min-h-9 items-center gap-1.5 rounded-full border-[1.5px] px-3.5 text-xs font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        pressed ? "border-foreground bg-foreground text-background" : "border-border-strong bg-card text-foreground hover:border-primary",
        className,
      )}
    >
      {children}
    </button>
  );
}

// ------------------------------------------------------------------------------ sections

/** One numbered part of a builder ("1 Pick your data"). */
export function BuilderSection({ n, title, hint, children, className, done }: { n: number; title: string; hint?: ReactNode; children: ReactNode; className?: string; done?: boolean }) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={cn("rounded-2xl border bg-card p-5 sm:p-6", className)}>
      <header className="mb-4 flex items-start gap-3">
        <span
          aria-hidden
          className={cn("display grid size-8 shrink-0 place-items-center rounded-full text-sm font-bold", done ? "bg-primary text-primary-foreground" : "bg-highlight-soft text-highlight-ink")}
        >
          {n}
        </span>
        <div className="min-w-0">
          <h2 id={id} className="text-xl leading-tight font-bold">
            {title}
          </h2>
          {hint ? <p className="mt-1 max-w-[60ch] text-muted-foreground">{hint}</p> : null}
        </div>
      </header>
      {children}
    </section>
  );
}

// ------------------------------------------------------------------------------ problems

/** The message (in plain words) of the first issue at (or under, with `deep`) `pointer`, for a field's `error`. */
export function issueText(issues: readonly Issue[] | undefined, pointer: string, deep = false): string | undefined {
  const here = deep ? issuesAt(issues, pointer) : (issues ?? []).filter((i) => (i.pointer ?? "") === pointer);
  const first = here.find((i) => (i.severity ?? "error") !== "info");
  return first ? plainIssue(first) : undefined;
}

/** Every issue at or under `pointer`, as small lines (warnings and notes included). */
export function IssueLines({ issues, pointer, className }: { issues: readonly Issue[] | undefined; pointer: string; className?: string }) {
  const here = issuesAt(issues, pointer);
  if (!here.length) return null;
  return (
    <ul className={cn("grid gap-1", className)}>
      {here.map((i, k) => (
        <li key={`${i.pointer ?? ""}-${k}`} role={(i.severity ?? "error") === "error" ? "alert" : undefined} className={cn("text-xs font-medium", (i.severity ?? "error") === "error" ? "text-destructive" : i.severity === "warning" ? "text-warning" : "text-muted-foreground")}>
          {plainIssue(i)}
        </li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------------------ metadata

/** Title (the name follows it until edited by hand), summary and tags of an item. */
export function MetadataFields({
  value,
  onChange,
  issues,
  titleLabel = "Give it a name people will recognise",
  titlePlaceholder,
  locked = false,
}: {
  value: LibraryMetadata;
  onChange: (m: LibraryMetadata) => void;
  issues?: readonly Issue[];
  titleLabel?: string;
  titlePlaceholder?: string;
  /** The name of a saved item cannot change (it is part of its identity). */
  locked?: boolean;
}) {
  const id = useId();
  const [nameEdited, setNameEdited] = useState(locked || (value.title !== "" && slugify(value.title, "") !== value.name));
  const [tagText, setTagText] = useState((value.tags ?? []).join(", "));
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label={titleLabel} htmlFor={`${id}-title`} error={issueText(issues ?? [], "/metadata/title")} className="sm:col-span-2" >
        <Input
          id={`${id}-title`}
          data-pointer="/metadata/title"
          value={value.title}
          maxLength={120}
          placeholder={titlePlaceholder}
          onChange={(e) => onChange({ ...value, title: e.target.value, ...(nameEdited ? {} : { name: slugify(e.target.value, "my-item") }) })}
        />
      </Field>
      <Field label="Short name" htmlFor={`${id}-name`} hint={locked ? "A saved item keeps its name." : "Lower-case letters, digits and dashes. It is how specs and YAML refer to it."} error={issueText(issues ?? [], "/metadata/name")}>
        <Input
          id={`${id}-name`}
          data-pointer="/metadata/name"
          className="font-mono text-xs"
          value={value.name}
          maxLength={64}
          readOnly={locked}
          onChange={(e) => {
            setNameEdited(true);
            onChange({ ...value, name: e.target.value });
          }}
        />
      </Field>
      <Field label="Tags (optional)" htmlFor={`${id}-tags`} hint="Separate tags with commas. At most 8." error={issueText(issues ?? [], "/metadata/tags")}>
        <Input
          id={`${id}-tags`}
          data-pointer="/metadata/tags"
          value={tagText}
          placeholder="retail, weekly"
          onChange={(e) => {
            setTagText(e.target.value);
            const tags = e.target.value
              .split(",")
              .map((t) => slugify(t.trim(), ""))
              .filter(Boolean)
              .slice(0, 8);
            onChange({ ...value, tags });
          }}
        />
      </Field>
      <Field label="What is it for? (optional)" htmlFor={`${id}-summary`} error={issueText(issues ?? [], "/metadata/summary")} className="sm:col-span-2">
        <Textarea
          id={`${id}-summary`}
          data-pointer="/metadata/summary"
          className="min-h-16"
          maxLength={300}
          value={value.summary ?? ""}
          placeholder="One or two sentences your teammates will read on the card."
          onChange={(e) => onChange({ ...value, summary: e.target.value })}
        />
      </Field>
    </div>
  );
}

/** Project or the whole tenant (tenant only for tenant admins, and only for a new item). */
export function ScopeChoice({ value, onChange, tenantAllowed }: { value: LibraryScope; onChange: (s: LibraryScope) => void; tenantAllowed: boolean }) {
  if (!tenantAllowed) return null;
  return (
    <fieldset className="grid gap-2">
      <legend className="mb-1 text-sm font-medium">Who can use it?</legend>
      <div className="flex flex-wrap gap-2">
        <ToggleChip pressed={value === "project"} onClick={() => onChange("project")}>
          This project
        </ToggleChip>
        <ToggleChip pressed={value === "tenant"} onClick={() => onChange("tenant")}>
          Everyone in the tenant
        </ToggleChip>
      </div>
    </fieldset>
  );
}

// ------------------------------------------------------------------------------ links

/**
 * A client-side link to Make your own (`/library`). The route is registered by the router
 * module; the cast keeps this file independent of the typed route tree.
 */
export function LibraryLink({ search, className, children }: { search: LibrarySearch; className?: string; children: ReactNode }) {
  return (
    <Link to={"/library"} search={search as never} className={className}>
      {children}
    </Link>
  );
}

// ------------------------------------------------------------------------------ numbers

/**
 * A number input that keeps what you type (an empty box, a lone minus, "0.") until it is a
 * number, then reports it. Outside changes (YAML edits, a "how sure" chip) replace the text.
 */
export function NumberInput({ value, onValue, fallback, ...props }: Omit<ComponentProps<"input">, "value" | "onChange" | "type"> & { value: number | null | undefined; onValue: (v: number | undefined) => void; fallback?: number }) {
  const [text, setText] = useState(value == null ? "" : String(value));
  const last = useRef(value);
  useEffect(() => {
    if (value === last.current) return;
    last.current = value;
    setText(value == null ? "" : String(value));
  }, [value]);
  return (
    <Input
      {...props}
      type="text"
      inputMode="decimal"
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        const t = e.target.value.trim();
        const n = t === "" ? undefined : Number(t);
        if (n === undefined || Number.isFinite(n)) {
          last.current = n ?? fallback;
          onValue(n);
        }
      }}
    />
  );
}
