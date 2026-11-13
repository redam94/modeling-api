/**
 * The toolbar of a list page (v0.21): one search box, a slot for the page's filters, and a
 * line that counts the results while a query is active. The page keeps the query in the URL
 * (`?q=`, `lib/list-query.ts`) and writes it with a history replace; the box sends the typed
 * text 250 ms after the last key.
 *
 * Signed-in pages only (the empty result shows a glyph the share link and the client portal
 * never load).
 */
import { Search, X } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { everyWord } from "@/features/search/rank";
import { useDebounced } from "@/hooks/use-debounced";
import { MAX_QUERY_CHARS } from "@/lib/list-query";
import { cn } from "@/lib/utils";

import { EmptyArt } from "./brand/empty-art";
import { EmptyState } from "./common";
import { Button } from "./ui/button";
import { Input } from "./ui/primitives";

/** The wait after the last key before the box sends its text. */
export const LIST_SEARCH_DEBOUNCE_MS = 250;

export interface ListCount {
  /** Things in the list now. */
  shown: number;
  /** Things the page loaded and filtered. Leave it out when the server filters. */
  total?: number | undefined;
  /** The list has more pages than the page loaded. */
  more?: boolean | undefined;
}

const one = (things: string) => (things.endsWith("s") ? things.slice(0, -1) : things);
const n = (v: number) => v.toLocaleString("en-US");

/** The line under the toolbar: "3 of 41 datasets", "3 in the first 200 datasets", "3 runs". */
export function countLine(things: string, { shown, total, more }: ListCount): string {
  if (total === undefined) return more ? `The first ${n(shown)} ${things}` : `${n(shown)} ${shown === 1 ? one(things) : things}`;
  return more ? `${n(shown)} in the first ${n(total)} ${things}` : `${n(shown)} of ${n(total)} ${things}`;
}

type Text = string | null | undefined;

/**
 * Does a thing answer the query of a list page? Each word of the query must be in one of the
 * texts. A word finds a `name` as the global search finds a title (a part of it, or its
 * letters in order); it finds `more` (a description, a reference) only as a part of the text.
 * The item groups of the global search use the same rule for the names (`everyWord`).
 */
export function matchesQuery(query: string, names: readonly Text[], more: readonly Text[] = []): boolean {
  const titles = names.filter((t): t is string => Boolean(t));
  const others = more.filter((t): t is string => Boolean(t)).map((t) => t.toLowerCase());
  return everyWord(query, titles, true, (w) => others.some((t) => t.includes(w)));
}

/**
 * Does the page show its toolbar? Always with a query in the URL (a link from the global search
 * must show its text), and while the list loads (`loaded` is `undefined`). A loaded list with
 * no things and no query has nothing to search: the page shows its first-time view only.
 */
export function toolbarShown(query: string, loaded: number | undefined): boolean {
  return Boolean(query) || loaded === undefined || loaded > 0;
}

/** The search box of the page takes the keyboard focus (the button that asks for it goes away). */
function focusListSearch() {
  document.querySelector<HTMLInputElement>("input[data-list-search]")?.focus();
}

export interface ListToolbarProps {
  /** The things of the list in the plural, lower case: "datasets". */
  things: string;
  /** The query in the URL (`""` when the URL has none). */
  value: string;
  /** Write the query to the URL (`""` removes the key). Use a history replace. */
  onChange: (query: string) => void;
  /** Shown as one line while a query is active. */
  count?: ListCount | undefined;
  /** The accessible name of the box. Default: "Search <things>". */
  label?: string;
  /** Default: the label. */
  placeholder?: string;
  /** The filters of the page: `FilterPills`, selects, a switch. */
  children?: ReactNode;
  className?: string;
}

export function ListToolbar({ things, value, onChange, count, label = `Search ${things}`, placeholder = label, children, className }: ListToolbarProps) {
  const [text, setText] = useState(value);
  // `seen`: the last query of the URL. `sent`: the query this box last sent or took. A query
  // of the URL that the box did not send comes from outside (a link, Back) and fills the box.
  const [seen, setSeen] = useState(value);
  const [sent, setSent] = useState(value);
  if (value !== seen) {
    setSeen(value);
    if (value !== sent) {
      setSent(value);
      setText(value);
    }
  }
  const input = useRef<HTMLInputElement>(null);
  const send = (raw: string) => {
    const next = raw.trim();
    if (next === sent) return;
    setSent(next);
    onChange(next);
  };
  const live = useRef({ text, send });
  useEffect(() => {
    live.current = { text, send };
  });
  const debounced = useDebounced(text, LIST_SEARCH_DEBOUNCE_MS);
  useEffect(() => {
    // A different text now: the user typed on, or a query from outside replaced the text.
    if (debounced === live.current.text) live.current.send(debounced);
  }, [debounced]);

  return (
    <div role="search" aria-label={`Search and filter ${things}`} className={cn("grid min-w-0 gap-2", className)}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <div className="relative w-full min-w-0 sm:w-auto sm:max-w-sm sm:min-w-64 sm:flex-1">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            ref={input}
            type="search"
            data-list-search=""
            aria-label={label}
            placeholder={placeholder}
            value={text}
            maxLength={MAX_QUERY_CHARS}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") send(text);
            }}
            className="rounded-full pr-9 pl-9 [&::-webkit-search-cancel-button]:appearance-none"
          />
          {text ? (
            <button
              type="button"
              aria-label="Clear the search box"
              className="focus-ring absolute top-1/2 right-2 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground"
              onClick={() => {
                setText("");
                send("");
                input.current?.focus();
              }}
            >
              <X aria-hidden className="size-3.5" />
            </button>
          ) : null}
        </div>
        {children}
      </div>
      <p role="status" data-testid="list-count" className="text-xs text-muted-foreground tabular empty:hidden">
        {value && count ? countLine(things, count) : null}
      </p>
    </div>
  );
}

/** The empty result of a search on a list page: the search glyph, one sentence and one button. */
export function ListNoMatches({
  things,
  query,
  filtered = false,
  firstPage,
  onClear,
  className,
}: {
  things: string;
  query: string;
  /** Other filters of the page are on too. */
  filtered?: boolean;
  /** The page filtered only this many loaded things (the list has more pages). */
  firstPage?: number | undefined;
  onClear: () => void;
  className?: string;
}) {
  return (
    <EmptyState
      illustration={<EmptyArt kind="search" />}
      title={`No ${things} match`}
      className={className}
      action={
        <Button
          variant="outline"
          className="rounded-full"
          onClick={() => {
            onClear();
            // This button goes away with the empty result: the focus goes to the search box.
            focusListSearch();
          }}
        >
          Clear the search
        </Button>
      }
    >
      {firstPage
        ? `The app found no match for “${query}” in the first ${n(firstPage)} ${things}.`
        : `The app found no ${things} for “${query}”${filtered ? " with these filters" : ""}.`}
    </EmptyState>
  );
}
